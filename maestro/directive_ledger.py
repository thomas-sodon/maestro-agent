"""Durable replay of dropped fire-or-close directives (proposal 090).

``SelfModel.next_task_directive`` is a one-shot slot holding exactly one string,
and several independent producers compete for it each iteration (audit bridges,
promotion hints, the record-time grounding binds). When a write-time
fire-or-close directive — "land #N or close it", minted after a ``success`` is
downgraded to ``partial`` because it cites an unlanded PR / own commit — loses
that race, it is historically discarded silently. The demanded disposition
evaporates and the unlanded PR keeps drawing laps because nothing steered the
next task at it.

This module makes those drops durable. :func:`offer_directive` fills the slot
when it is empty (the historical inline behaviour) and, when the slot is
occupied *and the feature is enabled*, appends the dropped directive — together
with the git grounding that justified it — to a small, bounded, file-locked
``dropped_directives.jsonl`` ledger. :func:`replay_dropped_directive` re-offers
the oldest entry whose grounding is **still live** (re-verified through injected
git oracles) into a later empty slot, and prunes entries whose PR has since
settled / whose SHA has landed, or that have aged out. Every decision is a
keyword-free git fact — zero SDK cost.

With the feature flag off, :func:`offer_directive` is a pure no-op on the
occupied-slot path (byte-identical to the pre-090 inline drop) and the replay
gate never runs, so nothing is written and nothing is replayed.

Storage sits behind a :class:`DroppedDirectiveBackend` seam (proposal 106): the
default :class:`JsonlDroppedDirectiveLedger` keeps the file-locked
``dropped_directives.jsonl`` bodies verbatim, and a SQLite adapter on the shared
``state.db`` is selected behind ``[paths] sqlite_state_enabled``. The two free
functions own the policy (the fill-slot race, :data:`_MAX_ENTRIES`,
:func:`_is_dead`); the backend owns only the atomic load-mutate-write.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from maestro.fileutil import atomic_write, file_lock

if TYPE_CHECKING:
    from maestro.consciousness import SelfModel

logger = logging.getLogger("maestro")

# JSONL ledger under ``data_dir``. One row per dropped directive:
# ``{"text", "source", "pr_numbers": [int], "shas": [str], "recorded_at"}``.
# Read-modify-written under :func:`~maestro.fileutil.file_lock` (like
# ``evolution_plans.jsonl``), never a pure event log — it is pruned and bounded.
_LEDGER_FILE = "dropped_directives.jsonl"

# Max rows retained. On append, the oldest surplus rows are dropped so the
# ledger stays bounded regardless of how often the slot loses the race.
_MAX_ENTRIES = 50

# A live entry whose grounding never resolves (its PR never settles / SHA never
# lands because the citation was noise) is aged out after this so the ledger
# self-empties rather than re-offering a stale directive forever.
_ENTRY_MAX_AGE = timedelta(days=30)


@dataclass(frozen=True)
class DirectiveGrounding:
    """The git facts that justify replaying a dropped directive.

    ``pr_numbers`` are cited PR numbers whose settlement (merge **or** close)
    makes the directive moot; ``shas`` are own-branch commit SHAs whose landing
    on ``origin/main`` does the same. An entry is *dead* once every cited PR is
    settled and every cited SHA has landed (or the entry ages out) — the
    replay gate re-verifies only this PR/SHA grounding, so an entry with no
    grounding at all is treated as immediately dead (never replayed).
    """

    pr_numbers: tuple[int, ...] = ()
    shas: tuple[str, ...] = ()


@dataclass(frozen=True)
class DroppedDirective:
    """One dropped directive persisted for later replay.

    The public record round-tripped by every :class:`DroppedDirectiveBackend`
    (JSONL or SQLite) and returned by the :func:`load_all_dropped_directives`
    seed source. Frozen so a ledger entry is never mutated in place.
    """

    text: str
    source: str
    pr_numbers: tuple[int, ...]
    shas: tuple[str, ...]
    recorded_at: str


def _ledger_path(data_dir: Path) -> Path:
    """Return the path to the dropped-directive ledger under *data_dir*."""
    return data_dir / _LEDGER_FILE


def _load_entries(path: Path) -> list[DroppedDirective]:
    """Load ledger rows oldest-first, tolerant of corruption.

    A missing/unreadable file yields an empty list; a malformed JSON line, a
    non-object row, or a row missing a non-empty ``text`` string is skipped
    (per-line tolerance, mirroring the memory JSONL readers). ``pr_numbers`` /
    ``shas`` degrade to empty tuples when absent or malformed; ``bool`` PR
    numbers are dropped (``bool`` subclasses ``int``).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[DroppedDirective] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        text = obj.get("text")
        if not isinstance(text, str) or not text:
            continue
        raw_prs = obj.get("pr_numbers")
        raw_shas = obj.get("shas")
        pr_numbers = (
            tuple(
                n
                for n in raw_prs
                if isinstance(n, int) and not isinstance(n, bool)
            )
            if isinstance(raw_prs, list)
            else ()
        )
        shas = (
            tuple(s for s in raw_shas if isinstance(s, str) and s)
            if isinstance(raw_shas, list)
            else ()
        )
        source = obj.get("source")
        recorded_at = obj.get("recorded_at")
        entries.append(
            DroppedDirective(
                text=text,
                source=source if isinstance(source, str) else "",
                pr_numbers=pr_numbers,
                shas=shas,
                recorded_at=recorded_at if isinstance(recorded_at, str) else "",
            ),
        )
    return entries


def _dedupe_key(
    entry: DroppedDirective,
) -> tuple[str, tuple[int, ...], tuple[str, ...]]:
    """Return the identity a re-offer is deduplicated on: text plus grounding."""
    return (entry.text, entry.pr_numbers, entry.shas)


def _row(entry: DroppedDirective) -> str:
    """Serialize one ledger entry to a JSONL row (no trailing newline)."""
    return json.dumps(
        {
            "text": entry.text,
            "source": entry.source,
            "pr_numbers": list(entry.pr_numbers),
            "shas": list(entry.shas),
            "recorded_at": entry.recorded_at,
        },
    )


def _write_entries(path: Path, entries: list[DroppedDirective]) -> None:
    """Rewrite the ledger with *entries* (oldest-first), atomically."""
    atomic_write(path, "".join(_row(e) + "\n" for e in entries))


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO timestamp, returning ``None`` when unparseable or naive.

    A naive datetime is rejected because the age comparison subtracts it from a
    timezone-aware ``now`` — mixing the two raises ``TypeError``. Mirrors the
    tolerant deadline parsing in :mod:`maestro.escalation_bridge`.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def load_all_dropped_directives(data_dir: Path) -> list[DroppedDirective]:
    """Return every dropped-directive row oldest-first — the SQLite seed source.

    Wraps the tolerant :func:`_load_entries` reader over
    ``<data_dir>/dropped_directives.jsonl``, sharing its per-line corruption
    tolerance, so enabling ``[paths] sqlite_state_enabled`` seeds the store from
    the in-flight ledger. Has a real call site (the
    :class:`~maestro.state_store.SqliteDroppedDirectiveLedger` one-time seed), so
    it does not trip the self-review uncalled-symbol gate. Read-only, zero SDK
    cost.
    """
    return _load_entries(_ledger_path(data_dir))


@runtime_checkable
class DroppedDirectiveBackend(Protocol):
    """The dropped-directive replay ledger persistence surface (proposal 106).

    Two *atomic* read-modify-write operations behind one seam so the bounded,
    pruned ledger can live in ``dropped_directives.jsonl`` (default) or in
    ``state.db`` behind ``[paths] sqlite_state_enabled`` with no call-site
    branching. Unlike the sibling ``FetchTruthBackend`` — whose methods are
    single inserts / lookups — each method here owns a whole load-mutate-write:
    the module keeps the policy (:data:`_MAX_ENTRIES`, :func:`_is_dead`) and the
    backend executes it inside one lock / transaction. ``@runtime_checkable``
    enables a structural ``isinstance`` check in the agent's selection tests.
    """

    def offer(self, entry: DroppedDirective) -> None:
        """Append *entry*, then bound to :data:`_MAX_ENTRIES` (drop oldest).

        Idempotent: when a stored row already matches *entry* on ``text``,
        ``pr_numbers`` and ``shas`` nothing is written, so the original row
        (and its ``recorded_at``, which the age-out counts from) stays.
        """
        ...

    def replay(
        self, is_dead: Callable[[DroppedDirective], bool],
    ) -> str | None:
        """Prune dead entries, re-offer + consume the oldest live entry's text.

        Partition the ledger via *is_dead*, rewrite the survivors dropping the
        dead entries **and** the oldest live one, and return that entry's
        ``text`` (``None`` when nothing lives).
        """
        ...


class JsonlDroppedDirectiveLedger:
    """JSONL-backed :class:`DroppedDirectiveBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the historical ``file_lock``-guarded
    load-mutate-write bodies verbatim, so :meth:`offer` and :meth:`replay` each
    stay a single atomic operation under one lock exactly as the pre-seam inline
    code did. The ledger file is ``<data_dir>/dropped_directives.jsonl``. Mirrors
    the :class:`~maestro.remediation_tracker.JsonlFetchTruthLedger` idiom.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def offer(self, entry: DroppedDirective) -> None:
        path = _ledger_path(self._data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path):
            entries = _load_entries(path)
            key = _dedupe_key(entry)
            if any(_dedupe_key(e) == key for e in entries):
                return
            entries.append(entry)
            if len(entries) > _MAX_ENTRIES:
                entries = entries[-_MAX_ENTRIES:]
            _write_entries(path, entries)

    def replay(
        self, is_dead: Callable[[DroppedDirective], bool],
    ) -> str | None:
        path = _ledger_path(self._data_dir)
        if not path.exists():
            return None
        with file_lock(path):
            entries = _load_entries(path)
            if not entries:
                return None
            live = [e for e in entries if not is_dead(e)]
            if not live:
                _write_entries(path, [])
                return None
            offered = live[0]
            _write_entries(path, live[1:])
            return offered.text


def offer_directive(
    model: SelfModel,
    text: str,
    *,
    source: str,
    grounding: DirectiveGrounding,
    backend: DroppedDirectiveBackend,
    enabled: bool,
) -> bool:
    """Fill the one-shot directive slot, or ledger the drop for later replay.

    When ``model.next_task_directive`` is empty, set it to *text* and return
    ``True`` — exactly the historical inline ``if not next_task_directive:``
    write. The model is **not** saved here; every caller persists on its own
    terms.

    When the slot is already occupied the directive would historically be
    discarded. If *enabled*, build a :class:`DroppedDirective` carrying *text*,
    *source* and *grounding* and hand it to ``backend.offer`` — which appends it
    to the bounded ledger (dropping the oldest surplus rows) atomically — so the
    replay gate can re-offer it into a later empty slot; return ``False``. When
    *enabled* is false this is a pure no-op returning ``False`` — byte-identical
    to the pre-090 inline drop, with nothing written.

    An offer whose *text* already occupies the slot did not lose the race, so it
    is not a drop: return ``False`` and write nothing. The level-triggered
    producers re-emit the same text every cycle, and banking those copies would
    evict genuinely distinct drops.
    """
    if not model.next_task_directive:
        model.next_task_directive = text
        return True
    if not enabled or model.next_task_directive == text:
        return False
    entry = DroppedDirective(
        text=text,
        source=source,
        pr_numbers=grounding.pr_numbers,
        shas=grounding.shas,
        recorded_at=datetime.now(UTC).isoformat(),
    )
    backend.offer(entry)
    return False


def _is_dead(
    entry: DroppedDirective,
    is_pr_settled: Callable[[int], bool],
    is_sha_landed: Callable[[str], bool],
    now_dt: datetime | None,
) -> bool:
    """Return whether *entry*'s grounding is fully resolved or it has aged out.

    An entry is dead once every cited PR is settled **and** every cited SHA has
    landed — so an entry citing only PRs is gated solely on those PRs, one
    citing only SHAs solely on those SHAs, and an entry with no grounding is
    vacuously dead (never replayed; non-git-grounded directives are out of
    scope for this gate). Past ``_ENTRY_MAX_AGE`` it is dead regardless, so a
    citation whose oracle never resolves cannot pin the entry open forever.
    """
    if now_dt is not None:
        recorded = _parse_iso(entry.recorded_at)
        if recorded is not None and now_dt - recorded >= _ENTRY_MAX_AGE:
            return True
    prs_settled = all(is_pr_settled(pr) for pr in entry.pr_numbers)
    shas_landed = all(is_sha_landed(sha) for sha in entry.shas)
    return prs_settled and shas_landed


def replay_dropped_directive(
    backend: DroppedDirectiveBackend,
    *,
    is_pr_settled: Callable[[int], bool],
    is_sha_landed: Callable[[str], bool],
    now: str,
) -> str | None:
    """Re-offer the oldest still-live dropped directive; prune the dead.

    Builds the ``is_dead`` predicate from the injected git oracles and the
    parsed *now* (the module-level :func:`_is_dead`, so the age-out policy and
    the "PR settled **and** SHA landed" rule stay here, not in the storage
    layer) and hands it to ``backend.replay``, which partitions the ledger,
    rewrites the survivors dropping the dead entries **and** the one being
    re-offered (the oldest live entry), and returns that entry's ``text`` (or
    ``None`` when nothing lives) atomically. The oracles are injected so the
    module is unit-testable without a gate or agent; *now* is the current UTC
    ISO timestamp used for the age cap.
    """
    now_dt = _parse_iso(now)

    def is_dead(entry: DroppedDirective) -> bool:
        return _is_dead(entry, is_pr_settled, is_sha_landed, now_dt)

    return backend.replay(is_dead)
