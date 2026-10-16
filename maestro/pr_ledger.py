"""Per-PR tending ledger — the single source of truth for remediation state.

Replaces three stamp/heuristic mechanisms that used to decide whether a PR
deserved another remediation attempt — the consecutive-no-op repetition
gate (``RemediationTracker``), the costume-signature dequeue gate
(``count_branch_resolved_partials`` grepping task-history summaries), and
the preflight-pass SHA pin (``preflight_passes.json``) — with one
deterministic rule:

    Every PR gets a bounded number of remediation attempts per
    origin-head SHA. Head movement (real, delivered progress) resets the
    budget automatically. An exhausted budget marks the PR *stuck* — a
    sticky, operator-visible state that the tend phase escalates and
    keeps surfacing until the PR moves or closes.

The ledger is stateless per operation: every mutation is one atomic
load-modify-write on the injected :class:`PRLedgerBackend` (the default
``pr_ledger.json`` store under :func:`maestro.fileutil.file_lock`, or
``state.db`` behind ``[paths] sqlite_state_enabled`` — proposal 106), so
the agent and the evolution engine can each hold their own instance over
the same store without racing each other.

Coverage boundary: only full remediation runs charge the ledger (see
:meth:`PRLedger.record_attempt`). The dequeue-face zero-cost gates
(merged-PR exclusion, merge-block, costume/branch-resolved,
preflight-pass) return before the charging seam and remain wired — they
bound their own skip paths, and removing them would NOT be covered by
this ledger.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from maestro.fileutil import atomic_write, file_lock

logger = logging.getLogger("maestro")

_LEDGER_FILE = "pr_ledger.json"

T = TypeVar("T")

#: Default LLM-remediation attempt budget per origin-head SHA. Overridden by
#: ``[loop] pr_remediation_max_attempts_per_head``.
DEFAULT_MAX_ATTEMPTS_PER_HEAD = 3


@dataclass
class PRLedgerEntry:
    """Durable per-PR tending state.

    ``observed_head`` is the last ``origin/<branch>`` head SHA the ledger
    saw; ``attempts_on_head`` counts full remediation runs made while the
    head stayed at that SHA (zero-cost dequeue-gate skips never charge
    the ledger — see :meth:`PRLedger.record_attempt`). ``stuck`` latches
    when a futile attempt exhausts the budget and clears only when the
    head advances, the PR closes, or tend routes a genuine branch-state
    change (:meth:`PRLedger.grant_fresh_attempts`) — never silently.
    """

    pr_number: int
    branch: str = ""
    observed_head: str = ""
    attempts_on_head: int = 0
    total_attempts: int = 0
    last_attempt_at: str | None = None
    last_outcome: str | None = None
    stuck: bool = False
    stuck_reason: str | None = None
    stuck_since: str | None = None
    escalated_at: str | None = None


def _entry_from_raw(raw: object) -> PRLedgerEntry | None:
    """Build an entry from one persisted dict, ``None`` on shape mismatch."""
    if not isinstance(raw, dict):
        return None
    pr_number = raw.get("pr_number")
    # ``bool`` subclasses ``int``, so a ``true`` here would otherwise key an
    # entry as ``True`` — equal to ``1``, colliding with the real PR #1.
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        return None
    entry = PRLedgerEntry(pr_number=pr_number)
    for fld in (
        "branch", "observed_head", "last_attempt_at", "last_outcome",
        "stuck_reason", "stuck_since", "escalated_at",
    ):
        value = raw.get(fld)
        if isinstance(value, str):
            setattr(entry, fld, value)
    for fld in ("attempts_on_head", "total_attempts"):
        value = raw.get(fld)
        # Same ``bool``-subclasses-``int`` trap: ``true`` would read as one
        # already-consumed attempt and shorten the per-head budget. The
        # convention lives by copy (see ``directive_ledger``, ``config``,
        # ``remediation_tracker``); a third loader should mirror it too.
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            setattr(entry, fld, value)
    if isinstance(raw.get("stuck"), bool):
        entry.stuck = raw["stuck"]
    return entry


def load_all_pr_ledger_entries(data_dir: Path) -> list[PRLedgerEntry]:
    """Every persisted :class:`PRLedgerEntry`, oldest-``pr_number``-first.

    The seed source the SQLite backend reads once to copy the legacy
    ``pr_ledger.json`` into ``state.db`` (proposal 106). Reuses the tolerant
    :func:`_entry_from_raw` per row — a malformed row is skipped, a missing
    file or ``OSError`` yields ``[]`` — exactly as :meth:`JsonPRLedger.read`
    does, then sorts by ``pr_number`` for a deterministic seed order.
    """
    path = data_dir / _LEDGER_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    entries: list[PRLedgerEntry] = []
    for value in raw.values():
        entry = _entry_from_raw(value)
        if entry is not None:
            entries.append(entry)
    return sorted(entries, key=lambda e: e.pr_number)


def entries_snapshot(
    entries: dict[int, PRLedgerEntry],
) -> dict[int, dict[str, object]]:
    """A deep, comparable snapshot of the map for change detection.

    Both backends persist a mutation's map only when it actually changed, so a
    no-op closure (a missing entry, nothing to reset, no stale rows) writes
    nothing — preserving the ledger's "quiet no-op writes nothing" contract on
    both stores. ``asdict`` yields a plain-dict copy whose ``==`` detects any
    field change, addition, or deletion.
    """
    return {num: asdict(entry) for num, entry in entries.items()}


@runtime_checkable
class PRLedgerBackend(Protocol):
    """The two persistence primitives :class:`PRLedger` needs (proposal 106).

    A lock-free :meth:`read` for the pure-read methods, and an atomic
    :meth:`mutate` that loads the map, applies the caller's *fn*, persists the
    whole map iff *fn* changed it, and returns *fn*'s result — so a
    load-modify-write stays one atomic unit on either substrate
    (``pr_ledger.json`` via :class:`JsonPRLedger`, or ``state.db`` via
    :class:`~maestro.state_store.SqlitePRLedger`). Only these two primitives
    move behind the seam; all of :class:`PRLedger`'s budget/latch policy and
    logging stay put.
    """

    def read(self) -> dict[int, PRLedgerEntry]:
        """Return the whole ``{pr_number: entry}`` map (``{}`` when absent)."""
        ...

    def mutate(self, fn: Callable[[dict[int, PRLedgerEntry]], T]) -> T:
        """Load the map, apply *fn*, persist iff changed, return *fn*'s result."""
        ...


class JsonPRLedger:
    """Default :class:`PRLedgerBackend` — the ``pr_ledger.json`` file store.

    Lifts the historical ``PRLedger._load`` / ``PRLedger._save`` bodies
    verbatim: :meth:`read` is the corrupt-tolerant JSON load and :meth:`mutate`
    wraps ``read → fn → save`` in the same :func:`~maestro.fileutil.file_lock`
    so cross-process atomicity is byte-for-byte preserved. This is the flag-off
    store (proposal 106); the ``state.db`` selection is
    :class:`~maestro.state_store.SqlitePRLedger`.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / _LEDGER_FILE

    def read(self) -> dict[int, PRLedgerEntry]:
        """Load the ledger, tolerant of a missing or corrupted file."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        entries: dict[int, PRLedgerEntry] = {}
        for value in raw.values():
            entry = _entry_from_raw(value)
            if entry is not None:
                entries[entry.pr_number] = entry
        return entries

    def _save(self, entries: dict[int, PRLedgerEntry]) -> None:
        payload = {
            str(num): asdict(entry) for num, entry in sorted(entries.items())
        }
        atomic_write(self._path, json.dumps(payload, indent=2))

    def mutate(self, fn: Callable[[dict[int, PRLedgerEntry]], T]) -> T:
        """Load-modify-write under the file lock; persist iff *fn* changed it."""
        with file_lock(self._path):
            entries = self.read()
            before = entries_snapshot(entries)
            result = fn(entries)
            if entries_snapshot(entries) != before:
                self._save(entries)
            return result


class PRLedger:
    """Per-PR remediation ledger over a selected :class:`PRLedgerBackend`.

    Persistence routes through the injected backend (``pr_ledger.json`` by
    default via :class:`JsonPRLedger`, or ``state.db`` behind
    ``[paths] sqlite_state_enabled``); all budget/latch policy and logging live
    here. The ledger holds no in-memory state between operations — every
    mutation is one atomic backend ``mutate`` — so the loop agent and the
    evolution engine can each hold their own instance over the same store.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        max_attempts_per_head: int = DEFAULT_MAX_ATTEMPTS_PER_HEAD,
        backend: PRLedgerBackend | None = None,
    ) -> None:
        self._max_attempts = max_attempts_per_head
        self._backend: PRLedgerBackend = (
            backend if backend is not None else JsonPRLedger(data_dir)
        )

    @staticmethod
    def _now_iso(now: datetime | None) -> str:
        return (now or datetime.now(UTC)).isoformat()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, pr_number: int) -> PRLedgerEntry | None:
        """Return the entry for *pr_number*, or ``None``."""
        return self._backend.read().get(pr_number)

    def can_attempt(self, pr_number: int) -> bool:
        """Whether *pr_number* still has remediation budget at its head."""
        entry = self._backend.read().get(pr_number)
        if entry is None:
            return True
        return not entry.stuck and entry.attempts_on_head < self._max_attempts

    def stuck_entries(self) -> list[PRLedgerEntry]:
        """All entries currently latched stuck, ordered by PR number."""
        return [
            entry
            for _, entry in sorted(self._backend.read().items())
            if entry.stuck
        ]

    def parked_entries(self) -> list[PRLedgerEntry]:
        """Entries parked by budget exhaustion but not latched stuck.

        A PR whose budget is consumed entirely by *progress* laps — local
        commits that never landed at origin because Tend declined to push a
        quality-gate-failing head — is blocked by :meth:`can_attempt` yet
        never latches ``stuck``: a progress lap's commit may still be
        awaiting Tend's delivery push, so :meth:`record_attempt` refuses to
        latch on it (latching would mark a progressing PR stuck). Without
        this, such a PR is silently dropped from re-routing with no operator
        signal. The state auto-clears — :meth:`observe_head` resets
        ``attempts_on_head`` to 0 on real head movement — so a delivered PR
        leaves this list with no durable false positive. Disjoint from
        :meth:`stuck_entries` (``not entry.stuck``), so a PR is never
        surfaced twice.
        """
        return [
            entry
            for _, entry in sorted(self._backend.read().items())
            if not entry.stuck and entry.attempts_on_head >= self._max_attempts
        ]

    def known_branches(self) -> set[str]:
        """All non-empty branch names recorded across the ledger's entries.

        Built on the tolerant backend :meth:`~PRLedgerBackend.read`
        (missing/corrupt store → ``{}``), so a read failure yields an empty set
        and never raises. Consumed by the owned-writable-branch producer
        (proposal 093) to confirm an evolution-PR head against the persistent
        ledger, complementing the live open-PR list.
        """
        return {
            entry.branch for entry in self._backend.read().values()
            if entry.branch
        }

    # ------------------------------------------------------------------
    # Mutations (each one an atomic backend load-modify-write)
    # ------------------------------------------------------------------

    def observe_head(
        self, pr_number: int, branch: str, head: str | None,
    ) -> PRLedgerEntry:
        """Record the current origin head for *pr_number*; reset on movement.

        A head that differs from the last observed one is delivered
        progress (Tend pushed something, main merged forward, or a human
        intervened), so the attempt budget resets and a stuck latch
        clears. ``None``/empty *head* is indeterminate and never resets —
        an unresolvable ref must not refill the budget.
        """
        def _apply(entries: dict[int, PRLedgerEntry]) -> PRLedgerEntry:
            entry = entries.get(pr_number)
            if entry is None:
                entry = PRLedgerEntry(pr_number=pr_number)
                entries[pr_number] = entry
            entry.branch = branch
            if head and head != entry.observed_head:
                if entry.observed_head or entry.stuck:
                    logger.info(
                        "PR ledger: head of PR #%s advanced to %s — "
                        "resetting attempt budget%s",
                        pr_number, head[:12],
                        " and clearing stuck latch" if entry.stuck else "",
                    )
                entry.observed_head = head
                entry.attempts_on_head = 0
                entry.stuck = False
                entry.stuck_reason = None
                entry.stuck_since = None
                entry.escalated_at = None
            return entry

        return self._backend.mutate(_apply)

    def record_attempt(
        self,
        pr_number: int,
        *,
        outcome: str,
        stuck_reason: str,
        branch: str = "",
        now: datetime | None = None,
    ) -> PRLedgerEntry:
        """Count one remediation attempt; latch stuck on futile exhaustion.

        Only full remediation runs charge the ledger: the sole production
        caller is ``_record_remediation_outcome``, which
        ``_run_task_phase`` reaches after ``run()`` completes. A consumed
        ``pending_pr_requests`` entry that a dequeue-face gate
        short-circuits (merged-PR exclusion, merge-block,
        costume/branch-resolved, preflight-pass) returns before that
        seam, so zero-cost gate skips never increment the ledger — those
        paths stay bounded by their own gates, and a PR perpetually
        skipped at the dequeue face never latches stuck here. Only a
        futile lap
        (*outcome* other than ``"progress"``) can complete the stuck
        latch: a progress lap's commit is awaiting Tend's push, and the
        :meth:`observe_head` reset that proves delivery trails the push
        by an iteration, so latching on it would mark a progressing PR
        stuck. Progress laps still consume budget — :meth:`can_attempt`
        parks re-routing until the head moves — but the budget refills
        only on delivered head movement, never on the local commit
        itself, so an undelivered-commit loop stays bounded.
        *stuck_reason* describes the blocker to name if this attempt
        exhausts the budget.
        """
        def _apply(entries: dict[int, PRLedgerEntry]) -> PRLedgerEntry:
            entry = entries.get(pr_number)
            if entry is None:
                entry = PRLedgerEntry(pr_number=pr_number)
                entries[pr_number] = entry
            if branch:
                entry.branch = branch
            entry.attempts_on_head += 1
            entry.total_attempts += 1
            entry.last_attempt_at = self._now_iso(now)
            entry.last_outcome = outcome
            if (
                not entry.stuck
                and outcome != "progress"
                and entry.attempts_on_head >= self._max_attempts
            ):
                entry.stuck = True
                entry.stuck_reason = stuck_reason
                entry.stuck_since = self._now_iso(now)
                logger.warning(
                    "PR ledger: PR #%s exhausted its remediation budget "
                    "(%d attempts at head %s) — marking stuck: %s",
                    pr_number, entry.attempts_on_head,
                    entry.observed_head[:12] or "<unknown>", stuck_reason,
                )
            return entry

        return self._backend.mutate(_apply)

    def grant_fresh_attempts(self, pr_number: int, *, reason: str) -> None:
        """Reset the attempt budget without head movement.

        Tend grants this when the PR's branch situation genuinely changed
        even though ``origin/<branch>`` did not move — a new merge
        conflict or a post-merge quality-gate failure routed by
        ``route_feedback``. Without the grant such a PR could never
        recover autonomously: its origin head can only advance by pushing
        the very resolution an exhausted budget blocks. Mirrors the
        ``RemediationTracker.reset`` the router applies to the same PR
        set. A missing entry, or one with nothing to reset, leaves the map
        unchanged, so the backend writes nothing.
        """
        def _apply(entries: dict[int, PRLedgerEntry]) -> None:
            entry = entries.get(pr_number)
            if entry is None or (
                entry.attempts_on_head == 0 and not entry.stuck
            ):
                return
            logger.info(
                "PR ledger: granting PR #%s a fresh attempt budget (%s)%s",
                pr_number, reason,
                " — clearing stuck latch" if entry.stuck else "",
            )
            entry.attempts_on_head = 0
            entry.stuck = False
            entry.stuck_reason = None
            entry.stuck_since = None
            entry.escalated_at = None

        self._backend.mutate(_apply)

    def mark_escalated(
        self, pr_number: int, *, now: datetime | None = None,
    ) -> None:
        """Record that the stuck state was escalated to the operator."""
        def _apply(entries: dict[int, PRLedgerEntry]) -> None:
            entry = entries.get(pr_number)
            if entry is None:
                return
            entry.escalated_at = self._now_iso(now)

        self._backend.mutate(_apply)

    def prune(self, keep: set[int]) -> list[int]:
        """Drop every entry whose PR is no longer open upstream.

        Returns the sorted PR numbers dropped this call — the "these PRs have
        left the open set" signal the tend pass routes into on-settlement
        record reconciliation (proposal 103). Returns ``[]`` when nothing was
        stale (the map is unchanged, so the backend writes nothing), so the
        caller can gate reconciliation on a non-empty list.
        """
        def _apply(entries: dict[int, PRLedgerEntry]) -> list[int]:
            stale = [num for num in entries if num not in keep]
            if not stale:
                return []
            for num in stale:
                del entries[num]
            logger.info(
                "PR ledger: pruned %d closed PR entr%s (%s)",
                len(stale), "y" if len(stale) == 1 else "ies",
                ", ".join(f"#{n}" for n in sorted(stale)),
            )
            return sorted(stale)

        return self._backend.mutate(_apply)

    def evict_settled(self, pr_number: int) -> bool:
        """Forget a settled PR's ledger entry; return whether one existed.

        The settlement "this PR is done — forget it" event (proposal 103): when
        a Maestro-owned PR merges or closes, its per-PR tending row — the
        origin-head SHA, the per-head attempt budget, and the sticky ``stuck``
        latch — should no longer gate any reasoning. Mirrors :meth:`prune`'s
        atomic backend load-modify-write, but deletes the single *pr_number*
        entry rather than every entry outside a keep set, so it complements the
        bulk open-set housekeeping :meth:`prune` runs on the tend pass — where
        ``prune`` waits for the next tend to enumerate open PRs, this forgets one
        settled PR the moment settlement is observed.

        Distinct from the budget-touching mutations (:meth:`observe_head`,
        :meth:`record_attempt`, :meth:`grant_fresh_attempts`): eviction never
        consults the attempt budget — a done PR's row is simply gone. Returns
        ``True`` when an entry existed (deleted and persisted), ``False`` when
        none did — a quiet no-op that leaves the map unchanged, so the backend
        writes nothing and a recurring per-cycle drive over an already-forgotten
        PR never rewrites the store. Inert until its sole caller — the
        on-settlement ``PRLedgerReconciler`` — reaches it.
        """
        def _apply(entries: dict[int, PRLedgerEntry]) -> bool:
            if pr_number not in entries:
                return False
            del entries[pr_number]
            logger.info(
                "PR ledger: forgot settled PR #%s — evicted its tending entry",
                pr_number,
            )
            return True

        return self._backend.mutate(_apply)


def format_stuck_summary(
    data_dir: Path,
    *,
    max_attempts_per_head: int = DEFAULT_MAX_ATTEMPTS_PER_HEAD,
    backend: PRLedgerBackend | None = None,
) -> str:
    """``STUCK-PR`` / ``PARKED-PR`` lines for the known-state summary.

    Returns ``""`` when nothing is stuck or parked. Zero SDK cost — reads the
    flag-selected store only (``pr_ledger.json`` by default, or ``state.db``
    when *backend* is the loop's :class:`~maestro.state_store.SqlitePRLedger`).
    Surfaces two budget-exhausted classes so contemplation and task generation
    see the unattended backlog instead of rediscovering it from stamps:

    * ``STUCK-PR`` — a futile lap exhausted the budget; the latch is sticky
      until the head moves or the PR closes, so manual action is required.
    * ``PARKED-PR`` — the budget was consumed by progress laps that
      committed locally but never landed at origin (see
      :meth:`PRLedger.parked_entries`); re-routing is parked but the state
      auto-clears once the head advances.

    *max_attempts_per_head* must match the configured budget
    (``[loop] pr_remediation_max_attempts_per_head``) so the parked
    threshold agrees with :meth:`PRLedger.can_attempt`. *backend* is the
    flag-selected :class:`PRLedgerBackend`; ``None`` reads the default JSON
    store, matching the read-only CLI callers.
    """
    ledger = PRLedger(
        data_dir,
        max_attempts_per_head=max_attempts_per_head,
        backend=backend,
    )
    stuck = ledger.stuck_entries()
    parked = ledger.parked_entries()
    if not stuck and not parked:
        return ""
    lines: list[str] = []
    if stuck:
        parts = [
            f"#{entry.pr_number} ({entry.stuck_reason or 'reason unrecorded'})"
            for entry in stuck
        ]
        lines.append(
            f"STUCK-PR: {len(stuck)} open PR(s) exhausted their remediation "
            f"budget without landing — manual action required: "
            f"{'; '.join(parts)}"
        )
    if parked:
        parts = [f"#{entry.pr_number}" for entry in parked]
        lines.append(
            f"PARKED-PR: {len(parked)} open PR(s) committed locally but never "
            f"landed at origin after exhausting their remediation budget — "
            f"re-routing is parked until the head advances: "
            f"{'; '.join(parts)}"
        )
    return "\n".join(lines)
