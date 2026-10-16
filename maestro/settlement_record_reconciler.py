"""On-settlement record-reconciler scaffold (proposal 103).

When a Maestro-owned PR settles (merges *or* closes), its trace lingers in a
handful of PR-keyed stores that today are cleared — if at all — by several
independent sites, each keyed on a *different* signal. The eventual owner drives
a **registry** of small per-store reconcilers off the single provenance-stamped
settlement signal (``MaestroAgent.is_pr_settled`` /
:func:`~maestro.pr_facts.observe_pr_merge_truth`), records every clear to a
durable ledger, and becomes the sole door through which a PR-keyed store may be
cleared on settlement.

This owner now covers all six PR-keyed stores. On top of the foundation — the
reconciler :class:`Protocol`, a frozen :class:`ReconcileResult`, the
:func:`reconcile_pr_settlement` driver, and the provenance ledger constant — it
registers only :class:`SelfModelReconciler` (the self-model itself — the
``limitations_known`` entries, the ``[audit]``/``[limitation]``-prefixed
``open_questions`` rows, and a sole-PR ``next_task_directive``) as the sole
module-level :data:`RECONCILER_REGISTRY` entry, and adds five **agent-composed**
reconcilers that cannot be constructed without agent context:
:class:`EscalationStanddownReconciler` (the escalation-handoff standdowns),
which holds the agent-selected
:class:`~maestro.escalation_bridge.EscalationLedgerBackend` and is prepended
**first** so the historical reconciler iteration order is preserved;
:class:`RemediationTrackerReconciler` (the per-PR remediation-tracker records),
which holds the single long-lived ``MaestroAgent.remediation_tracker`` instance;
the *record-only* :class:`SettledVerdictReconciler` (the window-independent
settled-verdicts phantom-rule pre-filter — the one store this owner *writes*
rather than clears), which holds the agent-selected
:class:`~maestro.audit_checks_settlement.SettledVerdictsBackend`;
:class:`StandDownVerdictReconciler` (the ``stand_down_verdicts`` suppression rows,
released head-agnostically on settlement), which holds the agent-selected
:class:`~maestro.remediation_tracker.StandDownVerdictBackend`; and
:class:`PRLedgerReconciler` (the per-PR remediation ledger — the origin-head SHA,
the per-head attempt budget, and the sticky ``stuck`` latch, forgotten per-PR on
settlement), which holds the agent-selected
:class:`~maestro.pr_ledger.PRLedger` (proposal 106; the escalation, stand-down,
and PR-ledger backends a JSONL/JSON sidecar by default, or the shared SQLite
store behind ``[paths] sqlite_state_enabled``). None can sit in the
agent-context-free module registry; all are instead appended to a per-agent
registry the agent passes at the drive site. The drive (in
:func:`~maestro.loop.self_audit.audit_self_model`) runs the registry
recurrently over the settled PRs the mechanical self-audit cycle observes this
cycle — those already in the per-iteration merge-truth memo, so the drive adds no
merge-oracle calls. The whole path stays gated behind the
``[audit] settlement_record_reconciler_enabled`` flag. Every store mutation is
idempotent, so a recurring pass is a no-op once the store is already in its
target state, and the driver writes a provenance row only when a reconciler
actually changed its store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from maestro.audit_checks_settlement import (
    upsert_settled_pr,
)
from maestro.consciousness import Consciousness
from maestro.escalation_bridge import EscalationLedgerBackend, clear_standdown
from maestro.fileutil import file_lock
from maestro.pr_facts import MergeTruth
from maestro.pr_ledger import PRLedger
from maestro.remediation_tracker import has_live_stand_down, release_stand_downs

if TYPE_CHECKING:
    from maestro.audit_checks_settlement import (
        SettledVerdictsBackend,
    )
    from maestro.remediation_tracker import (
        RemediationTracker,
        StandDownVerdictBackend,
    )

# Durable, additive, operator-inspectable ledger the on-settlement record
# reconciler (proposal 103) appends to — one provenance row each time a registry
# reconciler *changes* a settled PR's store (clears a trace, or attaches an
# operator-facing detail note); a silent no-op pass over an already-clean store
# writes nothing, so the ledger stays bounded even under a recurring per-cycle
# drive over the same settled PR. Absent until the first such change, never read
# by a hot path, and append-only JSONL, mirroring the append-on-transition
# ``settled_verdicts.jsonl`` idiom in :mod:`~maestro.audit_checks`.
_SETTLEMENT_RECONCILIATIONS_LEDGER = "settlement_reconciliations.jsonl"


@dataclass(frozen=True)
class ReconcileResult:
    """One reconciler's outcome for a single settled PR.

    Frozen so a reconciler cannot mutate its own verdict after returning it,
    mirroring :class:`~maestro.task_gates.GateOutcome`. ``cleared`` records
    whether the reconciler actually *changed its store this pass* — a trace
    *removal* for the clearing reconcilers, a *recorded settlement* for the
    record-only :class:`SettledVerdictReconciler` — so a store that was already
    in its target state (no trace to remove, or the settlement already recorded)
    returns ``cleared=False``; ``detail`` is an optional operator-facing note.
    """

    cleared: bool
    detail: str | None = None


@dataclass(frozen=True)
class SettlementReconciliationRecord:
    """One reconciler-driven store change for a settled PR — one provenance row.

    The typed form of the append-only ``settlement_reconciliations.jsonl`` row the
    driver has always written (``{pr_number, reconciler, cleared, detail,
    at_iteration}``): the PR that settled, the reconciler that changed its store,
    whether it cleared/recorded a trace this pass, an optional operator-facing
    ``detail`` note, and the drive's cycle counter. Frozen so a built row cannot be
    mutated before it lands, mirroring :class:`ReconcileResult`. Append-only with a
    single operator consumer and no hot-path reader (proposal 106), so — like
    :class:`~maestro.remediation_tracker.ProvenanceRecord` — it carries no unique
    key and no ``build_*`` constructor: the driver stamps every field.
    """

    pr_number: int
    reconciler: str
    cleared: bool
    detail: str | None
    at_iteration: int


@runtime_checkable
class SettlementReconciler(Protocol):
    """One per-store reconciler the driver dispatches on settlement.

    Mirrors the house ``@runtime_checkable`` :class:`typing.Protocol` idiom of
    :class:`~maestro.providers.base.Provider`: ``@runtime_checkable`` enables a
    structural ``isinstance`` check of the surface (attribute/method presence
    only — it does not verify signatures). A concrete reconciler owns exactly one
    PR-keyed store and clears that store's trace for a settled PR.
    """

    #: Stable identifier recorded in each provenance ledger row.
    name: str

    def reconcile(
        self, data_dir: Path, pr_number: int, truth: MergeTruth, iteration: int,
    ) -> ReconcileResult:
        """Reconcile this store for the settled PR ``pr_number``.

        ``iteration`` is the drive's cycle counter, threaded through for a
        reconciler that records it into its own store's row (the settled-verdict
        recorder); the trace-clearing reconcilers ignore it.
        """
        ...


class EscalationStanddownReconciler:
    """Clear a settled PR's escalation-handoff standdown (proposal 103, store 1).

    The registry's first concrete :class:`SettlementReconciler`. It owns exactly
    one PR-keyed store — the escalation-handoff standdowns
    (``escalation_handoffs.json`` by default, or ``state.db`` behind
    ``[paths] sqlite_state_enabled``) — and removes that store's trace for a
    settled PR by delegating to :func:`~maestro.escalation_bridge.clear_standdown`.
    That primitive is idempotent (a no-op returning ``False`` when the PR carries
    no standdown), so a recurring drive over an already-clean store reports
    ``cleared=False`` with no detail — a silent no-op the driver records with no
    ledger row, never a spurious side effect. The clear is gated on
    ``truth.settled`` so a not-settled PR the ledger-backed candidate set still
    names (a reopened PR, say) is never cleared — see :meth:`reconcile`.

    Since the standdown ledger moved behind a
    :class:`~maestro.escalation_bridge.EscalationLedgerBackend` (proposal 106),
    this reconciler is **stateful**: like :class:`RemediationTrackerReconciler`,
    :class:`StandDownVerdictReconciler`, and :class:`PRLedgerReconciler`, and
    unlike the ``data_dir``-only :class:`SelfModelReconciler`, its constructor
    takes the agent-selected backend and :meth:`reconcile` clears through it, so
    it cannot sit in the agent-context-free module :data:`RECONCILER_REGISTRY`.
    The agent appends ``EscalationStanddownReconciler(self.escalation_standdowns)``
    to its per-agent registry at the drive site — placed **first** so the
    historical reconciler iteration order is preserved. ``data_dir`` is ignored:
    the injected backend already owns its store.
    """

    #: Stable identifier recorded in each provenance ledger row.
    name = "escalation_standdown"

    def __init__(self, backend: EscalationLedgerBackend) -> None:
        self._backend = backend

    def reconcile(
        self, data_dir: Path, pr_number: int, truth: MergeTruth, iteration: int,
    ) -> ReconcileResult:
        """Remove ``pr_number``'s escalation standdown when ``truth`` is settled.

        Gates the clear on ``truth.settled``: the driver is meant to reach a
        reconciler only for a settled PR, but the caller's candidate set is the
        ledger-backed settled set, which can still name a PR the live signal now
        reports *not* settled — a closed PR reopened after its ``Remediate``
        record scrolled out of the retraction window. Clearing such a PR's
        standdown would violate the clear-*on-settlement* contract, so a
        not-settled ``truth`` is a no-op (``cleared=False``). Otherwise delegates
        to :func:`~maestro.escalation_bridge.clear_standdown` over the injected
        backend, itself idempotent (``False`` when the PR carries no standdown).
        Ignores ``data_dir``: the injected backend already owns its store.
        """
        if not truth.settled:
            return ReconcileResult(cleared=False, detail=None)
        cleared = clear_standdown(self._backend, pr_number)
        detail = "removed escalation standdown" if cleared else None
        return ReconcileResult(cleared=cleared, detail=detail)


class SelfModelReconciler:
    """Evict a settled PR's traces from the self-model (proposal 103, store 2).

    Owns exactly one PR-keyed store — the self-model (``self_model.json``): the
    ``limitations_known`` entries, the ``[audit]``/``[limitation]``-prefixed
    ``open_questions`` rows, and a ``next_task_directive`` — and removes those of
    its rows whose *sole* cited PR is the settled one by delegating the in-memory
    decision to :meth:`~maestro.consciousness.SelfModel.reconcile_settled_pr`
    (itself gated on ``truth.settled``, so a not-settled PR the caller's
    candidate set still names is never cleared). It persists only when that
    mutation reports a real change, so a recurring drive over an already-clean
    self-model reports ``cleared=False`` with no detail — a silent no-op the
    driver records with no ledger row. Because the persist only *removes* rows or
    clears the directive, :meth:`~maestro.consciousness.Consciousness.save_self_model`'s
    cap and truncation enforcement can never be violated by this write.
    """

    #: Stable identifier recorded in each provenance ledger row.
    name = "self_model"

    def reconcile(
        self, data_dir: Path, pr_number: int, truth: MergeTruth, iteration: int,
    ) -> ReconcileResult:
        """Drop ``pr_number``'s sole-PR self-model rows when ``truth`` settled.

        Loads the self-model under ``data_dir``, applies the pure in-memory
        :meth:`~maestro.consciousness.SelfModel.reconcile_settled_pr`, and — only
        when it reports a change — saves the shrunk model and returns
        ``cleared=True`` with an operator-facing detail note. When nothing
        changed (a not-settled ``truth``, or no row whose sole cited PR is
        ``pr_number``) it neither writes nor reports a change.
        """
        consciousness = Consciousness(data_dir)
        model = consciousness.load_self_model()
        if not model.reconcile_settled_pr(pr_number, truth):
            return ReconcileResult(cleared=False, detail=None)
        consciousness.save_self_model(model)
        return ReconcileResult(
            cleared=True, detail="evicted settled-PR self-model rows",
        )


class RemediationTrackerReconciler:
    """Forget a settled PR's remediation-tracker records (proposal 103, store 3).

    Owns exactly one PR-keyed store — the per-PR remediation outcomes the
    :class:`~maestro.remediation_tracker.RemediationTracker` gates re-routing on
    (its ``_data`` / ``_last_items`` / ``_skips`` / ``_merge_block_skips`` axes)
    — and forgets a settled PR's entry by delegating to
    :meth:`~maestro.remediation_tracker.RemediationTracker.evict_settled`.

    Unlike the escalation-standdown and self-model reconcilers, which reach their
    stores through ``data_dir`` on each call, the remediation tracker is a single
    long-lived in-memory instance (``MaestroAgent.remediation_tracker``,
    constructed once at agent init and never reloaded) whose every mutation
    re-persists the *entire* in-memory dict. A reconciler that opened a fresh
    ``RemediationTracker`` to evict on disk would be silently clobbered the next
    time the shared instance persisted, and the recurring per-cycle drive would
    re-evict and re-log it forever. So this reconciler is **stateful**: its
    constructor takes the live tracker (injected — imported only under
    ``TYPE_CHECKING`` to avoid an import cycle, never constructed here) and
    :meth:`reconcile` mutates that shared instance directly, exactly as the
    branch-state ``reset(pr_number)`` call already does. ``evict_settled`` persists
    only when it actually removed something, so a recurring drive over an
    already-clean store reports ``cleared=False`` with no detail — a silent no-op
    the driver records with no ledger row. ``data_dir`` is ignored: the injected
    tracker already owns its path.
    """

    #: Stable identifier recorded in each provenance ledger row.
    name = "remediation_tracker"

    def __init__(self, tracker: RemediationTracker) -> None:
        self._tracker = tracker

    def reconcile(
        self, data_dir: Path, pr_number: int, truth: MergeTruth, iteration: int,
    ) -> ReconcileResult:
        """Evict ``pr_number``'s tracker records when ``truth`` is settled.

        Gates the clear on ``truth.settled`` — mirroring
        :class:`EscalationStanddownReconciler` — so a PR the ledger-backed
        candidate set still names but the live signal now reports *not* settled (a
        reopened PR) is never cleared. Otherwise delegates to the injected
        tracker's :meth:`~maestro.remediation_tracker.RemediationTracker.evict_settled`,
        itself idempotent (``False``, no persist, when the PR carries no records),
        and reports ``cleared`` with a detail note only on a real eviction.
        Ignores ``data_dir``: the injected tracker already owns its path.
        """
        if not truth.settled:
            return ReconcileResult(cleared=False, detail=None)
        cleared = self._tracker.evict_settled(pr_number)
        detail = (
            "evicted settled-PR remediation-tracker entry" if cleared else None
        )
        return ReconcileResult(cleared=cleared, detail=detail)


class StandDownVerdictReconciler:
    """Release a settled PR's stand-down verdict (proposal 103, store 4).

    Owns exactly one PR-keyed store — the stand-down verdicts in
    ``stand_down_verdicts.jsonl`` — and lifts a settled PR's suppression by
    appending one head-agnostic release row through
    :func:`~maestro.remediation_tracker.release_stand_downs`. Until now those
    rows were voided only by an exact ``(pr, head_sha)`` SHA-miss (a new commit
    changes the head) or by a human demand's head-agnostic release row; neither
    fires on merge, so a settled PR's suppression lingered — the dequeue gate
    kept consulting :func:`~maestro.remediation_tracker.find_stand_down` for a PR
    already done — until a head change or message happened to release it. This
    reconciler makes settlement itself the release.

    The clear is head-agnostic by design: a settled PR's suppression must lift
    whatever its last recorded head SHA was, which the exact-SHA release cannot
    express. Since the stand-down ledger moved behind a
    :class:`~maestro.remediation_tracker.StandDownVerdictBackend` (proposal 106) —
    a JSONL sidecar by default, or the shared SQLite store behind
    ``[paths] sqlite_state_enabled`` — this reconciler is **stateful**: like
    :class:`RemediationTrackerReconciler` and :class:`SettledVerdictReconciler`,
    and unlike the ``data_dir``-only reconcilers, its constructor takes the
    agent-selected backend and :meth:`reconcile` reads/writes through it, so it
    cannot sit in the agent-context-free module :data:`RECONCILER_REGISTRY`. The
    agent appends ``StandDownVerdictReconciler(self.stand_down_verdicts)`` to its
    per-agent registry at the drive site, exactly as it appends the tracker-bound
    reconciler. To stay idempotent under the recurring per-cycle drive it appends
    a release row *only* when a live (unreleased) verdict actually exists — probed
    by :func:`~maestro.remediation_tracker.has_live_stand_down` — so an
    already-clear store returns ``cleared=False`` with no detail, a silent no-op
    the driver records with no ledger row, never a fresh release row every cycle.
    ``data_dir`` is ignored: the injected backend already owns its store. The clear
    is gated on ``truth.settled`` so a reopened PR the ledger-backed candidate set
    still names is never released — see :meth:`reconcile`.
    """

    #: Stable identifier recorded in each provenance ledger row.
    name = "stand_down_verdict"

    def __init__(self, backend: StandDownVerdictBackend) -> None:
        self._backend = backend

    def reconcile(
        self, data_dir: Path, pr_number: int, truth: MergeTruth, iteration: int,
    ) -> ReconcileResult:
        """Release ``pr_number``'s stand-down verdict when ``truth`` is settled.

        Gates the clear on ``truth.settled`` — mirroring
        :class:`EscalationStanddownReconciler` — so a PR the ledger-backed
        candidate set still names but the live signal now reports *not* settled (a
        reopened PR) is never released. Otherwise probes
        :func:`~maestro.remediation_tracker.has_live_stand_down` over the injected
        backend: only when a live verdict exists does it append a head-agnostic
        release row through :func:`~maestro.remediation_tracker.release_stand_downs`
        and report ``cleared=True`` with a detail note. An already-clear store (no
        live verdict) is a no-op (``cleared=False``, ``detail=None``), keeping the
        recurring drive idempotent and the append-only ledger bounded. Ignores
        ``data_dir``: the injected backend already owns its store.
        """
        if not truth.settled:
            return ReconcileResult(cleared=False, detail=None)
        if not has_live_stand_down(pr_number, self._backend):
            return ReconcileResult(cleared=False, detail=None)
        release_stand_downs(pr_number, self._backend)
        return ReconcileResult(
            cleared=True, detail="released settled-PR stand-down verdict",
        )


class PRLedgerReconciler:
    """Forget a settled PR's remediation-ledger entry (proposal 103, store 5).

    Owns exactly one PR-keyed store — the per-PR remediation ledger
    (:class:`~maestro.pr_ledger.PRLedger`): the origin-head SHA, the per-head
    attempt budget, and the sticky ``stuck`` latch — and forgets a settled PR's
    row by delegating to :meth:`~maestro.pr_ledger.PRLedger.evict_settled`. That
    per-PR delete is distinct from the bulk
    :meth:`~maestro.pr_ledger.PRLedger.prune` the tend pass runs over the open-PR
    set: ``prune`` only fires when a tend pass enumerates open PRs, so a
    merged/closed PR's row lingers until the next tend happens to sweep it; this
    reconciler forgets it the moment settlement is observed, mirroring
    :class:`RemediationTrackerReconciler`'s own settlement eviction.

    Since the per-PR ledger moved behind a
    :class:`~maestro.pr_ledger.PRLedgerBackend` (proposal 106) —
    ``pr_ledger.json`` by default, or the shared SQLite store behind
    ``[paths] sqlite_state_enabled`` — this reconciler is **stateful**: like
    :class:`RemediationTrackerReconciler`, :class:`SettledVerdictReconciler`, and
    :class:`StandDownVerdictReconciler`, and unlike the ``data_dir``-only
    reconcilers, its constructor takes the agent-selected live ``PRLedger``
    (already wrapping the flag-selected backend) and :meth:`reconcile` evicts
    through it, so it cannot sit in the agent-context-free module
    :data:`RECONCILER_REGISTRY`. The agent appends
    ``PRLedgerReconciler(self.pr_ledger)`` to its per-agent registry at the drive
    site, exactly as it appends the tracker-bound reconciler. ``data_dir`` is
    ignored: the injected ledger already owns its store, and ``evict_settled``
    never consults the attempt budget so the configured budget is immaterial to
    the delete. ``evict_settled`` persists only when it actually removed a row, so
    a recurring drive over an already-forgotten PR reports ``cleared=False`` with
    no detail — a silent no-op the driver records with no ledger row. The clear is
    gated on ``truth.settled`` so a reopened PR the ledger-backed candidate set
    still names is never evicted — see :meth:`reconcile`.
    """

    #: Stable identifier recorded in each provenance ledger row.
    name = "pr_ledger"

    def __init__(self, ledger: PRLedger) -> None:
        self._ledger = ledger

    def reconcile(
        self, data_dir: Path, pr_number: int, truth: MergeTruth, iteration: int,
    ) -> ReconcileResult:
        """Evict ``pr_number``'s ledger entry when ``truth`` is settled.

        Gates the clear on ``truth.settled`` — mirroring
        :class:`EscalationStanddownReconciler` — so a PR the ledger-backed
        candidate set still names but the live signal now reports *not* settled (a
        reopened PR) is never evicted. Otherwise delegates to the injected
        ledger's :meth:`~maestro.pr_ledger.PRLedger.evict_settled`, itself
        idempotent (``False``, no persist, when the PR carries no ledger row), and
        reports ``cleared`` with a detail note only on a real eviction. Ignores
        ``data_dir``: the injected ledger already owns its store.
        """
        if not truth.settled:
            return ReconcileResult(cleared=False, detail=None)
        cleared = self._ledger.evict_settled(pr_number)
        detail = "evicted settled-PR ledger entry" if cleared else None
        return ReconcileResult(cleared=cleared, detail=detail)


class SettledVerdictReconciler:
    """Record a settled PR into the settled-verdicts pre-filter (proposal 103, store 6).

    The one *record-only* reconciler — every other registry entry **clears** a
    trace, this one **writes** one. It owns exactly one PR-keyed store — the
    window-independent settled-verdicts pre-filter (read by
    :func:`~maestro.audit_checks_settlement.load_settled_prs`) that suppresses the
    phantom-remediation audit rules once a PR has settled — and records the
    settlement by delegating to :func:`~maestro.audit_checks_settlement.upsert_settled_pr`,
    which appends exactly one settle record and is itself settled-gated and
    idempotent (``False``, no append, when the PR is already effectively
    recorded). Folding this write into the registry is what makes *every*
    on-settlement PR-keyed store mutation flow through the single
    :func:`reconcile_pr_settlement` owner and land a uniform, operator-inspectable
    provenance row.

    Since the settled-verdicts pre-filter moved behind a
    :class:`~maestro.audit_checks_settlement.SettledVerdictsBackend` (proposal 106) — a
    JSONL sidecar by default, or the shared SQLite store behind
    ``[paths] sqlite_state_enabled`` — this reconciler is **stateful**: like
    :class:`RemediationTrackerReconciler`, and unlike the ``data_dir``-only
    reconcilers, its constructor takes the agent-selected backend and
    :meth:`reconcile` writes through it, so it cannot sit in the
    agent-context-free module :data:`RECONCILER_REGISTRY`. The agent appends
    ``SettledVerdictReconciler(self.settled_verdicts)`` to its per-agent registry
    at the drive site, exactly as it appends the tracker-bound reconciler.
    ``data_dir`` is ignored: the injected backend already owns its store. It
    threads ``iteration`` through to the settle record's ``settled_at_iteration``.
    The record is gated on ``truth.settled`` (mirroring the other reconcilers'
    first-line early-return, and matching :func:`upsert_settled_pr`'s own gate) so
    a reopened PR the ledger-backed candidate set still names is never recorded as
    settled — see :meth:`reconcile`. Its ``ReconcileResult.cleared`` reads as
    *changed the store this pass* (a recorded settlement), not a trace removal, so
    an already recorded PR reports ``cleared=False`` with no detail — a silent
    no-op the driver records with no ledger row, keeping the recurring per-cycle
    drive idempotent.
    """

    #: Stable identifier recorded in each provenance ledger row.
    name = "settled_verdict"

    def __init__(self, backend: SettledVerdictsBackend) -> None:
        self._backend = backend

    def reconcile(
        self, data_dir: Path, pr_number: int, truth: MergeTruth, iteration: int,
    ) -> ReconcileResult:
        """Record ``pr_number``'s settlement when ``truth`` is settled.

        Gates the record on ``truth.settled`` — mirroring
        :class:`EscalationStanddownReconciler` — so a PR the ledger-backed
        candidate set still names but the live signal now reports *not* settled (a
        reopened PR) is never recorded. Otherwise delegates to
        :func:`~maestro.audit_checks_settlement.upsert_settled_pr` over the injected backend,
        itself settled-gated and idempotent (``False``, no append, when the PR is
        already recorded), and maps its ``bool`` return onto ``cleared`` — ``True``
        with a detail note only when a settle record was actually written. Threads
        ``iteration`` into the settle record's ``settled_at_iteration``. Ignores
        ``data_dir``: the injected backend already owns its store.
        """
        if not truth.settled:
            return ReconcileResult(cleared=False, detail=None)
        wrote = upsert_settled_pr(self._backend, pr_number, truth, iteration)
        detail = (
            "recorded settlement into settled-verdicts pre-filter" if wrote else None
        )
        return ReconcileResult(cleared=wrote, detail=detail)


#: The ordered registry the driver iterates (first writer wins by list order).
#: Holds only :class:`SelfModelReconciler` — the sole reconciler constructible
#: without agent context. The agent composes the five **stateful** reconcilers —
#: :class:`EscalationStanddownReconciler` (holding the flag-selected
#: :class:`~maestro.escalation_bridge.EscalationLedgerBackend`, prepended
#: **first** so the historical iteration order is preserved),
#: :class:`RemediationTrackerReconciler` (holding the live tracker), the
#: record-only :class:`SettledVerdictReconciler` (holding the selected
#: :class:`~maestro.audit_checks_settlement.SettledVerdictsBackend`),
#: :class:`StandDownVerdictReconciler` (holding the selected
#: :class:`~maestro.remediation_tracker.StandDownVerdictBackend`), and
#: :class:`PRLedgerReconciler` (holding the flag-selected
#: :class:`~maestro.pr_ledger.PRLedger`, proposal 106) — around this tuple at the
#: drive site, since none can be constructed without agent context. Callers that
#: pass an explicit empty registry keep the pure-no-op path (see the helper tests).
RECONCILER_REGISTRY: tuple[SettlementReconciler, ...] = (
    SelfModelReconciler(),
)


def _append_reconciliation_row(path: Path, row: dict[str, object]) -> None:
    """Append one provenance row to the ledger under its own lock.

    Each row is flushed in its own :func:`~maestro.fileutil.file_lock` +
    ``open(path, "a")`` block (the append-only JSONL idiom of
    :func:`~maestro.audit_checks_settlement.reconcile_settled_verdicts`) so a completed
    reconciler's provenance is durable *before* the next reconciler runs — the
    ledger is never opened until there is a row to write, so an immediate raise
    leaves no empty file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path), open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


@runtime_checkable
class SettlementReconciliationBackend(Protocol):
    """The settlement-reconciliation provenance persistence surface (proposal 106).

    One writer (:func:`reconcile_pr_settlement`) and no hot-path reader go through
    this seam — the trace is append-only and operator-inspectable only — so the
    ledger can live in ``settlement_reconciliations.jsonl`` or in ``state.db``
    behind ``[paths] sqlite_state_enabled`` with no driver branching. Mirrors the
    append-only :class:`~maestro.remediation_tracker.PrProvenanceBackend` idiom;
    ``@runtime_checkable`` enables a structural ``isinstance`` check in the agent's
    backend-selection tests.
    """

    def append(self, record: SettlementReconciliationRecord) -> None:
        """Append one reconciler-driven store-change row (append-only, no reader)."""
        ...


class JsonlSettlementReconciliationLedger:
    """JSONL-backed :class:`SettlementReconciliationBackend` — the default store.

    Binds a ``data_dir`` and delegates ``append`` to the module-level
    :func:`_append_reconciliation_row` — which stays the JSONL write core, so the
    per-row :func:`~maestro.fileutil.file_lock` + ``open(path, "a")`` body (and its
    lazy file creation: the ledger is never opened until there is a row to write)
    keep one code path shared with the pre-proposal-106 driver. Serializes the
    record back to the same ``{pr_number, reconciler, cleared, detail,
    at_iteration}`` dict the ledger has always held.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def append(self, record: SettlementReconciliationRecord) -> None:
        _append_reconciliation_row(
            self._data_dir / _SETTLEMENT_RECONCILIATIONS_LEDGER,
            {
                "pr_number": record.pr_number,
                "reconciler": record.reconciler,
                "cleared": record.cleared,
                "detail": record.detail,
                "at_iteration": record.at_iteration,
            },
        )


def _reconciliation_record_from_row(
    parsed: object,
) -> SettlementReconciliationRecord | None:
    """Return a :class:`SettlementReconciliationRecord` from a decoded row, or ``None``.

    A row is dropped when it is not a dict or any field is missing / the wrong
    type — ``pr_number`` / ``at_iteration`` must be non-``bool`` ints,
    ``reconciler`` a str, ``cleared`` a bool, and ``detail`` either ``None`` or a
    str — mirroring the strict, corrupt-tolerant guard of
    :func:`~maestro.remediation_tracker._provenance_record_from_row`. ``bool`` is
    rejected for the integer fields because it subclasses ``int``.
    """
    if not isinstance(parsed, dict):
        return None
    pr_number = parsed.get("pr_number")
    reconciler = parsed.get("reconciler")
    cleared = parsed.get("cleared")
    at_iteration = parsed.get("at_iteration")
    detail = parsed.get("detail")
    if (
        not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or not isinstance(reconciler, str)
        or not isinstance(cleared, bool)
        or not isinstance(at_iteration, int)
        or isinstance(at_iteration, bool)
        or (detail is not None and not isinstance(detail, str))
    ):
        return None
    return SettlementReconciliationRecord(
        pr_number=pr_number,
        reconciler=reconciler,
        cleared=cleared,
        detail=detail,
        at_iteration=at_iteration,
    )


def load_all_settlement_reconciliations(
    data_dir: Path,
) -> list[SettlementReconciliationRecord]:
    """Return every reconciliation row oldest-first — the SQLite seed source.

    Reads the whole ``settlement_reconciliations.jsonl`` in file (append /
    oldest-first) order with corrupt-tail tolerance: blank lines, lines that do
    not JSON-decode, and rows that fail :func:`_reconciliation_record_from_row`
    are silently skipped; a missing file yields ``[]``. Seeding the SQLite store in
    chronological order lets its ``AUTOINCREMENT`` ``id`` reproduce the append
    order, mirroring
    :func:`~maestro.remediation_tracker.load_all_provenance_records`. A real call
    site (the :class:`~maestro.state_store.SqliteSettlementReconciliationLedger`
    one-time seed), so it does not trip the self-review uncalled-symbol gate.
    """
    path = data_dir / _SETTLEMENT_RECONCILIATIONS_LEDGER
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        lines = list(f)
    records: list[SettlementReconciliationRecord] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        record = _reconciliation_record_from_row(parsed)
        if record is None:
            continue
        records.append(record)
    return records


def reconcile_pr_settlement(
    data_dir: Path,
    pr_number: int,
    truth: MergeTruth,
    *,
    registry: tuple[SettlementReconciler, ...] = RECONCILER_REGISTRY,
    backend: SettlementReconciliationBackend,
    iteration: int,
) -> list[ReconcileResult]:
    """Drive every registered reconciler over one settled PR.

    Iterates *registry* in order (mirroring
    :func:`~maestro.task_gates.run_gate_chain`), calling each reconciler's
    :meth:`SettlementReconciler.reconcile`. A :class:`SettlementReconciliationRecord`
    — ``{pr_number, reconciler, cleared, detail, at_iteration}`` — is appended
    through the agent-selected *backend* (a JSONL sidecar by default, or the shared
    SQLite store behind ``[paths] sqlite_state_enabled``, proposal 106)
    **immediately after that reconciler returns** *only when the reconciler
    reported a change*: it cleared a trace (``cleared=True``) or attached an
    operator-facing ``detail`` note. A silent no-op pass (``cleared=False`` with no
    detail) —
    what an already-clean store returns on every recurring cycle — writes
    nothing, so the ledger stays a bounded state-change log even as the audit
    cycle re-drives the same settled PR each iteration, mirroring the
    append-on-transition ``settled_verdicts.jsonl`` idiom this module cites as
    its model. Nothing is written when the registry is empty. A raising
    reconciler is **not** swallowed — it propagates (fail-loud invariant).
    Because rows are flushed per reconciler rather than batched at the end, a
    mid-loop raise leaves the rows of the reconcilers that already ran (and
    changed their PR-keyed stores) durably recorded, while the raising
    reconciler — which did not complete — writes no row: provenance follows the
    side effects that actually happened. Returns the per-reconciler
    :class:`ReconcileResult` list for caller/test inspection.
    """
    results: list[ReconcileResult] = []
    for reconciler in registry:
        result = reconciler.reconcile(data_dir, pr_number, truth, iteration)
        results.append(result)
        # Record only an actual change — a clear, or a no-clear pass carrying a
        # detail note. Skipping the silent no-op row (``cleared=False`` with no
        # detail) that an already-clean store returns each recurring cycle keeps
        # the durable ledger from growing without bound as the audit cycle
        # re-drives the same warm-memo settled PR every iteration.
        if result.cleared or result.detail is not None:
            backend.append(
                SettlementReconciliationRecord(
                    pr_number=pr_number,
                    reconciler=reconciler.name,
                    cleared=result.cleared,
                    detail=result.detail,
                    at_iteration=iteration,
                ),
            )

    return results
