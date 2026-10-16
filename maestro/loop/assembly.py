"""Agent construction carved out of ``maestro.agent`` (proposal 147, Step 8).

Holds the ``MaestroAgent.__init__`` wiring body: it binds the injected
collaborators, builds the ledgers, trackers and stores the loop reads, the
settlement reconciler registry, the VCS / code-host / issue clients, the
phase-runner registry and the extracted controllers. It moves intact to
bring ``maestro/agent.py`` under its source budget.

The extraction is behaviour-preserving: the same body, the same order, the
same effects — only the receiver changed from ``self`` to a first positional
``agent`` argument, and the per-assignment annotations moved to class-level
declarations on ``MaestroAgent`` (an annotation on a non-``self`` attribute
target is not a declaration the type checker accepts). ``MaestroAgent.__init__``
is a one-call stub forwarding every parameter. Every module global the body
reads is imported from its defining module, so a test patches it as
``maestro.loop.assembly.<name>``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from maestro import conscious_workspace, deed_ledger
from maestro.audit_checks import (
    JsonImpactReviewSurfacedStore,
    JsonlGateActivationLedger,
)
from maestro.audit_checks_calibration import (
    JsonlPredictionLedger,
    JsonlPremiseLedger,
)
from maestro.audit_checks_phantom import (
    JsonOrphanedCureStore,
    JsonUnconfirmedOwnLandingStore,
)
from maestro.audit_checks_settlement import (
    JsonlSettledVerdictsLedger,
)
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.contributions import ContributionTracker
from maestro.cost_tracking import JsonlCostSampleStore
from maestro.directive_ledger import JsonlDroppedDirectiveLedger
from maestro.escalation_bridge import JsonEscalationLedger
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.lever_capture import JsonlLeverAttemptsLedger
from maestro.loop.iteration import _LOOP_PHASES
from maestro.loop.lifecycle import LifecycleController
from maestro.loop.phases import REGISTERED_PHASE_NAMES, validate_registered_phase_order
from maestro.loop.pr_remediation import PrRemediationController
from maestro.loop.task_generation import TaskGenerator
from maestro.memory import JsonlVersionArchiveLedger, Memory
from maestro.outcome_grounding import JsonMergeRequestServicedStore
from maestro.planner import Planner
from maestro.pr_ledger import JsonPRLedger, PRLedger
from maestro.remediation_tracker import (
    JsonlFetchTruthLedger,
    JsonlObligationForkLedger,
    JsonlPrProvenanceLedger,
    JsonlStandDownVerdictLedger,
    JsonPreflightPassLedger,
    JsonRemediationTrackerStore,
    RemediationTracker,
)
from maestro.settlement_record_reconciler import (
    RECONCILER_REGISTRY,
    EscalationStanddownReconciler,
    JsonlSettlementReconciliationLedger,
    PRLedgerReconciler,
    RemediationTrackerReconciler,
    SettledVerdictReconciler,
    StandDownVerdictReconciler,
)
from maestro.state_store import (
    SqliteConsciousEpisodeLedger,
    SqliteCostSampleStore,
    SqliteDeedLedger,
    SqliteDroppedDirectiveLedger,
    SqliteEscalationLedger,
    SqliteFetchTruthLedger,
    SqliteGateActivationLedger,
    SqliteImpactReviewSurfacedStore,
    SqliteLeverAttemptsLedger,
    SqliteMergeRequestServicedStore,
    SqliteObligationForkLedger,
    SqliteOrphanedCureStore,
    SqliteOwnedBacklogLedger,
    SqlitePredictionLedger,
    SqlitePreflightPassStore,
    SqlitePremiseLedger,
    SqlitePRLedger,
    SqlitePrProvenanceLedger,
    SqliteRemediationTrackerStore,
    SqliteSettledVerdictsLedger,
    SqliteSettlementReconciliationLedger,
    SqliteStandDownVerdictLedger,
    SqliteTaskQueue,
    SqliteUnconfirmedOwnLandingStore,
    SqliteVersionArchiveLedger,
    StateStore,
)
from maestro.stats import JsonlOwnedBacklogLedger
from maestro.task_queue import TaskQueue

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.email import EmailClient
    from maestro.evolution import EvolutionEngine
    from maestro.issues import IssueTracker
    from maestro.messaging import MessageStore
    from maestro.notifications.base import Notifier
    from maestro.vcs import VCSClient

logger = logging.getLogger("maestro")


def assemble_agent(
    agent: MaestroAgent,
    config: Config,
    consciousness: Consciousness,
    journal: Journal,
    memory: Memory,
    executor: Executor,
    planner: Planner,
    evolution: EvolutionEngine,
    notifier: Notifier,
    contributions: ContributionTracker | None = None,
    message_store: MessageStore | None = None,
    vcs: VCSClient | None = None,
    email_client: EmailClient | None = None,
    issues: IssueTracker | None = None,
    agent_tools: dict[str, Any] | None = None,
) -> None:
    agent.config = config
    agent.consciousness = consciousness
    agent.journal = journal
    agent.memory = memory
    agent.executor = executor
    agent.planner = planner
    agent.evolution = evolution
    agent.notifier = notifier
    # The introspection tool registry (``{"registry": ToolRegistry}``, the
    # same shape ``Executor`` receives). Present only when built via
    # ``cli._build_agent``; ``None`` for the many test constructions that omit
    # it. Read solely by ``_grounded_reflection_active`` /
    # ``_build_contemplation_session_options`` to grant the priced reflection
    # its bounded read-only retrieval surface behind
    # ``[loop] grounded_reflection_enabled`` (proposal 132).
    agent._agent_tools = agent_tools
    agent.message_store = message_store
    agent.email_client = email_client
    if issues is not None:
        agent.issues = issues
    else:
        # No injected client (typically a test): build from the WorkTarget
        # owner (proposal 107) so a ``[work]`` redirect reaches the fallback
        # too. ``wt.github_repo`` defaults to ``config.github_repo``.
        from maestro.issues import IssueTracker as _IssueTracker
        agent.issues = _IssueTracker(
            config.work_target().github_repo, timeout=config.gh_timeout,
        )
    agent.contributions = contributions or ContributionTracker(
        config.workspace, config.memory_dir,
        git_timeout=config.git_quick_timeout,
    )
    # Candidate-task queue. ``generate_task`` enqueues pre-vetted ideas;
    # ``_run_task_phase`` pops the head every iteration. Default: the
    # JSON-backed queue stored alongside ``task_history.jsonl``. When
    # ``[paths] sqlite_state_enabled`` is set, the same FIFO round-trips
    # through ``state.db`` via :class:`SqliteTaskQueue`, seeded once from the
    # legacy JSON (never deleted, so flipping the flag off is a clean
    # rollback). Both satisfy :class:`TaskQueueBackend`, so no call site
    # branches on which is live.
    # Per-SDK-call cost samples share the same substrate and flag: when on,
    # one ``StateStore`` on ``state.db`` backs both the queue and the cost
    # time-series (:class:`SqliteCostSampleStore`) else JSONL, both via
    # :class:`CostSampleBackend` (prop 106).
    # The deed ledger — the append-only record of fired terminal acts — rides
    # the same flag and shares the *same* ``StateStore`` instance when on
    # (seeded once from the legacy ``deed_ledger.jsonl`` so the all-time count
    # survives the flip; the JSONL sidecar is retained as flag-off rollback).
    # The owned-backlog ledger — one snapshot per contemplation of the
    # ``pending_review`` backlog size, whose signed delta the OWNED-NET-DELTA
    # sensor surfaces — rides the same flag and shared ``StateStore`` too
    # (seeded once from ``owned_backlog_ledger.jsonl`` so the cycle-over-cycle
    # history survives the flip; the JSONL sidecar is flag-off rollback).
    # The PR-admission provenance ledger — one row per tend-route
    # re-admission into ``pending_pr_requests``, whose trailing window the
    # PR-READMISSION sensor reads — rides the same flag and shared
    # ``StateStore`` too (seeded once from ``pending_pr_provenance.jsonl`` so
    # the readmission window survives the flip; the JSONL sidecar is flag-off
    # rollback). Measurement-only: it gates nothing.
    # The obligation-fork marker ledger — one row per remediation task/turn
    # recording the HEAD-vs-``origin/<branch>`` fork the owned-``AWAITING
    # _PUSH`` verdict floor reads — rides the same flag and shared
    # ``StateStore`` too (seeded once from ``obligation_fork_markers.jsonl``
    # so in-flight fork markers survive the flip; the JSONL sidecar is
    # flag-off rollback). Unlike the sensor ledgers this one *gates*: its
    # single reader is :func:`gate_remediation_verdict`.
    # The fetch-truth marker ledger — one row per remediation task/turn
    # recording that ``git fetch`` + ``--is-ancestor`` ran before any status
    # word (the fetch-before-word floor) — is the direct sibling of the
    # obligation-fork ledger and the last remediation *marker* on raw JSONL.
    # It rides the same flag and shared ``StateStore`` too (seeded once from
    # ``fetch_truth_markers.jsonl`` so in-flight markers survive the flip; the
    # JSONL sidecar is flag-off rollback). Like the obligation-fork ledger it
    # *gates*: :func:`gate_remediation_verdict`'s fetch floor reads it.
    # The preflight-pass ledger — one ``{pr_number: head_sha}`` row per PR
    # confirmed passing the pre-remediation quality-gate preflight, read by
    # the tend dequeue's preflight-pass SHA gate to skip a confirmed no-op —
    # rides the same flag and shared ``StateStore`` too (seeded once from
    # ``preflight_passes.json`` so recorded passing heads survive the flip;
    # the JSON sidecar is flag-off rollback). Unlike the append-only marker
    # ledgers it is a per-PR *keyed replace*: only the latest passing head
    # matters, so its table upserts on the ``pr_number`` primary key.
    # Conscious-episode ledger (proposal 130) — the append-only store the
    # observe-only ``attend`` phase writes one immutable episode to per pass.
    # Dual-store like ``deed_ledger``: both adapters satisfy
    # ``ConsciousEpisodeBackend`` so every call site stays unbranched.
    # The gate-activation trace — the append-only record of every gate firing,
    # read back by the fire-rate / directive-compliance / phantom-landing /
    # narration-conversion sensors and the data-sufficiency instrument — rides
    # the same flag and shared ``StateStore`` when on (seeded once from the
    # legacy ``gate_activations.jsonl`` so the all-time trace survives the
    # flip; the JSONL sidecar is retained flag-off rollback). A pure append
    # trace: both adapters satisfy :class:`GateActivationBackend`, so the ~18
    # writer sites and the loop-side readers never branch on which is live
    # (proposal 106).
    # The prediction sensor ledger — the append-only log of task-outcome
    # predictions, priced-reflection self-predictions, and their deferred
    # verdicts, read back by the prediction-accuracy sensor, the reflection
    # confrontation, the data-sufficiency instrument, and the
    # ``measure-fork-authorship`` CLI — rides the same flag and shared
    # ``StateStore`` when on (seeded once from the legacy ``predictions
    # .jsonl`` so the all-time trace survives the flip; the JSONL sidecar is
    # retained flag-off rollback). Both adapters satisfy
    # :class:`PredictionBackend`, so every writer and reader routes through
    # the injected backend and never branches on which store is live
    # (proposal 106).
    # The premise-accuracy leading indicator (issue #2304) — the append-only
    # log of, per accepted self-directed task, how many ``.py`` paths its
    # goal referenced and how many of those are missing on disk, read back by
    # the ``measure-premise-accuracy`` CLI. Rides the same flag and shared
    # ``StateStore`` when on (seeded once from the legacy
    # ``premise_observations.jsonl`` so the trace survives the flip; the JSONL
    # sidecar is retained flag-off rollback). Both adapters satisfy
    # :class:`PremiseBackend`, so the accept-site writer and the CLI reader
    # never branch on which store is live (the proposal 106 dual-store fold).
    # The per-PR remediation ledger — one durable ``PRLedgerEntry`` per open
    # PR (its last-observed ``origin/<branch>`` head, per-head attempt budget,
    # and sticky ``stuck`` latch) — rides the same flag and shared
    # ``StateStore`` when on (seeded once from the legacy ``pr_ledger.json``
    # so the in-flight budget/latch state survives the flip; the JSON sidecar
    # is retained flag-off rollback). Both adapters satisfy
    # :class:`PRLedgerBackend`, so ``PRLedger`` and every PR-servicing reader
    # never branch on which store is live (proposal 106).
    # The per-PR remediation-repetition tracker's four gate maps — the
    # consecutive no-op streaks the re-route gate reasons over, the
    # outstanding review-item baseline, and the repetition / merge-block skip
    # counts — ride the same flag and shared ``StateStore`` when on (seeded
    # once from the legacy ``remediation_tracker.json``, whose schema version
    # rides along so a pre-#2796 sidecar still fires its one-time merge-block
    # reconciliation after the flip; the JSON sidecar is retained flag-off
    # rollback). Both adapters satisfy :class:`RemediationTrackerBackend`, so
    # ``RemediationTracker``'s gating policy never branches on which store is
    # live (proposal 106).
    # The escalation-standdown ledger — the durable ``number``-keyed "handed
    # up to Thomas — stop minting remediation laps" records (per-PR handoffs
    # plus issue-keyed claimed-escalation rows), each with its serviced
    # carve-out set and optional expiry — rides the same flag and shared
    # ``StateStore`` when on (seeded once from the legacy
    # ``escalation_handoffs.json`` so the in-flight standdowns survive the
    # flip; the JSON sidecar is retained flag-off rollback). Both adapters
    # satisfy :class:`EscalationLedgerBackend`, so every escalation reader,
    # deliverer, and reconciler routes through the injected backend and never
    # branches on which store is live (proposal 106).
    if config.sqlite_state_enabled:
        state_store = StateStore(config.memory_dir / "state.db")
        agent.task_queue = SqliteTaskQueue(
            state_store,
            legacy_json_path=config.memory_dir / "task_queue.json",
        )
        agent.cost_samples = SqliteCostSampleStore(state_store)
        agent.deed_ledger = SqliteDeedLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.conscious_episodes = SqliteConsciousEpisodeLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.owned_backlog = SqliteOwnedBacklogLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.pr_provenance = SqlitePrProvenanceLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.settlement_reconciliations = SqliteSettlementReconciliationLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.obligation_fork = SqliteObligationForkLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.fetch_truth = SqliteFetchTruthLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.preflight_passes = SqlitePreflightPassStore(
            state_store,
            legacy_json_dir=config.memory_dir,
        )
        agent.dropped_directives = SqliteDroppedDirectiveLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.settled_verdicts = SqliteSettledVerdictsLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.stand_down_verdicts = SqliteStandDownVerdictLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.unconfirmed_own_landing = SqliteUnconfirmedOwnLandingStore(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.orphaned_cure = SqliteOrphanedCureStore(
            state_store,
            legacy_json_dir=config.memory_dir,
        )
        agent.impact_review_surfaced = SqliteImpactReviewSurfacedStore(
            state_store,
            legacy_json_dir=config.memory_dir,
        )
        agent.merge_request_serviced = SqliteMergeRequestServicedStore(
            state_store,
            legacy_json_dir=config.memory_dir,
        )
        agent.lever_attempts = SqliteLeverAttemptsLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.gate_activations = SqliteGateActivationLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.predictions = SqlitePredictionLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.premise_observations = SqlitePremiseLedger(
            state_store,
            legacy_jsonl_dir=config.memory_dir,
        )
        agent.version_archive = (
            SqliteVersionArchiveLedger(
                state_store,
                legacy_jsonl_dir=config.memory_dir,
            )
        )
        agent.pr_ledger_backend = SqlitePRLedger(
            state_store,
            legacy_json_dir=config.memory_dir,
        )
        agent.remediation_tracker_backend = SqliteRemediationTrackerStore(
            state_store,
            legacy_json_dir=config.memory_dir,
        )
        agent.escalation_standdowns = SqliteEscalationLedger(
            state_store,
            legacy_json_dir=config.memory_dir,
        )
        # Single deterministic whole-fleet migration point (proposal 106).
        # Eagerly seed every legacy sidecar into the shared ``state.db`` now,
        # under the same gate, so the fleet is guaranteed present from the
        # first iteration instead of trickling in on scattered first-accesses
        # — and the later reverse-export rollback has every table populated.
        # Idempotent: a table one of the adapters above (or an earlier
        # ``_build_agent`` memory-side backend over the same ``state.db``)
        # already seeded lazily is reported ``already_seeded``, not
        # re-counted, so this one loud line stays honest.
        migration_summary = state_store.migrate_from_legacy(
            config.memory_dir,
        )
        logger.info(
            "state-store migration (state.db): %s",
            migration_summary.render(),
        )
    else:
        agent.task_queue = TaskQueue(
            config.memory_dir / "task_queue.json",
        )
        agent.cost_samples = JsonlCostSampleStore(config.cost_samples_path)
        agent.deed_ledger = deed_ledger.JsonlDeedLedger(config.memory_dir)
        agent.conscious_episodes = conscious_workspace.JsonlConsciousEpisodeLedger(
            config.memory_dir,
        )
        agent.owned_backlog = JsonlOwnedBacklogLedger(config.memory_dir)
        agent.pr_provenance = JsonlPrProvenanceLedger(config.memory_dir)
        agent.settlement_reconciliations = (
            JsonlSettlementReconciliationLedger(config.memory_dir)
        )
        agent.obligation_fork = JsonlObligationForkLedger(config.memory_dir)
        agent.fetch_truth = JsonlFetchTruthLedger(config.memory_dir)
        agent.preflight_passes = JsonPreflightPassLedger(config.memory_dir)
        agent.dropped_directives = JsonlDroppedDirectiveLedger(
            config.memory_dir,
        )
        agent.settled_verdicts = JsonlSettledVerdictsLedger(
            config.memory_dir,
        )
        agent.stand_down_verdicts = JsonlStandDownVerdictLedger(
            config.memory_dir,
        )
        agent.unconfirmed_own_landing = JsonUnconfirmedOwnLandingStore(
            config.memory_dir,
        )
        agent.orphaned_cure = JsonOrphanedCureStore(
            config.memory_dir,
        )
        agent.impact_review_surfaced = JsonImpactReviewSurfacedStore(
            config.memory_dir,
        )
        agent.merge_request_serviced = JsonMergeRequestServicedStore(
            config.memory_dir,
        )
        agent.lever_attempts = JsonlLeverAttemptsLedger(config.memory_dir)
        agent.gate_activations = JsonlGateActivationLedger(config.memory_dir)
        agent.predictions = JsonlPredictionLedger(config.memory_dir)
        agent.premise_observations = JsonlPremiseLedger(config.memory_dir)
        agent.version_archive = JsonlVersionArchiveLedger(config.memory_dir)
        agent.pr_ledger_backend = JsonPRLedger(config.memory_dir)
        agent.remediation_tracker_backend = JsonRemediationTrackerStore(
            config.memory_dir,
        )
        agent.escalation_standdowns = JsonEscalationLedger(config.memory_dir)
    agent.remediation_tracker = RemediationTracker(
        config.memory_dir,
        threshold=config.remediation_noop_threshold,
        enabled=config.remediation_noop_gate_enabled,
        backend=agent.remediation_tracker_backend,
    )
    agent.pr_ledger = PRLedger(
        config.memory_dir,
        max_attempts_per_head=config.pr_remediation_max_attempts_per_head,
        backend=agent.pr_ledger_backend,
    )
    # On-settlement record-reconciler registry (proposal 103). The module
    # ``RECONCILER_REGISTRY`` is built with no agent context, so the five
    # stateful reconcilers — the escalation-standdown one (holding the
    # flag-selected ``self.escalation_standdowns`` backend so a settled PR's
    # standdown is cleared on the live store, proposal 106), the
    # tracker-bound one (holding this shared long-lived
    # ``self.remediation_tracker`` instance and mutating it directly), the
    # record-only settled-verdict one (holding the selected
    # ``self.settled_verdicts`` backend), the stand-down-verdict one (holding
    # the selected ``self.stand_down_verdicts`` backend), and the PR-ledger
    # one (holding the flag-selected ``self.pr_ledger`` so eviction reaches
    # the live store) — cannot sit in it. Compose a per-agent registry: the
    # escalation reconciler goes **first** so the historical iteration order
    # (escalation-standdown, then the module ``SelfModelReconciler``, then the
    # rest) is preserved, and ``_audit_self_model`` passes the whole tuple to
    # the flag-gated ``reconcile_pr_settlement`` drive.
    agent._settlement_reconciler_registry = (
        EscalationStanddownReconciler(agent.escalation_standdowns),
        *RECONCILER_REGISTRY,
        RemediationTrackerReconciler(agent.remediation_tracker),
        SettledVerdictReconciler(agent.settled_verdicts),
        StandDownVerdictReconciler(agent.stand_down_verdicts),
        PRLedgerReconciler(agent.pr_ledger),
    )
    if vcs is not None:
        agent._vcs = vcs
    else:
        # Fallback build reads the WorkTarget owner's repo workspace
        # (proposal 107); defaults to ``config.workspace``.
        from maestro.vcs import SubprocessVCS
        agent._vcs = SubprocessVCS(
            config.work_target().repo_workspace,
            timeout=config.git_timeout,
            quick_timeout=config.git_quick_timeout,
        )
    # PR-remediation collaborator (proposal 068 step 1). Holds the
    # tend-feedback router, dequeue-face gates, and preflight; the
    # same-named agent methods are thin delegating shims.
    agent._pr_remediation = PrRemediationController(agent)
    # Task-generation collaborator (proposal 068 step 2). Holds the
    # ``generate_task`` queue curator with its gate stack; the agent
    # method is a one-line delegating shim.
    agent._task_generator = TaskGenerator(agent)
    # Lifecycle/throttle collaborator (proposal 068 step 3). Holds the
    # phase-cadence gate, lifecycle-cursor reader, status update, and
    # throttle/peak/maintenance transition notifiers behind shims.
    agent._lifecycle = LifecycleController(agent)
    # Phase-runner registry (proposal 106). Maps every ``_LOOP_PHASES``
    # member to its bound ``_phase_*`` method so ``run_loop``'s dispatch
    # resolves each with one table lookup — phase order and phase bodies
    # are both data now, with no surviving inline ``elif phase == "..."``
    # ladder. The registry covers the persistent-session phases
    # (``contemplate`` / ``evolve`` / ``curate_pending_plans`` share the
    # promoted ``self._session_a`` / ``self._session_b``) alongside the mechanical
    # phases. Each runner is
    # referenced by attribute here (not by string) so the symbol use is
    # statically visible — guarding against the uncalled-symbol gate. The
    # invariants are pinned in ``tests/test_loop_phases.py`` and re-asserted
    # at construction so a drift between this map and
    # ``REGISTERED_PHASE_NAMES`` / ``_LOOP_PHASES`` fails loud.
    validate_registered_phase_order(_LOOP_PHASES)
    agent._phase_runners = {
        "ensure_main_branch": agent._phase_ensure_main_branch,
        "check_contributions": agent._phase_check_contributions,
        "process_messages": agent._phase_process_messages,
        "tend_evolution_prs": agent._phase_tend_evolution_prs,
        "attend": agent._phase_attend,
        "contemplate": agent._phase_contemplate,
        "respond_to_messages": agent._phase_respond_to_messages,
        "implement_approved_plans": agent._phase_implement_approved_plans,
        "check_merged_plans": agent._phase_check_merged_plans,
        "review_artifacts": agent._phase_review_artifacts,
        "execute_approved_publications": (
            agent._phase_execute_approved_publications
        ),
        "process_rejected_plans": agent._phase_process_rejected_plans,
        "generate_task": agent._phase_generate_task,
        "run_task": agent._phase_run_task,
        "evolve": agent._phase_evolve,
        "audit_self_model": agent._phase_audit_self_model,
        "curate_pending_plans": agent._phase_curate_pending_plans,
        "dream": agent._phase_dream,
    }
    assert set(agent._phase_runners) == set(REGISTERED_PHASE_NAMES)
    # Open-evolution-PR count, updated by ``tend_evolution_prs`` each
    # pass. ``_implement_approved_plans`` compares this against
    # ``config.max_open_evolution_prs`` to enforce the concurrency cap.
    # Defaults to 0 so the gate is open before the first tend run.
    agent._open_evolution_pr_count = 0
    agent._running = False
    # Monotonic flag: once True, never reverted.  ``shutdown()`` checks
    # this instead of ``_running`` to guarantee idempotency even when
    # ``run_loop()``'s compensating ``finally`` blocks restore
    # ``_running = True`` between sub-operation exit and the outer
    # ``finally``'s second ``shutdown()`` call.  Issue #189.
    agent._shutdown_initiated = False
    agent._current_task = None
    # Set by ``run()``'s host-state stamp gate (step 4c) to the deterministic
    # ``classify_merge_block`` reason when a PR remediation was downgraded
    # ``success`` -> ``partial`` because the resolution had not reached a
    # mergeable/merged host state; ``None`` otherwise. Threaded out so
    # ``_record_remediation_outcome`` can fold the same host verdict into
    # the repetition gate without re-fetching host state (#2457).
    agent._last_remediation_merge_block = None
    # Provenance of the pre-stamp merge-truth gate's downgrade (proposal 087
    # step 5): the ``MergeTruth.source`` / ``observed_at`` of the
    # ``is_pr_settled`` observation that refused a ``success`` stamp, or
    # ``None`` when the gate did not fire. Threaded into
    # ``_record_remediation_outcome`` so the post-task tracker records *how*
    # and *when* merge state was read, not just that the stamp was refused.
    agent._last_remediation_merge_source = None
    agent._last_remediation_merge_observed_at = None
    # Per-iteration memo for the ``is_pr_settled`` chokepoint (proposal 087
    # step 3): ``pr_number -> MergeTruth`` so every PR-servicing face this
    # iteration shares one fetch-first observation instead of re-deriving
    # merge state. Cleared in ``run_loop``'s fresh-iteration reset; a fact
    # past ``[loop] merge_truth_ttl_seconds`` is re-observed (step 4).
    agent._merge_truth_memo = {}
    # Per-iteration memo for the ``observe_pr_readiness`` chokepoint
    # (proposal 089 step 2): ``pr_number -> PRReadiness`` so every face that
    # asks "is this PR ready and waiting?" this iteration shares one
    # aggregated observation instead of re-deriving each blocker dimension.
    # Cleared in ``run_loop``'s fresh-iteration reset alongside the
    # merge-truth memo; a fact past ``[loop] merge_truth_ttl_seconds`` is
    # re-observed. Reuses the same TTL discipline as ``_merge_truth_memo``.
    agent._pr_readiness_memo = {}
    # Per-iteration cache of the CI-failure oracle's surfaced-line fact
    # (proposal 102 follow-on): ``pr_number -> did the failing-check log yield
    # a concrete first-error line this lap``. Written in
    # ``_diagnose_red_root_on_ci_red`` after the oracle computes its
    # ``CIFailure`` tuple, read at record time by ``run``'s §5c-iv-c red-read
    # bind so "a concrete red was surfaced for this PR this lap" is readable
    # without re-fetching the log or parsing ``red-root.md``. Cleared in
    # ``run_loop``'s fresh-iteration reset alongside the merge-truth /
    # readiness memos. Absent key means the oracle did not run this lap
    # (``.get(pr, False)`` reads it as "nothing surfaced").
    agent._ci_oracle_surfaced = {}
    # Per-iteration memo for the ahead-of-origin reachability probe the
    # chosen-stand-down guard reads (``branch_name -> ahead_count``): the
    # count of a PR branch's commits absent from every ``origin`` ref
    # (``git rev-list --count <branch> --not --remotes=origin``). The write
    # site fetches-first (once per branch per iteration) then counts; a
    # positive count is an owned, pushable act that vetoes a false external
    # quiet. Cleared in ``run_loop``'s fresh-iteration reset alongside the
    # merge-truth / readiness memos.
    agent._ahead_of_origin_memo = {}
    # Per-iteration snapshot of the open own ``maestro/evolve/*`` PR
    # enumeration for the WIP-focus gatherer (proposal 092). Both the
    # head-of-chain ``wip_primacy`` gate and the audit→directive bridge can
    # call ``_current_task_focus`` in one iteration; the readiness memo above
    # already dedupes the per-PR observation, but the ``gh pr list`` call
    # behind ``get_open_evolution_pr_states`` was not memoized. Dedicated to
    # ``_current_task_focus`` (other callers keep their fresh reads); cleared
    # in ``run_loop``'s fresh-iteration reset. ``None`` means "not yet
    # enumerated this iteration".
    agent._open_owned_pr_states_memo = None
    # This iteration's listing failure (#4107), reused instead of re-listing.
    agent._open_owned_pr_states_error = None
    # The :class:`PrRemediationState` the last finished remediation lap
    # classified to, set by ``_record_remediation_outcome`` via
    # ``classify_remediation_outcome_state``. A typed handle on the post-task
    # classification for tests and future telemetry, so neither has to
    # re-parse the free-text summary (mirrors ``_last_remediation_merge_block``).
    agent._last_remediation_state = None
    # Set by ``run()``'s comment-completeness gate (step 4d) to the count of
    # eligible review items still outstanding after a PR remediation —
    # inline review threads lacking a terminal reply plus body-overflow
    # findings lacking an acknowledgement — or ``None`` when the gate did
    # not run (non-remediation task, or a merged/closed PR). Threaded out so
    # ``_record_remediation_outcome`` reuses the gate's fetches instead of
    # re-querying the host for the repetition gate's no-op check (#2663).
    agent._last_remediation_items_remaining = None
    agent._current_phase = None
    agent._shutdown_event = None
    agent._signal_handlers_installed = False
    agent._iteration_usage = None
    # Per-iteration dedup for non-budget-gated cadence phases
    # (``review_artifacts``, ``tend_evolution_prs``, ``audit_self_model``).
    # During a budget throttle the iteration counter freezes (see
    # ``run_loop``), so a frozen ``iteration % N == 0`` value would
    # otherwise re-fire those phases on every wall-clock body. This
    # process-local map remembers the last iteration value at which each
    # phase fired and short-circuits a repeat fire on the same value.
    # Not persisted across restarts: on restart, ``first_iteration_after_restart``
    # bypasses the dedup for the two phases that honor it; the third
    # (``audit_self_model``) is gated only by cadence, so any harmless
    # extra fire on restart is bounded to one cycle.
    agent._last_phase_iteration = {}
    # Consecutive-remediation streak counter (process-local). Counts how
    # many task slots in a row selected from ``pending_pr_requests``; when
    # it reaches ``[loop] max_consecutive_remediation`` (cap > 0) the next
    # slot bypasses ``pending_pr_requests`` and falls through to the queue
    # head — only when one exists — breaking the remediation-monoculture
    # where six unlanded PRs re-feed the scheduler forever. An empty queue
    # leaves remediation eligible (nothing to divert to). Reset to 0 on
    # any non-remediation selection. Not persisted: a restart starts the streak fresh, which
    # is harmless (at most ``cap`` extra remediation slots before the cap
    # re-engages).
    agent._consecutive_remediation = 0
    # Iteration-start status update fires once per unique iteration
    # value. Under budget throttle the iteration counter freezes (see
    # ``run_loop``) and on mid-iteration resume it doesn't change
    # either — without this guard each frozen / resumed body would
    # re-send an identical ``<b>Iter N</b> — Starting`` Telegram
    # message. Process-local; on restart the first body re-sends once
    # and then dedups, matching the bounded-extra-fire semantics of
    # ``_last_phase_iteration``.
    agent._last_status_iteration = None
    # Loop progress cursor — updated after every phase so that
    # ``shutdown()`` can persist where we are without needing parameters.
    # ``_resume_phase is None`` means "the iteration named in
    # ``_resume_iteration`` finished cleanly; the next iteration starts
    # at the first phase". A non-None value names the next phase to run.
    agent._resume_iteration = 0
    agent._resume_phase = None
    # Self-regulating burn-rate throttle. When the rolling-window cost
    # rate reaches ``max_loop_budget_usd_per_hour``, the inter-iteration
    # sleep is stretched by a multiplier proportional to ``rate / limit``
    # (capped at ``budget_throttle_max_multiplier``). All phases keep
    # running; only the gap between iterations stretches. The field
    # holds the ISO timestamp of the transition into throttled mode so
    # exit/entry are one-shot notified instead of spammed.
    agent._budget_throttle_entered_at = None
    # Peak-hours scheduled throttle — one-bit edge tracker for
    # :meth:`_maybe_notify_peak_throttle_transition`. Persisted to
    # ``lifecycle.json`` so a restart inside the configured window does
    # not re-fire the entry notification, matching the burn-rate side's
    # one-shot semantics.
    agent._peak_throttle_active_persisted = False
    # Operator-initiated maintenance mode. Toggled by Telegram
    # ``/maintenance on|off``. Persisted to the ``maintenance.json``
    # sidecar so the message handler does not race the loop's
    # lifecycle writer.
    agent._maintenance_mode_active = False
    # Edge-flag mirror for one-shot milestone journaling — kept in
    # ``lifecycle.json`` parallel to ``_peak_throttle_active_persisted``
    # so a restart inside maintenance mode does not re-fire entry.
    agent._maintenance_milestone_persisted = False
    # Rate-limit extended idle. Set when in-place rate-limit retries
    # exhaust ``_MAX_CONSECUTIVE_RATE_LIMITS``. Persisted to
    # ``lifecycle.json`` so a restart during idle resumes in the probe
    # loop rather than blindly retrying the failed phase.
    agent._rate_limit_idle_entered_at = None
    # Per-iteration ephemeral state, promoted from ``run_loop`` locals so
    # that ``_persist_progress`` and ``_write_lifecycle_shutdown`` can
    # mirror them to ``lifecycle.json``. On a mid-iteration rate-limit
    # retry or restart we restore these from disk so the resumed phase
    # sees the same context the failed pass had.
    agent._iter_message_context = ""
    # Reply-worthy subset of it (no pre-deploy notices).
    agent._iter_reply_context = ""
    agent._iter_reflection = ""
    # Snapshot built once per fresh iteration for the status update and
    # reused by ``contemplate`` (#1211). Not persisted to lifecycle.json:
    # a cold mid-iteration resume (fresh process) leaves it ``None`` so
    # ``contemplate`` falls back to building its own. The status block
    # overwrites it on every fresh iteration before ``contemplate`` runs,
    # so no per-iteration reset is needed (a reset would run after the
    # status block that populates it and clobber the fresh snapshot).
    agent._iter_diag_snapshot = None
    # Conscious-access workspace continuity (proposal 130). The id of the
    # most recently recorded ``ConsciousMoment``, mirrored into both lifecycle
    # writers and restored at ``run_loop`` startup so the next ``attend`` pass
    # can prove a ``linked`` continuity (predecessor id == restored id) rather
    # than mislabelling a normal restart as a ``disabled_interval``.
    agent._active_conscious_moment_id = None
    # The full active moment (proposal 130 broadcast) and the receipts proving
    # which consumer received it this iteration. The moment is retained in
    # memory by ``_phase_attend`` so ``_broadcast_moment_prefix`` can render it
    # without a ledger re-read; the receipts are reset per moment and, at the
    # iteration-completion seam, persisted into a superseding episode by
    # ``_finalize_conscious_episode``.
    agent._active_conscious_moment = None
    agent._active_broadcast_receipts = []
    # Plan ids that cited this iteration's moment (proposal 130); appended by
    # ``evolve`` on the lone-``propose_plan`` path when broadcast is active,
    # drained into the finalized episode's ``cited_plan_ids`` and cleared by
    # ``_finalize_conscious_episode``, and reset each iteration by
    # ``_phase_attend`` alongside the receipt list.
    agent._active_cited_plan_ids = []
    # Task ids that cited this iteration's moment (proposal 130); the
    # task-generation analogue of ``_active_cited_plan_ids``. Appended by
    # ``generate_task`` for each *surviving* enqueued task carrying a stamped
    # ``workspace_moment_id`` (post queue-cap), drained into the finalized
    # episode's ``cited_task_ids`` and cleared by
    # ``_finalize_conscious_episode``, and reset each iteration by
    # ``_phase_attend`` alongside the receipt list.
    agent._active_cited_task_ids = []
    # One-shot marker set in ``run_loop`` from the startup ``crashed`` flag
    # and consumed by the first ``attend`` pass, so a moment recorded after a
    # crash is classified ``crash_gap`` rather than ``linked``.
    agent._pending_crash_gap = False
    # Gaps the stuck-gap tripwire (proposal 155 step 5) labelled
    # ``needs-decision`` this tick, queued by ``select_fuel`` and drained by
    # ``deliver_gap_decision_asks`` right after evolve's fuel read.
    agent._pending_gap_asks = []
    # SDK session IDs for phase-level session reuse. Populated when
    # ``enable_session_reuse`` is true AND a session has produced at least
    # one response in the current iteration. Persisted to lifecycle.json
    # so a retry/restart can resume the transcript via ``--resume``.
    agent._iter_session_a_id = None
    agent._iter_session_b_id = None
    # Per-iteration persistent phase sessions (proposal 106). Promoted from
    # ``run_loop`` locals so the migrated ``_phase_*`` runners — which are
    # bound methods and cannot touch a caller's locals — can share the
    # slot-A (contemplation) and slot-B (evolution/curation) sessions the loop
    # threads across phases. Reset to None at the top of each iteration
    # body, disconnected and nulled in the iteration-end ``finally``. The
    # ``_iter_session_*_id`` string mirrors above are a separate concern
    # (persisted SDK resume ids) and stay independent.
    agent._session_a = None
    agent._session_b = None
    # True when the current iteration is being resumed mid-phase (either
    # from disk on startup, or from the in-process rate-limit handler
    # choosing a non-zero ``start_phase_index``). Suppresses the
    # ``iteration += 1`` guard and the per-iteration state-reset gates.
    agent._resumed_mid_iteration = False
    # Consecutive in-process rate-limit retries on the same iteration.
    # Reset on clean iteration end. When exceeded, the handler re-raises
    # so the outer ``finally`` invokes ``shutdown(crashed=True)``.
    agent._consecutive_rate_limits = 0
    # Consecutive in-process CLI-crash retries on the same iteration.
    # Reset on clean iteration end. When exceeded, the handler re-raises
    # so a deterministic crash loop hits the crash-triage path instead
    # of spinning forever.
    agent._consecutive_cli_crashes = 0
    # Consecutive in-process stream-stall retries on the same iteration.
    # STREAM_STALL is the safety-net classification raised when the SSE
    # stream goes silent past ``sdk_stream_idle_timeout_seconds``. Reset
    # on clean iteration end.
    agent._consecutive_stream_stalls = 0
    # Consecutive in-process stale-session retries on the same iteration.
    # STALE_SESSION clears the persisted ids and re-enters the phase with
    # fresh sessions. Reset on clean iteration end. When exceeded, the
    # handler re-raises — a defensive crash-out against a classifier
    # regression that could otherwise loop forever.
    agent._consecutive_stale_sessions = 0
