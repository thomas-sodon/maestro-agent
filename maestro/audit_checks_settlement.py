"""Settled-verdicts ledger — the audit rules' settled-PR memo.

Split out of :mod:`maestro.audit_checks` by proposal 147 Step 9: the
``settled_verdicts.jsonl`` record type, its JSONL backend, and the
reconciler / upsert writers. Zero SDK cost; imports nothing from
:mod:`maestro.audit_checks`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from maestro.fileutil import file_lock
from maestro.pr_facts import MergeTruth
from maestro.remediation_tracker import (
    _REMEDIATION_GOAL_PATTERN,
    _read_recent_task_tail,
)

logger = logging.getLogger(__name__)

# Durable, operator-inspectable ledger the standing settlement reconciler
# (proposal 097) appends to — a settle row for each distinct remediation-verdict
# PR the ``is_pr_settled`` chokepoint has observed settled (merged *or* closed),
# plus a superseding retraction row whenever live merge-truth later contradicts
# one. Absent until the first reconcile, never read by a hot path, and additive
# (append-only JSONL); :func:`load_settled_prs` collapses the rows last-row-wins
# into the effective set. The phantom sensors pre-filter on that set, so each
# finding fires at most once and then genuinely self-extinguishes.
_SETTLED_VERDICTS_LEDGER = "settled_verdicts.jsonl"


@dataclass(frozen=True)
class SettledVerdictRecord:
    """One settled-verdicts observation persisted for the phantom-rule pre-filter.

    The public record round-tripped by every :class:`SettledVerdictsBackend`
    (JSONL or SQLite) and produced by the :func:`load_all_settled_verdicts` seed
    source. A *settle* row carries ``settled=True`` with a ``kind`` of
    ``"merged"`` or ``"closed"`` (``iteration`` its ``settled_at_iteration`` and
    an optional ``verdict_task_id``); a *retraction* row carries
    ``settled=False``, an empty ``kind``, no ``verdict_task_id``, and
    ``iteration`` its ``retracted_at_iteration``. Frozen so a ledger entry is
    never mutated in place — mirrors
    :class:`~maestro.directive_ledger.DroppedDirective`.
    """

    pr_number: int
    settled: bool
    kind: str
    iteration: int
    verdict_task_id: str | None


@runtime_checkable
class SettledVerdictsBackend(Protocol):
    """The settled-verdicts pre-filter persistence surface (proposal 106).

    Two last-row-wins collapse reads plus one batched append behind one seam so
    the window-independent phantom-rule pre-filter can live in
    ``settled_verdicts.jsonl`` (default) or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching.
    :meth:`effective_settled_prs` is the settle/retract collapse read by
    :func:`load_settled_prs`; :meth:`merged_settled_prs` is the
    ``kind == "merged"`` subset the base-advance-race sensor
    (:func:`maestro.remediation_tracker.measure_base_advance_races`) needs. Both
    collapse per ``pr_number``, last row wins. :meth:`append` writes settle
    *and* retraction rows in one batch. ``@runtime_checkable`` enables a
    structural ``isinstance`` check in the agent's selection tests. Mirrors the
    :class:`~maestro.directive_ledger.DroppedDirectiveBackend` bundle.
    """

    def effective_settled_prs(self) -> frozenset[int]:
        """Return the PRs whose final row is a settle (settle/retract collapse)."""
        ...

    def merged_settled_prs(self) -> frozenset[int]:
        """Return the PRs whose final row is a ``kind == "merged"`` settle."""
        ...

    def append(self, records: Iterable[SettledVerdictRecord]) -> None:
        """Append *records* (settle and/or retraction rows) in one batch."""
        ...


def load_all_settled_verdicts(data_dir: Path) -> list[SettledVerdictRecord]:
    """Return every settled-verdicts row oldest-first — the SQLite seed source.

    Walks ``<data_dir>/settled_verdicts.jsonl`` in file order, mapping each
    well-formed row to a :class:`SettledVerdictRecord`: a *settle* row (no
    ``retracted_at_iteration`` key) yields ``settled=True`` with its ``kind`` /
    ``settled_at_iteration`` / ``verdict_task_id``, and a *retraction* row (an
    integer ``retracted_at_iteration``) yields ``settled=False``. Shares the
    per-line corruption tolerance of the collapse walks
    (:meth:`JsonlSettledVerdictsLedger.effective_settled_prs`): an absent or
    unreadable file yields ``[]``, blank / malformed / non-mapping lines are
    skipped, only an integer ``pr_number`` contributes (``bool`` rejected), and
    a damaged retraction (a non-integer ``retracted_at_iteration``) is skipped
    entirely so it never resurrects a voided PR. Has a real call site (the
    :class:`~maestro.state_store.SqliteSettledVerdictsLedger` one-time seed), so
    it does not trip the self-review uncalled-symbol gate. Read-only, zero SDK
    cost.
    """
    path = data_dir / _SETTLED_VERDICTS_LEDGER
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[SettledVerdictRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        pr = row.get("pr_number")
        if not isinstance(pr, int) or isinstance(pr, bool):
            continue
        if "retracted_at_iteration" not in row:
            kind = row.get("kind")
            settled_at = row.get("settled_at_iteration")
            task_id = row.get("verdict_task_id")
            records.append(
                SettledVerdictRecord(
                    pr_number=pr,
                    settled=True,
                    kind=kind if isinstance(kind, str) else "",
                    iteration=(
                        settled_at
                        if isinstance(settled_at, int)
                        and not isinstance(settled_at, bool)
                        else 0
                    ),
                    verdict_task_id=task_id if isinstance(task_id, str) else None,
                ),
            )
            continue
        retracted_at = row["retracted_at_iteration"]
        if not isinstance(retracted_at, int) or isinstance(retracted_at, bool):
            continue
        records.append(
            SettledVerdictRecord(
                pr_number=pr,
                settled=False,
                kind="",
                iteration=retracted_at,
                verdict_task_id=None,
            ),
        )
    return records


def _settled_record_to_row(record: SettledVerdictRecord) -> dict[str, object]:
    """Serialize one record to its JSONL row shape (settle or retraction).

    Reproduces the row shapes :func:`reconcile_settled_verdicts` and
    :func:`upsert_settled_pr` historically appended, so a
    :meth:`JsonlSettledVerdictsLedger.effective_settled_prs` walk over an append
    reads byte-for-byte as before: a settle record →
    ``{pr_number, settled_at_iteration, kind, verdict_task_id}``; a retraction
    record → ``{pr_number, retracted_at_iteration}``.
    """
    if record.settled:
        return {
            "pr_number": record.pr_number,
            "settled_at_iteration": record.iteration,
            "kind": record.kind,
            "verdict_task_id": record.verdict_task_id,
        }
    return {
        "pr_number": record.pr_number,
        "retracted_at_iteration": record.iteration,
    }


class JsonlSettledVerdictsLedger:
    """JSONL-backed :class:`SettledVerdictsBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the historical last-row-wins walks verbatim:
    :meth:`effective_settled_prs` is the settle/retract collapse formerly inline
    in :func:`load_settled_prs`, and :meth:`merged_settled_prs` is the
    ``kind == "merged"`` collapse formerly in
    :func:`maestro.remediation_tracker._read_merged_settled_prs`. :meth:`append`
    keeps the ``file_lock`` + append-JSONL block, one row per record. The ledger
    file is ``<data_dir>/settled_verdicts.jsonl``. Mirrors the
    :class:`~maestro.directive_ledger.JsonlDroppedDirectiveLedger` idiom.

    Discriminating a **retraction** on row *shape* (an integer
    ``retracted_at_iteration`` key) rather than a ``kind`` value mirrors the
    ``stand_down_verdicts.jsonl`` release-row idiom in
    :mod:`~maestro.remediation_tracker`: a reader unaware of the new shape skips
    it rather than mistaking it for a settlement. A row carrying the key with a
    **non-integer** value is a damaged retraction and is skipped entirely (the
    PR keeps whatever state earlier rows established), because reading it as a
    settle would let a corrupt ``true``/``"3"`` resurrect a PR a valid
    retraction had already voided — and the failure direction that matters is
    suppression. The collapse means a PR that settles, is retracted (a closed PR
    reopened, say), then re-settles reads as settled again, and a
    settle → retract sequence reads as absent.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def effective_settled_prs(self) -> frozenset[int]:
        """Return the PR numbers *effectively* settled (settle/retract collapse).

        Tolerant of a damaged ledger the same way the audit sidecar loaders are:
        an absent, unreadable, or corrupt file reads as an empty set, so a
        missing ledger cleanly means "nothing settled yet." Blank and malformed
        lines are skipped, and only rows carrying an integer ``pr_number``
        contribute (``bool`` rejected, so a corrupted ``true`` is never mistaken
        for PR 1).
        """
        path = self._data_dir / _SETTLED_VERDICTS_LEDGER
        if not path.exists():
            return frozenset()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "Skipping unreadable settled-verdicts ledger %s", path,
            )
            return frozenset()
        effective: dict[int, bool] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            pr = row.get("pr_number")
            if not isinstance(pr, int) or isinstance(pr, bool):
                continue
            if "retracted_at_iteration" not in row:
                effective[pr] = True
                continue
            retracted_at = row["retracted_at_iteration"]
            if not isinstance(retracted_at, int) or isinstance(retracted_at, bool):
                # Damaged retraction: leave the PR's prior state untouched
                # rather than resurrecting it as a settle.
                continue
            effective[pr] = False
        return frozenset(pr for pr, settled in effective.items() if settled)

    def merged_settled_prs(self) -> frozenset[int]:
        """Return the PRs whose final row is a merged settle (``kind`` collapse).

        Walks rows in file order keeping last-row-wins state per ``pr_number``,
        discriminating settle from retraction on the **same** row shape
        :meth:`effective_settled_prs` uses — the presence of a
        ``retracted_at_iteration`` key — so both collapse reads, and
        :func:`load_all_settled_verdicts` feeding the SQLite keyed-replace seed,
        agree on every row. A settle row (no ``retracted_at_iteration``) is merged
        iff its ``kind`` is exactly ``"merged"``; a *kind-less* or
        non-string-``kind`` settle degrades to not-merged — matching the
        ``kind=""`` normalization the seed source and the SQLite twin apply —
        rather than being a no-op that could leave a stale merged state standing
        (the parity divergence proposal 106 closes). A retraction row (an integer
        ``retracted_at_iteration``) marks the PR not-merged, and a *damaged*
        retraction (a non-integer value) is skipped, leaving prior state — again
        exactly as :meth:`effective_settled_prs` does. A missing file reads as the
        empty set, and blank/malformed/non-mapping lines are skipped. ``bool``
        ``pr_number`` values are rejected (they subclass ``int``).
        """
        path = self._data_dir / _SETTLED_VERDICTS_LEDGER
        merged: dict[int, bool] = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                lines = list(f)
            for line in lines:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                pr = row.get("pr_number")
                if not isinstance(pr, int) or isinstance(pr, bool):
                    continue
                if "retracted_at_iteration" not in row:
                    # Settle row: merged iff ``kind`` is exactly "merged". A
                    # kind-less (corrupt) settle degrades to not-merged, matching
                    # load_all_settled_verdicts / the SQLite keyed-replace seed.
                    merged[pr] = row.get("kind") == "merged"
                    continue
                retracted_at = row["retracted_at_iteration"]
                if isinstance(retracted_at, int) and not isinstance(
                    retracted_at, bool,
                ):
                    merged[pr] = False
                # else: damaged retraction — keep prior state (skip).
        return frozenset(pr for pr, is_merged in merged.items() if is_merged)

    def append(self, records: Iterable[SettledVerdictRecord]) -> None:
        """Append *records* to the JSONL ledger under one ``file_lock``.

        Settle and retraction rows share the one append block (the idiom the
        pre-seam :func:`reconcile_settled_verdicts` used); nothing is opened when
        *records* is empty, so an immediate return leaves no file.
        """
        rows = [_settled_record_to_row(r) for r in records]
        if not rows:
            return
        path = self._data_dir / _SETTLED_VERDICTS_LEDGER
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path), open(path, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")


def load_settled_prs(backend: SettledVerdictsBackend) -> frozenset[int]:
    """Return the PR numbers *effectively* settled, via *backend*.

    A thin reader over :meth:`SettledVerdictsBackend.effective_settled_prs` — the
    settle/retract last-row-wins collapse now owned by the backend
    (:class:`JsonlSettledVerdictsLedger` by default, or the SQLite store behind
    ``[paths] sqlite_state_enabled``). The collapse semantics and corruption
    tolerance live with the backend; this remains the named reader the phantom
    sensors' pre-filter is expressed in terms of.
    """
    return backend.effective_settled_prs()


def reconcile_settled_verdicts(
    data_dir: Path,
    n: int,
    iteration: int,
    *,
    is_pr_settled: Callable[[int], MergeTruth],
    backend: SettledVerdictsBackend,
) -> frozenset[int]:
    """Bind the trailing remediation verdicts to the merge-truth chokepoint.

    Reads the trailing *n* ``task_history.jsonl`` records via
    :func:`_read_recent_task_tail` (still under *data_dir*); for each
    ``Remediate PR #N`` record (matched with :data:`_REMEDIATION_GOAL_PATTERN`,
    whose ``group(1)`` is the bare PR number, ``goal`` with a ``title`` fallback)
    consults the injected ``is_pr_settled`` chokepoint **once per distinct PR** —
    the returned :class:`~maestro.pr_facts.MergeTruth` is cached locally. Every PR
    the chokepoint reports ``settled`` (merged *or* closed) is added to the
    returned ``frozenset`` and **upserted** into *backend* as one
    :class:`SettledVerdictRecord` — appended only when the PR is not already
    *effectively* recorded, so repeated reconciles over the same tail are
    idempotent. ``kind`` is ``"merged"`` when :attr:`MergeTruth.branch_landed`,
    else ``"closed"``.

    The returned pre-filter is **ledger-backed, not window-bound**: it is
    everything :func:`load_settled_prs` already holds, plus everything this
    cycle observed settled, less anything this cycle retracted — the effective
    set the body maintains in ``recorded``. A PR settled in cycle *N* stays
    pre-filtered in cycle *N+k* with no further chokepoint call, even once its
    ``Remediate PR #N`` record has scrolled off the trailing window — the hole
    that let rules 12/14 re-fire on a settled (especially closed-unmerged)
    PR for as long as the *claiming* record survived the window.

    The complementary retraction path keeps the durable set honest **for the
    PRs this cycle's window names**: a PR the chokepoint now reports *not*
    settled while the ledger records it as settled (a closed PR reopened, say)
    gets one ``{pr_number, retracted_at_iteration}`` row appended and is dropped
    from both the effective set and the returned one. Because a PR that
    settles → retracts → re-settles is no longer effectively recorded, its
    re-settle writes a fresh settle row rather than staying permanently
    retracted.

    Retraction is therefore *window-bound where the pre-filter is not*, and that
    asymmetry is deliberate: the chokepoint is consulted only for PRs a trailing
    ``Remediate PR #N`` record names, so once a reopened PR's last such record
    scrolls off the tail there is no candidate to re-verify and its settle row
    stands until a fresh remediation lap re-cites it. Live merge-truth overrides
    the ledger while a citing record survives the window — not unconditionally.
    Re-verifying ledger-only PRs would buy the missing case at the cost of a
    per-cycle chokepoint call for every PR ever settled; broadening the
    candidate surface that way is out of scope for this increment.

    Zero SDK cost: the only I/O is the history tail read, the injected
    chokepoint's own seams, and the backend append (settle *and* retraction
    records share the one :meth:`SettledVerdictsBackend.append` call).
    Merge-truth is reached *only* through the injected chokepoint — no bare git
    subprocess in this body. Genuine errors surface to the audit-phase guard
    exactly as the phantom rules' do; there is no broad swallow-and-continue
    guard here.
    """
    if n <= 0:
        return frozenset()

    # ``recorded`` is the *effective* settled set, kept current as records are
    # staged: a retraction discards its PR, a settle adds it. Membership is
    # therefore the whole append guard — one record per state change, and the
    # returned pre-filter is just this set.
    recorded = set(load_settled_prs(backend))
    truth_cache: dict[int, MergeTruth] = {}
    new_records: list[SettledVerdictRecord] = []
    for task in _read_recent_task_tail(data_dir, n):
        goal = task.get("goal")
        if not isinstance(goal, str) or not goal:
            goal = task.get("title")
        if not isinstance(goal, str) or not goal:
            continue
        match = _REMEDIATION_GOAL_PATTERN.match(goal)
        if match is None:
            continue
        pr_number = int(match.group(1))
        if pr_number not in truth_cache:
            truth_cache[pr_number] = is_pr_settled(pr_number)
        truth = truth_cache[pr_number]
        if not truth.settled:
            # Live truth contradicts a ledger record: supersede it with a
            # retraction so the durable set cannot outlive the settlement.
            # Discarding here also makes a second record citing the same PR a
            # no-op, so one contradiction writes exactly one retraction record.
            if pr_number in recorded:
                recorded.discard(pr_number)
                new_records.append(
                    SettledVerdictRecord(
                        pr_number=pr_number,
                        settled=False,
                        kind="",
                        iteration=iteration,
                        verdict_task_id=None,
                    ),
                )
            continue
        if pr_number in recorded:
            continue
        recorded.add(pr_number)
        task_id = task.get("task_id")
        new_records.append(
            SettledVerdictRecord(
                pr_number=pr_number,
                settled=True,
                kind="merged" if truth.branch_landed else "closed",
                iteration=iteration,
                verdict_task_id=task_id if isinstance(task_id, str) else None,
            ),
        )

    if new_records:
        backend.append(new_records)

    return frozenset(recorded)


def upsert_settled_pr(
    backend: SettledVerdictsBackend,
    pr_number: int,
    truth: MergeTruth,
    iteration: int,
) -> bool:
    """Record one settlement observation into the settled-verdicts pre-filter.

    Proposal 103. The window-independent complement to
    :func:`reconcile_settled_verdicts`: where that binder only learns about a PR
    while a trailing ``Remediate PR #N`` record survives the rolling
    ``task_history.jsonl`` slice, this upsert records *any* settlement the caller
    already observed — in particular a PR that lands through an ordinary
    evolution-plan merge and was never the subject of a remediation lap — so the
    :func:`load_settled_prs` pre-filter no longer depends on a remediation record
    happening to survive the window.

    Appends exactly one *settle* :class:`SettledVerdictRecord` to *backend* —
    ``verdict_task_id`` ``None`` (this observation is not tied to a remediation
    verdict task) and ``kind`` ``"merged"`` when :attr:`MergeTruth.branch_landed`
    else ``"closed"`` — but only when *truth* is ``settled`` **and** *pr_number*
    is not already *effectively* recorded (``pr_number not in
    load_settled_prs(backend)``). Repeated observations of the same PR are
    therefore idempotent, and a not-settled *truth* writes nothing: retraction of
    a reopened PR stays owned by :func:`reconcile_settled_verdicts` for the PRs
    its window names. Returns whether a record was written.

    The settle-record shape mirrors :func:`reconcile_settled_verdicts` exactly, so
    both writers stay collapsible last-row-wins by :func:`load_settled_prs`. Zero
    SDK cost: the only I/O is the idempotency read and, at most, one append.
    """
    if not truth.settled:
        return False
    if pr_number in load_settled_prs(backend):
        return False
    backend.append(
        [
            SettledVerdictRecord(
                pr_number=pr_number,
                settled=True,
                kind="merged" if truth.branch_landed else "closed",
                iteration=iteration,
                verdict_task_id=None,
            ),
        ],
    )
    return True
