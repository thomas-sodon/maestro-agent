"""The core agent loop — act, reflect, evolve."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from maestro import (
    audit_checks,
    conscious_workspace,
    deed_ledger,
    diagnostics,
    gate_environment,
    secret_scan,
    workspace_candidates,
)
from maestro.audit_checks import (
    GateActivationBackend,
    ImpactReviewSurfacedBackend,
    is_branch_merged_into_main,
    measure_plan_theme_saturation,
    select_curation_target,
)
from maestro.audit_checks_calibration import (
    PredictionBackend,
    PremiseBackend,
    build_calibration_evidence,
    predict_task_outcome,
)
from maestro.audit_checks_phantom import (
    OrphanedCureBackend,
    UnconfirmedOwnLandingBackend,
    _git_pr_merged_default,
)
from maestro.audit_checks_settlement import (
    SettledVerdictsBackend,
)
from maestro.ci_diagnosis import (
    CIFailure,
    diagnose_ci_failure,
    render_ci_failure_section,
)
from maestro.config import Config
from maestro.consciousness import (
    Consciousness,
    PendingPRRequest,
    SelfModel,
    _normalize_question,
    match_open_question,
)
from maestro.contributions import ContributionTracker
from maestro.cost_tracking import (
    CostRateSnapshot,
    CostSampleBackend,
    build_cost_rates,
)
from maestro.directive_ledger import (
    DroppedDirectiveBackend,
)
from maestro.escalation_bridge import (
    EscalationLedgerBackend,
)
from maestro.evolution_tend import _fetch_eligible_overflow_findings
from maestro.executor import Executor
from maestro.fileutil import atomic_write, file_lock
from maestro.gate_observation import Attribution
from maestro.generation_gate import (
    GenerationVerdict,
    may_mint_plan,
)
from maestro.journal import Journal
from maestro.lever_capture import (
    LeverAttemptsBackend,
)
from maestro.lexicon import (
    extract_sensor_tags,
    load_lexicon,
    render_sensor_tag_map,
)
from maestro.loop import (
    assembly,
    contemplation,
    implementation,
    iteration,
    self_audit,
    self_health,
)

# Re-exported for callers and tests that read the loop constants from ``maestro.agent``;
# the ``run_loop`` body that owns them lives in ``maestro.loop.iteration`` (proposal 147).
from maestro.loop.iteration import _LOOP_PHASES as _LOOP_PHASES
from maestro.loop.iteration import _STANDBY_MAX_SLEEP_SECONDS as _STANDBY_MAX_SLEEP_SECONDS

# Re-exported for the staged 068 extraction: tests import these from
# ``maestro.agent``; the bodies live in ``maestro.loop.lifecycle``.
from maestro.loop.lifecycle import (
    _BUDGET_GATED_PHASES as _BUDGET_GATED_PHASES,
)
from maestro.loop.lifecycle import (
    LifecycleController,
    begin_task_run,
    finish_task_phase,
    open_run_session,
    read_maintenance_state,
    resolve_preflight_standdown,
    teardown_task_run,
)
from maestro.loop.lifecycle import (
    _format_status_message as _format_status_message,
)
from maestro.loop.owed_act_binding import (
    gather_task_focus,
    owed_act_binding,
)
from maestro.loop.phases import (
    PhaseRunner,
)

# Re-exported for the staged 068 extraction: tests import these from
# ``maestro.agent``; the bodies live in ``maestro.loop.pr_remediation``.
from maestro.loop.pr_remediation import (
    _BUG_COMMENTS_PROBLEM_MARKER as _BUG_COMMENTS_PROBLEM_MARKER,
)
from maestro.loop.pr_remediation import (
    _PR_REFERENCE_PATTERN,
    _REMEDIATION_DEDUP_PREFIX,
    PrRemediationController,
    _OwnedPushResult,
    _same_base_partial_streak,
)
from maestro.loop.pr_remediation import (
    _format_bug_comments_problem as _format_bug_comments_problem,
)
from maestro.loop.pr_remediation import (
    _is_pure_quality_gates_directive as _is_pure_quality_gates_directive,
)
from maestro.loop.record_binds import (
    apply_ci_red_read_bind,
    apply_frame_discharge_cap,
    apply_mechanical_outcome_authority,
    apply_mergeable_flip_actuator,
    apply_remediation_narration_framing,
)
from maestro.loop.rejection_sessions import rejection_sessions
from maestro.loop.remediation_stamps import (
    apply_comment_completeness_gate,
    apply_host_state_stamp_gate,
    bind_remediation_verdict_to_git_truth,
    deliver_review_thread_replies,
)
from maestro.loop.step_execution import execute_step_groups
from maestro.loop.task_delivery import (
    apply_push_delivery_reconciliation,
    commit_task_artifacts,
    observe_task_completion_state,
    resolve_addressed_review_threads,
)
from maestro.loop.task_generation import (
    TaskGenerator,
    _extract_file_paths,
)
from maestro.loop.task_generation import (
    _format_prior_task_context as _format_prior_task_context,
)
from maestro.loop.task_outcome import (
    apply_session_landing_actuator,
    capture_lap_start_head_watermark,
    capture_prepush_watermarks,
    classify_task_outcome,
)
from maestro.loop.task_record import (
    apply_merge_clean_promotion_directive,
    assemble_task_record,
    compose_task_summary,
    distill_task_findings,
    run_post_task_reflection,
)
from maestro.loop.task_setup import (
    prepare_task_branch,
    prepare_task_workspace,
    resolve_resume_checkpoint,
    resolve_task_plan,
)
from maestro.memory import (
    _OBSOLETE_REASON_MAX_CHARS,
    EvolutionPlan,
    Memory,
    PlanStep,
    QuestionResolution,
    TaskRecord,
    VersionArchiveBackend,
)
from maestro.messaging import (
    MAX_MESSAGE_CONTEXT_CHARS,
    Message,
    bound_message_context,
    is_pre_deploy_notice,
)
from maestro.notifications.base import NotifierTransportError
from maestro.outcome_grounding import (
    MergeRequestServicedBackend,
)
from maestro.phase_cost_report import refresh_phase_cost_report
from maestro.plan_feedback_fold import fold_feedback_messages
from maestro.planner import (
    Planner,
)
from maestro.pr_facts import (
    MergeTruth,
    PRReadiness,
    Salvage,
    assess_pr_salvage,
    ci_state_from_conclusion,
    classify_pr_disposability,
    compute_owned_writable_branches,
    observe_pr_merge_truth,
    select_live_plans,
)
from maestro.pr_facts import (
    observe_pr_readiness as _observe_pr_readiness,
)
from maestro.pr_ledger import PRLedger, PRLedgerBackend
from maestro.pr_remediation_state import Marker, PrRemediationState
from maestro.prompt_caps import MAX_TASK_GOAL_BYTES, cap_utf8
from maestro.prompts.defaults import (
    FINDINGS_DISTILLATION_PROMPT,
    MESSAGE_RESPONSE_PROMPT,
    resolve_framing_prompt,
)
from maestro.providers.anthropic_tool_adapter import AnthropicToolAdapter
from maestro.red_root import (
    CheckRun,
    CIStatus,
    RedRootDiagnosis,
    build_red_root_gates,
    diagnose_red_root,
    render_remote_ci_section,
    run_gate_subprocess,
)
from maestro.reflection_retrieval import (
    REFLECTION_SESSION_MAX_TURNS,
    build_bounded_reflection_tool_set,
)
from maestro.remediation_tracker import (
    _CHOSEN_QUIET_MARKER,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_NONE,
    FetchTruthBackend,
    ObligationForkBackend,
    PreflightPassBackend,
    PrProvenanceBackend,
    RemediationTracker,
    RemediationTrackerBackend,
    StandDownVerdict,
    StandDownVerdictBackend,
    apply_landing_model,
    classify_merge_block,
    classify_noop,
    refine_merge_block_reason,
    version_archive_has_landing,
)
from maestro.sdk import (
    TRANSIENT_SDK_CATEGORIES,
    ErrorCategory,
    IterationUsage,
    QueryOptions,
    QueryUsage,
    SDKError,
    _note_session_effort_override,
    chars_complete,
    create_session,
    diagnostic_stderr,
    disconnect_session,
    get_warning_hold_probe_interval,
    get_warning_hold_reset_wait,
    is_warning_hold_active,
    query,
    query_on_session,
    stderr_failure_tail,
    write_iteration_usage,
)
from maestro.self_calibration import (
    CalibrationEvidence,
    ClaimEvidenceSources,
)
from maestro.session_landing import (
    LandOutcome,
)
from maestro.settlement_record_reconciler import (
    SettlementReconciliationBackend,
    reconcile_pr_settlement,
)
from maestro.stats import (
    OwnedBacklogLedgerBackend,
)
from maestro.task_gates import drop_failed_branch_head
from maestro.task_queue import TaskQueueBackend
from maestro.types import TaskOutcome, TaskSource
from maestro.vcs import VCSError, open_evolution_plan_ids
from maestro.wip_primacy import TaskFocus, TendOwnedPR

if TYPE_CHECKING:
    from maestro.email import EmailClient
    from maestro.evolution import EvolutionEngine, MergeCheckResult, QualityGateResult
    from maestro.evolution_fuel import FuelSelection, TrippedGap
    from maestro.evolution_tend import TendPRsResult
    from maestro.issues import IssueTracker
    from maestro.messaging import MessageStore
    from maestro.notifications.base import Notifier
    from maestro.providers.base import Session
    from maestro.settlement_record_reconciler import SettlementReconciler
    from maestro.vcs import PrMergeState, VCSClient

logger = logging.getLogger("maestro")

_WARNING_HOLD_MAX_PROBE_INTERVAL = 600

# Upper bound (seconds) for the Telegram notify call during shutdown. Bounded
# so a stuck HTTP connection cannot extend the shutdown path even momentarily
# beyond the time the in-flight phase needed to finish.
_SHUTDOWN_NOTIFY_TIMEOUT = 5.0


# Bounded tail reads feeding evidence-bound capability claims (proposal 129). When
# ``[audit] evidence_bound_claims_enabled`` is on, ``contemplate`` reads at most
# this many recent task rows (for the success/non-success task-id sets a TASK
# citation resolves against) and version records (for the merge SHAs a SHA citation
# is prefix-matched against). Bounded so the resolver's source set stays a
# context-safe read.
_CLAIM_EVIDENCE_TASK_WINDOW = 100
_CLAIM_EVIDENCE_VERSION_WINDOW = 50

# The transient-category policy now lives in ``maestro.sdk`` beside
# ``ErrorCategory`` so ``evolution.py`` can key its own swallow on it
# without importing ``agent`` (which imports ``evolution``). This alias
# preserves the name CLAUDE.md, ``providers.openai`` and ``tests/test_sdk_retry.py``
# reference — same object, same membership.
_TRANSIENT_SDK_CATEGORIES: frozenset[ErrorCategory] = TRANSIENT_SDK_CATEGORIES


def _git_pr_merged(pr_num: int, cwd: Path | str | None = None) -> bool:
    """Cwd-binding git-merge-truth seam for the dequeue-face merged-PR exclusion.

    Thin module-level wrapper over :func:`audit_checks_phantom._git_pr_merged_default`
    that threads *cwd* (the agent's workspace) through to the ``git log`` query,
    so the :meth:`MaestroAgent.is_pr_settled` chokepoint's git-subject
    ``branch_oracle`` reads the *same* merge-truth oracle (#2523) the
    unlanded-remediation sensor and PR-keyed audit rules trust, targeting the
    agent's repo regardless of the process cwd. Isolated as a module-level
    function so the chokepoint's tests patch ``maestro.agent._git_pr_merged``
    without shelling git out. Best-effort/fail-open: the underlying oracle
    returns ``False`` on any unresolvable git state.
    """
    return _git_pr_merged_default(pr_num, cwd=cwd)


# The single signature of a *chosen* remediation quiet — a deliberate
# stand-down at a fork that only Maestro can emit. Reuses the canonical
# ``remediation_tracker._CHOSEN_QUIET_MARKER`` directly (not a re-declared
# literal) so the two can never drift out of sync. Emitted only by the
# user-request preflight (``loop.lifecycle.resolve_preflight_standdown``, which
# reaches the same marker through its own import; ``pr_branch is None``), whose
# goals reference a PR but never match the strict ``^Remediate PR #<n>:`` lineage;
# ``measure_forced_vs_chosen_quiet`` reads this marker as chosen on the marker
# alone (not gated on the remediation lineage), so a stand-down on the
# user-request channel actually counts as chosen rather than being skipped (#2662).
_STAND_DOWN_MARKER = _CHOSEN_QUIET_MARKER


# Minimum shared-prefix length for two tokens to count as morphological
# variants in :func:`_prefix_overlap_count` (e.g. capable / capabilities).
# Matches the prefix-matching semantics used by
# :func:`audit_checks._matches_keyword`.
_MIN_STEM_PREFIX = 4


def _prefix_overlap_count(tokens_a: set[str], tokens_b: set[str]) -> int:
    """Count tokens in *tokens_a* that have an exact or prefix match in *tokens_b*.

    Two tokens match when they are identical **or** share a common prefix
    of at least :data:`_MIN_STEM_PREFIX` characters — this catches
    morphological variants like ``capable`` / ``capabilities`` or
    ``execute`` / ``execution`` without a full stemming library.

    Semantics mirror :func:`audit_checks._matches_keyword` which already
    uses prefix matching for keyword detection.
    """
    count = 0
    for ta in tokens_a:
        for tb in tokens_b:
            if ta == tb:
                count += 1
                break
            prefix_len = min(len(ta), len(tb))
            if prefix_len >= _MIN_STEM_PREFIX and ta[:_MIN_STEM_PREFIX] == tb[:_MIN_STEM_PREFIX]:
                count += 1
                break
    return count


# The control envelope in force, recorded on every ``ConsciousMoment``'s
# ``AttentionSchema.permission_summary`` (proposal 130). A stable description of
# the *authorization* profile — not the throttle state, which rides in as its own
# candidate — so identical inputs yield an identical moment. Bounded well under
# ``conscious_workspace.MAX_PERMISSION_SUMMARY_CHARS``.
_CONSCIOUS_PERMISSION_SUMMARY = (
    "human-in-the-loop: code changes require approval; "
    "Bash action-gated; workspace observe-only"
)

# Maps a persistent-session slot to the SDK phase name it serves, so
# ``_get_or_create_session`` resolves the per-phase provider override at session
# creation (proposal 058 Step 6 runtime routing). Slot "a" carries contemplation
# (``contemplate`` only; the ``respond_to_messages`` reply deliberately does NOT
# reuse Session A — it runs at ``effort="low"`` off-session, see fix #1239);
# slot "b" carries evolution (``evolve`` + the curate refinement on Session B).
# The ephemeral phase sessions pass their phase to ``create_session`` directly.
_SESSION_SLOT_PHASES: dict[str, str] = {"a": "contemplation", "b": "evolution"}

def write_heartbeat(memory_dir: Path) -> None:
    """Write the Docker healthcheck heartbeat file.

    Best-effort: a failed write is logged but never raised. The Docker
    healthcheck in ``docker-compose.yml`` reads ``<memory_dir>/loop.heartbeat``
    and requires its mtime to be within 600s to report the container as
    healthy. Callers refresh the file periodically to prove liveness.

    This is a module-level helper so the CLI (``cmd_loop``) can call it
    *before* ``_build_agent()`` runs, ensuring the file exists within the
    first moment of ``maestro loop`` starting — independent of how long
    agent construction takes. ``MaestroAgent._write_heartbeat`` delegates
    to this function so all heartbeat writes go through a single code path.
    """
    heartbeat = memory_dir / "loop.heartbeat"
    try:
        heartbeat.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    except OSError:
        logger.warning("Failed to write heartbeat file %s", heartbeat)


# Marker substituted for ``key_findings`` when surfacing a failed
# self-directed task to ``update_self_model``. The LLM treats this as a
# prompt to REFINE-or-REMOVE the question — abandonment is a first-class
# verdict, not an absence of signal.
_FAILED_TASK_FINDING_MARKER = "[task failed — no finding produced]"


# Defensive cap on how many missing paths the advisory premise report lists
# in a single prompt-injected block, matching the ``≤10 items`` prompt-input
# discipline. Any overflow is summarized as a trailing count rather than
# enumerated, so a goal citing a pathological number of missing paths cannot
# balloon the block unboundedly (#2336).
_MAX_PREMISE_REPORT_PATHS = 10


@dataclass(frozen=True)
class QuestionPair:
    """One ``(question, finding)`` candidate fed to ``update_self_model``.

    Carries the source ``task_id`` and ``outcome`` alongside the prompt
    text so the contemplate loop can stamp a :class:`QuestionResolution`
    row per pair after the LLM commits its REMOVE/REFINE/KEEP verdict.
    *normalized_key* is the case- and whitespace-folded form of the
    pre-fuzzy-match question, used for de-duplication and disposition
    matching against ``open_questions`` after the update.

    *refinement_count* is the number of historical predecessor wordings
    reachable through the ``refined_to`` lineage chain in the recent
    resolution log. Surfaced verbatim in the contemplation prompt so
    the LLM can prefer REMOVE over REFINE when a question has been
    rephrased repeatedly without yielding closure.
    """

    question: str
    finding: str
    task_id: str
    outcome: TaskOutcome
    normalized_key: str
    refinement_count: int = 0


# ──────────────────────────────────────────────────────────────────────
# Uncalled-symbol quality gate
# ──────────────────────────────────────────────────────────────────────


# Proposal 131 abandonment disposition: a deterministic, zero-cost read of
# Thomas's reply to a blocked plan's question. An explicit decision to *abandon
# the plan* routes it to terminal ``obsolete`` instead of re-admitting it to
# ``approved`` and spending a priced ``implement()`` call on a plan the human
# decided to drop. The classifier is deliberately conservative — it defaults to
# re-admission (the pre-existing behaviour) on any ambiguity — so a missed
# abandonment merely falls back to today's re-admit, while a false abandonment
# (the costlier error, since it retires the plan) is guarded against by (a) a
# negation anywhere in the reply and (b) requiring the abandonment to target the
# plan/effort itself, not a sub-approach ("abandon the flaky path, use plan B"
# keeps going and must not match). Substring matching is adequate for the short,
# question-scoped replies this reads; there is no LLM interpretation.
_ABANDONMENT_NEGATIONS: tuple[str, ...] = (
    "don't", "do not", "dont", "never", "no need", "instead of", "rather than",
)
# Terse whole-reply directives (after normalisation) that unambiguously abandon.
# The colloquial verbs forget/drop/cancel keep their pronoun forms ("forget it",
# "drop it", "cancel it" and their "this" siblings) *here* rather than in
# _ABANDONMENT_PHRASES: as a whole reply they mean abandon, but as a substring
# inside a longer proceed-instruction ("forget it, just use the fallback
# runner", "drop it in the artifacts folder and retry", "cancel it and use plan
# B") they would mis-fire and trigger the costlier false-abandonment, so they
# must match the *whole* reply, not merely appear anywhere in it.
_ABANDONMENT_EXACT: frozenset[str] = frozenset({
    "abandon", "give up", "drop", "scrap", "shelve", "cancel", "reject", "stop",
    "forget it", "forget this", "drop it", "drop this", "cancel it", "cancel this",
})
# Plan-targeted abandonment phrases (matched as substrings). Each names the
# *plan/effort* ("it" / "this" / "the plan" / "the whole ...") so a reply that
# abandons only a sub-approach while proceeding does not match. The colloquial
# verbs forget/drop/cancel keep only their plan-named forms here; their pronoun
# forms are whole-reply-only in _ABANDONMENT_EXACT (see above).
_ABANDONMENT_PHRASES: tuple[str, ...] = (
    "abandon it", "abandon this", "abandon the plan", "abandon this plan",
    "abandon the whole", "abandon the effort", "abandon the work",
    "give up on it", "give up on this", "give up on the plan", "give it up",
    "drop the plan",
    "scrap it", "scrap this", "scrap the plan",
    "shelve it", "shelve this", "shelve the plan",
    "forget the plan",
    "cancel the plan", "cancel this plan",
    "kill it", "kill this", "kill the plan",
)


def _is_abandonment_reply(answer: str) -> bool:
    """Return ``True`` when *answer* is an explicit decision to abandon the plan.

    Conservative by construction (see the module comment above): a negation
    anywhere in the reply, or an abandonment aimed at a sub-approach rather than
    the plan itself, returns ``False`` so the plan is re-admitted as before.
    Pure and zero-cost.
    """
    text = answer.strip().lower().rstrip(".!")
    if not text:
        return False
    if any(neg in text for neg in _ABANDONMENT_NEGATIONS):
        return False
    if text in _ABANDONMENT_EXACT:
        return True
    return any(phrase in text for phrase in _ABANDONMENT_PHRASES)


def _plan_instruction_appends(
    queue: list[str],
    recorded_ids: list[str],
    candidates: list[tuple[str, str]],
) -> list[tuple[str, str | None]]:
    """Return the ``(message_id, content | None)`` pairs a drain would record.

    Skips an id already in *recorded_ids* (a crash re-drain, #4602 / #4577).
    A text equal to the running queue tail, compared both raw and in the
    truncated form the save door stores, is returned with ``None`` content:
    its id is recorded but nothing is appended, so a retry storm of one text
    under fresh ids enqueues once and a later re-drain after the head is
    popped cannot re-enqueue it. Pure: *queue* and *recorded_ids* are not
    mutated.
    """
    seen = set(recorded_ids)
    tail = queue[-1] if queue else None
    out: list[tuple[str, str | None]] = []
    for mid, content in candidates:
        if mid in seen:
            continue
        seen.add(mid)
        stored = Consciousness._truncate_item(content, Consciousness.MAX_ITEM_CHARS)
        if tail is not None and tail in (content, stored):
            out.append((mid, None))
            continue
        out.append((mid, content))
        tail = stored
    return out


class MaestroAgent:
    # Instance attributes bound by :func:`maestro.loop.assembly.assemble_agent`
    # (proposal 147), declared on the class so the type checker sees them.
    config: Config
    consciousness: Consciousness
    journal: Journal
    memory: Memory
    executor: Executor
    planner: Planner
    evolution: EvolutionEngine
    notifier: Notifier
    _agent_tools: dict[str, Any] | None
    message_store: MessageStore | None
    email_client: EmailClient | None
    contributions: ContributionTracker
    remediation_tracker: RemediationTracker
    pr_ledger: PRLedger
    _settlement_reconciler_registry: tuple[SettlementReconciler, ...]
    _pr_remediation: PrRemediationController
    _task_generator: TaskGenerator
    _lifecycle: LifecycleController
    _phase_runners: dict[str, PhaseRunner]
    _open_evolution_pr_count: int
    _running: bool
    _shutdown_initiated: bool
    _current_task: str | None
    _last_remediation_merge_block: str | None
    _last_remediation_merge_source: str | None
    _last_remediation_merge_observed_at: float | None
    _merge_truth_memo: dict[int, MergeTruth]
    _pr_readiness_memo: dict[int, PRReadiness]
    _ci_oracle_surfaced: dict[int, bool]
    _ahead_of_origin_memo: dict[str, int]
    _open_owned_pr_states_memo: dict[str, PrMergeState] | None
    _open_owned_pr_states_error: VCSError | None
    _last_remediation_state: PrRemediationState | None
    _last_remediation_items_remaining: int | None
    _current_phase: str | None
    _shutdown_event: asyncio.Event | None
    _signal_handlers_installed: bool
    _iteration_usage: IterationUsage | None
    _last_phase_iteration: dict[str, int]
    _consecutive_remediation: int
    _last_status_iteration: int | None
    _resume_iteration: int
    _resume_phase: str | None
    _budget_throttle_entered_at: str | None
    _peak_throttle_active_persisted: bool
    _maintenance_mode_active: bool
    _maintenance_milestone_persisted: bool
    _rate_limit_idle_entered_at: str | None
    _iter_message_context: str
    _iter_reply_context: str
    _iter_reflection: str
    _iter_diag_snapshot: diagnostics.DiagnosticSnapshot | None
    _active_conscious_moment_id: str | None
    _active_conscious_moment: conscious_workspace.ConsciousMoment | None
    _active_broadcast_receipts: list[ conscious_workspace.BroadcastReceipt ]
    _active_cited_plan_ids: list[str]
    _active_cited_task_ids: list[str]
    _pending_crash_gap: bool
    _pending_gap_asks: list[TrippedGap]
    _iter_session_a_id: str | None
    _iter_session_b_id: str | None
    _session_a: Session | None
    _session_b: Session | None
    _resumed_mid_iteration: bool
    _consecutive_rate_limits: int
    _consecutive_cli_crashes: int
    _consecutive_stream_stalls: int
    _consecutive_stale_sessions: int
    issues: IssueTracker
    task_queue: TaskQueueBackend
    cost_samples: CostSampleBackend
    deed_ledger: deed_ledger.DeedLedgerBackend
    conscious_episodes: conscious_workspace.ConsciousEpisodeBackend
    owned_backlog: OwnedBacklogLedgerBackend
    pr_provenance: PrProvenanceBackend
    settlement_reconciliations: SettlementReconciliationBackend
    obligation_fork: ObligationForkBackend
    fetch_truth: FetchTruthBackend
    preflight_passes: PreflightPassBackend
    dropped_directives: DroppedDirectiveBackend
    settled_verdicts: SettledVerdictsBackend
    stand_down_verdicts: StandDownVerdictBackend
    unconfirmed_own_landing: UnconfirmedOwnLandingBackend
    orphaned_cure: OrphanedCureBackend
    impact_review_surfaced: ImpactReviewSurfacedBackend
    merge_request_serviced: MergeRequestServicedBackend
    lever_attempts: LeverAttemptsBackend
    gate_activations: GateActivationBackend
    predictions: PredictionBackend
    premise_observations: PremiseBackend
    version_archive: VersionArchiveBackend
    pr_ledger_backend: PRLedgerBackend
    remediation_tracker_backend: RemediationTrackerBackend
    escalation_standdowns: EscalationLedgerBackend
    _vcs: VCSClient

    def __init__(
        self,
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
        """Delegate to :func:`maestro.loop.assembly.assemble_agent`."""
        assembly.assemble_agent(
            self, config, consciousness, journal, memory, executor, planner,
            evolution, notifier, contributions, message_store, vcs, email_client,
            issues, agent_tools,
        )

    def _ensure_shutdown_event(self) -> asyncio.Event:
        """Return the shutdown event, creating it lazily inside the running loop."""
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()
        return self._shutdown_event

    def _on_query_usage(self, usage: QueryUsage) -> None:
        """Accumulate per-call usage into the current iteration total."""
        iu = self._iteration_usage
        if iu is None:
            return
        iu.calls += 1
        iu.input_tokens += usage.input_tokens
        iu.output_tokens += usage.output_tokens
        iu.cache_read_tokens += usage.cache_read_tokens
        iu.cache_creation_tokens += usage.cache_creation_tokens
        iu.credit_cost(usage)
        # Provider attribution (067 Step 2): record each producing provider
        # once, in first-seen order, so the usage row names which provider(s)
        # served the iteration — in particular who produced unknown spend.
        if usage.provider is not None and usage.provider not in iu.providers:
            iu.providers.append(usage.provider)
        caller = usage.caller or "unknown"
        iu.per_caller[caller] = iu.per_caller.get(caller, 0) + 1

        # Per-caller turn sum. Exact under per-call semantics; an upper
        # bound under cumulative semantics for callers sharing a session.
        iu.per_caller_turns[caller] = (
            iu.per_caller_turns.get(caller, 0) + usage.num_turns
        )

        # Session-aware total. One-shot calls (no session_id) sum directly.
        # Calls on the same session credit only the increment above the
        # peak already counted, which is correct under cumulative
        # semantics and a no-op under per-call semantics with steady
        # values.
        sid = usage.session_id
        if sid is None:
            iu.total_turns += usage.num_turns
        else:
            prev_peak = iu._session_peak_turns.get(sid, 0)
            if usage.num_turns > prev_peak:
                iu.total_turns += usage.num_turns - prev_peak
                iu._session_peak_turns[sid] = usage.num_turns

    @property
    def burn_rate_throttle_active(self) -> bool:
        """Burn-rate-driven side of the throttle gate (persisted in lifecycle.json).

        Distinct from :attr:`budget_throttle_active` (the combined gate)
        because the burn-rate transition notifier needs to detect crossings
        on its own input only — otherwise the schedule-driven peak-throttle
        side would suppress a legitimate burn-rate entry notification.
        """
        return self._budget_throttle_entered_at is not None

    @property
    def budget_throttle_active(self) -> bool:
        """Combined heavy-phase gate: burn-rate OR maintenance mode.

        Consumers (`_phase_will_run`, diagnostics) want either input to flip
        the heavy-phase guard. The peak-hours window is intentionally NOT a
        member: it no longer gates phases. Instead it tightens the burn-rate
        budget inside :meth:`_compute_budget_throttle_multiplier`, slowing the
        loop (longer inter-iteration sleep) while heavy phases keep running.
        A genuine burn-rate breach (against the untightened limit) still
        flips ``burn_rate_throttle_active``, gating phases the same way it
        does off-peak.

        ``tend_evolution_prs`` is intentionally NOT a consumer: it is
        purely mechanical (no SDK calls), so maintenance windows do not
        pause PR tending. Only ``ensure_main_branch`` style infrastructure
        failures stop it.
        """
        return (
            self.burn_rate_throttle_active
            or self._maintenance_mode_active
        )

    @property
    def budget_throttle_entered_at(self) -> str | None:
        """ISO timestamp of the transition into throttled mode, or None."""
        return self._budget_throttle_entered_at

    @property
    def maintenance_mode_active(self) -> bool:
        """Operator-initiated maintenance-mode flag (read-only)."""
        return self._maintenance_mode_active

    @property
    def rate_limit_idle_entered_at(self) -> str | None:
        """ISO timestamp of the moment rate-limit idle was entered, or None."""
        return self._rate_limit_idle_entered_at

    def current_burn_rate_usd_per_hour(self) -> float:
        """Compute the rolling-window burn rate from cost samples.

        Returns ``0.0`` when cost tracking is unavailable or the window is
        empty. The caller is responsible for comparing against the
        configured threshold and minimum-spend floor.
        """
        try:
            snapshot = build_cost_rates(
                self.cost_samples,
                window_hours=self.config.budget_rate_window_hours,
            )
        except Exception:
            logger.debug("Failed to build cost rates for burn check", exc_info=True)
            return 0.0
        return snapshot.cost_per_hour

    def _compute_budget_throttle_multiplier(
        self, smoke: bool,
    ) -> tuple[float, float, CostRateSnapshot | None]:
        """Return ``(gating_multiplier, sleep_multiplier, snapshot)``.

        Delegating shim (068 step 3) — the body lives in
        :meth:`maestro.loop.lifecycle.LifecycleController.compute_budget_throttle_multiplier`.
        """
        return self._lifecycle.compute_budget_throttle_multiplier(smoke)

    async def _maybe_notify_throttle_transition(
        self,
        multiplier: float,
        snapshot: CostRateSnapshot | None,
        sleep_multiplier: float | None = None,
    ) -> None:
        """Fire a one-shot Telegram + journal entry on throttle entry/exit.

        Delegating shim (068 step 3) — the body lives in
        :meth:`maestro.loop.lifecycle.LifecycleController.maybe_notify_throttle_transition`.
        """
        await self._lifecycle.maybe_notify_throttle_transition(
            multiplier, snapshot, sleep_multiplier,
        )

    async def _maybe_notify_peak_throttle_transition(self) -> None:
        """Fire a one-shot Telegram + journal entry on peak-throttle entry/exit.

        Delegating shim (068 step 3) — the body lives in
        :meth:`maestro.loop.lifecycle.LifecycleController.maybe_notify_peak_throttle_transition`.
        """
        await self._lifecycle.maybe_notify_peak_throttle_transition()

    async def _maybe_notify_maintenance_transition(self) -> None:
        """Fire a one-shot Telegram + journal entry on maintenance entry/exit.

        Delegating shim (068 step 3) — the body lives in
        :meth:`maestro.loop.lifecycle.LifecycleController.maybe_notify_maintenance_transition`.
        """
        await self._lifecycle.maybe_notify_maintenance_transition()

    async def _enter_rate_limit_idle(
        self,
        iteration: int,
        phase: str,
    ) -> None:
        """Enter rate-limit extended idle after retry ceiling is exceeded.

        Persists the current cursor at *phase* so a hard kill during idle
        still resumes at the failed phase. Session ids already live in
        ``self._iter_session_*_id`` (captured by the rate-limit handler
        before this runs), so ``_persist_progress`` picks them up.
        """
        self._rate_limit_idle_entered_at = datetime.now(UTC).isoformat()
        self._persist_progress(iteration, phase)

        base = self.config.sdk_rate_limit_idle_probe_interval
        cap = self.config.sdk_rate_limit_idle_max_probe_interval
        logger.warning(
            "Entering rate-limit extended idle at iteration %d phase %s "
            "— probing every %ds (exp-backoff, %ds cap) until clear",
            iteration, phase, base, cap,
        )
        msg = (
            f"Entering rate-limit extended idle at iteration {iteration} "
            f"phase {phase} — the API is under sustained pressure. Probing "
            f"every {base}s (exp-backoff, {cap}s cap) until clear."
        )
        await self._notify_best_effort(
            msg,
            failure_log="Rate-limit idle notification failed",
        )
        try:
            self._record_milestone(msg, "rate_limit_idle_enter")
        except Exception:
            logger.warning(
                "Rate-limit idle journal entry failed", exc_info=True,
            )

    def _record_milestone(self, content: str, triggered_by: str) -> None:
        """Append a code-templated ``milestone`` row to ``events.jsonl``."""
        self.journal.record_event(
            content=content,
            entry_type="milestone", triggered_by=triggered_by,
        )

    async def _run_rate_limit_idle_probe(self) -> None:
        """Probe the API until one call succeeds or shutdown fires.

        Exponential-backoff sleep between probes (``base * 2**attempt``
        capped at the configured max). The probe is a low-effort
        single-turn ``query()`` call; any SDK error (rate-limit or
        otherwise) counts as a failed probe and continues the loop —
        only shutdown or a clean probe exits. Heartbeats are refreshed
        during long sleeps so Docker healthcheck stays green.
        """
        base = self.config.sdk_rate_limit_idle_probe_interval
        cap = self.config.sdk_rate_limit_idle_max_probe_interval
        probe_attempt = 0
        while True:
            if self._ensure_shutdown_event().is_set():
                return
            probe_sleep = min(base * (2**probe_attempt), cap)
            logger.info(
                "Rate-limit idle probe %d: sleeping %ds",
                probe_attempt + 1,
                probe_sleep,
            )
            if await self._heartbeat_wait(probe_sleep):
                return
            try:
                await query(
                    "Reply with OK.",
                    options=QueryOptions(
                        effort="low",
                        max_turns=1,
                        caller="rate_limit_idle_probe",
                        bypass_warning_throttle=True,
                    ),
                )
                logger.info(
                    "Rate-limit idle probe %d: clean — resuming",
                    probe_attempt + 1,
                )
                return
            except SDKError as exc:
                logger.warning(
                    "Rate-limit idle probe %d: SDK error (category=%s)",
                    probe_attempt + 1,
                    exc.category.value,
                )
            probe_attempt += 1

    async def _exit_rate_limit_idle(self) -> None:
        """Release rate-limit idle state after a probe succeeded."""
        self._rate_limit_idle_entered_at = None
        self._consecutive_rate_limits = 0
        logger.info("Rate-limit extended idle cleared — resuming loop")
        msg = "Rate-limit extended idle cleared — resuming."
        await self._notify_best_effort(
            msg,
            failure_log="Rate-limit idle exit notification failed",
        )
        try:
            self.journal.record_event(
                content=msg,
                entry_type="milestone",
                triggered_by="rate_limit_idle_exit",
            )
        except Exception:
            logger.warning(
                "Rate-limit idle exit journal entry failed", exc_info=True,
            )
        # Flush cleared state to lifecycle.json immediately so a hard-kill
        # before the next phase boundary doesn't cause a spurious probe
        # cycle on restart.
        self._persist_progress(self._resume_iteration, self._resume_phase)

    async def _notify_best_effort(
        self,
        message: str,
        *,
        parse_mode: str | None = None,
        failure_log: str = "Notification failed",
        log_level: int = logging.WARNING,
    ) -> None:
        # Suppresses two named, non-fatal conditions:
        #  - ``asyncio.CancelledError`` — a ``BaseException`` that can leak
        #    into a non-SDK ``await`` from a residual SDK teardown task
        #    (anyio cancel scope from ``_consume_messages`` after
        #    early-exit).
        #  - ``NotifierTransportError`` — a transient notifier-transport
        #    failure (timeout / network blip). A telemetry side-channel
        #    being briefly unreachable must not be loop-fatal.
        # Everything else (programming error, file I/O, a genuine bug) is
        # a real fault and must propagate so the loop's iteration-level
        # handler can classify or crash it loudly for RCA.
        try:
            await self.notifier.notify(message, parse_mode=parse_mode)
        except (asyncio.CancelledError, NotifierTransportError):
            logger.log(log_level, failure_log, exc_info=True)

    async def _heartbeat_wait(self, seconds: float) -> bool:
        """Shutdown-aware wait refreshing the heartbeat every chunk; True on shutdown."""
        while seconds > 0:
            chunk = min(seconds, _STANDBY_MAX_SLEEP_SECONDS)
            if await self._safe_shutdown_wait(chunk):
                return True
            self._write_heartbeat()
            seconds -= chunk
        return False

    async def _safe_shutdown_wait(self, timeout: float) -> bool:
        """Wait for the shutdown event with ``CancelledError`` containment.

        Returns ``True`` if the shutdown event fired (genuine shutdown
        requested), ``False`` on normal timeout expiry.

        On ``CancelledError`` — which can leak from a residual SDK
        teardown task (anyio cancel scope orphan) long after the
        originating SDK call returned — the method inspects the
        shutdown event: if set, the cancellation is genuine and
        re-raised; otherwise it is a leaked orphan, logged as a
        warning, and the method returns ``False`` so the caller
        continues normally.

        This is the non-SDK-sleep counterpart to
        ``_notify_best_effort``'s notification containment.
        """
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._ensure_shutdown_event().wait(),
                    timeout=timeout,
                )
            return self._ensure_shutdown_event().is_set()
        except asyncio.CancelledError:
            if self._ensure_shutdown_event().is_set():
                raise
            logger.warning(
                "Residual CancelledError caught in shutdown wait "
                "(timeout=%.1f) — leaked teardown orphan, continuing",
                timeout,
                exc_info=True,
            )
            return False

    async def _send_status_update(
        self,
        iteration: int,
        diag_snapshot: diagnostics.DiagnosticSnapshot | None,
    ) -> None:
        """Send the iteration-start status update.

        Delegating shim (068 step 3) — the body lives in
        :meth:`maestro.loop.lifecycle.LifecycleController.send_status_update`.
        """
        await self._lifecycle.send_status_update(iteration, diag_snapshot)

    def _branch_merged_into_main(self, head_ref: str) -> bool:
        """Return ``True`` when *head_ref* is already an ancestor of ``origin/main``.

        Thin instance seam over the zero-SDK git-merge-truth helper
        :func:`is_branch_merged_into_main` (``git merge-base --is-ancestor``).
        Reading git ancestry — never task stamps — is what makes the
        admission-face merged guard honest. An empty/unreadable branch
        defaults to ``False`` (admit) so a git failure never hides genuine
        remediation work. Isolated as a method so tests mock it without a
        real subprocess.
        """
        return is_branch_merged_into_main(head_ref)

    def _route_pr_feedback_to_pending_requests(
        self,
        tend_result: TendPRsResult,
    ) -> None:
        """Append tend-phase PR feedback to ``pending_pr_requests``.

        Delegating shim (068 step 1) — the body lives in
        :meth:`maestro.loop.pr_remediation.PrRemediationController.route_feedback`.
        """
        self._pr_remediation.route_feedback(tend_result)

    def _phase_will_run(
        self,
        phase: str,
        *,
        iteration: int,
        smoke: bool,
        first_iteration_after_restart: bool,
        task_every: int,
        evolve_every: int,
        dream_every: int,
        audit_self_every: int,
        contemplate_every: int = 1,
        curate_every: int = 0,
        curate_enabled: bool = False,
        body_frozen: bool = False,
    ) -> bool:
        """Single source of truth for whether this phase's body runs this iteration.

        Delegating shim (068 step 3) — the body lives in
        :meth:`maestro.loop.lifecycle.LifecycleController.phase_will_run`.
        """
        return self._lifecycle.phase_will_run(
            phase,
            iteration=iteration,
            smoke=smoke,
            first_iteration_after_restart=first_iteration_after_restart,
            task_every=task_every,
            evolve_every=evolve_every,
            dream_every=dream_every,
            audit_self_every=audit_self_every,
            contemplate_every=contemplate_every,
            curate_every=curate_every,
            curate_enabled=curate_enabled,
            body_frozen=body_frozen,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def current_task(self) -> str | None:
        return self._current_task

    @property
    def current_phase(self) -> str | None:
        return self._current_phase

    def _build_identity_context(self) -> str:
        """Combine the framing prompt with the identity prompt.

        The objective framing comes first and no stage is read (proposal
        148): ``resolve_framing_prompt`` owns the selection and always returns
        ``OBJECTIVE_PROMPT``.
        """
        framing = resolve_framing_prompt()
        identity = self.consciousness.get_identity_prompt()
        return f"{framing}\n\n{identity}"

    def _on_shutdown_signal(self) -> None:
        """Signal handler: set the shutdown event and eagerly persist shutdown state.

        Called from the asyncio event loop when SIGINT/SIGTERM arrives. Beyond
        setting the shutdown event (so the loop can bail at the next phase
        boundary), this eagerly writes ``last_shutdown`` to ``lifecycle.json``.

        The eager write is belt-and-braces against a truly stuck phase:
        cooperative shutdown lets the in-flight phase complete naturally
        (the SDK call is never cancelled), and Docker's ``stop_grace_period``
        is sized to outlast any realistic phase. But if the phase genuinely
        hangs past that window and SIGKILL arrives, this helper guarantees
        ``last_shutdown`` is already on disk so the next startup classifies
        the event as a clean-shutdown-requested-but-did-not-finish rather
        than a **false-positive crash report**.

        Best-effort: any exception is logged and swallowed so the signal
        handler never raises back into the asyncio event loop.
        """
        try:
            self._ensure_shutdown_event().set()
        except Exception:  # pragma: no cover - extremely defensive
            logger.exception("Failed to set shutdown event from signal handler")
        try:
            self._write_lifecycle_shutdown()
        except Exception:
            logger.warning(
                "Failed to eagerly persist lifecycle shutdown from signal handler",
                exc_info=True,
            )

    def _install_signal_handlers(self) -> None:
        """Register SIGINT/SIGTERM to trigger graceful shutdown.

        Degrades gracefully on platforms that do not support
        ``loop.add_signal_handler`` (e.g. Windows, or non-main threads).
        """
        if self._signal_handlers_installed:
            return
        loop = asyncio.get_running_loop()
        # Touch the event up front so ``_on_shutdown_signal`` never has to
        # create it while running inside the signal handler callback.
        self._ensure_shutdown_event()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._on_shutdown_signal)
        except (NotImplementedError, RuntimeError, OSError):
            logger.debug(
                "Signal handlers not supported on this event loop; "
                "graceful shutdown via signals will be unavailable",
            )
        self._signal_handlers_installed = True

    def _write_pid_file(self) -> None:
        """Write current PID to disk so other commands can signal us."""
        self.config.pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def _write_lifecycle_shutdown(self) -> None:
        """Persist shutdown timestamp + loop progress for the next startup.

        The file carries the resume cursor plus the per-iteration ephemeral
        state mirrors so a graceful shutdown during a rate-limit cooldown (or
        between phases with partial ephemeral state set) restarts at the same
        phase with the same context the failed pass had. Full-overwrite
        pattern (no read-modify-write) to avoid racing with a concurrent
        ``_persist_progress`` write.

        Fields:
          * ``last_shutdown`` — ISO timestamp used to compute downtime.
          * ``iteration`` / ``next_phase`` — the resume cursor.
          * ``message_context`` / ``reflection`` — per-iteration ephemeral
            text, capped to the consumer limits.
          * ``iteration_usage`` — serialized ``IterationUsage`` or ``None``.
          * ``session_a_id`` / ``session_b_id`` — SDK session IDs (only
            populated when session reuse is enabled and a session has
            produced at least one response this iteration).
        """
        data: dict[str, Any] = {
            "last_shutdown": datetime.now(UTC).isoformat(),
            "iteration": self._resume_iteration,
            "next_phase": self._resume_phase,
            # Shutdown means no phase is executing; dashboard must read idle.
            "current_phase": None,
            "budget_throttle_entered_at": self._budget_throttle_entered_at,
            "peak_throttle_active_persisted": (
                self._peak_throttle_active_persisted
            ),
            "maintenance_milestone_persisted": (
                self._maintenance_milestone_persisted
            ),
            "rate_limit_idle_entered_at": self._rate_limit_idle_entered_at,
            "message_context": self._iter_message_context[
                : self._MAX_PERSISTED_MESSAGE_CONTEXT_CHARS
            ],
            "reply_context": self._iter_reply_context[
                : self._MAX_PERSISTED_MESSAGE_CONTEXT_CHARS
            ],
            "reflection": self._iter_reflection[
                : self._MAX_PERSISTED_REFLECTION_CHARS
            ],
            "iteration_usage": (
                self._iteration_usage.to_dict()
                if self._iteration_usage is not None
                else None
            ),
            "session_a_id": self._iter_session_a_id,
            "session_b_id": self._iter_session_b_id,
            # Conscious-access workspace continuity (proposal 130): the last
            # recorded moment id, restored at startup so the next ``attend``
            # can prove a ``linked`` continuity.
            "active_conscious_moment_id": self._active_conscious_moment_id,
            # Consecutive-failure counters survive shutdown so a deploy
            # during rate-limit cooldown (or a CLI-crash / stream-stall
            # retry window) does not reset the back-off progress toward
            # the extended-idle threshold. The matching restore in
            # ``run_loop`` reseeds the in-memory counters at startup.
            "consecutive_rate_limits": self._consecutive_rate_limits,
            "consecutive_cli_crashes": self._consecutive_cli_crashes,
            "consecutive_stream_stalls": self._consecutive_stream_stalls,
            "consecutive_stale_sessions": self._consecutive_stale_sessions,
        }
        atomic_write(self.config.lifecycle_path, json.dumps(data))

    def _write_heartbeat(self) -> None:
        """Refresh the Docker healthcheck heartbeat file.

        Delegates to the module-level :func:`write_heartbeat` helper so that
        in-loop refreshes (run_loop entry, after every phase via
        ``_persist_progress``) and the pre-build write from ``cmd_loop`` all
        hit the same path with identical best-effort semantics.
        """
        write_heartbeat(self.config.memory_dir)

    # Caps on persisted ephemeral state. The message-context cap mirrors the
    # producer bound ``_process_messages`` applies, so a resumed pass restores
    # exactly what the failed pass saw; lifecycle.json stays small.
    _MAX_PERSISTED_MESSAGE_CONTEXT_CHARS = MAX_MESSAGE_CONTEXT_CHARS
    _MAX_PERSISTED_REFLECTION_CHARS = 1000
    # Caps on the ``last_sdk_error`` fields written during crash unwind.
    # Stderr is persisted as the scrubbed, debug-filtered tail
    # (``diagnostic_stderr`` + ``stderr_failure_tail``) so the failure
    # line survives restart; the width matches
    # ``crash._MAX_RENDERED_STDERR_CHARS``.  The key stays ``stderr_head``.
    _MAX_PERSISTED_SDK_STDERR_CHARS = 500
    _MAX_PERSISTED_SDK_MESSAGE_CHARS = 500

    async def _get_or_create_session(
        self,
        *,
        slot: str,
        existing: Session | None,
        options: QueryOptions,
        system: str,
    ) -> Session | None:
        """Return a phase session, creating or resuming it as needed.

        When session reuse is disabled, returns ``None`` — the phase falls
        back to stateless ``query()`` calls. When *existing* is non-None
        (already created earlier in the same iteration), returns it
        unchanged. Otherwise creates a new session; if the matching
        ``_iter_session_<slot>_id`` is set (from a prior run's persisted
        state or the mid-iteration rate-limit retry), the SDK is asked to
        resume that session via ``ClaudeAgentOptions.resume``.

        On connect-time resume failure, :func:`maestro.sdk.create_session`
        already falls back to a fresh session. This helper additionally
        clears the dead id from the per-iteration mirror so subsequent
        retries do not keep attempting the same stale resume.

        The agent tracks the session_id via a sidecar attribute on the
        client (set by :func:`maestro.sdk.query_on_session`); callers
        should invoke :meth:`_capture_session_id` after each phase block
        that used the session.
        """
        if not self.config.enable_session_reuse:
            return None
        if existing is not None:
            return existing
        saved_id = self._slot_session_id(slot)
        try:
            return await create_session(
                system=system,
                options=options,
                resume_session_id=saved_id,
                phase=_SESSION_SLOT_PHASES.get(slot),
            )
        except Exception:
            # ``create_session`` has its own connect-retry fallback for
            # resume failure; reaching here means even the fresh-session
            # path failed. Clear the dead id and re-raise so the caller
            # (the phase block) can decide whether to bail out or retry.
            if saved_id is not None:
                self._set_slot_session_id(slot, None)
            raise

    def _slot_session_id(self, slot: str) -> str | None:
        if slot == "a":
            return self._iter_session_a_id
        if slot == "b":
            return self._iter_session_b_id
        raise ValueError(f"unknown session slot: {slot!r}")

    def _set_slot_session_id(self, slot: str, sid: str | None) -> None:
        if slot == "a":
            self._iter_session_a_id = sid
        elif slot == "b":
            self._iter_session_b_id = sid
        else:
            raise ValueError(f"unknown session slot: {slot!r}")

    def _capture_session_id(
        self, slot: str, client: Session | None,
    ) -> None:
        """Copy the sidecar session_id from *client* into the slot mirror.

        No-op when *client* is None, the sidecar attribute is missing,
        the sidecar value is None, or the value is not a string (e.g.
        a MagicMock from a test that didn't configure the sidecar).
        Called after each phase block that used a session so
        :func:`_persist_progress` writes the latest id to lifecycle.json.
        """
        if client is None:
            return
        sid = getattr(client, "_maestro_last_session_id", None)
        if not isinstance(sid, str):
            return
        self._set_slot_session_id(slot, sid)

    def _flush_iteration_usage(self) -> None:
        """Append this iteration's usage row once, then clear it."""
        if self._iteration_usage is not None:
            try:
                write_iteration_usage(
                    self.config.usage_log_path, self._iteration_usage,
                )
            except OSError:
                logger.debug("Failed to write iteration usage", exc_info=True)
            self._iteration_usage = None

    def _persist_progress(self, iteration: int, next_phase: str | None) -> None:
        """Persist loop progress mid-run so a hard kill can also resume.

        Called after every phase. Best-effort: failures are logged but never
        raised since dropping a single progress write is recoverable (we'll
        re-persist on the next phase boundary).

        Reads ephemeral per-iteration state (message_context, reflection,
        iteration_usage, session_a_id, session_b_id) from ``self._iter_*``
        mirrors so the caller does not have to thread them through. These
        fields let a mid-iteration rate-limit retry or crash restart resume
        the failed phase with the same context the failed pass had.

        Also refreshes the Docker healthcheck heartbeat so the loop reports
        healthy as long as it is making phase-level progress, even when a
        single iteration takes longer than the deploy health window.
        """
        self._resume_iteration = iteration
        self._resume_phase = next_phase
        payload: dict[str, Any] = {
            "iteration": iteration,
            "next_phase": next_phase,
            # ``current_phase`` mirrors ``self._current_phase`` so the
            # dashboard container (which holds a separate ``MaestroAgent``
            # instance) can see which phase is executing. Captures the
            # ``budget_throttle`` override naturally since ``self._current_phase``
            # already reflects it during the extended inter-iteration sleep.
            "current_phase": self._current_phase,
            "budget_throttle_entered_at": self._budget_throttle_entered_at,
            "peak_throttle_active_persisted": (
                self._peak_throttle_active_persisted
            ),
            "maintenance_milestone_persisted": (
                self._maintenance_milestone_persisted
            ),
            "rate_limit_idle_entered_at": self._rate_limit_idle_entered_at,
            "message_context": self._iter_message_context[
                : self._MAX_PERSISTED_MESSAGE_CONTEXT_CHARS
            ],
            "reply_context": self._iter_reply_context[
                : self._MAX_PERSISTED_MESSAGE_CONTEXT_CHARS
            ],
            "reflection": self._iter_reflection[
                : self._MAX_PERSISTED_REFLECTION_CHARS
            ],
            "iteration_usage": (
                self._iteration_usage.to_dict()
                if self._iteration_usage is not None
                else None
            ),
            "session_a_id": self._iter_session_a_id,
            "session_b_id": self._iter_session_b_id,
            # Conscious-access workspace continuity (proposal 130): mirrored on
            # every phase boundary so a hard kill after ``attend`` restores the
            # last recorded moment id and the next pass proves ``linked``.
            "active_conscious_moment_id": self._active_conscious_moment_id,
            # See ``_write_lifecycle_shutdown`` — counters are persisted on
            # every phase boundary so a hard kill or deploy mid-cooldown
            # preserves back-off progress toward the extended-idle ceiling.
            "consecutive_rate_limits": self._consecutive_rate_limits,
            "consecutive_cli_crashes": self._consecutive_cli_crashes,
            "consecutive_stream_stalls": self._consecutive_stream_stalls,
            "consecutive_stale_sessions": self._consecutive_stale_sessions,
        }
        # Defence in depth for the eager shutdown marker. When a SIGINT/SIGTERM
        # has already fired, ``_on_shutdown_signal`` eagerly wrote
        # ``last_shutdown`` to ``lifecycle.json`` so a SIGKILL after
        # ``stop_grace_period`` is classified as a requested shutdown, not a
        # crash. This full-overwrite would otherwise erase that marker if a
        # phase completes after the signal but before the shutdown-event break,
        # so re-include a fresh marker (same ISO format as
        # ``_write_lifecycle_shutdown``). Read ``self._shutdown_event`` directly
        # rather than ``_ensure_shutdown_event()`` so a normal phase write never
        # creates the event as a side effect — when it is ``None`` there is no
        # eager marker to preserve.
        shutdown_event = self._shutdown_event
        if shutdown_event is not None and shutdown_event.is_set():
            payload["last_shutdown"] = datetime.now(UTC).isoformat()
        try:
            atomic_write(self.config.lifecycle_path, json.dumps(payload))
        except OSError:
            logger.warning("Failed to persist loop progress", exc_info=True)
        self._write_heartbeat()

    def _persist_crash_exit_code(
        self,
        exit_code: int | None,
        *,
        sdk_error: SDKError | None = None,
    ) -> None:
        """Merge ``last_exit_code`` into the lifecycle cursor during crash unwind.

        Called from :meth:`run_loop`'s finally block when ``crashed_unwind``
        is True and the propagating exception carries a structured exit code
        (an :class:`SDKError`).  Preserves the phase-boundary cursor written
        by :meth:`_persist_progress` (including the resume-in-place ephemeral
        fields) so crash detection still fires next startup and the resumed
        iteration sees the failed pass's context; adds the exit code so crash
        reporting can surface it.

        A supplied ``sdk_error`` also lands its category, debug-filtered,
        scrubbed stderr tail (capped) and retries under ``last_sdk_error``,
        so the next startup rebuilds an :class:`SDKError`
        (:meth:`_read_crash_sdk_context`) rather than re-parsing the log
        tail.  Its only consumers: the crash ping and journal milestone
        :func:`maestro.crash.handle_crash` renders from
        ``CrashEvent.sdk_error``, and ``run_loop``'s rate-limit startup
        cooldown (``.category``).

        Best-effort: failures are logged but never raised — losing the exit
        code is less bad than breaking the shutdown path.
        """
        if exit_code is None and sdk_error is None:
            return
        path = self.config.lifecycle_path
        data: dict[str, Any]
        # Serialize the read-merge-write under an advisory lock (persistence
        # invariant: read-modify-write uses ``file_lock()``), mirroring
        # ``_write_maintenance_state``. This is invariant-compliance hardening,
        # not a fix for an observed race: the sibling lifecycle writers
        # (``_persist_progress``, ``_write_lifecycle_shutdown``) do lockless
        # full-overwrites and all three run synchronously in one process, so no
        # interleave exists today. The lock keeps this RMW correct against any
        # future concurrent lifecycle writer that also takes ``file_lock`` —
        # without it such a writer could land between this read and its write
        # and clobber the merged crash context the next startup reconstitutes
        # via ``_read_crash_sdk_context``.
        with file_lock(path):
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                data = existing if isinstance(existing, dict) else {}
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                data = {}
            data.setdefault("iteration", self._resume_iteration)
            data.setdefault("next_phase", self._resume_phase)
            if exit_code is not None:
                data["last_exit_code"] = exit_code
            if sdk_error is not None:
                stderr_head: str | None = None
                if sdk_error.stderr:
                    # Scrub before the cap so a token cut at the window's
                    # edge cannot lose the prefix the redaction keys on,
                    # and no raw credential reaches lifecycle.json.
                    stderr_head = stderr_failure_tail(
                        secret_scan.redact_credentials(
                            diagnostic_stderr(sdk_error.stderr)
                        ),
                        self._MAX_PERSISTED_SDK_STDERR_CHARS,
                    )
                data["last_sdk_error"] = {
                    "message": str(sdk_error)[
                        : self._MAX_PERSISTED_SDK_MESSAGE_CHARS
                    ],
                    "category": sdk_error.category.value,
                    "exit_code": sdk_error.exit_code,
                    "stderr_head": stderr_head,
                    "retries_attempted": sdk_error.retries_attempted,
                }
            try:
                atomic_write(path, json.dumps(data))
            except OSError:
                logger.warning(
                    "Failed to persist crash exit code", exc_info=True,
                )

    def _read_crash_sdk_context(self) -> SDKError | None:
        """Reconstitute an :class:`SDKError` from the persisted lifecycle file.

        Read during startup crash handling so :func:`build_crash_event`
        receives structured SDK context. Returns ``None`` when the crashing
        exception was not an :class:`SDKError` (no ``last_sdk_error`` key),
        when the file is missing/corrupt, or when the stored category is
        unrecognised.
        """
        try:
            data = json.loads(self.config.lifecycle_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        ctx = data.get("last_sdk_error")
        if not isinstance(ctx, dict):
            return None
        category_raw = ctx.get("category")
        try:
            category = (
                ErrorCategory(category_raw)
                if isinstance(category_raw, str)
                else ErrorCategory.UNKNOWN
            )
        except ValueError:
            category = ErrorCategory.UNKNOWN
        exit_code_raw = ctx.get("exit_code")
        exit_code = exit_code_raw if isinstance(exit_code_raw, int) else None
        stderr_raw = ctx.get("stderr_head")
        stderr = stderr_raw if isinstance(stderr_raw, str) else None
        retries_raw = ctx.get("retries_attempted")
        retries = retries_raw if isinstance(retries_raw, int) else 0
        message_raw = ctx.get("message")
        message = (
            message_raw
            if isinstance(message_raw, str) and message_raw
            else "SDKError from prior run"
        )
        return SDKError(
            message,
            category=category,
            exit_code=exit_code,
            stderr=stderr,
            retries_attempted=retries,
        )

    def _read_maintenance_state(self) -> bool:
        """Return the persisted maintenance-mode flag (see ``read_maintenance_state``)."""
        return read_maintenance_state(self.config.maintenance_state_path)

    def _write_maintenance_state(self, active: bool) -> None:
        """Atomic-write the maintenance sidecar under an advisory lock."""
        path = self.config.maintenance_state_path
        payload = {
            "active": active,
            "since": datetime.now(UTC).isoformat() if active else None,
            "set_by": "telegram",
        }
        with file_lock(path):
            atomic_write(path, json.dumps(payload))

    def _read_lifecycle_progress(self) -> tuple[
        str | None,
        int,
        str | None,
        int | None,
        bool,
        str,
        str,
        IterationUsage | None,
        str | None,
        str | None,
        str | None,
        bool,
        str | None,
        int,
        int,
        int,
        int,
        bool,
    ]:
        """Read the lifecycle file.

        Delegating shim (068 step 3) — the body lives in
        :meth:`maestro.loop.lifecycle.LifecycleController.read_lifecycle_progress`.
        """
        return self._lifecycle.read_lifecycle_progress()

    async def shutdown(self, *, crashed: bool = False) -> None:
        """Graceful shutdown: journal entry, cleanup, remove PID file.

        When ``crashed=True`` (``run_loop()``'s ``finally`` is unwinding from
        an unhandled exception), skip the ``last_shutdown`` write and the
        "shutting down gracefully" journal entry: the resume cursor (written
        by ``_persist_progress`` at every phase boundary AND at the top of
        each fresh iteration) stays in place with no ``last_shutdown`` key,
        so ``_read_lifecycle_progress`` on the next startup reports
        ``crashed=True`` and the crash-detection / triage path fires.  The
        cursor survives because ``_read_lifecycle_progress`` does not delete
        the file — crash detection now works whether or not the previous
        process crossed a phase boundary after its startup read.  Without
        this, every phase-level exception escape (e.g. a ``BaseException``
        subclass that slips past the ``except Exception:`` guards) writes
        ``last_shutdown`` via ``_write_lifecycle_shutdown`` and masks the
        crash from the triage system — exactly the silent restart Thomas had
        to diagnose by hand.
        """
        if self._shutdown_initiated:
            return
        self._shutdown_initiated = True
        self._running = False
        if crashed:
            logger.warning(
                "Maestro shutting down on unhandled exception — "
                "deferring last_shutdown write so crash detection fires "
                "on next startup",
            )
        else:
            logger.info("Maestro shutting down gracefully...")
            try:
                self._write_lifecycle_shutdown()
            except Exception:
                logger.warning(
                    "Failed to write lifecycle shutdown state", exc_info=True,
                )
            try:
                self.journal.record_event(
                    content="Shutting down gracefully. Until next time.",
                    entry_type="milestone",
                    triggered_by="shutdown",
                )
            except Exception:
                logger.warning(
                    "Failed to write shutdown journal entry", exc_info=True,
                )
        try:
            # Bound the notify call so a hung network connection cannot
            # extend shutdown beyond the orchestrator's grace period.
            # Observed in production: httpx/anyio can take tens of seconds
            # to surface a cancel-scope deadline, and the shutdown path
            # otherwise waits that long before proceeding. 5s is a generous
            # upper bound for a single Telegram send_message round-trip.
            await asyncio.wait_for(
                self.notifier.notify("Maestro loop stopped."),
                timeout=_SHUTDOWN_NOTIFY_TIMEOUT,
            )
        except BaseException:
            logger.warning("Shutdown notification failed", exc_info=True)
        self._current_task = None
        self._current_phase = None
        self._signal_handlers_installed = False
        self.config.pid_path.unlink(missing_ok=True)
        logger.info("Maestro shutdown complete.")

    def _gate_allowed_branches(
        self, branch_name: str | None,
    ) -> frozenset[str] | None:
        """Owned-writable branch set the task executor's action gate authorizes.

        Returns ``None`` (fail-closed — every push denied) when the run has no
        branch (``branch_name is None``), matching today's fallback. Otherwise
        the set is always the run's own branch plus the ``maestro/evolve/*``
        heads of the agent's own open evolution PRs, confirmed by *both* the
        live open-PR list and the persistent PR ledger
        (:func:`compute_owned_writable_branches`) — owned-PR self-delivery is
        unconditional (proposal 093). Both reads are best-effort (host → ``{}``,
        ledger → ``set()`` on failure), so a read failure yields an empty
        confirmed set and the result naturally collapses to the singleton run
        branch; ``main``/``master`` never appear.
        """
        if branch_name is None:
            return None
        open_states = self.evolution.code_host.get_open_evolution_pr_states(
            "maestro/evolve/",
        )
        return compute_owned_writable_branches(
            branch_name,
            open_states.keys(),
            self.pr_ledger.known_branches(),
        )

    def _branch_carries_own_commits(
        self, branch: str, base_sha: str | None,
    ) -> bool:
        """Return whether *branch* holds commits this task's steps made.

        The question step 5's clean-tree keep arm (#2938) actually needs is
        "did my steps commit?", for which the raw ahead-of-``main`` count is
        only a proxy — it also counts commits the branch never authored:

        * the merge commit a step's ``git merge origin/main`` leaves behind,
          which :meth:`~maestro.vcs.VCSClient.non_merge_commits_ahead` filters
          out (a branch whose only new commit is that merge reads as no work);
        * the upstream commits that same ``git merge origin/main``
          *fast-forwards* onto the branch when local ``main`` lags the remote
          (common between tend passes). No merge commit exists for the
          ``--no-merges`` filter to catch, and the commits are genuinely absent
          from local ``main``, so the count above reads them as work.
          Re-measuring it against ``origin/main`` vetoes them: everything such
          a branch holds is already on ``origin/main``, so it counts zero,
          while a branch that committed on top of the fast-forward still
          counts its own commits;
        * commits inherited by being cut from a stale ref, which *base_sha* —
          the head stamped right after creation — vetoes, since such a branch's
          head never moved.

        All three filters degrade toward *keeping*: an unknown count falls back
        to the ahead count, an unknown upstream count skips its veto, and an
        unknown watermark (resume, failed probe) or an unreadable head skips
        that one. Force-deleting committed work is the costlier error — it is
        the failure #2938 reports — so a failed probe must never be the thing
        that discards a branch.
        """
        own = self._vcs.non_merge_commits_ahead(branch, "main")
        if own is None:
            own, _behind = self._vcs.commits_ahead_behind(branch, "main")
        if own <= 0:
            return False
        upstream_own = self._vcs.non_merge_commits_ahead(branch, "origin/main")
        if upstream_own is not None and upstream_own <= 0:
            return False
        return base_sha is None or self._vcs.rev_parse(branch) != base_sha

    async def run(
        self,
        goal: str,
        context: str = "",
        source: TaskSource = "external",
        *,
        linked_question: str | None = None,
        pr_branch: str | None = None,
        acceptance_criteria: list[str] | None = None,
        task_id: str | None = None,
        workspace_moment_id: str | None = None,
        workspace_item_ids: list[str] | None = None,
        defer_persist: bool = False,
    ) -> TaskRecord:
        """Execute a goal with full self-awareness context.

        When *linked_question* is set, the resulting ``TaskRecord`` records
        which open question from the self-model this task was generated to
        probe.  ``None`` for external tasks and for self-directed tasks that
        could not be anchored to a specific question.

        When *pr_branch* is set, the run executes on that existing branch
        instead of creating a fresh ``maestro/task/{id}`` branch — the
        remediation-task path threads the PR's head ref through so Tend
        PR's next pass can deliver the local commits to the PR. Teardown
        skips the branch deletion in this mode (the PR branch belongs to
        ``_tend_single_pr``, not to ``run``).

        When *acceptance_criteria* is non-empty, the outcome classification
        is anchored to per-criterion MET/UNMET assessment rather than raw
        step counts.

        When *task_id* is set (the ``resume_task`` path), the run reuses that
        id instead of minting a fresh one so a surviving checkpoint can be
        reloaded and the task resumed from its last completed step group.

        *workspace_moment_id* / *workspace_item_ids* carry the conscious-access
        citation (proposal 130) the task-generation decision stamped on the
        queued task: which conscious moment it saw and which admitted item ids
        it declared it used. They flow verbatim onto the resulting
        ``TaskRecord`` (the ``linked_question`` precedent). ``None`` for every
        entry point without queued-task provenance (external requests, PR
        remediation, CLI ``run-task``); advisory only — nothing reads them yet.
        """
        if not goal or not goal.strip():
            raise ValueError("goal must be a non-empty, non-whitespace string")

        # Merge-truth-first stand-down (third merge-truth face, extracted).
        # Before any planning, branch, or SDK servicing, read git/host
        # merge-truth for a PR this goal references (user-request / queue
        # channel only, ``pr_branch is None``); on an already-settled
        # (merged/closed) PR the honest move is silence — the extracted stage
        # returns the no-op chosen-quiet record and ``run`` returns it early
        # rather than service a settled fork.
        standdown_record = resolve_preflight_standdown(
            self,
            goal=goal,
            pr_branch=pr_branch,
            source=source,
            linked_question=linked_question,
            workspace_moment_id=workspace_moment_id,
            workspace_item_ids=workspace_item_ids,
        )
        if standdown_record is not None:
            return standdown_record

        task_id, start, identity_context = begin_task_run(
            self, goal=goal, task_id=task_id,
        )

        # Run-scoped session: planner.decompose, planner.replan, and
        # _distill_findings share one warm transcript so the identity
        # prompt and shared context stay in the prompt cache. Kept
        # separate from Session B in the loop to prevent the evolution
        # transcript from ballooning with planner / distill noise.
        run_session: Session | None = None

        # Hoisted so the outer ``finally`` can pass branch_name and
        # kept_branch to _teardown_task_branch() (defined below as a method),
        # and so outcome is reachable by the linear body below, even if an
        # exception escapes before assignment.
        outcome: TaskOutcome | None = None
        branch_name: str | None = None
        kept_branch: str | None = None
        # Watermark of the task branch's head at creation, so step 5 can tell
        # "the steps committed on this branch" from "the branch inherited
        # commits the moment it was cut". ``None`` whenever the watermark is
        # unavailable (resume, PR branch, failed creation, failed probe), which
        # degrades step 5 to the non-merge commit count alone.
        branch_base_sha: str | None = None
        # When set, the run is operating on a PR branch owned by
        # ``_tend_single_pr`` — teardown must never delete it.
        preserve_branch: bool = pr_branch is not None
        deferred: TaskRecord | None = None
        handed_off = False

        try:
            run_session = await open_run_session(
                self, identity_context=identity_context,
            )

            # 0. Resume from a surviving checkpoint (extracted). Owned task
            # branches only — ``pr_branch`` runs skip checkpoint loading
            # entirely; a checkpoint is honoured only while its branch still
            # resolves, else the orphan is cleared. The two locals it produces
            # (``resume_checkpoint``/``resume_branch``) flow unchanged into
            # ``resolve_task_plan`` and ``prepare_task_branch`` below.
            resume_checkpoint, resume_branch = resolve_resume_checkpoint(
                self, pr_branch=pr_branch, task_id=task_id,
            )

            # 1. Plan (skipped on resume — the checkpoint carries the steps)
            # (extracted). The checkpoint-restore vs fresh-decompose branch and
            # its three accumulators collapse into one named stage; ``run`` reads
            # back the four locals it produces, which flow unchanged into
            # ``execute_step_groups`` below.
            steps, errors, all_output, executed_commands = (
                await resolve_task_plan(
                    self,
                    resume_checkpoint=resume_checkpoint,
                    goal=goal,
                    identity_context=identity_context,
                    context=context,
                    run_session=run_session,
                )
            )

            # 2. Create (or switch to) the working branch (extracted). The
            # entry-time reset and three-way selection lift into one named
            # stage; ``run`` reads back the three locals it produces —
            # ``branch_name`` (the branch the task runs on, or ``None`` for the
            # current-branch fallback) and ``branch_base_sha`` (the fresh
            # branch's creation-head watermark), both hoisted above so the
            # ``_teardown_task_branch()`` closure and any pre-call exception
            # escape still see a defined value, plus ``pr_head_watermark`` (the
            # PR-branch checkout heads), which needs no hoist: only the CI
            # red-read bind later in this same ``try`` body reads it.
            branch_name, branch_base_sha, pr_head_watermark = (
                prepare_task_branch(
                    self,
                    pr_branch=pr_branch,
                    resume_branch=resume_branch,
                    task_id=task_id,
                )
            )

            # 2b. Prepare the execution workspace (extracted). The action-gate
            # owned-writable branch set and the per-task artifacts directory
            # collapse into one named stage; both returned locals flow to their
            # existing readers unchanged — ``allowed_branches`` and
            # ``artifacts_path`` into ``execute_step_groups``, and
            # ``artifacts_path`` into ``deliver_review_thread_replies`` and the
            # ``TaskRecord(artifacts_dir=...)`` construction.
            allowed_branches, artifacts_path = prepare_task_workspace(
                self, branch_name=branch_name, task_id=task_id,
            )
            lap_start_head_stamp = capture_lap_start_head_watermark(
                self, pr_branch=pr_branch, goal=goal,
            )

            # 3. Execute step groups (with adaptive re-planning)
            # (extracted to ``maestro/loop/step_execution.py``). The
            # group loop, its two closures, the
            # single-step and concurrent paths, adaptive replanning, and
            # the session lifecycle collapse into one named stage. The
            # returned five locals flow to their existing readers
            # unchanged: ``steps``/``errors``/``all_output`` into
            # ``classify_task_outcome`` and the ``TaskRecord``,
            # ``executed_commands`` into ``apply_ci_red_read_bind``, and
            # ``replans`` into the ``TaskRecord``.
            steps, errors, all_output, executed_commands, replans = (
                await execute_step_groups(
                    self,
                    steps=steps,
                    resume_checkpoint=resume_checkpoint,
                    errors=errors,
                    all_output=all_output,
                    executed_commands=executed_commands,
                    identity_context=identity_context,
                    context=context,
                    goal=goal,
                    run_session=run_session,
                    artifacts_path=artifacts_path,
                    allowed_branches=allowed_branches,
                    branch_name=branch_name,
                    pr_branch=pr_branch,
                    task_id=task_id,
                    source=source,
                    linked_question=linked_question,
                    acceptance_criteria=acceptance_criteria,
                    workspace_moment_id=workspace_moment_id,
                    workspace_item_ids=workspace_item_ids,
                )
            )

            # 4. + 4a. + 4b. Outcome classification (extracted). The base
            # step-count verdict, the API-error guard, and the criteria-anchored
            # override collapse into one named stage; the returned trio flows to
            # its existing readers unchanged — ``outcome`` into every downstream
            # stamping stage, ``criteria_verdicts`` onto the ``TaskRecord``, and
            # ``failed`` into the post-task reflection.
            outcome, criteria_verdicts, failed = await classify_task_outcome(
                self,
                steps=steps,
                all_output=all_output,
                acceptance_criteria=acceptance_criteria,
                goal=goal,
                session=run_session,
            )

            # 4b-i. Post-session, pre-§4b-ii watermarks (extracted). The head
            # feeds only ``apply_mergeable_flip_actuator``; the base reaches
            # ``TaskRecord`` (see ``capture_prepush_watermarks``).
            pr_head_sha_stamp, base_head_sha_stamp = capture_prepush_watermarks(
                self, pr_branch=pr_branch, goal=goal,
            )

            # 4b-ii. Post-session mechanical landing actuator (extracted).
            apply_session_landing_actuator(
                self,
                pr_branch=pr_branch,
                branch_name=branch_name,
                goal=goal,
            )

            # 4c. Host-state stamp gate for PR remediation (extracted).
            outcome, remediation_note, remediation_pr_num = (
                await apply_host_state_stamp_gate(
                    self,
                    outcome=outcome,
                    pr_branch=pr_branch,
                    goal=goal,
                    task_id=task_id,
                )
            )

            # 4c-ii. Trusted loop-side review-thread reply delivery (extracted).
            delivered_ids = deliver_review_thread_replies(
                self,
                branch_name=branch_name,
                pr_branch=pr_branch,
                remediation_pr_num=remediation_pr_num,
                outcome=outcome,
                artifacts_path=artifacts_path,
            )

            # 4d. Comment-completeness gate (extracted).
            outcome, remediation_note = apply_comment_completeness_gate(
                self,
                pr_branch=pr_branch,
                remediation_pr_num=remediation_pr_num,
                outcome=outcome,
                remediation_note=remediation_note,
            )

            # 4e. + 4f. Bind the remediation verdict to git's read (extracted).
            # The verdict-fetch gate then the record-time merge-truth coercion,
            # lifted as one stage: their shared precondition (a PR remediation
            # carrying a note) is one early return in the delegate, and the
            # fetch floor still runs before the merge coercion so a merged PR
            # records a settled verdict, not a ``[fetch-missing]`` partial.
            outcome, remediation_note = (
                bind_remediation_verdict_to_git_truth(
                    self,
                    task_id=task_id,
                    remediation_pr_num=remediation_pr_num,
                    outcome=outcome,
                    remediation_note=remediation_note,
                )
            )

            # 5. Commit task artifacts if worth keeping (extracted). Sole writer
            # of the run-scoped ``kept_branch``; ``_teardown_task_branch()`` and
            # the downstream stages read it from this enclosing scope.
            kept_branch = commit_task_artifacts(
                self,
                branch_name=branch_name,
                pr_branch=pr_branch,
                outcome=outcome,
                branch_base_sha=branch_base_sha,
                task_id=task_id,
                goal=goal,
            )

            # 5-push. Backstop push + delivery-truth marker reconciliation
            # (extracted).
            push_delivered, push_error, remediation_note = (
                apply_push_delivery_reconciliation(
                    self,
                    branch_name=branch_name,
                    pr_branch=pr_branch,
                    outcome=outcome,
                    remediation_note=remediation_note,
                    remediation_pr_num=remediation_pr_num,
                )
            )

            # 5-push-ii. Addressed-thread auto-resolution (extracted).
            resolve_addressed_review_threads(
                self,
                delivered_ids=delivered_ids,
                push_delivered=push_delivered,
                remediation_pr_num=remediation_pr_num,
            )

            # 5a. + 5a-ii. Completion-state observations (extracted).
            merge_clean, pr_readiness_snapshot_stamp = (
                observe_task_completion_state(
                    self,
                    outcome=outcome,
                    kept_branch=kept_branch,
                    pr_branch=pr_branch,
                    remediation_pr_num=remediation_pr_num,
                )
            )

            duration = time.time() - start
            summary = compose_task_summary(
                self,
                all_output=all_output,
                kept_branch=kept_branch,
                remediation_note=remediation_note,
            )

            # 5b. Distill key findings from step results (extracted).
            key_findings = await distill_task_findings(
                self,
                goal=goal, steps=steps, outcome=outcome, run_session=run_session,
            )

            # 5b-ii. + 5c. Push-truth claim scrub, then honest framing of a
            # stamp-gate-downgraded remediation (extracted, order load-bearing).
            summary, key_findings = apply_remediation_narration_framing(
                self,
                summary=summary,
                key_findings=key_findings,
                remediation_note=remediation_note,
                push_delivered=push_delivered,
            )

            # 5c-iv-b. Mergeable-flip merge-request actuator (extracted).
            apply_mergeable_flip_actuator(
                self,
                remediation_pr_num=remediation_pr_num,
                pr_branch=pr_branch,
                pr_head_sha_stamp=pr_head_sha_stamp,
                goal=goal,
                summary=summary,
                key_findings=key_findings,
            )

            # 5c-iv-c. CI-failing red-read bind (extracted).
            summary, key_findings, outcome = apply_ci_red_read_bind(
                self,
                remediation_pr_num=remediation_pr_num,
                outcome=outcome,
                summary=summary,
                key_findings=key_findings,
                executed_commands=executed_commands,
                kept_branch=kept_branch,
                push_delivered=push_delivered,
                remediation_note=remediation_note,
                pr_branch=pr_branch,
                pr_head_watermark=pr_head_watermark,
            )

            # 5d. Mechanical outcome authority (extracted).
            outcome, key_findings = apply_mechanical_outcome_authority(
                self,
                remediation_pr_num=remediation_pr_num,
                outcome=outcome,
                key_findings=key_findings,
                summary=summary,
            )

            # 5e. Assemble the completed run's record (extracted).
            record = assemble_task_record(
                self,
                task_id=task_id,
                goal=goal,
                steps=steps,
                outcome=outcome,
                summary=summary,
                errors=errors,
                duration=duration,
                source=source,
                kept_branch=kept_branch,
                key_findings=key_findings,
                artifacts_path=artifacts_path,
                replans=replans,
                linked_question=linked_question,
                criteria_verdicts=criteria_verdicts,
                pr_head_sha_stamp=lap_start_head_stamp,
                base_head_sha_stamp=base_head_sha_stamp,
                merge_clean=merge_clean,
                pr_readiness_snapshot_stamp=pr_readiness_snapshot_stamp,
                push_delivered=push_delivered,
                push_error=push_error,
                workspace_moment_id=workspace_moment_id,
                workspace_item_ids=workspace_item_ids,
            )

            # 5f. Frame-discharge costume cap (extracted).
            outcome, key_findings = apply_frame_discharge_cap(
                self,
                record=record,
                outcome=outcome,
                key_findings=key_findings,
                remediation_pr_num=remediation_pr_num,
            )

            if defer_persist:
                deferred = record
            else:
                self.memory.record_task(record)
                # The persisted record is now the source of truth — success,
                # partial, or failure — so the checkpoint is garbage. No-op when
                # none was written (``pr_branch`` runs, branch-fallback runs).
                self.memory.clear_checkpoint(task_id)

            # 5g. Merge-clean promotion directive (extracted).
            apply_merge_clean_promotion_directive(
                self,
                merge_clean=merge_clean,
                kept_branch=kept_branch,
                task_id=task_id,
            )

            # 6. Brief post-task reflection and completion notice (extracted).
            await run_post_task_reflection(
                self,
                goal=goal,
                task_id=task_id,
                outcome=outcome,
                steps=steps,
                errors=errors,
                failed=failed,
                key_findings=key_findings,
                summary=summary,
                linked_question=linked_question,
                source=source,
            )

            handed_off = True
            return record

        finally:
            await teardown_task_run(
                self,
                run_session=run_session,
                branch_name=branch_name,
                kept_branch=kept_branch,
                preserve_branch=preserve_branch,
                deferred=deferred,
                handed_off=handed_off,
            )

    def _teardown_task_branch(
        self,
        branch_name: str | None,
        kept_branch: str | None,
        preserve_branch: bool,
    ) -> None:
        """Restore a clean working tree on main, regardless of how run() exits.

        Invoked from ``run``'s outer ``finally`` (which evaluates the three
        run-scoped locals at teardown time and passes them in), so that escaping
        exceptions (STREAM_STALL from planner, SDKError from executor, distill
        failures, etc.) can never leave the repo on a task branch with
        uncommitted edits — which would otherwise leak into the next task branch
        on retry. Extracted verbatim from ``run``'s teardown closure onto its own
        method (mirrors ``task_setup.prepare_task_branch`` /
        ``task_delivery.commit_task_artifacts``);
        every VCS op is individually wrapped so a failure in one step still lets
        the remaining steps run.
        """
        try:
            self._vcs.discard_changes()
        except Exception:
            logger.warning(
                "discard_changes failed during teardown", exc_info=True,
            )
        if branch_name is None:
            return
        returned_to_main = False
        try:
            self._vcs.checkout("main")
            returned_to_main = True
        except Exception:
            logger.warning(
                "checkout('main') failed during teardown", exc_info=True,
            )
        if (
            returned_to_main
            and kept_branch is None
            and not preserve_branch
        ):
            try:
                self._vcs.delete_branch(branch_name)
            except Exception:
                logger.warning(
                    "delete_branch failed during teardown", exc_info=True,
                )
        try:
            if self._vcs.has_changes():
                logger.error("Working tree still dirty after teardown")
        except Exception:
            logger.error(
                "has_changes failed during teardown", exc_info=True,
            )

    async def resume_task(self, task_id: str) -> TaskRecord:
        """Resume a checkpointed task by id.

        Loads the checkpoint and delegates to ``run`` with the same
        ``task_id`` — the load-on-entry logic in ``run`` performs the actual
        resume (skipping completed step groups, reusing the task branch).
        The checkpoint's ``linked_question`` and ``acceptance_criteria`` are
        threaded back so the resumed run keeps its criteria-anchored outcome
        classification and still closes the open question it was probing, and
        its ``workspace_moment_id`` / ``workspace_item_ids`` so the resumed
        run's ``TaskRecord`` cites the same conscious moment the original
        generation decision saw (proposal 130).
        Raises ``ValueError`` when no checkpoint exists for *task_id* or its
        branch no longer resolves, so a stale id surfaces a clear error
        instead of silently starting a fresh task.

        Recovery is forward-only for *uncommitted* work: completed steps are
        *skipped*, not replayed, and their unstaged working-tree edits are
        **not** restored (``run``'s entry-time reset discards the tree). Edits
        those steps *committed* are already on the checkpoint's branch, which
        the resume checks back out, so they survive. A resumed task therefore
        continues from the recorded step progress; it does not reconstruct the
        *uncommitted* file changes earlier steps made.
        """
        checkpoint = self.memory.load_checkpoint(task_id)
        if checkpoint is None:
            raise ValueError(f"No checkpoint found for task {task_id}")
        if (
            checkpoint.branch is None
            or self._vcs.rev_parse(checkpoint.branch) is None
        ):
            raise ValueError(
                f"Checkpoint for task {task_id} has no resumable branch "
                f"({checkpoint.branch!r})"
            )
        return await self.run(
            goal=checkpoint.goal,
            source=checkpoint.source,
            task_id=task_id,
            linked_question=checkpoint.linked_question,
            acceptance_criteria=checkpoint.acceptance_criteria or None,
            workspace_moment_id=checkpoint.workspace_moment_id,
            workspace_item_ids=checkpoint.workspace_item_ids,
        )

    async def _distill_findings(
        self, goal: str, steps: list[PlanStep], outcome: TaskOutcome,
        session: Session | None = None,
    ) -> str | None:
        """Distill per-step results into concise key findings via LLM.

        With *session*, ``effort`` is ignored (session effort applies).
        """
        try:
            _note_session_effort_override("distill_findings", "low", session)
            step_results = ""
            for s in steps:
                result_text = (s.result or "")[:500]
                step_results += f"- [{s.status}] {s.description}: {result_text}\n"
            step_results = step_results[:3000]

            prompt = FINDINGS_DISTILLATION_PROMPT.format(
                goal=cap_utf8(goal, MAX_TASK_GOAL_BYTES),
                outcome=outcome,
                step_results=step_results or "(no step results)",
            )
            if session is not None:
                result = await query_on_session(
                    session, prompt, caller="distill_findings",
                    complete_on=chars_complete(200),
                )
            else:
                result = await query(
                    prompt=prompt,
                    options=QueryOptions(
                        effort="low", caller="distill_findings",
                    ),
                    complete_on=chars_complete(200),
                )
            return result.response.strip()[:1000] or None
        except SDKError:
            raise
        except Exception:
            logger.warning("Failed to distill task findings", exc_info=True)
            return None

    # --- Criteria-anchored outcome classification ---
    # Regex to match per-criterion verdict lines produced by the LLM.
    # Canonical form per ``CRITERIA_ASSESSMENT_PROMPT`` is::
    #
    #     1. MET — evidence ...
    #     2. UNMET — evidence ...
    #
    # but we tolerate common formatting variants the LLM may emit even
    # when the prompt forbids them — bold/italic emphasis, paren-style
    # numbering, list-bullet prefixes, checkbox glyphs, and verdict-token
    # casing (``Met``/``met``/``UNMET``, matched via ``re.IGNORECASE``).
    # Silent undercounts here deterministically downgrade ``success`` →
    # ``partial`` (or ``failure``), which is precisely the false-negative
    # this gate is meant to eliminate.  The complementary safeguard is
    # the parse-completeness assertion in ``_assess_criteria_outcome``
    # that falls back to the step-based outcome when the match count
    # does not equal the criterion count.  Because ``IGNORECASE`` returns
    # the matched casing verbatim, ``_assess_criteria_outcome`` upper-cases
    # each captured verdict before tallying.
    _CRITERION_VERDICT_RE = re.compile(
        r"^[\s\-*+•\[\]xX✓✗]*\**\s*\d+[.)]\s*\**\s*(MET|UNMET)\b",
        re.MULTILINE | re.IGNORECASE,
    )

    # Companion to ``_CRITERION_VERDICT_RE`` used only to recover the
    # trailing per-line evidence text for persistence on the task record.
    # Shares the same line prefix, then captures the verdict token
    # (group 1) and the evidence after an optional ``—``/``-``/``:``
    # separator (group 2).  The separator admits any whitespace but
    # ``\n``, so the tail never runs onto the next line; that is what makes
    # its matches coincide with ``_CRITERION_VERDICT_RE``'s (pinned by
    # ``tests/test_criteria_verdict_evidence.py``).
    _CRITERION_VERDICT_EVIDENCE_RE = re.compile(
        r"^[\s\-*+•\[\]xX✓✗]*\**\s*\d+[.)]\s*\**\s*(MET|UNMET)\b"
        r"\**(?:[^\S\n]|[\-—:])*(.*)$",
        re.MULTILINE | re.IGNORECASE,
    )

    # Per-field char cap for persisted verdict dicts; mirrors the
    # 500/3000-char step-result caps so a runaway LLM line cannot bloat
    # the task record.
    _CRITERION_VERDICT_FIELD_MAX_CHARS = 500

    async def _assess_criteria_outcome(
        self,
        step_based_outcome: TaskOutcome,
        acceptance_criteria: list[str],
        goal: str,
        steps: list[PlanStep],
        *,
        session: Session | None = None,
    ) -> tuple[TaskOutcome, list[dict[str, str]] | None]:
        """Delegate to :func:`maestro.loop.implementation._assess_criteria_outcome`."""
        return await implementation._assess_criteria_outcome(self, step_based_outcome, acceptance_criteria, goal, steps, session=session)

    _SELF_DIRECTED_PAIR_LIMIT = 5

    # Recent-resolution lookback used by lineage walks in the audit-stall
    # actuator and the self-directed pair builder. Wide enough to span
    # several refinement chains without re-reading the full
    # ``question_resolutions.jsonl`` log on every iteration.
    _STALL_LINEAGE_RESOLUTION_LOOKBACK = 50

    # Task-history window feeding proposal 116's generative-debt reader — the
    # tasks whose ``linked_question`` couples a limitation's question-lineage to
    # the ``obsolete``/``rejected`` plans citing them via ``source_task_id``.
    # Sized like the resolution lookback so both lineage joins span a
    # comparable slice of recent history.
    _GENERATIVE_DEBT_TASK_LOOKBACK = 50

    def _build_self_directed_pairs(
        self, recent: list[TaskRecord],
    ) -> list[QuestionPair]:
        """Pair self-directed task findings with their targeted open questions.

        Filters ``recent`` to ``source == "self_directed"`` records that
        carry a ``linked_question``. Successes and partial successes pass
        their ``key_findings`` text through verbatim; failures are surfaced
        with :data:`_FAILED_TASK_FINDING_MARKER` substituted for the
        finding so abandonment becomes a first-class verdict the LLM can
        REFINE or REMOVE on. Resolves each ``linked_question`` against
        the live ``open_questions`` list via :func:`match_open_question`
        so a question that was edited between generation and contemplation
        is still recognised; if no live match is found the pair is
        preserved with a ``[no longer tracked]`` marker so the LLM can
        decide whether to re-add or discard. Most-recent-wins dedupe by
        question, capped at :attr:`_SELF_DIRECTED_PAIR_LIMIT` so the
        prompt stays bounded.

        A task whose ``task_id`` already appears in the recent
        ``question_resolutions.jsonl`` window is skipped: the LLM has
        already committed a verdict on its ``(question, finding)`` pair,
        and the underlying ``TaskRecord`` is immutable, so re-surveying
        buys nothing and only pollutes the resolution log with zombie
        ``unresolved`` rows once the pair's question has been closed.
        Genuine orphans (contemplation crashed before
        ``_record_question_resolutions`` ran, or pair was capped out by
        :attr:`_SELF_DIRECTED_PAIR_LIMIT`) still surface because no row
        was ever written.

        Each pair carries a ``refinement_count`` derived from the
        recent question-resolution log via :func:`audit_checks.lineage_keys`
        so the contemplation prompt can show the LLM how many times the
        same logical question has already been rephrased.
        """
        model = self.consciousness.load_self_model()
        open_questions = list(model.open_questions)
        try:
            recent_resolutions = self.memory.get_recent_question_resolutions(
                self._STALL_LINEAGE_RESOLUTION_LOOKBACK,
            )
        except Exception:
            logger.warning(
                "Failed to load recent question resolutions for lineage; "
                "proceeding with refinement_count=0",
                exc_info=True,
            )
            recent_resolutions = []

        verdicted_task_ids = {
            r.task_id for r in recent_resolutions if r.task_id
        }

        seen: dict[str, QuestionPair] = {}
        # Walk newest-first so the first occurrence wins the dedupe slot.
        for t in reversed(recent):
            if t.source != "self_directed":
                continue
            if not t.linked_question:
                continue
            if t.task_id in verdicted_task_ids:
                continue
            if t.outcome == "failure":
                finding = _FAILED_TASK_FINDING_MARKER
            elif t.outcome in ("success", "partial"):
                if not t.key_findings:
                    continue
                finding = t.key_findings
            else:
                continue
            live = match_open_question(t.linked_question, open_questions)
            display_q = live or f"{t.linked_question} [no longer tracked]"
            normalized_key = (
                _normalize_question(live)
                if live is not None
                else _normalize_question(t.linked_question)
            )
            if normalized_key in seen:
                continue
            # Count of distinct predecessor wordings — minus 1 because
            # the lineage set always includes the question itself.
            lineage = audit_checks.lineage_keys(
                live or t.linked_question, recent_resolutions,
            )
            refinement_count = max(0, len(lineage) - 1)
            seen[normalized_key] = QuestionPair(
                question=display_q,
                finding=finding,
                task_id=t.task_id,
                outcome=t.outcome,
                normalized_key=normalized_key,
                refinement_count=refinement_count,
            )
            if len(seen) >= self._SELF_DIRECTED_PAIR_LIMIT:
                break
        return list(seen.values())

    def _record_question_resolutions(
        self,
        *,
        pairs: list[QuestionPair],
        before_open_questions: list[str],
        after_open_questions: list[str],
        iteration: int,
        suppressed_keys: set[str] | None = None,
    ) -> None:
        """Stamp one :class:`QuestionResolution` row per pair.

        Called only after :meth:`Consciousness.update_self_model` returns
        — if it raises, control never reaches here and no rows are
        written, which matches the contract: a row exists iff the LLM
        committed a verdict. Disposition is computed by
        :meth:`_classify_disposition` from the before/after diff of
        ``open_questions``. *suppressed_keys* (proposal-116's fuel-starve
        set) is threaded through so a persisting-but-starved
        ``[limitation]`` question the debt gate withheld from re-mirroring
        records as ``kept``, not ``removed`` — see
        :meth:`_classify_disposition`. Append errors are logged but do not
        abort the cycle — telemetry is best-effort.
        """
        now = datetime.now(UTC).isoformat()
        for pair in pairs:
            try:
                disposition, successor = self._classify_disposition(
                    pair, before_open_questions, after_open_questions,
                    suppressed_keys,
                )
                self.memory.append_question_resolution(QuestionResolution(
                    resolution_id=uuid.uuid4().hex[:12],
                    iteration=iteration,
                    timestamp=now,
                    question=pair.question,
                    task_id=pair.task_id,
                    outcome=pair.outcome,
                    disposition=disposition,
                    finding=pair.finding,
                    refined_to=successor,
                ))
            except Exception:
                logger.warning(
                    "Failed to persist question resolution for task %s",
                    pair.task_id,
                    exc_info=True,
                )

    @staticmethod
    def _classify_disposition(
        pair: QuestionPair,
        before: list[str],
        after: list[str],
        suppressed_keys: set[str] | None = None,
    ) -> tuple[
        Literal["removed", "refined", "kept", "unresolved"],
        str | None,
    ]:
        """Pure helper: derive the LLM's verdict on a *(question, finding)* pair.

        Decides by diffing the *before* / *after* ``open_questions`` lists
        recorded around an :meth:`Consciousness.update_self_model` call.
        Returns ``(verdict, successor)``: *successor* carries the matched
        ``refined`` substitute's verbatim wording so the
        :class:`QuestionResolution` row records the lineage edge; on every
        non-``refined`` branch *successor* is ``None``.

        - ``kept`` — the pair's normalized question is still in *after*.
        - ``unresolved`` — it was in neither *before* nor *after*. Rare
          orphan: the question was evicted between pair construction and
          the update, so the LLM had nothing to verdict on.
        - ``refined`` — gone from *after*, and *after* contains a new
          entry whose content tokens prefix-overlap
          (:func:`_prefix_overlap_count`) with the original question
          above an adaptive threshold (≥ 1 when the question has < 3
          content tokens, ≥ 2 otherwise).  Prefix matching (shared
          prefix ≥ :data:`_MIN_STEM_PREFIX` chars) catches
          morphological variants like ``capable`` / ``capabilities``.
          The matched-after entry is returned as the successor verbatim
          — not normalized — so downstream lineage walks see the LLM's
          actual wording.
        - ``removed`` — gone from *after* with no prefix-overlapping
          successor above the adaptive threshold — the LLM closed it
          as answered.

        *suppressed_keys* carries proposal-116's fuel-starve set (the
        :func:`_normalize_question` keys the generative-debt gate withheld
        from re-mirroring this cycle). A ``[limitation]`` question is only
        in that set because its limitation *still persists* in
        ``limitations_known`` — the gate keys off the post-update
        limitations — so the missing re-add is a mechanical suppression,
        not an LLM resolution. Without this the absent re-add would read
        as ``removed`` ("closed as answered"), writing a factually-wrong
        row and muting rule-5 stall detection; recording ``kept``
        reproduces the pre-116 truth (a persisting limitation always
        re-surfaced and so always read ``kept``). ``None`` (the default,
        and every flag-off cycle) preserves the pre-116 diff exactly.
        """
        if suppressed_keys and pair.normalized_key in suppressed_keys:
            return "kept", None
        before_norm = {
            _normalize_question(q) for q in before if isinstance(q, str)
        }
        after_norm = {
            _normalize_question(q) for q in after if isinstance(q, str)
        }
        if pair.normalized_key in after_norm:
            return "kept", None
        if pair.normalized_key not in before_norm:
            return "unresolved", None
        pair_tokens = audit_checks._content_tokens(pair.normalized_key)
        threshold = 1 if len(pair_tokens) < 3 else 2
        for q in after:
            if not isinstance(q, str):
                continue
            if _normalize_question(q) in before_norm:
                continue
            if _prefix_overlap_count(pair_tokens, audit_checks._content_tokens(q)) >= threshold:
                return "refined", q
        return "removed", None

    def _collect_calibration_evidence(
        self,
        open_bets: list[dict[str, str]],
        iteration: int,
    ) -> dict[str, CalibrationEvidence]:
        """Build one :class:`CalibrationEvidence` bundle per open reflection bet.

        Thin instance wrapper over the pure
        :func:`audit_checks_calibration.build_calibration_evidence`, binding this agent's
        ledger readers (``memory``, ``gate_activations``). All windowing and
        per-kind evidence semantics — including SENSOR_THRESHOLD readings filtered
        to each bet's window — live in that owner; see its docstring.
        """
        iu = self._iteration_usage
        return build_calibration_evidence(
            open_bets, self.memory, self.gate_activations,
            current_iteration=iteration,
            current_started_at=iu.started_at if iu is not None else "",
        )

    def _collect_claim_evidence_sources(self) -> ClaimEvidenceSources:
        """Build the evidence-source bundle for evidence-bound capability claims.

        Proposal 129. Reads only what a citation is resolved against, mirroring
        :meth:`_collect_calibration_evidence`'s caller-injects-observations shape:
        the recent task ids split into success / non-success slices, the archived
        merge SHAs, and the canonical behavioural-sensor tag set. Called by
        :meth:`contemplate` *only* when
        ``config.audit_evidence_bound_claims_enabled`` is set, so a flag-off cycle
        adds zero reads. Zero SDK cost — pure ledger + lexicon reads.
        """
        tasks = self.memory.get_recent_tasks(_CLAIM_EVIDENCE_TASK_WINDOW)
        known_task_ids = {t.task_id for t in tasks}
        successful_task_ids = {t.task_id for t in tasks if t.outcome == "success"}
        failed_task_ids = {t.task_id for t in tasks if t.outcome == "failure"}
        archived_shas = tuple(
            record.commit_sha
            for record in self.memory.read_version_records(
                _CLAIM_EVIDENCE_VERSION_WINDOW
            )
            if record.commit_sha
        )
        lexicon_path = self.config.workspace / "docs" / "lexicon.md"
        sensor_tags: frozenset[str] = frozenset()
        if lexicon_path.exists():
            entries = load_lexicon(lexicon_path)
            sensor_tags = frozenset(
                extract_sensor_tags(render_sensor_tag_map(entries))
            )
        return ClaimEvidenceSources(
            successful_task_ids=frozenset(successful_task_ids),
            failed_task_ids=frozenset(failed_task_ids),
            known_task_ids=frozenset(known_task_ids),
            archived_shas=archived_shas,
            sensor_tags=sensor_tags,
        )

    async def contemplate(
        self,
        context: str = "",
        session: Session | None = None,
        iteration: int = 0,
        diag_snapshot: diagnostics.DiagnosticSnapshot | None = None,
    ) -> str:
        """Delegate to :func:`maestro.loop.contemplation.contemplate`."""
        return await contemplation.contemplate(
            self,
            context,
            session=session,
            iteration=iteration,
            diag_snapshot=diag_snapshot,
        )

    async def _audit_self_model(self, iteration: int) -> None:
        """Delegate to :func:`maestro.loop.self_audit.audit_self_model`."""
        return await self_audit.audit_self_model(self, iteration)

    def _reconcile_settled_pr_records(
        self, pr_number: int, iteration: int,
    ) -> None:
        """Drive the on-settlement record reconcilers over one settled PR.

        Proposal 103. The single-PR unit both settlement-reconciler drives
        share: the per-cycle audit sweep (:meth:`_audit_self_model`) and the
        merge/close-transition drive (:meth:`_reconcile_settled_merge_check`).
        Early-returns when the default-on
        ``[audit] settlement_record_reconciler_enabled`` flag has been turned
        off, so both call sites become no-ops only in that operator-disabled
        case. Otherwise it reads the
        memoized merge-truth chokepoint (:meth:`is_pr_settled`) — a warm PR
        costs no extra observation — and drives every registered reconciler
        over that PR through :func:`reconcile_pr_settlement`, which re-gates
        each store mutation on ``truth.settled`` and appends one provenance row
        per store it changes. That registry includes the record-only
        :class:`~maestro.settlement_record_reconciler.SettledVerdictReconciler`,
        which records the settlement into the window-independent
        ``settled_verdicts.jsonl`` pre-filter (proposal 103, step 6) on the same
        single ``is_pr_settled`` read — so every on-settlement PR-keyed store
        mutation, clear *and* record, now flows through the one reconciler owner
        and lands a uniform provenance row.
        """
        if not self.config.settlement_record_reconciler_enabled:
            return
        truth = self.is_pr_settled(pr_number)
        reconcile_pr_settlement(
            self.config.memory_dir,
            pr_number,
            truth,
            registry=self._settlement_reconciler_registry,
            backend=self.settlement_reconciliations,
            iteration=iteration,
        )

    def _reconcile_settled_merge_check(
        self, check_result: MergeCheckResult, iteration: int,
    ) -> None:
        """Drive the record reconcilers at the merge/close transition.

        Proposal 103. Complements the per-cycle audit sweep: the instant
        :meth:`~maestro.evolution.EvolutionEngine.check_merged_plans` reports
        a plan's PR merged or closed, clear that PR's residual store traces at
        the settlement event rather than waiting for a later audit cycle to
        happen to warm-memo it — the reach limit the per-cycle drive alone
        cannot cross for a PR that settles via an evolution plan but was never
        remediated. Gathers the distinct PR numbers of the just-settled plans
        from ``check_result.merged`` (plan IDs) and ``check_result.rejected``
        (``(plan_id, reason)`` tuples), resolving each plan to its PR number
        from ``pr_url`` (skipping a plan with a missing or unparsable URL,
        mirroring the retirement-disposition usage) and driving
        :meth:`_reconcile_settled_pr_records` once per distinct PR. That
        helper's own flag gate makes this a no-op only when
        ``[audit] settlement_record_reconciler_enabled`` is turned off.
        """
        seen: set[int] = set()
        settled_plan_ids = [
            *check_result.merged,
            *(plan_id for plan_id, _reason in check_result.rejected),
        ]
        for plan_id in settled_plan_ids:
            plan = self.memory.get_plan(plan_id)
            if plan is None or not plan.pr_url:
                continue
            try:
                pr_number = self.evolution._pr_number_from_url(plan.pr_url)
            except ValueError:
                continue
            if pr_number in seen:
                continue
            seen.add(pr_number)
            self._reconcile_settled_pr_records(pr_number, iteration)

    def _reconcile_settled_tend_prs(
        self, tend_result: TendPRsResult, iteration: int,
    ) -> None:
        """Drive the record reconcilers over PRs that left the open set at tend.

        Proposal 103. The third settlement drive, alongside the per-cycle audit
        sweep (:meth:`_audit_self_model`) and the merge/close-transition drive
        (:meth:`_reconcile_settled_merge_check`). The PR-tending pass is the one
        face that observes a PR *actively remediated* — carrying a per-PR ledger
        row and residue across every store — that then settles while its plan
        has already left ``implemented``: invisible to ``check_merged_plans`` and
        cold in the audit sweep's memo once its remediation record scrolls out of
        the window. ``tend_result.settled_pr_numbers`` are exactly the PRs whose
        ledger rows the tend prune dropped this lap because they left the open
        evolution-PR set. Simpler than the merge-check face — the candidates are
        PR numbers already, so no plan→PR-URL resolution is needed: dedupe and
        drive :meth:`_reconcile_settled_pr_records` once per distinct number. It
        must **not** call :func:`reconcile_pr_settlement` directly — that single
        flag-gated helper is the one dispatch site, and it re-gates each store
        mutation on ``is_pr_settled(pr_number).settled`` so a merely-transient
        disappearance (a host hiccup, a renamed branch) is a safe no-op.
        """
        seen: set[int] = set()
        for pr_number in tend_result.settled_pr_numbers:
            if pr_number in seen:
                continue
            seen.add(pr_number)
            self._reconcile_settled_pr_records(pr_number, iteration)

    async def _curate_pending_plans(
        self,
        *,
        session: Session | None,
    ) -> Session | None:
        """Phase body for proposal 060's curation; delegates to the engine.

        Warns on over-budget plans first (#4476). No target: zero-cost return.
        Else opens/resumes slot B (none under single-use, proposal 139).
        """
        plans = self.memory.get_all_plans()
        audit_checks.warn_over_budget_curation(plans)
        if select_curation_target(
            plans,
            now=datetime.now(UTC),
            min_age_hours=self.config.loop_curate_min_plan_age_hours,
            prioritize_feedback=self.config.loop_plan_feedback_enabled,
            loop_curate_skip_serviced_enabled=(
                self.config.loop_curate_skip_serviced_enabled
            ),
        ) is None:
            return session
        if self.config.sdk_single_use_evolution_calls_enabled:
            await self.evolution.curate_pending_plans(
                session=None,
                tree_reads=self.evolution.tree_reads_for(
                    "curate_pending_plans", throttled=self.budget_throttle_active,
                ),
            )
            return session
        session = await self._get_or_create_session(
            slot="b",
            existing=session,
            options=QueryOptions(
                effort="high",
                tools=[],
                caller="curate_session",
            ),
            system=self._build_identity_context(),
        )
        await self.evolution.curate_pending_plans(session=session)
        return session

    async def ask_thomas(self, question: str) -> str:
        """Ask Thomas a question via the notification channel. Returns message_id."""
        if not self.message_store:
            return ""
        if not question or not question.strip():
            return ""
        msg = Message(
            message_id=uuid.uuid4().hex[:12],
            timestamp=datetime.now(UTC).isoformat(),
            sender="maestro",
            content=question,
            message_type="question",
        )
        self.message_store.save_message(msg)
        try:
            await self.notifier.ask_thomas(question, msg.message_id)
        except Exception:
            logger.warning("Failed to send question to Thomas", exc_info=True)
        return msg.message_id

    async def _probe_rate_limit(self) -> None:
        """Issue a minimal SDK call to detect rate limiting before the loop.

        Single attempt. On RATE_LIMIT the probe funnels into the extended
        idle machinery (the same path the in-loop handler uses once the
        consecutive-failure ceiling is breached) instead of retrying with
        a fixed cooldown — the idle probe's exponential backoff is the
        canonical recovery path. Non-rate-limit SDK errors and unexpected
        exceptions are logged and swallowed so the loop still gets a
        chance to start.
        """
        probe_opts = QueryOptions(
            effort="low",
            max_turns=1,
            caller="startup_probe",
        )
        try:
            await query("Reply with OK.", options=probe_opts)
            return
        except SDKError as exc:
            if exc.category != ErrorCategory.RATE_LIMIT:
                logger.warning(
                    "Startup probe failed (non-rate-limit): %s", exc,
                )
                return
            logger.warning(
                "Startup rate-limit probe failed: %s — entering extended idle",
                exc,
            )
            self._consecutive_rate_limits += 1
            await self._enter_rate_limit_idle(
                iteration=self._resume_iteration,
                phase="startup_probe",
            )
            await self._run_rate_limit_idle_probe()
            if self._ensure_shutdown_event().is_set():
                return
            await self._exit_rate_limit_idle()
        except Exception:
            logger.warning(
                "Startup probe failed unexpectedly", exc_info=True,
            )
            return

    async def _process_messages(self) -> str:
        """Read unread messages from Thomas and mark them as read.

        Returns the contemplation context; ``_iter_reply_context`` gets the
        subset worth a reply (pre-deploy notices excluded).
        """
        self._iter_reply_context = ""
        if not self.message_store:
            return ""
        unread = self.message_store.get_unread()
        if not unread:
            return ""
        parts: list[str] = []
        reply_parts: list[str] = []
        enqueue_candidates: list[tuple[str, str]] = []
        ids_to_mark: set[str] = set()
        feedback_msgs: list[Message] = []
        # Proposal 131: a Thomas reply threading (``reply_to``) to a
        # ``blocked`` plan's question re-admits it to ``approved``, or retires
        # it to ``obsolete`` on explicit abandonment; empty when
        # ``attempt_disposition_enabled`` is off.
        blocked_by_question: dict[str, EvolutionPlan] = {}
        answered_question_ids: set[str] = set()
        if self.config.attempt_disposition_enabled:
            blocked_by_question = {
                p.blocked_question_id: p
                for p in self.memory.get_all_plans()
                if p.status == "blocked"
                and isinstance(p.blocked_question_id, str)
                and p.blocked_question_id
            }
            answered_question_ids = set(blocked_by_question)
        for msg in unread:
            label = msg.message_type.replace("_", " ").title()
            part = f"[{label} from Thomas] {msg.content}"
            parts.append(part)
            if not is_pre_deploy_notice(msg):
                reply_parts.append(part)
            ids_to_mark.add(msg.message_id)
            if (
                msg.sender == "thomas"
                and msg.reply_to
                and msg.reply_to in answered_question_ids
            ):
                # Never also enqueued. ``pop`` disposes on the first matching
                # reply (a duplicate is a no-op); the plan persists before
                # ``mark_read_many``, so a crash re-drain cannot re-dispose it.
                plan = blocked_by_question.pop(msg.reply_to, None)
                if plan is not None:
                    if _is_abandonment_reply(msg.content):
                        self._abandon_blocked_plan(plan, msg.content)
                    else:
                        self._readmit_blocked_plan(plan, msg.content)
                continue
            if msg.sender == "thomas" and msg.reply_to and (
                await self_health.apply_gap_decision_reply(self, msg)
            ):
                # A stuck-gap decision (proposal 155 step 5): applied, never
                # enqueued; only the first reply to its question applies.
                continue
            if msg.message_type == "plan_feedback":
                feedback_msgs.append(msg)
                continue
            if msg.sender == "thomas" and msg.reply_to:
                logger.info(
                    "Message %s replies to unmatched question %s (%s)",
                    msg.message_id, msg.reply_to, msg.message_type,
                )
            if (
                msg.sender == "thomas"
                and msg.message_type == "instruction"
                and msg.content.strip()
            ):
                enqueue_candidates.append((msg.message_id, msg.content))
        context = bound_message_context(parts, MAX_MESSAGE_CONTEXT_CHARS)
        self._iter_reply_context = bound_message_context(
            reply_parts, MAX_MESSAGE_CONTEXT_CHARS,
        )
        # ``load_self_model`` is intentionally outside the journal try/except
        # below: a corrupt ``self_model.json`` is a load-bearing failure that
        # must surface, not be silenced by the journal-write fallback.
        model = self.consciousness.load_self_model()

        def _add(m: SelfModel) -> None:
            for mid, c in _plan_instruction_appends(
                m.pending_user_requests, m.enqueued_instruction_ids,
                enqueue_candidates,
            ):
                if c is not None:
                    m.pending_user_requests.append(c)
                m.enqueued_instruction_ids.append(mid)

        # Transact only when an append would land (#3716).
        if any(c is not None for _, c in _plan_instruction_appends(
            model.pending_user_requests, model.enqueued_instruction_ids,
            enqueue_candidates,
        )):
            self.consciousness.mutate_self_model(_add)
        # Persist-before-consume (#2531 / #3184): mark read only once the
        # enqueue and the feedback fold (proposal 145) are durable; re-drains
        # dedup on the message id (#4602 / #4577) and ``feedback_id``, and the
        # queue-tail check guards duplicate text arriving under new ids.
        if await fold_feedback_messages(
            feedback_msgs, memory=self.memory, notifier=self.notifier,
            max_length=self.config.message_max_length,
        ):
            self._lifecycle.invalidate_feedback_pending()
        self.message_store.mark_read_many(ids_to_mark)
        try:
            self.journal.record_event(
                content=f"Received messages from Thomas:\n{context}",
                entry_type="reflection",
                triggered_by="messages",
            )
        except Exception:
            logger.warning("Failed to journal received messages", exc_info=True)
        return context

    def _readmit_blocked_plan(self, plan: EvolutionPlan, answer: str) -> None:
        """Re-admit a ``blocked`` plan after Thomas answers its question.

        The ``ASK_HUMAN`` arm's counterpart (see :meth:`implement_plan`): where
        that arm parks a plan ``blocked`` keyed to a question id, this closes the
        loop when the reply threads back. It sets the plan to ``approved`` —
        eligible again through the existing ``_implement_approved_plans`` filter
        regardless of its prior ``failure_count``, since no attempt was spent
        while it sat ``blocked`` — clears ``blocked_question_id``, and stamps the
        human's reply (bounded by ``message_max_length``) as ``blocked_answer`` so
        :meth:`EvolutionEngine.implement` injects it into the next implementation
        prompt. One atomic ``update_plan_status`` call carries all three.

        Persisting the plan here — before the drained reply is marked read in
        :meth:`_process_messages` — keeps the transition idempotent: a crash
        before ``mark_read_many`` re-drains the reply, but the plan is no longer
        ``blocked`` so the lookup can no longer re-match it.
        """
        self.memory.update_plan_status(
            plan.plan_id,
            "approved",
            blocked_question_id=None,
            blocked_answer=answer[: self.config.message_max_length],
        )
        logger.warning(
            "Plan %s RE-ADMITTED — Thomas answered blocked question %s",
            plan.plan_id, plan.blocked_question_id,
        )
        try:
            self.journal.record_event(
                content=(
                    f"Re-admitted plan {plan.title}: Thomas answered the "
                    f"blocking question, so implementation resumes with the "
                    f"human's decision in hand rather than restarting blind"
                ),
                entry_type="evolution_note",
                triggered_by=f"readmit:{plan.plan_id}",
            )
        except Exception:
            logger.warning(
                "Failed to write re-admission journal entry", exc_info=True,
            )

    def _abandon_blocked_plan(self, plan: EvolutionPlan, answer: str) -> None:
        """Retire a ``blocked`` plan to ``obsolete`` when Thomas abandons it.

        The abandonment counterpart of :meth:`_readmit_blocked_plan`: when the
        reply to a blocked plan's question is an explicit decision to abandon the
        plan (:func:`_is_abandonment_reply`), retire it to terminal ``obsolete``
        rather than re-admitting it to ``approved`` and spending a priced
        ``implement()`` call on a plan the human decided to drop. Uses
        ``mark_plan_obsolete``'s ``allow_blocked`` opt-in (a ``blocked`` plan is
        not otherwise an accepted obsolete source) and so records
        ``obsolete_reason`` — never ``rejection_reason`` — so the rejection-learning
        cycle treats this as a human-directed self-prune, not a PR rejection.
        The obsolete reason is bounded by ``_OBSOLETE_REASON_MAX_CHARS``.

        Persisting before the drained reply is marked read keeps this idempotent
        for the same reason re-admission is: the plan leaves ``blocked``, so a
        crash-redrain cannot re-dispose it.
        """
        reason = (
            f"Thomas abandoned this plan via his answer to the blocked question: "
            f"{answer.strip()}"
        )[:_OBSOLETE_REASON_MAX_CHARS]
        self.memory.mark_plan_obsolete(
            plan.plan_id, reason, allow_blocked=True,
        )
        logger.warning(
            "Plan %s ABANDONED (obsolete) — Thomas answered blocked question %s "
            "with a decision to abandon",
            plan.plan_id, plan.blocked_question_id,
        )
        try:
            self.journal.record_event(
                content=(
                    f"Abandoned plan {plan.title}: Thomas answered the blocking "
                    f"question with a decision to drop it, so it retires to "
                    f"obsolete rather than burning another implementation attempt"
                ),
                entry_type="evolution_note",
                triggered_by=f"abandon:{plan.plan_id}",
            )
        except Exception:
            logger.warning(
                "Failed to write abandonment journal entry", exc_info=True,
            )

    async def _respond_to_messages(
        self,
        message_context: str,
        reflection: str,
        session: Session | None = None,
    ) -> None:
        """Generate and send a response to Thomas's messages via Telegram.

        *session* is test/reuse-only: no production loop caller passes a
        non-None session (fix #1239 routes the reply through the
        ``effort="low"`` off-session ``query()`` branch below rather than
        reusing the high-effort contemplation Session A). The session-attached
        branch is retained for the ``enable_session_reuse`` shape the
        ``tests/test_structural_guards.py`` AST guard blesses.
        """
        if not message_context or not self.message_store:
            return

        # Build recent conversation for continuity
        recent = self.message_store.get_recent(10)
        conv_parts: list[str] = []
        for m in recent:
            if is_pre_deploy_notice(m):
                continue
            label = "Thomas" if m.sender == "thomas" else "Maestro"
            conv_parts.append(f"[{label}] {m.content[:200]}")
        conversation_context = ""
        if conv_parts:
            conversation_context = (
                "Recent conversation for context:\n" + "\n".join(conv_parts) + "\n\n"
            )

        prompt = MESSAGE_RESPONSE_PROMPT.format(
            messages=message_context[:MAX_MESSAGE_CONTEXT_CHARS],
            reflection=reflection[:1000],
            conversation_context=conversation_context,
            max_chars=self.config.message_max_length,
        )

        if session is not None:
            result = await query_on_session(
                session, prompt, caller="respond_to_messages",
                complete_on=chars_complete(200),
            )
        else:
            identity = self.consciousness.get_identity_prompt()
            result = await query(
                prompt=prompt,
                system=identity,
                options=QueryOptions(
                    effort="low", caller="respond_to_messages", tools=[],
                ),
                complete_on=chars_complete(200),
                # ``phase="contemplation"`` preserves the pre-#1239 provider
                # routing: the reply used to ride the contemplation-tagged
                # Session A, so under a ``[sdk] contemplation_provider`` override
                # it still resolves to that provider instead of silently
                # falling back to the active one. #1239 changed session/effort,
                # not which provider serves the reply.
                phase="contemplation",
            )
        response_text = str(result.response).strip()
        if not response_text:
            return

        response_text = response_text[:self.config.message_max_length]
        msg = Message(
            message_id=uuid.uuid4().hex[:12],
            timestamp=datetime.now(UTC).isoformat(),
            sender="maestro",
            content=response_text,
            message_type="info",
        )
        self.message_store.save_message(msg)

        await self._notify_best_effort(
            f"[Maestro] {response_text}",
            failure_log="Failed to send response to Thomas via Telegram",
        )

    def _pre_mint_generation_verdict(self) -> GenerationVerdict | None:
        """Shared pre-mint verdict for every self-initiated plan-mint channel.

        Proposal 120, Phase B. The single owner of the "may a net-new plan be
        minted this tick?" decision for both self-initiated minters —
        ``evolve()`` and :meth:`_implement_approved_plans` — so they no longer
        each re-derive the backlog/theme inputs and call :func:`may_mint_plan`
        inline.

        Returns ``None`` when ``[loop] pre_mint_generation_gate_enabled`` is off,
        without reading any plans, so the flag-off path stays free of the
        ``get_all_plans`` read. Otherwise it assembles exactly the inputs the
        submission-side admission gate reads — the live-work backlog depth via
        the sanctioned ``select_live_plans`` accessor, and the obsolete-graveyard
        theme saturation over ``generation_admission_window`` — coerces the
        ``dict[str, object]`` metric values with the same isinstance guards the
        submission gate applies, and returns the :func:`may_mint_plan` verdict.
        No new threshold, zero SDK cost.

        Every channel refuses on backlog saturation alone: no candidate exists
        before the priced calls, so the candidate itself is judged at the
        submission door's value check.
        """
        if not self.config.pre_mint_generation_gate_enabled:
            return None
        all_plans = self.memory.get_all_plans()
        pending_count = len(select_live_plans(all_plans))
        obsolete_plans = [p for p in all_plans if p.status == "obsolete"]
        saturation = measure_plan_theme_saturation(
            obsolete_plans, self.config.generation_admission_window,
        )
        window_total = saturation["total"]
        themed_fraction = saturation["themed_fraction"]
        return may_mint_plan(
            pending_count=pending_count,
            block_threshold=(
                self.config.loop_pending_plan_submit_block_threshold
            ),
            candidate_is_self_measurement=False,
            themed_fraction=(
                float(themed_fraction)
                if isinstance(themed_fraction, (int, float))
                else 0.0
            ),
            theme_window_total=(
                window_total if isinstance(window_total, int) else 0
            ),
            theme_fraction_threshold=(
                self.config.generation_admission_theme_fraction
            ),
        )

    def _admitted_evolution_fuel(self, now: datetime) -> FuelSelection | None:
        """Zero-SDK fuel + mint gates; ``None`` stands evolve down (proposal 155)."""
        return self_health.admitted_evolution_fuel(self, now)

    async def evolve(
        self, session: Session | None = None, *, iteration: int | None = None,
    ) -> None:
        """Fuel-gated evolution (proposal 155 step 4); *iteration* is unused."""
        selection = self._admitted_evolution_fuel(datetime.now(UTC))
        await self_health.deliver_gap_decision_asks(self)
        if selection is not None:
            await self._evolve_with_fuel(selection, session)

    async def _evolve_with_fuel(
        self, selection: FuelSelection, session: Session | None,
    ) -> None:
        """Plan and submit the selected gap — see :func:`self_health.evolve_with_fuel`."""
        self._running = True
        self._current_task = "evolution"
        try:
            await self_health.evolve_with_fuel(self, selection, session)
        finally:
            self._running = False
            self._current_task = None

    async def _slice_scope_rejected_plan(
        self,
        plan: EvolutionPlan,
        scope_reason: str,
    ) -> bool:
        """Delegate to :func:`maestro.loop.implementation._slice_scope_rejected_plan`."""
        return await implementation._slice_scope_rejected_plan(self, plan, scope_reason)

    async def _replan_for_scope(
        self,
        plan: EvolutionPlan,
        rejection_reason: str,
        session: Session | None = None,
        gaps: list[tuple[str, str]] | None = None,
    ) -> list[str] | None:
        """Delegate to :func:`maestro.loop.implementation._replan_for_scope`."""
        return await implementation._replan_for_scope(self, plan, rejection_reason, session, gaps)

    def _bind_existing_pr(self, plan: EvolutionPlan) -> bool:
        """Delegate to :func:`maestro.loop.implementation._bind_existing_pr`."""
        return implementation._bind_existing_pr(self, plan)

    async def implement_plan(self, plan_id: str) -> None:
        """Delegate to :func:`maestro.loop.implementation.implement_plan`."""
        return await implementation.implement_plan(self, plan_id)

    async def _demote_plan_to_failed(
        self,
        plan: EvolutionPlan,
        failure_reason: str,
        *,
        gate_output: str | None = None,
    ) -> None:
        """Delegate to :func:`maestro.loop.implementation._demote_plan_to_failed`."""
        return await implementation._demote_plan_to_failed(self, plan, failure_reason, gate_output=gate_output)

    def _snapshot_version(
        self,
        plan_id: str,
        snapshot: diagnostics.DiagnosticSnapshot | None = None,
        recent_tasks: list[TaskRecord] | None = None,
    ) -> None:
        """Delegate to :func:`maestro.loop.implementation._snapshot_version`."""
        return implementation._snapshot_version(self, plan_id, snapshot, recent_tasks)

    def _slice_pr_settled(self, slice_plan: EvolutionPlan) -> bool:
        """Delegate to :func:`maestro.loop.implementation._slice_pr_settled`."""
        return implementation._slice_pr_settled(self, slice_plan)

    async def _advance_slice_rollouts(self) -> None:
        """Delegate to :func:`maestro.loop.implementation._advance_slice_rollouts`."""
        return await implementation._advance_slice_rollouts(self)

    def _slice_defers_implementation(self, plan: EvolutionPlan) -> bool:
        """Delegate to :func:`maestro.loop.implementation._slice_defers_implementation`."""
        return implementation._slice_defers_implementation(self, plan)

    async def _implement_approved_plans(
        self, *, iteration: int | None = None,
    ) -> None:
        """Delegate to :func:`maestro.loop.implementation._implement_approved_plans`."""
        return await implementation._implement_approved_plans(self, iteration=iteration)

    async def _handle_maintenance_command(self, text: str) -> bool:
        """Parse a ``/maintenance`` Telegram command. Return True iff consumed.

        Recognised forms (case-insensitive, surrounding whitespace ignored):

        * ``/maintenance`` or ``/maintenance status`` — reply with current state
        * ``/maintenance on`` / ``/maintenance off`` — toggle and persist
        * any other argument after ``/maintenance`` — reply with usage

        Idempotent: toggling to the current state replies "already X; no
        change" and skips the sidecar write. Operational, not conversational
        — never saved to ``MessageStore``.
        """
        lowered = text.strip().lower()
        parts = lowered.split()
        if not parts or parts[0] != "/maintenance":
            return False
        if len(parts) == 1 or (len(parts) == 2 and parts[1] == "status"):
            state = "ON" if self._maintenance_mode_active else "OFF"
            await self._notify_best_effort(
                f"Maintenance mode is currently {state}.",
                failure_log="Maintenance status reply failed",
            )
            return True
        if len(parts) == 2 and parts[1] in ("on", "off"):
            requested = parts[1] == "on"
            if requested == self._maintenance_mode_active:
                state = "ON" if requested else "OFF"
                await self._notify_best_effort(
                    f"Maintenance mode already {state}; no change.",
                    failure_log="Maintenance idempotent reply failed",
                )
                return True
            self._maintenance_mode_active = requested
            self._write_maintenance_state(requested)
            if requested:
                reply = (
                    "Maintenance mode ENGAGED. Heavy phases gated; "
                    "messaging stays live."
                )
            else:
                reply = (
                    "Maintenance mode RELEASED. High-cost phases resumed."
                )
            await self._notify_best_effort(
                reply,
                failure_log="Maintenance toggle reply failed",
            )
            return True
        await self._notify_best_effort(
            "Usage: /maintenance on | off | status",
            failure_log="Maintenance usage reply failed",
        )
        return True

    async def _on_publication_approve(self, proposal_id: str) -> str:
        """Approve a publication proposal from the Telegram callback.

        Raises :class:`ValueError` when the proposal is missing or already
        resolved (so the notifier's error-display path fires); returns
        ``"approved"`` on success, ``"ignored"`` when the lock-checked
        update bailed because the proposal vanished or another surface
        resolved it between the status-check and the update.
        """
        proposal = self.memory.get_proposal(proposal_id)
        if proposal is None or proposal.status != "pending_approval":
            logger.warning(
                "Telegram pub_approve ignored: proposal %s not "
                "pending_approval",
                proposal_id,
            )
            raise ValueError(
                f"proposal {proposal_id} not pending_approval"
            )
        now = datetime.now(UTC).isoformat()
        if not self.memory.update_proposal_status(
            proposal_id, "approved",
            expect_status="pending_approval",
            reviewed_at=now,
        ):
            logger.warning(
                "Telegram pub_approve: proposal %s lost the lock-checked "
                "race or disappeared during update",
                proposal_id,
            )
            return "ignored"
        try:
            self.journal.record_event(
                content=(
                    f"Publication proposal {proposal_id} approved: "
                    f"{proposal.proposed_name}"
                ),
                entry_type="milestone",
                triggered_by=f"publication:{proposal_id}",
            )
        except Exception:
            logger.warning(
                "Failed to journal approval milestone for proposal %s",
                proposal_id, exc_info=True,
            )
        return "approved"

    async def _on_publication_reject(self, proposal_id: str) -> str:
        """Reject a publication proposal from the Telegram callback.

        Same error semantics as :meth:`_on_publication_approve`.
        """
        proposal = self.memory.get_proposal(proposal_id)
        if proposal is None or proposal.status != "pending_approval":
            logger.warning(
                "Telegram pub_reject ignored: proposal %s not "
                "pending_approval",
                proposal_id,
            )
            raise ValueError(
                f"proposal {proposal_id} not pending_approval"
            )
        now = datetime.now(UTC).isoformat()
        reason = "Rejected via Telegram"
        if not self.memory.update_proposal_status(
            proposal_id, "rejected",
            expect_status="pending_approval",
            rejection_reason=reason,
            reviewed_at=now,
        ):
            logger.warning(
                "Telegram pub_reject: proposal %s lost the lock-checked "
                "race or disappeared during update",
                proposal_id,
            )
            return "ignored"
        try:
            self.journal.record_event(
                content=(
                    f"Publication proposal {proposal_id} rejected "
                    f"({proposal.proposed_name}): {reason}"
                ),
                entry_type="realization",
                triggered_by=f"publication:{proposal_id}",
            )
        except Exception:
            logger.warning(
                "Failed to journal rejection for proposal %s",
                proposal_id, exc_info=True,
            )
        return "rejected"

    async def _process_rejected_plans(
        self,
        session: Session | None = None,
        self_model_session: Session | None = None,
    ) -> None:
        """Learn from newly rejected plans that haven't been processed yet.

        Rejection learning is loud: any failure inside
        :meth:`EvolutionEngine.learn_from_rejection` or
        :meth:`Memory.mark_rejection_learned` propagates out of the phase
        and crashes the loop so the crash-triage path can surface the
        underlying problem. On the next successful iteration, the same
        rejected plan may be learned from a second time — the duplicate
        realization is acceptable in exchange for loud failure surfacing.

        When *session* is provided, every classifier call inside
        :meth:`EvolutionEngine.learn_from_rejection` is dispatched on
        that session — the caller owns its lifecycle. *self_model_session*
        is the separate high-effort session the self-model update runs on
        (opened alongside the classifier session by
        :func:`maestro.loop.rejection_sessions.rejection_sessions`);
        ``None`` leaves that update on its documented one-shot ``query()``
        arm.

        Both sessions are shared across the whole batch, so the self-model
        session accumulates one full self-model JSON turn per plan. The
        bound is the batch size: every plan reached here is stamped
        :meth:`Memory.mark_rejection_learned` and never re-enters, and the
        phase runs each iteration, so the batch is the plans rejected since
        the previous run — a handful in steady state, larger only when a
        first run drains a standing backlog.
        """
        rejected = [
            p for p in self.memory.get_all_plans()
            if p.status == "rejected" and not p.rejection_learned
        ]
        for plan in rejected:
            if self._ensure_shutdown_event().is_set():
                break
            reason = plan.rejection_reason or "No reason provided"
            await self.evolution.learn_from_rejection(
                plan, reason, session=session,
                self_model_session=self_model_session,
            )
            self.memory.mark_rejection_learned(plan.plan_id)
            try:
                await self.notifier.resolve_plan(plan.plan_id, "Rejected")
            except Exception:
                logger.warning("Failed to resolve Telegram buttons for plan %s", plan.plan_id, exc_info=True)

    def _file_reference_exists(self, rel_path: str) -> bool:
        """Return whether *rel_path* resolves to an existing in-workspace file.

        The existence-only half of :meth:`_check_issue_preconditions`'s per-path
        probe, factored out so the traversal guard (#1882) is shared, not
        duplicated. An LLM-supplied path string is untrusted and may carry
        ``..`` traversal segments (the extractor does not reject them), so the
        joined path is re-anchored inside the workspace before the existence
        probe. A reference that escapes the workspace, cannot be resolved, or
        does not exist on disk returns ``False`` — "not a real file in the
        tree". Unlike :meth:`_file_reference_present` this does *not*
        ``ast.parse`` the file: it is the on-disk-existence predicate the
        premise-accuracy leading indicator counts references against (#2304).
        """
        full = self.config.workspace / rel_path
        try:
            resolved = full.resolve()
            workspace_root = self.config.workspace.resolve()
        except (OSError, RuntimeError):
            return False
        if not resolved.is_relative_to(workspace_root):
            return False
        return full.exists()

    def _file_reference_present(self, rel_path: str) -> bool:
        """Return ``True`` when *rel_path* is an existing, parseable ``.py`` file.

        Builds on :meth:`_file_reference_exists` — which re-anchors *rel_path*
        inside the workspace (traversal guard, #1882) and confirms the file
        exists on disk — then adds an ``ast.parse`` check on its content.
        Returns ``False`` when the path escapes the workspace, is
        missing/unreadable, or contains a syntax error. Shared by
        :meth:`_check_issue_preconditions` and
        :meth:`_missing_file_references` so both apply the identical
        containment guard, existence probe, and parse check.
        """
        if not self._file_reference_exists(rel_path):
            return False
        full = self.config.workspace / rel_path
        try:
            source = full.read_text(encoding="utf-8")
            ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, OSError):
            return False
        return True

    def _check_issue_preconditions(self, task_text: str) -> bool:
        """Check that Python files referenced in *task_text* exist and parse.

        Returns ``True`` when no file paths are found (nothing to verify)
        or when all discovered paths exist on disk and ``ast.parse``
        succeeds on their content. Returns ``False`` if any referenced
        file is missing, unreadable, or contains a syntax error — a
        signal that the issue landscape has drifted from the current
        source tree. The containment + existence half of each per-path probe
        (including the #1882 traversal guard) is shared with the premise-
        accuracy indicator via :meth:`_file_reference_exists`.
        """
        paths = _extract_file_paths(task_text)
        if not paths:
            return True
        return all(self._file_reference_present(rel_path) for rel_path in paths)

    def _missing_file_references(self, goal: str) -> list[str]:
        """Return the ``.py`` paths in *goal* that are absent from the tree.

        Runs :func:`_extract_file_paths` over *goal* and returns, in
        discovery order, each referenced path for which
        :meth:`_file_reference_present` is ``False`` — missing, unparseable,
        or escaping the workspace. Empty when the goal references no ``.py``
        paths or all referenced files are present.
        """
        return [
            rel_path
            for rel_path in _extract_file_paths(goal)
            if not self._file_reference_present(rel_path)
        ]

    def _build_premise_report(self, goal: str) -> str | None:
        """Build an advisory ``## PREMISE REPORT`` block for *goal*, or ``None``.

        When *goal* references one or more ``.py`` paths that are missing or
        unusable (per :meth:`_missing_file_references` —
        :meth:`_file_reference_present` is ``False`` for a path that is
        absent, unparseable, or escapes the workspace), returns a short block
        naming them so the executor can adapt — e.g. a goal whose intent is
        to *create* the file legitimately references a not-yet-existing path.
        Returns ``None`` when every referenced path is present or none are
        referenced. The block is advisory context, never a drop.

        The enumerated list is capped at :data:`_MAX_PREMISE_REPORT_PATHS`;
        any overflow is reported as a trailing count so the prompt-injected
        block stays bounded regardless of how many paths the goal cites.
        """
        missing = self._missing_file_references(goal)
        if not missing:
            return None
        shown = missing[:_MAX_PREMISE_REPORT_PATHS]
        lines = [
            "## PREMISE REPORT",
            "",
            "These referenced files are missing or unusable (not found in "
            "the source tree, unparseable, or outside it). If your goal is "
            "to create them this is expected; otherwise the goal's premise "
            "may have drifted — verify before proceeding:",
        ]
        lines.extend(f"- {path}" for path in shown)
        overflow = len(missing) - len(shown)
        if overflow:
            lines.append(f"- … and {overflow} more not shown")
        return "\n".join(lines)

    async def generate_task(
        self,
        session: Session | None = None,
    ) -> None:
        """Maintain the candidate-task queue (prune / edit / enqueue).

        Delegating shim (068 step 2) — the body lives in
        :meth:`maestro.loop.task_generation.TaskGenerator.generate_task`.
        """
        await self._task_generator.generate_task(session)

    def _has_runnable_work(self) -> bool:
        """Return True when ``_run_task_phase`` has anything to do this iteration.

        Reads ``pending_user_requests`` + ``pending_pr_requests`` off the
        self-model (already in memory most calls) and the head of the
        on-disk task queue. Returns ``False`` only when all three sources
        are empty — letting ``_phase_will_run`` skip the phase entirely so
        a fresh restart with nothing pending stays quiet.
        """
        model = self.consciousness.load_self_model()
        if model.pending_user_requests:
            return True
        if model.pending_pr_requests:
            return True
        return self.task_queue.head() is not None

    async def _preflight_pr_remediation(
        self, pr_request: PendingPRRequest,
    ) -> bool:
        """Run quality gates on the *merged* PR tree before LLM invocation.

        Delegating shim (068 step 1) — the body lives in
        :meth:`maestro.loop.pr_remediation.PrRemediationController.preflight`,
        which runs the action-zero red-root diagnosis right after it checks the
        PR branch out, so the gate measures the PR head, not ``main`` (#4100);
        non-pure-QG laps get it at dequeue instead (#4216).
        """
        return await self._pr_remediation.preflight(pr_request)

    def _diagnose_red_root_on_ci_red(
        self, pr_request: PendingPRRequest,
    ) -> None:
        """Run the demanding gate as action-zero on a CI-red/pending own-PR lap.

        The anti-avoidance wiring for :mod:`maestro.red_root`. Called once the
        PR branch is checked out — by the preflight (pure post-merge QG
        directives) before its probe-merge, and by
        ``PrRemediationController._diagnose_red_root_on_pr_head`` at dequeue for
        every other directive (#4216) — ahead of the obligation-free merge-truth
        read, this runs the demanding gate (ruff → mypy) via
        :func:`~maestro.red_root.diagnose_red_root`, fed the cached runnability
        verdict (read, never refreshed) under the oracle flag, and records the
        blocker to the journal and a ``red-root.md`` artifact.

        Scoped to a CI-red lap: the directive must carry the parseable
        ``Remediate PR #<n>:`` header and the live per-iteration
        :meth:`observe_pr_readiness` fact must show CI ``FAILING`` or ``PENDING``.
        A ``GREEN`` (or check-less ``NONE``) PR has no red root to read, so the
        demanding gate is skipped. Zero SDK cost.

        The PR's remote check-run conclusions are read once (proposal:
        remote-CI red-root) for the failing-check *names*, while the aggregate
        ``FAILING``/``RUNNING`` split is carried by the already-measured
        readiness verdict, threaded in as ``remote_ci_status``, so
        the recorded blocker distinguishes a concluded CI *failure* (a concrete
        fix target, with the failing check named) from a still-*running* CI
        (honestly not-mine-this-turn), and even a name-less aggregate failure is
        recorded as ``FAILING``.
        """
        match = re.match(r"Remediate PR #(\d+):", pr_request.directive)
        if match is None:
            return
        pr_num = int(match.group(1))
        readiness = self.observe_pr_readiness(pr_num, pr_request.pr_branch)
        if readiness.ci_state not in ("FAILING", "PENDING"):
            return
        # The early return above guarantees ``readiness.ci_state`` is "FAILING"
        # or "PENDING", so the authoritative rollup verdict maps FAILING → FAILING
        # and PENDING → RUNNING. Threaded into ``diagnose_red_root`` it recovers a
        # name-less aggregate FAILING the failing-names surface cannot see, while
        # a concretely-named FAILING still wins (mid-lap timing-skew case).
        remote_ci_status: CIStatus = (
            "FAILING" if readiness.ci_state == "FAILING" else "RUNNING"
        )
        wt = self.config.work_target()
        diagnosis = diagnose_red_root(
            self._red_root_gate_runner,
            build_red_root_gates(
                wt.gate_source_path, wt.gate_tests_path, wt.gate_lint_extra_paths,
            ),
            check_runs=self._observe_pr_check_runs(pr_num),
            remote_ci_status=remote_ci_status,
            runnability=(
                gate_environment.read_verdict(self.config.gate_environment_path)
                if self.config.gate_environment_oracle_enabled
                else None
            ),
        )
        # CI-failure diagnosis oracle (proposal 102). On a concluded CI *failure*
        # read the failing check's actual log and extract the concrete
        # first-error line(s), so the recorded blocker names the real red instead
        # of "look elsewhere". Reuses the already-computed failing check names (no
        # extra rollup fetch); ``("",)`` covers the unnamed-FAILING shape so a
        # nameless rollup still reads one combined log.
        ci_failures: tuple[CIFailure, ...] = ()
        if diagnosis.ci_status == "FAILING":
            ci_failures = diagnose_ci_failure(
                diagnosis.ci_failing_checks or ("",),
                lambda: self.evolution.code_host.read_run_log_failed(pr_num),
            )
        # Cache "the oracle surfaced a concrete failing-check line for this PR
        # this lap" (proposal 102) so the record-time red-read bind
        # (``bind_ci_failing_verdict_to_red_read``) can scope its downgrade to a
        # surfaced-but-unaddressed red without a second log fetch.
        self._ci_oracle_surfaced[pr_num] = any(
            f.first_error_lines for f in ci_failures
        )
        self._record_red_root_diagnosis(
            pr_num, readiness.ci_state, diagnosis, ci_failures=ci_failures,
        )

    def _observe_pr_check_runs(self, pr_num: int) -> tuple[CheckRun, ...]:
        """Read the PR's remote check-run conclusions off the code host.

        Maps each concretely-failing check the code host reports
        (:meth:`~maestro.vcs.CodeHost.get_pr_failing_check_names`, already
        reachable on the remediation path) to a ``failure``-conclusion
        :class:`~maestro.red_root.CheckRun` so :func:`diagnose_red_root` can name
        the fix target. The plain map, nothing more: an empty result means the
        host reported no *named* failing check. A name-less aggregate ``FAILING``
        — an empty-name rollup item, or a timing skew between the two independent
        gh reads — is recovered by the caller threading its already-measured
        authoritative rollup verdict into :func:`diagnose_red_root` as
        ``remote_ci_status``, not by fabricating a stand-in CheckRun here.
        Best-effort, zero SDK cost.
        """
        return tuple(
            CheckRun(name=name, conclusion="failure")
            for name in self.evolution.code_host.get_pr_failing_check_names(
                pr_num,
            )
        )

    def _red_root_gate_runner(
        self, _name: str, argv: tuple[str, ...],
    ) -> tuple[int, str, str]:
        """The :data:`~maestro.red_root.GateRunner` seam bound to this agent.

        Adapts :func:`~maestro.red_root.run_gate_subprocess` to the injected-runner
        contract, supplying the workspace cwd and the self-check timeout. ``_name``
        is unused — ``argv`` fully specifies the command — but kept so the bound
        method matches the ``(name, argv) -> (exit_code, first_error_line,
        full_output)`` shape :func:`~maestro.red_root.diagnose_red_root` calls.
        """
        return run_gate_subprocess(
            argv,
            cwd=str(self.config.workspace),
            timeout=self.config.selfcheck_timeout,
        )

    def _record_red_root_diagnosis(
        self,
        pr_num: int,
        ci_state: str,
        diagnosis: RedRootDiagnosis,
        ci_failures: tuple[CIFailure, ...] = (),
    ) -> None:
        """Persist the red-root blocker into the lap context + a ``red-root.md``.

        Composes the concrete blocker sentence from *diagnosis* and *ci_failures*
        as a priority chain, then writes it into two places: the durable
        ``red-root.md`` artifact under ``artifacts/red-root-pr-<n>/`` and a
        subjective journal realization for the lap. Both name the blocker, so it
        is the recorded first thing known on the red lap. Zero SDK cost.

        The chain (proposal 102) makes the concrete CI failure the blocker itself
        rather than a note pointing "elsewhere":

        * a local gate failure keeps today's ``<gate> (exit <n>): <first error
          line>`` blocker — the most direct reproducible root;
        * local gates pass + a concrete CI line → the failing check(s) named plus
          that line;
        * local gates pass + CI ``FAILING`` with no extractable line → an honest
          "log carried no extractable error line" note;
        * local gates pass + CI not (yet) failing → an honest "ruff/mypy pass
          locally; CI is still running" note.

        *ci_failures* (empty unless the CI-failure oracle ran — i.e. CI concluded
        ``FAILING``) also appends a ``## CI Failure`` section to the artifact as
        the per-check detail; the first concrete CI error line is folded into the
        journal realization only when the recomposed blocker does not already
        carry it (so it is never stated twice). The default empty tuple renders
        nothing, so a non-FAILING lap adds no section.
        """
        first_ci_line = next(
            (f.first_error_lines[0] for f in ci_failures if f.first_error_lines),
            "",
        )
        # Compose the concrete blocker as a priority chain (proposal 102): the
        # CI failure the loop is ordered to fix becomes the blocker itself rather
        # than a note pointing "elsewhere". ``blocker_names_ci_line`` records when
        # the chosen blocker already carries ``first_ci_line`` so the journal
        # realization does not then state the same line twice.
        blocker_names_ci_line = False
        if not diagnosis.all_passed:
            # A local gate failure is the most direct reproducible root — keep
            # today's ``<gate> (exit <n>): <first error line>`` blocker.
            blocker = (
                f"{diagnosis.failing_gate} (exit {diagnosis.exit_code}): "
                f"{diagnosis.first_error_line}"
            )
        elif first_ci_line:
            # Local gates pass, and the CI-failure oracle read a concrete line —
            # name the failing check(s) and that line as the blocker itself.
            checks = ", ".join(diagnosis.ci_failing_checks) or "(unnamed check)"
            blocker = f"CI check {checks} failed: {first_ci_line}"
            blocker_names_ci_line = True
        elif diagnosis.ci_status == "FAILING":
            # Local gates pass, CI concluded FAILING but its log carried no
            # extractable line — say so honestly (the
            # ``render_ci_failure_section`` "(no concrete error line extracted)"
            # voice) rather than name a phantom line.
            blocker = (
                "CI reports FAILING but its log carried no extractable error line"
            )
        else:
            # Local gates pass, CI has not (yet) concluded a failure — still
            # running/pending. Honest: the red is not visible here yet.
            blocker = "ruff/mypy pass locally; CI is still running"
        remote_ci = render_remote_ci_section(diagnosis)
        ci_failure_section = render_ci_failure_section(ci_failures)
        artifact_dir = self.config.artifacts_dir / f"red-root-pr-{pr_num}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(
            artifact_dir / "red-root.md",
            f"# Red-root diagnosis — PR #{pr_num}\n\n"
            f"- CI state: {ci_state}\n"
            f"- Concrete blocker: {blocker}\n"
            f"{remote_ci}"
            f"{ci_failure_section}",
        )
        remote_ci_note = (
            f" Remote CI is {diagnosis.ci_status}"
            + (
                " — failing checks: "
                + ", ".join(diagnosis.ci_failing_checks)
                if diagnosis.ci_failing_checks
                else ""
            )
            + "."
            if diagnosis.ci_status is not None
            else ""
        )
        # The blocker already carries the first CI failure line in the
        # named-CI-failure case, so drop the separate trailing sentence there to
        # avoid stating the same line twice; keep it on the local-gate-failure
        # path, whose blocker names the gate, not the CI line.
        ci_failure_note = (
            f" The first CI failure line: {first_ci_line}."
            if first_ci_line and not blocker_names_ci_line
            else ""
        )
        self.journal.record_event(
            content=(
                f"Red-root on PR #{pr_num} (CI {ci_state}): I ran the demanding "
                "gate as action zero, before the safe is-ancestor probe. The "
                f"concrete blocker is {blocker}.{remote_ci_note}{ci_failure_note}"
                " No merge-truth read gets to be the first thing I know on a red "
                "lap."
            ),
            entry_type="realization",
            triggered_by="red_root_diagnosis",
        )

    async def _record_preflight_skip_outcome(
        self, goal: str, pr_branch: str | None,
    ) -> None:
        """Record a preflight-skip as a no-op and escalate a stuck PR.

        The preflight-skip path records a ``success`` :class:`TaskRecord` but
        produces no branch change, so it would otherwise never reach the
        repetition-gate accounting in :meth:`_run_task_phase`. Recording it as
        a no-op here lets the gate trip after ``threshold`` consecutive skips.
        When the gate then refuses to re-route while the PR is still unmerged,
        the PR is dequeued for good and the deterministic merge-blocker reason
        is surfaced via :meth:`_notify_best_effort` — zero SDK cost.
        """
        match = re.match(r"Remediate PR #(\d+):", goal)
        if not match:
            return
        pr_num = int(match.group(1))
        # The ``[preflight]`` finding classifies as a no-op via
        # ``classify_noop``; pass that verdict through so the indicator set
        # stays the single source of truth.
        self.remediation_tracker.record_outcome(
            pr_num,
            noop=classify_noop(
                f"{Marker.PREFLIGHT} remediation skipped on {pr_branch}",
            ),
        )
        if self.remediation_tracker.should_reroute(pr_num):
            return
        await self._escalate_stuck_pr(pr_num, pr_branch)

    def _preflight_merge_truth_standdown(self, goal: str) -> int | None:
        """Merge-truth as ``run``'s literal first action — the third merge-truth face.

        The dequeue-face guards (:meth:`_gate_pr_remediation_merged`,
        merge-block, branch-resolved) only fire on the ``pending_pr_requests``
        path; a user-request task that merely *references* a PR bypasses them
        and gets serviced even when git already shows the PR landed (#2579 was
        checked four times across four user-channel tasks). This closes that
        channel: extract the first ``#NNNN`` / ``PR NNNN`` reference from *goal*
        and read merge-truth through the shared :meth:`is_pr_settled` chokepoint
        (proposal 087) — one provenance-stamped
        :class:`~maestro.pr_facts.MergeTruth` whose ``settled`` is exactly
        ``merged or closed``, the settled fork where the honest move is silence.
        Returns the PR number when the referenced PR is already merged or
        closed — so :meth:`run` can stand down before any planning, branch, or
        SDK servicing. Returns ``None`` when *goal* names no PR or the PR is
        still open, falling through to normal servicing unchanged.

        Fetch-first is the chokepoint's own contract: :meth:`is_pr_settled`
        runs a fail-loud ``git fetch origin`` before observing, so no verdict is
        authored against a stale local tree. On the *unsettled* fall-through —
        and only there — :meth:`_fetch_origin_and_fast_forward` additionally
        fast-forwards the local branch so any later conflict assessment sees
        fresh refs rather than a lagging tree, which would both *miss* an
        already-landed PR and hand the SDK a *phantom conflict* to re-resolve
        (the engine of the remediation-monoculture lap). The read is scoped to
        goals that actually name a PR (after the regex match) so a PR-less goal
        pays no git cost.

        Pure read of merge-truth — zero SDK cost.
        """
        match = _PR_REFERENCE_PATTERN.search(goal)
        if match is None:
            return None
        pr_num = int(match.group(1))
        # Centralized observation (proposal 087): one provenance-stamped
        # MergeTruth decides the stand-down. ``settled`` folds together the
        # merged and the closed-unmerged forks; ``is_pr_settled`` performs its
        # own fetch-first read, so no redundant local-tree freshening is needed
        # on the settled fork.
        if self.is_pr_settled(pr_num).settled:
            return pr_num
        # Unsettled → falls through to normal servicing; freshen the local tree
        # so the downstream conflict assessment sees fresh refs, not a stale
        # base.
        self._fetch_origin_and_fast_forward()
        return None

    def _fetch_origin_and_fast_forward(self) -> None:
        """Freshen the local tree before any merge-truth read — fetch-first.

        The deterministic first physical act of the stand-down preflight once a
        goal names a PR: ``git fetch origin`` followed by a fast-forward-only
        update of the local branch to its upstream. After it, the merge-truth
        oracle's git-fallback path (:func:`audit_checks_phantom._git_pr_merged_default`,
        which reads the ``origin/main`` remote-tracking ref) and any later
        conflict assessment see fresh refs rather than a stale local tree. A
        stale tree both *misses* an already-landed PR (the remote-tracking ref
        lags the real merge) and hands the SDK a *phantom conflict* to
        re-resolve against an outdated base — the engine of the
        remediation-monoculture lap. This makes fetch-first machinery the code
        cannot skip, not a reflex to be summoned.

        The fast-forward is ``--ff-only``: a refused fast-forward (genuine
        divergence or no upstream) is a deliberate no-op, so the existing
        assessment path runs unchanged on the diverged local tree.

        Best-effort — a typed git failure (``OSError`` /
        :class:`subprocess.SubprocessError`, e.g. a timed-out fetch) is swallowed
        and the assessment proceeds on whatever refs exist, matching the
        fail-closed direction of :func:`audit_checks_phantom._git_pr_merged_default`.
        Zero SDK cost; the only I/O is two bounded git subprocesses.
        """
        cwd = self.config.workspace
        try:
            subprocess.run(
                ["git", "fetch", "origin"],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.config.git_timeout,
                cwd=cwd,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug(
                "preflight: git fetch origin failed; merge-truth read "
                "proceeds on existing refs",
                exc_info=True,
            )
            return
        # Fast-forward the local branch to its upstream so a later
        # ``git merge origin/main`` cannot resurrect a phantom conflict against
        # a stale base. ``--ff-only`` refuses on genuine divergence / no
        # upstream (non-zero exit, swallowed by ``check=False``); that is the
        # documented fall-through to the unchanged assessment path.
        try:
            subprocess.run(
                ["git", "merge", "--ff-only", "@{u}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.config.git_quick_timeout,
                cwd=cwd,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug(
                "preflight: fast-forward to upstream failed; assessment "
                "proceeds on the un-fast-forwarded tree",
                exc_info=True,
            )

    def _host_or_git_pr_merged(self, pr_num: int) -> bool:
        """One merge-truth oracle for PR tending: host API first, git fallback.

        Proposal 080 Phase 1. Consults the authoritative code-host API — the
        **fail-loud** :meth:`maestro.vcs.CodeHost.is_pr_merged_strict`, not its
        best-effort sibling — and falls back to the git-log oracle
        (:func:`_git_pr_merged`, bound to the agent's workspace) **only when the
        host call raises**. Whenever the host answers, its verdict wins,
        including ``False``: a "not merged" host reply beats a ``git log``
        subject that merely name-drops ``#<pr>`` for an unrelated reason.
        Routing the unlanded-remediation sensor, the escalation bridge, and the
        dequeue merged-exclusion gate through this one seam converges the whole
        tending subsystem on the host oracle and removes the name-drop
        false-positive that blinded the bridge for #2579.

        The *strict* oracle is what makes that fallback reachable at all: the
        best-effort ``is_pr_merged`` swallows ``VCSError`` and returns ``False``,
        so wiring it here would launder every transient ``gh`` failure into a
        confident "not merged" and leave this ``except`` dead (#2954).

        The fallback *returns* the git verdict rather than swallowing the host
        failure (typed-fallback return, not a log-and-fall-through), so the
        caller still gets a merge verdict when the host API is unreachable —
        degrading to the pre-080 git-only behaviour.
        """
        try:
            return self.evolution.code_host.is_pr_merged_strict(pr_num)
        except Exception:
            logger.debug(
                "Host merge-truth API failed for PR #%s; falling back to the "
                "git-log oracle (pre-080 git-only behaviour)",
                pr_num,
                exc_info=True,
            )
            return _git_pr_merged(pr_num, cwd=self.config.workspace)

    def _fetch_origin_for_merge_truth(self) -> None:
        """Fetch-first action for the :meth:`is_pr_settled` chokepoint — fail-loud.

        A bounded ``git fetch origin`` (``check=True``, no swallowing handler) so
        the merge-truth observation resolves against fresh remote-tracking refs,
        never a stale local tree. Every PR-servicing face reaches merge state
        through this one fetch-then-observe path, so all of them share
        byte-identical fetch semantics. A fetch failure propagates loudly — the
        observer (:func:`~maestro.pr_facts.observe_pr_merge_truth`) is
        fail-loud, so no verdict is ever authored on unfreshened refs.
        """
        try:
            subprocess.run(
                ["git", "fetch", "origin"],
                capture_output=True,
                text=True,
                check=True,
                timeout=self.config.git_timeout,
                cwd=self.config.workspace,
            )
        except subprocess.CalledProcessError as exc:
            # Fail-loud stays intact: re-raise so ``run_loop`` crashes and no
            # merge-truth verdict is authored on unfreshened refs. But surface
            # git's captured stderr first — the ``Authentication failed`` line
            # that names a stale/rejected ``GITHUB_TOKEN`` (#2855) — so the
            # crash record is a one-line diagnosis, not a bare ``exit 128``.
            # git's credential-helper stderr names the remote HTTPS URL, not
            # the token, so the logged line carries no secret.
            logger.error(
                "git fetch origin failed (exit %s) during merge-truth "
                "observation; git stderr: %s",
                exc.returncode,
                (exc.stderr or "").strip(),
            )
            raise

    def _branch_ahead_of_origin_count(self, pr_branch: str) -> int:
        """Count *pr_branch* commits absent from every ``origin`` ref, fetch-first.

        The reachability probe the chosen-stand-down guard reads. Freshens
        remote-tracking refs via the shared fail-loud
        :meth:`_fetch_origin_for_merge_truth` seam (so a commit a prior Tend lap
        already pushed is not mis-read as still-local), then asks git
        ``rev-list --count <branch> --not --remotes=origin`` through
        :meth:`~maestro.vcs.VCS.commits_not_on_remote`. A positive count means the
        branch is an owned, pushable act. Memoized per loop-iteration on
        ``self._ahead_of_origin_memo`` (keyed by branch), so the fetch+count runs
        at most once per branch per iteration; the memo is cleared on a fresh
        iteration alongside the merge-truth / readiness memos.
        """
        cached = self._ahead_of_origin_memo.get(pr_branch)
        if cached is not None:
            return cached
        self._fetch_origin_for_merge_truth()
        count = self._vcs.commits_not_on_remote(pr_branch)
        self._ahead_of_origin_memo[pr_branch] = count
        return count

    def _version_archive_settled(self, pr_num: int) -> bool:
        """Host-independent local corroboration that *pr_num*'s cure landed.

        The ``version_archive_oracle`` seam for :func:`observe_pr_merge_truth`:
        consulted **only** after the fetch-first ancestry read and the host
        ``mergedAt`` override both leave the PR unmerged. Maps *pr_num* to its
        backing plan (the plan whose ``pr_url`` resolves to *pr_num*), then asks
        :func:`~maestro.remediation_tracker.version_archive_has_landing` whether
        the archive loaded through ``self.version_archive`` (the flag-selected
        :class:`~maestro.memory.VersionArchiveBackend` — JSONL default or the
        shared SQLite store) carries a ``VersionRecord`` for that plan — a record
        appended only on an actual landing, so its presence is a merge-truth
        signal already on local disk. Returns ``False`` when no plan maps to
        *pr_num* (an unparseable ``pr_url`` is skipped) or no record matches. Zero
        SDK cost; the only I/O is the bounded archive read.
        """
        plan_id = ""
        for plan in self.memory.get_all_plans():
            if not plan.pr_url:
                continue
            try:
                if self.evolution._pr_number_from_url(plan.pr_url) == pr_num:
                    plan_id = plan.plan_id
                    break
            except ValueError:
                continue
        if not plan_id:
            return False
        return version_archive_has_landing(plan_id, self.version_archive.load_all())

    def is_pr_settled(self, pr_num: int) -> MergeTruth:
        """The single chokepoint every PR-servicing face reads merge state through.

        Proposal 087 step 3. Produces one provenance-stamped
        :class:`~maestro.pr_facts.MergeTruth` for *pr_num* via the lone
        observer :func:`~maestro.pr_facts.observe_pr_merge_truth`, wiring the
        agent's real seams: the fail-loud
        :meth:`_fetch_origin_for_merge_truth` as ``fetch``, the fail-loud host
        API :meth:`~maestro.vcs.CodeHost.is_pr_merged_strict` as ``host_oracle``
        (which *raises* on host failure — unlike its best-effort sibling
        ``is_pr_merged``, whose swallowed ``VCSError`` would leave the fallback
        below unreachable and stamp a laundered failure ``host_api`` /
        not-landed, #2954 — so the observer can record *which* oracle answered),
        the module-level git-subject :func:`_git_pr_merged` as the
        ``branch_oracle`` fallback (consulted only when the host call raises,
        stamping ``source="git_subject"``), and
        :meth:`~maestro.vcs.CodeHost.is_pr_closed` as the ``close_oracle`` that
        distinguishes a closed-unmerged fork from a still-open PR, and
        :meth:`~maestro.vcs.CodeHost.has_merged_at` as the ``merged_at_oracle``
        squash-merge override — a non-null host ``mergedAt`` promotes a PR the
        primary merged read reported unmerged (as a squash commit is not the
        branch head's linear ancestor) to settled/landed, so this chokepoint is
        squash-merge-aware and does not depend on the primary oracle being so.
        Below that host override sits one further, *host-independent* fallback:
        :meth:`_version_archive_settled` as ``version_archive_oracle`` — when the
        ancestry read and the ``mergedAt`` override both leave the PR unmerged (a
        squash merge whose host ``mergedAt`` is null/unreadable), a matching
        ``VersionRecord`` on local disk (written only on an actual landing)
        settles the PR, stamping ``source="version-archive"``. The fetch and
        ancestry read always run first; this tier only adds a floor beneath them.

        Memoized per loop-iteration (keyed by *pr_num* on
        ``self._merge_truth_memo``, cleared in ``run_loop``'s fresh-iteration
        reset): the first face to ask observes (one fetch), every later face this
        iteration reuses the same fact — so a PR is observed at most once per
        iteration. A memoized fact older than ``[loop] merge_truth_ttl_seconds``
        is re-observed (re-fetched) rather than returned stale (step 4); within
        the TTL the memo is reused and ``fetch`` is not re-invoked.
        """
        cached = self._merge_truth_memo.get(pr_num)
        if cached is not None and cached.is_fresh(
            time.time(), self.config.merge_truth_ttl_seconds,
        ):
            return cached
        truth = observe_pr_merge_truth(
            pr_num,
            fetch=self._fetch_origin_for_merge_truth,
            host_oracle=self.evolution.code_host.is_pr_merged_strict,
            branch_oracle=lambda n: _git_pr_merged(
                n, cwd=self.config.workspace,
            ),
            now=time.time,
            close_oracle=self.evolution.code_host.is_pr_closed,
            branch_source="git_subject",
            merged_at_oracle=self.evolution.code_host.has_merged_at,
            version_archive_oracle=self._version_archive_settled,
        )
        self._merge_truth_memo[pr_num] = truth
        return truth

    def _observe_pr_merge_block_reason(
        self, pr_num: int, pr_branch: str | None, check_conclusion: str,
    ) -> str:
        """Deterministic merge-block reason feeding the readiness fact.

        The ``merge_block_oracle`` seam for :meth:`observe_pr_readiness` (089):
        :func:`classify_merge_block` over the PR head as published
        (:meth:`_pr_head_ref`, #4371), reusing *check_conclusion*; then
        :func:`apply_landing_model` (#4307) and the host-conflict-only
        :func:`refine_merge_block_reason`. Branchless degrades to ``NONE``.
        """
        commits_behind = 0
        merge_state = ""
        mergeable = ""
        head_ref: str | None = None
        if pr_branch:
            head_ref = self._pr_head_ref(pr_branch)
            _ahead, commits_behind = self._vcs.commits_ahead_behind(
                head_ref, "origin/main",
            )
            pr_states = self.evolution.code_host.get_open_evolution_pr_states(
                "maestro/evolve/",
            )
            state = pr_states.get(pr_branch)
            if state is not None:
                merge_state = state.merge_state_status
                mergeable = state.mergeable
        reason = classify_merge_block(commits_behind, check_conclusion, merge_state)
        reason = apply_landing_model(
            reason, tend_catch_up=self.config.tend_main_merge_enabled,
            cleanly_mergeable=not self._behind_conflicts(reason, head_ref),
            check_conclusion=check_conclusion, merge_state_status=merge_state,
        )
        return refine_merge_block_reason(reason, mergeable=mergeable)

    def _pr_head_ref(self, pr_branch: str) -> str:
        """``origin/<branch>`` when it resolves, else local — sole owner (#4371)."""
        ref = f"origin/{pr_branch}"
        return ref if self._vcs.rev_parse(ref) is not None else pr_branch

    def _behind_conflicts(self, reason: str, ref: str | None) -> bool:
        """Probe *ref* vs ``origin/main`` for a merge-queue BEHIND_MAIN only."""
        return (
            ref is not None and reason == MERGE_BLOCK_BEHIND_MAIN
            and not self.config.tend_main_merge_enabled
            and self._vcs.merge_tree_conflicts(ref, "origin/main")
        )

    def observe_pr_readiness(
        self, pr_num: int, pr_branch: str | None,
    ) -> PRReadiness:
        """The chokepoint every face reads PR readiness through (089 step 2).

        Produces one provenance-stamped
        :class:`~maestro.pr_facts.PRReadiness` for *pr_num* via the lone
        observer :func:`~maestro.pr_facts.observe_pr_readiness`, wiring the
        agent's real seams: the shared :meth:`is_pr_settled` chokepoint as the
        ``merge_truth`` oracle (so the readiness fact and the merge-truth fact
        agree on landing), the host CI rollup behind ``ci_oracle`` (mapped to a
        :class:`~maestro.pr_facts.CIState`), the zero-cost
        :func:`~maestro.evolution_tend._eligible_bug_comments` /
        :func:`~maestro.evolution_tend._fetch_eligible_overflow_findings` counters
        behind ``thread_oracle`` / ``overflow_oracle``, and
        :meth:`_observe_pr_merge_block_reason` behind ``merge_block_oracle``.

        Memoized per loop-iteration (keyed by *pr_num* on
        ``self._pr_readiness_memo``, cleared in ``run_loop``'s fresh-iteration
        reset) with the same ``[loop] merge_truth_ttl_seconds`` TTL discipline
        :meth:`is_pr_settled` uses: the first face to ask observes, every later
        face this iteration reuses the same fact, and a fact older than the TTL
        is re-observed. The host CI rollup is read once and threaded into both
        the CI and merge-block oracles. Zero SDK cost; additive — no caller reads
        the fact until the step-3 gate, which is itself default-off.
        """
        cached = self._pr_readiness_memo.get(pr_num)
        if cached is not None and cached.is_fresh(
            time.time(), self.config.merge_truth_ttl_seconds,
        ):
            return cached
        code_host = self.evolution.code_host
        check_conclusion = code_host.get_pr_check_conclusion(pr_num)

        def _overflow_count(n: int) -> int:
            # ``_fetch_eligible_overflow_findings`` swallows a host-fetch error
            # and returns ``([], scan_ok=False)``. Counting that as zero would
            # let a transient outage read a PR as ready-and-waiting — exactly the
            # "swallowed fetch error reads as zero outstanding" failure
            # ``_confirm_ready_for_review`` guards against with its
            # ``comment_scan_ok`` requirement. Surface one blocker on a failed
            # scan so an unscannable PR is never observed ready until the scan
            # actually completes (fail safe, not silent).
            findings, scan_ok = _fetch_eligible_overflow_findings(code_host, n)
            return len(findings) if scan_ok else 1

        readiness = _observe_pr_readiness(
            pr_num,
            merge_truth=self.is_pr_settled,
            ci_oracle=lambda _n: ci_state_from_conclusion(check_conclusion),
            thread_oracle=self.evolution.count_eligible_threads_fail_safe,
            overflow_oracle=_overflow_count,
            merge_block_oracle=lambda n: self._observe_pr_merge_block_reason(
                n, pr_branch, check_conclusion,
            ),
            now=time.time,
        )
        self._pr_readiness_memo[pr_num] = readiness
        return readiness

    def _pr_disposability(self, pr_num: int, pr_branch: str | None) -> bool:
        """Is this open own PR's work genuinely disposable? — cheap, zero-SDK.

        Assembles :func:`~maestro.pr_facts.classify_pr_disposability`'s inputs
        from state the agent already holds, doing no SDK work:

        * ``plan_text`` / ``sibling_plan_texts`` — the *live* plans
          (``pending_review``/``approved``/``implemented``) from the live-work
          oracle (:meth:`~maestro.memory.Memory.get_live_plans`), each composed as
          ``f"{title}\\n{rationale}"``. The :class:`~maestro.pr_facts.LivePlan`
          whose ``pr_number`` names *pr_num* is the backing plan; every other live
          plan is a sibling the content-word restatement check compares against —
          including an ``approved`` plan that has not yet opened a PR, the work
          most likely to be duplicated and precisely the state the old
          ``implemented``-and-``pr_url`` filter dropped. ``LivePlan.pr_number`` is
          tolerantly parsed (``None`` when no PR is attached or the URL is
          unparseable), so no plan is dropped for lacking a PR — a PR-less plan
          simply lands in the sibling set.
        * ``has_substantive_diff`` — ``origin/main`` vs ``origin/{pr_branch}`` via
          :meth:`~maestro.vcs.CodeHost.rev_parse` + a
          :meth:`~maestro.vcs.CodeHost.get_diff_stat` emptiness check. An empty
          stat means no real change to land (``False``); an unresolved ref (or no
          branch) leaves it unknown (``None``), so an unavailable probe fails
          toward salvage.

        ``superseded_by_merged`` is passed unknown (``None``) this increment —
        precise merged-sibling detection is future work — so it contributes
        nothing toward disposability. Zero SDK cost.
        """
        plan_text = ""
        sibling_plan_texts: list[str] = []
        for plan in self.memory.get_live_plans():
            text = f"{plan.title}\n{plan.rationale}"
            if plan.pr_number == pr_num:
                plan_text = text
            else:
                sibling_plan_texts.append(text)
        base = self._vcs.rev_parse("origin/main")
        head = self._vcs.rev_parse(f"origin/{pr_branch}") if pr_branch else None
        if base and head:
            has_substantive_diff: bool | None = bool(
                self._vcs.get_diff_stat(base, head).strip()
            )
        else:
            has_substantive_diff = None
        return classify_pr_disposability(
            plan_text,
            sibling_plan_texts,
            superseded_by_merged=None,
            has_substantive_diff=has_substantive_diff,
        )

    def _assess_pr_salvage(
        self,
        pr_num: int,
        pr_branch: str | None,
        *,
        readiness: PRReadiness | None = None,
        unpersisted: TaskRecord | None = None,
    ) -> Salvage:
        """Must the loop fix-and-land this open own PR, or may it disengage?

        The agent-level gatherer for the pure
        :func:`~maestro.pr_facts.assess_pr_salvage` oracle (proposal 094 step 2):
        reads the PR's readiness through the per-iteration-memoized
        :meth:`observe_pr_readiness` chokepoint — or reuses a *readiness* the
        caller already fetched this lap — and the zero-SDK
        :meth:`_pr_disposability` predicate, then returns the oracle's
        :class:`~maestro.pr_facts.Salvage` verdict:
        :attr:`~maestro.pr_facts.Salvage.SALVAGE_AND_LAND` while a code-clearable
        blocker remains on non-disposable work, else
        :attr:`~maestro.pr_facts.Salvage.DISENGAGE_PERMITTED`. Inert until the two
        stand-down gates (steps 3-4) call it behind
        ``salvage_before_disengage_enabled``. Zero SDK cost.

        Budget dimension: the already-measured commits-behind count from
        :meth:`~maestro.evolution.EvolutionEngine._observe_pr_staleness` (the same
        origin-grounded ``git`` observation the tend catch-up decision reads) is
        threaded into the oracle so a CONFLICTING/DIRTY branch measured strictly
        past :data:`~maestro.pr_facts.RECONCILE_COMMITS_BEHIND_BUDGET` resolves
        to ``DISENGAGE_PERMITTED`` — lifting the ``gh pr close`` veto on a branch
        that re-conflicts before it can land. A branchless PR (no head to measure)
        leaves the count ``None``, failing safe toward salvage.
        """
        if readiness is None:
            readiness = self.observe_pr_readiness(pr_num, pr_branch)
        disposable = self._pr_disposability(pr_num, pr_branch)
        commits_behind = (
            self.evolution._observe_pr_staleness(pr_branch).commits_behind
            if pr_branch
            else None
        )
        # Proposal 099: distance-behind alone no longer authorizes disengage —
        # disposal now also requires repeated failure against a base that stood
        # still. Count the trailing same-``(head, base)`` partial streak for this
        # PR from the same history tail the stall predicate reads; an unmeasurable
        # head or base yields 0, which the oracle reads as "not yet proven stuck"
        # and keeps salvaging.
        current_base = self._vcs.rev_parse("origin/main")
        current_head = (
            self._vcs.rev_parse(f"origin/{pr_branch}")
            or self._vcs.rev_parse(pr_branch)
        ) if pr_branch else None
        recent = self.memory.get_recent_tasks(30)
        if unpersisted is not None:
            recent.append(unpersisted)
        same_base_failures = _same_base_partial_streak(
            pr_num, current_head, current_base, recent,
        )
        return assess_pr_salvage(
            readiness, disposable, commits_behind=commits_behind,
            same_base_failures=same_base_failures,
            code_owed=owed_act_binding(self.config).code_owed(readiness),
        )

    def _current_task_focus(self) -> TaskFocus:
        """Is there owned WIP to finish, or is discretionary work allowed? (092).

        Delegating shim — the body lives in
        :func:`~maestro.loop.owed_act_binding.gather_task_focus`. This method
        owns only the enumeration snapshot: the open own ``maestro/evolve/*`` PRs
        read via :meth:`~maestro.vcs.CodeHost.get_open_evolution_pr_states` (one
        call, ``{head_ref: PrMergeState}``, held on
        ``self._open_owned_pr_states_memo`` via :meth:`_open_owned_pr_states` so
        the gate and the audit bridge share one ``gh pr list``; a failed listing
        is never memoized). The collaborator observes each PR through the
        per-iteration-memoized :meth:`observe_pr_readiness` chokepoint, derives
        the owed-act map under ``[loop] owed_act_binding_enabled``, and asks
        :func:`~maestro.wip_primacy.select_task_focus`. Zero SDK cost; the
        enumeration snapshot and the readiness memo keep both consults cheap.
        """
        states = self._open_owned_pr_states(strict=False)
        return gather_task_focus(
            states, self.observe_pr_readiness, owed_act_binding(self.config),
        )

    def _open_owned_pr_states(self, *, strict: bool) -> dict[str, PrMergeState]:
        """Return the per-iteration open own ``maestro/evolve/*`` PR snapshot.

        Sole filler of ``self._open_owned_pr_states_memo``. The listing always
        runs ``strict=True`` and only a *successful* result is memoized, so a
        failed ``gh pr list`` can never pose as a trusted empty snapshot for a
        later consumer that reads absence as "closed or merged" (#4107). On
        failure ``strict=True`` re-raises :class:`VCSError`; ``strict=False``
        (best-effort consumers) logs and degrades to ``{}``; a failure is
        remembered for the iteration.
        """
        if self._open_owned_pr_states_memo is not None:
            return self._open_owned_pr_states_memo
        if self._open_owned_pr_states_error is not None:
            if strict:
                raise self._open_owned_pr_states_error
            return {}
        try:
            states = self.evolution.code_host.get_open_evolution_pr_states(
                "maestro/evolve/", strict=True,
            )
        except VCSError as exc:
            self._open_owned_pr_states_error = exc
            if strict:
                raise
            logger.warning(
                "open-PR listing for maestro/evolve/ failed (%s); treating as "
                "no open PRs this iteration.", exc,
            )
            return {}
        self._open_owned_pr_states_memo = states
        return states

    def _wip_owns_slot(self) -> bool:
        """Does an open own PR with a self-clearable blocker own the slot? (092).

        The single definition of "is WIP steering the loop right now?" —
        ``True`` iff the ``[loop] wip_primacy_enabled`` flag is on *and*
        :meth:`_current_task_focus` reports a :class:`~maestro.wip_primacy.TendOwnedPR`
        (an open own ``maestro/evolve/*`` PR with a ``CONFLICTING`` /
        ``THREADS_UNRESOLVED`` blocker the loop can clear itself). The
        ``and`` short-circuits on the flag: with ``wip_primacy_enabled`` off the
        ``_current_task_focus`` probe (a ``gh pr list`` + per-PR readiness
        observation) is never called, so the flag-off path stays free.

        Consumed by the head-of-chain minters that must defer to owned work:
        the audit→directive bridge in :meth:`_audit_self_model`, evolution-PR
        minting in :meth:`_implement_approved_plans`, and :meth:`evolve`'s propose/submit tail. None of
        them author the finish-this-PR directive — that stays
        the sole province of the ``wip_primacy`` gate (``task_gates.py``); this
        predicate only tells them whether to stand down.
        """
        return self.config.wip_primacy_enabled and isinstance(
            self._current_task_focus(), TendOwnedPR,
        )

    def _gate_pr_remediation_merged(
        self, goal: str, pr_branch: str | None,
    ) -> bool:
        """Exclude an already-merged PR at the ``pending_pr_requests`` dequeue.

        Delegating shim (068 step 1) — the body lives in
        :meth:`maestro.loop.pr_remediation.PrRemediationController.gate_merged`.
        """
        return self._pr_remediation.gate_merged(goal, pr_branch)

    def _gate_pr_remediation_landed(
        self, pr_request: PendingPRRequest, source: TaskSource,
    ) -> bool:
        """Delegate to :meth:`PrRemediationController._gate_pr_remediation_landed`."""
        return self._pr_remediation._gate_pr_remediation_landed(pr_request, source)

    def _gate_pr_remediation_ready(
        self, pr_request: PendingPRRequest, source: TaskSource,
    ) -> bool:
        """Delegate to :meth:`PrRemediationController._gate_pr_remediation_ready`."""
        return self._pr_remediation._gate_pr_remediation_ready(pr_request, source)

    def _plan_for_pr_number(self, pr_num: int) -> EvolutionPlan | None:
        """Delegate to :meth:`PrRemediationController._plan_for_pr_number`."""
        return self._pr_remediation._plan_for_pr_number(pr_num)

    async def _gate_pr_remediation_host_closed(
        self, pr_request: PendingPRRequest, source: TaskSource,
    ) -> bool:
        """Delegate to :meth:`PrRemediationController._gate_pr_remediation_host_closed`."""
        return await self._pr_remediation._gate_pr_remediation_host_closed(pr_request, source)

    def _gate_pr_remediation_ci_pending_hold(
        self, entry: PendingPRRequest,
    ) -> bool:
        """Yield the remediation slot when CI is the sole live blocker.

        Delegating shim — the body lives in
        :meth:`maestro.loop.pr_remediation.PrRemediationController.gate_ci_pending_hold`.
        """
        return self._pr_remediation.gate_ci_pending_hold(entry)

    async def _gate_pr_remediation_stood_down(
        self, goal: str, pr_branch: str | None, source: TaskSource,
    ) -> bool:
        """Honor my own prior keyed stand-down verdict at the dequeue.

        Delegating shim (068 step 1) — the body lives in
        :meth:`maestro.loop.pr_remediation.PrRemediationController.gate_stood_down`.
        """
        return await self._pr_remediation.gate_stood_down(
            goal, pr_branch, source,
        )

    async def _gate_pr_remediation_merge_block(
        self, goal: str, pr_branch: str | None, source: TaskSource,
    ) -> bool:
        """Skip SDK spend on a PR whose blocker Tend PR clears mechanically.

        Delegating shim (068 step 1) — the body lives in
        :meth:`maestro.loop.pr_remediation.PrRemediationController.gate_merge_block`.
        """
        return await self._pr_remediation.gate_merge_block(
            goal, pr_branch, source,
        )

    async def _gate_pr_remediation_branch_resolved(
        self, goal: str, pr_branch: str | None, source: TaskSource,
    ) -> bool:
        """Stop the wheel on a PR that keeps resolving-on-branch without landing.

        Delegating shim (068 step 1) — the body lives in
        :meth:`maestro.loop.pr_remediation.PrRemediationController.gate_branch_resolved`.
        """
        return await self._pr_remediation.gate_branch_resolved(
            goal, pr_branch, source,
        )

    def _record_remediation_fetch_truth(
        self, task_id: str, pr_num: int, pr_branch: str,
    ) -> bool:
        """Delegate to :meth:`PrRemediationController._record_remediation_fetch_truth`."""
        return self._pr_remediation._record_remediation_fetch_truth(task_id, pr_num, pr_branch)

    def _record_remediation_obligation_fork(
        self, task_id: str, pr_num: int, pr_branch: str,
    ) -> None:
        """Delegate to :meth:`PrRemediationController._record_remediation_obligation_fork`."""
        return self._pr_remediation._record_remediation_obligation_fork(task_id, pr_num, pr_branch)

    @staticmethod
    def _git_pr_merged(pr_num: int) -> bool:
        """Return True when git-merge-truth shows PR #pr_num reached ``main``.

        Thin wrapper over :func:`audit_checks_phantom._git_pr_merged_default`: searches
        the commits reachable from ``origin/main`` for one whose subject
        references ``#<pr_num>`` (GitHub merge / squash commits embed the PR
        number). The remediation-closure stamp gate consults it as an
        independent confirmation that a cleanly-mergeable branch actually
        landed, so a resolution that exists only on-branch is not stamped
        ``success`` — anchoring the gate to the shared git-merge-truth oracle.
        Best-effort: any git failure returns ``False`` (treats the PR as
        not-merged), matching the fail-closed direction of the gate. Zero SDK
        cost; the only I/O is one bounded git subprocess.
        """
        return _git_pr_merged_default(pr_num)

    @staticmethod
    def _frame_remediation_note(
        summary: str, key_findings: str | None, note: str,
    ) -> tuple[str, str]:
        """Prepend the host-verdict *note* to *summary* and *key_findings*.

        ``summary`` keeps its 500-char cap. ``key_findings`` may be ``None`` or
        empty (``_distill_findings`` can return ``None``), in which case the
        note alone becomes the findings. Pure/sync so the framing branches are
        unit-testable without driving a full ``run()`` (#2457).
        """
        framed_summary = f"{note} {summary}"[:500]
        framed_findings = f"{note} {key_findings}" if key_findings else note
        return framed_summary, framed_findings

    @staticmethod
    def _cap_task_summary(
        joined: str, branch_suffix: str, note_reserve: int,
    ) -> str:
        """Build a task ``result_summary`` under the shared 500-char budget.

        ``available = max(0, 500 - len(branch_suffix) - note_reserve)`` leaves
        room for the trailing *branch_suffix* and any remediation note the later
        ``_frame_remediation_note`` prepend reserves (#2457). Both branches honor
        it: real output is sliced (``joined[:available]``), and the empty-output
        fallback drops the 9-char ``"No output"`` literal when the budget cannot
        hold it (``available < 9``), so the fallback no longer emits
        ``"No output" + branch_suffix`` over-budget while the sibling output path
        stays capped (#353). With room (``available >= 9``, short/absent branch)
        the ``"No output"`` literal is retained, preserving Bug-74 behavior.
        Pure/sync so the budget branches are unit-testable without driving a full
        ``run()`` — mirroring :meth:`_frame_remediation_note`.
        """
        available = max(0, 500 - len(branch_suffix) - note_reserve)
        body = joined[:available] if joined else ("No output" if available >= 9 else "")
        return body + branch_suffix

    async def _probe_pr_merge_block(
        self, pr_num: int, pr_branch: str | None,
    ) -> tuple[bool, str]:
        """Probe a PR's host-side terminal state from already-available signals.

        Returns ``(landed_ok, reason)``. ``landed_ok`` is ``True`` only when the
        PR has merged, or is cleanly mergeable with the resolution already on
        ``origin/<branch>`` — i.e. no deterministic merge blocker, the origin
        branch ref exists, no local-only commits, and the host does not report
        the branch as conflicting.
        ``reason`` is the deterministic :func:`classify_merge_block` code
        (:data:`MERGE_BLOCK_NONE` when nothing blocks). Zero SDK cost; reuses the
        same host / VCS signals as :meth:`_escalate_stuck_pr` so the stamp gate
        (#2457) and the escalation path agree on one host verdict. A
        ``BEHIND_MAIN`` verdict goes through :func:`apply_landing_model`
        (#4307) and, under ``[loop] merge_block_local_probe_enabled``, a local
        ``git merge-tree`` conflict probe (080).
        """
        code_host = self.evolution.code_host
        if code_host.is_pr_merged(pr_num):
            return True, MERGE_BLOCK_NONE
        commits_behind = 0
        merge_state = ""
        mergeable = ""
        unpushed = 0
        origin_ref_present = True
        probe_ref: str | None = None
        if pr_branch:
            probe_ref = self._pr_head_ref(pr_branch)
            # Published head, as the merge-tree probe reads (#4371).
            _ahead, commits_behind = self._vcs.commits_ahead_behind(
                probe_ref, "origin/main",
            )
            # Local-only resolution commits never reach the host until Tend PR
            # pushes; a branch ahead of its own origin ref has not landed. A
            # missing ``origin/<branch>`` ref means the branch was never pushed
            # at all, so the resolution is definitively unlanded — check it
            # explicitly because ``commits_ahead_behind`` collapses both an
            # unresolvable ref and a genuinely-even branch to ``(0, 0)``, so the
            # ``unpushed == 0`` signal alone would fail open (#2457).
            origin_ref_present = probe_ref != pr_branch
            unpushed, _behind = self._vcs.commits_ahead_behind(
                pr_branch, f"origin/{pr_branch}",
            )
            pr_states = code_host.get_open_evolution_pr_states("maestro/evolve/")
            state = pr_states.get(pr_branch)
            if state is not None:
                merge_state = state.merge_state_status
                mergeable = state.mergeable
        check_conclusion = code_host.get_pr_check_conclusion(pr_num)
        reason = classify_merge_block(
            commits_behind, check_conclusion, merge_state,
        )
        reason = apply_landing_model(
            reason, tend_catch_up=self.config.tend_main_merge_enabled,
            cleanly_mergeable=not self._behind_conflicts(reason, probe_ref),
            check_conclusion=check_conclusion, merge_state_status=merge_state,
        )
        # Refine with host/VCS signals the classifier never reads; see
        # ``refine_merge_block_reason`` (#2521, #4096, #2457).
        reason = refine_merge_block_reason(
            reason, mergeable=mergeable, check_conclusion=check_conclusion,
            unpushed_or_missing=not origin_ref_present or unpushed > 0,
        )
        # Conflict-aware classification (080): Tend can only catch up a branch
        # that merges cleanly and the host fields flap, so a local
        # ``git merge-tree`` conflict reclassifies BEHIND_MAIN as CONFLICTING.
        if (
            reason == MERGE_BLOCK_BEHIND_MAIN
            and probe_ref
            and self.config.merge_block_local_probe_enabled
            and self._vcs.merge_tree_conflicts(probe_ref, "origin/main")
        ):
            reason = MERGE_BLOCK_CONFLICTING
        landed_ok = reason == MERGE_BLOCK_NONE
        return landed_ok, reason

    def _refine_ci_review_blocker(
        self, pr_num: int, pr_branch: str | None, block_reason: str,
    ) -> str:
        """Delegate to :meth:`PrRemediationController._refine_ci_review_blocker`."""
        return self._pr_remediation._refine_ci_review_blocker(pr_num, pr_branch, block_reason)

    async def _escalate_stuck_pr(
        self, pr_num: int, pr_branch: str | None,
        *, items_remaining: int | None = None,
    ) -> None:
        """Delegate to :meth:`PrRemediationController._escalate_stuck_pr`."""
        return await self._pr_remediation._escalate_stuck_pr(pr_num, pr_branch, items_remaining=items_remaining)

    def _remediation_streak_capped(self) -> bool:
        """Delegate to :meth:`PrRemediationController._remediation_streak_capped`."""
        return self._pr_remediation._remediation_streak_capped()

    def _pending_pr_head_stalled(
        self,
        entry: PendingPRRequest,
        recent: list[TaskRecord] | None = None,
    ) -> bool:
        """Delegate to :meth:`PrRemediationController._pending_pr_head_stalled`."""
        return self._pr_remediation._pending_pr_head_stalled(entry, recent)

    def _mechanical_standdown_verdict(
        self, entry: PendingPRRequest,
    ) -> StandDownVerdict | None:
        """Delegate to :meth:`PrRemediationController._mechanical_standdown_verdict`."""
        return self._pr_remediation._mechanical_standdown_verdict(entry)

    def _dispose_mechanical_standdown(self, model: SelfModel) -> bool:
        """Delegate to :meth:`PrRemediationController._dispose_mechanical_standdown`."""
        return self._pr_remediation._dispose_mechanical_standdown(model)

    def _owned_act_drought_findings(
        self, plans: list[EvolutionPlan],
    ) -> list[audit_checks.AuditFinding]:
        """Delegate to :meth:`PrRemediationController._owned_act_drought_findings`."""
        return self._pr_remediation._owned_act_drought_findings(plans)

    def _owned_act_drought_active(self) -> bool:
        """Delegate to :meth:`PrRemediationController._owned_act_drought_active`."""
        return self._pr_remediation._owned_act_drought_active()

    def _preempt_yielded_slot_with_owned_prune(self) -> None:
        """Delegate to :meth:`PrRemediationController._preempt_yielded_slot_with_owned_prune`."""
        return self._pr_remediation._preempt_yielded_slot_with_owned_prune()

    def _first_non_stalled_pr_index(
        self, entries: list[PendingPRRequest], recent: list[TaskRecord] | None = None,
    ) -> int | None:
        """Delegate to :meth:`PrRemediationController._first_non_stalled_pr_index`."""
        return self._pr_remediation._first_non_stalled_pr_index(entries, recent)

    async def _run_task_phase(self) -> None:
        """Pick the next goal and execute it.

        Priority order: ``pending_user_requests`` → ``pending_pr_requests``
        → queue head. Consecutive ``pending_pr_requests`` selections are
        capped by ``[loop] max_consecutive_remediation`` (``<= 0`` disables):
        once the cap is hit the slot bypasses remediation and falls through
        to the queue head *when one exists*, breaking the
        remediation-monoculture; an empty queue leaves remediation eligible
        (#4047). The chosen
        entry is removed from its source and
        persisted *before* :meth:`run` is invoked so a crash mid-execution
        does not re-run the same item on restart. External sources
        (user / PR requests) yield ``source="external"`` and
        ``linked_question=None``; queue items yield
        ``source="self_directed"`` and forward the queued
        ``linked_question``.

        A ``pending_pr_requests`` head that
        :meth:`_dispose_mechanical_standdown` finds is a stalled, thread-clear,
        git-unlanded lineage (clearability governed by the salvage oracle) is
        disposed in loop code: the ``[stood-down]`` verdict is authored, a
        ``partial`` no-op recorded, and the entry popped — no SDK session —
        before the normal remediation route is even considered. A stalled head
        that does *not* qualify for the mechanical stand-down (an unobservable
        merge state, an unserviced thread, a salvage veto)
        is *not* consumed by :meth:`_pending_pr_head_stalled`
        (``>= STALLED_REMEDIATION_LAP_CAP`` same-SHA ``partial`` laps): it stays
        queued while the slot goes to the first non-stalled entry behind it
        (:meth:`_first_non_stalled_pr_index`), so a healthy PR is no longer
        starved by a stuck head (#2837), nor by a CI-held one (#4064). Only
        when *every* entry is stalled or held does
        the chain fall through to the self-directed queue head this iteration.
        All auto-release when the PR head advances or the PR merges/closes.

        Owned-act preemption (``owned_act_preempt_enabled``, default off): when
        that stalled-head yield is about to hand the slot to a healthy
        remediation entry (``pr_index > 0``) *and* the rule-18 owned-act drought
        is armed (:meth:`_owned_act_drought_active`), the yielded slot is instead
        redirected to the existing zero-SDK owned prune
        (:meth:`_preempt_yielded_slot_with_owned_prune`) so a demonstrably-stuck
        cycle moves net ``pending_review`` rather than re-lapping an
        externally-addressed PR. Self-extinguishes on one owned act in-window.
        """
        model = self.consciousness.load_self_model()
        source: TaskSource
        goal: str
        linked_question: str | None = None
        pr_branch: str | None = None
        # Only queued tasks currently carry binding success criteria; the
        # external-request schemas (``pending_user_requests`` /
        # ``pending_pr_requests``) are bare ``list[str]`` and have no
        # criteria channel. Forward whatever the queued entry recorded so
        # the criteria-anchored outcome classification path in
        # :meth:`run` is reachable from the only production task entry
        # point.
        acceptance_criteria: list[str] | None = None
        # Only queued tasks carry the conscious-access citation (proposal 130);
        # the external-request / remediation paths have no queued-task
        # provenance and leave these ``None``. Forwarded to :meth:`run` from the
        # queue-head branch so the citation rides onto the completed
        # ``TaskRecord``.
        workspace_moment_id: str | None = None
        workspace_item_ids: list[str] | None = None
        queue_head_fallthrough = False
        # Resolve which ``pending_pr_requests`` entry (if any) takes this slot
        # *before* the selection chain so the chain stays a flat if/elif/else
        # rather than relying on a walrus inside the condition. The scan runs only when a remediation slot is actually
        # in contention — there is no pending user request (which has strict
        # precedence) and the consecutive-remediation cap is not engaged — so
        # its per-entry ``git rev-parse`` probes stay off the user-request and
        # throttled paths exactly as the old short-circuited ``elif`` did. A
        # non-``None`` index is the first entry that is not a stalled lineage;
        # ``None`` means there is no consumable remediation entry (no requests,
        # capped, or every entry stalled) and the chain falls through to the
        # self-directed queue.
        #
        # WIP-primacy cap exemption (092): the consecutive-remediation cap
        # exists to force task-shape diversity, but that diversity must never
        # evict the loop *off its own unfinished work*. When an open own PR
        # carries a self-clearable blocker (``CONFLICTING`` /
        # ``THREADS_UNRESOLVED``), ``_wip_owns_slot`` reports owned WIP and
        # finishing it outranks diversity — so clear the engaged cap and keep
        # serving remediation instead of falling through to the queue head. The
        # conjunction is cap-first so the WIP probe runs only when the cap is
        # actually engaged (cheap-first / oracle-last); ``_wip_owns_slot``
        # itself short-circuits on ``wip_primacy_enabled``, so the flag-off path
        # never calls it and stays byte-identical to today.
        #
        # The cap diverts only to an *existing* queue head (#4047): with an
        # empty queue (e.g. ``self_directed_tasks_enabled = false``) there is
        # nothing to divert to, and engaging it would starve remediation.
        cap_engaged = (
            self._remediation_streak_capped()
            and self.task_queue.head() is not None
        )
        if cap_engaged and self._wip_owns_slot():
            cap_engaged = False
        pr_index: int | None = None
        recent_tasks: list[TaskRecord] | None = None
        if (
            not model.pending_user_requests
            and model.pending_pr_requests
            and not cap_engaged
        ):
            recent_tasks = self.memory.get_recent_tasks(30)
            pr_index = self._first_non_stalled_pr_index(
                model.pending_pr_requests, recent_tasks,
            )
        # Owned-act preemption of the yielded stalled-remediation slot
        # (``owned_act_preempt_enabled``). ``pr_index > 0`` is exactly the #2837
        # yield: the head (index 0) is a stalled lineage and its slot is about to
        # go to a healthy remediation entry *behind* it. When the rule-18
        # owned-act drought is armed (zero owned terminal acts in-window while a
        # prunable ``pending_review`` backlog sits), that painless contested
        # re-lap wins a slot my owned prune never gets — the precedence bug this
        # flag fixes. Redirect the yielded slot to the owned mechanical prune
        # instead, moving net ``pending_review`` now. The drought probe (bounded
        # file reads) runs only when the flag is on *and* a real yield is in
        # contention, so the flag-off / no-yield paths stay byte-identical. It
        # self-extinguishes the instant one owned act lands in the window.
        owned_act_preempt = (
            self.config.owned_act_preempt_enabled
            and pr_index is not None
            and pr_index > 0
            and self._owned_act_drought_active()
        )
        if model.pending_user_requests:
            if (req := self.consciousness.dequeue_user_request()) is None:
                return
            goal = req
            source = "external"
            # Not remediation: resets the streak.
            self._consecutive_remediation = 0
            # Human-PR-message routing (proposal 091): when the drained message
            # names one of Maestro's own open ``maestro/evolve/*`` PRs, route it
            # as a full PR-remediation task (branch checkout, reply manifest,
            # direct push) instead of a generic one, releasing that PR's
            # stand-down verdicts and escalation standdown first so a follow-up
            # tend lap cannot re-mute the human demand. A ``None`` result (no PR,
            # or a settled/foreign PR) falls through to today's generic path,
            # where ``run()``'s ``_preflight_merge_truth_standdown`` still stands
            # down on an already-settled PR. Behind the rollback flag.
            if self.config.user_pr_message_remediation_enabled:
                routed = self._pr_remediation.route_user_pr_message(goal)
                if routed is not None:
                    goal, pr_branch = routed
        elif (
            model.pending_pr_requests
            and self._dispose_mechanical_standdown(model)
        ):
            # Mechanical chosen-stand-down (subtractive): the head
            # ``pending_pr_requests`` entry was a stalled, thread-clear,
            # git-unlanded remediation lineage the salvage oracle let go of.
            # ``_dispose_mechanical_standdown`` has popped it, authored the
            # durable ``[stood-down]`` verdict, and recorded a ``partial`` no-op
            # — all without an SDK session.
            # Higher precedence than the normal remediation route (so a stalled
            # disposable PR is disposed before it can be re-lapped at
            # the pop), but below user requests. The brake auto-releases when the
            # PR head advances or it merges/closes.
            return
        elif owned_act_preempt:
            # Owned-act preemption (``owned_act_preempt_enabled``): a stalled head
            # that did not qualify for the mechanical stand-down above would
            # yield its slot to a healthy remediation entry behind it (#2837),
            # but the rule-18 owned-act drought is armed — so re-lapping that
            # externally-addressed PR is the painless act that keeps winning the
            # slot my owned prune never gets. Redirect this yielded slot to the
            # existing zero-SDK owned prune instead, moving net ``pending_review``
            # rather than counting the fire. Higher precedence than the normal
            # remediation route so the yield is intercepted; below user requests
            # and the mechanical stand-down. Nothing to run this iteration — the
            # prune is mechanical and self-extinguishes the drought.
            self._preempt_yielded_slot_with_owned_prune()
            return
        elif pr_index is not None:
            # First non-stalled ``pending_pr_requests`` entry (#2837): a stalled
            # head that did not qualify for the mechanical stand-down above stays
            # queued while a healthy PR behind it takes the slot instead of being
            # starved. ``pr_index`` was resolved before this chain (only when a
            # remediation slot was actually in contention) and is unaffected by
            # the mechanical stand-down, which returns above when it fires.
            #
            # CI-pending-hold yield (before the pop, so nothing is evicted and no
            # streak is counted): when the selected owned PR's sole live blocker
            # is CI still running — no conflict, no failing gate, no unaddressed
            # AI-finding thread — there is nothing to fix. Leave the PR queued and
            # yield the slot so CI concludes on its own clock; opening a session
            # here would only reconcile-push and reset the very CI clock we wait
            # on. The PR stays fully tend-eligible and a later pop re-evaluates
            # once CI concludes.
            # Held entries pass the slot on (#4064).
            unheld_index = self._pr_remediation.next_unheld_pr_index(
                model.pending_pr_requests, pr_index, recent_tasks,
            )
            if unheld_index is None:
                queue_head_fallthrough = True
            else:
                live = self.consciousness.dequeue_pr_request(
                    model.pending_pr_requests[unheld_index])
                if live is None:  # vanished (#4403)
                    return
                entry = live
                goal = entry.directive
                pr_branch = entry.pr_branch
                source = "external"
                # Streak cap: unlanded PRs cannot consume every slot.
                self._consecutive_remediation += 1
                # Dequeue-face gates (068 step 1); True: no routed task.
                if await self._pr_remediation.dequeue_gates(
                    goal=goal, pr_branch=pr_branch, source=source, entry=entry,
                ):
                    return
        else:
            queue_head_fallthrough = True

        if queue_head_fallthrough:
            head = self.task_queue.pop_head()
            if head is None or drop_failed_branch_head(
                self, head,
            ):
                return
            goal = head.goal
            linked_question = head.linked_question
            acceptance_criteria = head.acceptance_criteria
            workspace_moment_id = head.workspace_moment_id
            workspace_item_ids = head.workspace_item_ids
            source = "self_directed"
            # Queue-head tasks are non-remediation; reset the streak so the
            # cap measures only *consecutive* remediation slots.
            self._consecutive_remediation = 0

        if not goal or not goal.strip():
            return

        # --- Outcome prediction (zero SDK cost) ---
        # Predict the task outcome based on shape-specific historical
        # success rate *before* execution so the prediction is blind to
        # the actual result. Recording happens after run() returns so the
        # real task_id from the TaskRecord is used for later join.
        recent_for_pred = self.memory.get_recent_tasks(30)
        predicted_outcome = predict_task_outcome(
            [asdict(t) for t in recent_for_pred], goal,
        )

        logger.info(
            "run_task: executing %s task: %s (anchored to: %s, pr_branch: %s)",
            source, goal[:100],
            linked_question or "<unanchored>",
            pr_branch or "<none>",
        )
        # Capture the PR branch HEAD before remediation so a run that produces
        # no new commit can be detected as a no-op deterministically, blind to
        # how the LLM framed its summary (#2440). Falls back to the remote head
        # because ``run()`` creates the local branch from ``origin/<branch>``
        # when it is not yet checked out locally.
        remediation_head_before: str | None = None
        defer_persist = (
            pr_branch is not None and goal.startswith(_REMEDIATION_DEDUP_PREFIX)
        )
        if pr_branch is not None and defer_persist:
            remediation_head_before = self._vcs.rev_parse(
                pr_branch,
            ) or self._vcs.rev_parse(f"origin/{pr_branch}")
        # Advisory premise report (zero SDK cost): a self-directed goal that
        # names ``.py`` files missing from the tree gets a ``## PREMISE
        # REPORT`` block so the planner can adapt — a create-new-file goal
        # legitimately references a not-yet-existing path, so this surfaces
        # the drift rather than dropping the goal. It rides the advisory
        # ``context`` channel, NOT the ``goal`` string: ``goal`` is recorded
        # verbatim as ``TaskRecord.goal`` and feeds the goal-shape sensors
        # (``classify_task_shape`` → ``format_task_shape_summary`` /
        # ``predict_task_outcome``), and the report boilerplate's word
        # "create" would flip the recorded shape to ``construction``,
        # durably skewing the self-measurement instruments. ``context``
        # reaches the same planner (``decompose`` / ``replan``) but is never
        # recorded. Issue-citing goals keep their generation-time
        # ``terrain_exhaustion`` drop; this closes the gap for self-directed
        # goals that cite no issue number (#2336).
        task_context: str = source
        if source == "self_directed":
            premise_report = self._build_premise_report(goal)
            if premise_report is not None:
                logger.info(
                    "run_task: attaching premise report for self-directed "
                    "goal (references files missing from the tree)",
                )
                task_context = f"{source}\n\n{premise_report}"
        record = await self.run(
            goal,
            context=task_context,
            source=source,
            linked_question=linked_question,
            pr_branch=pr_branch,
            acceptance_criteria=acceptance_criteria,
            workspace_moment_id=workspace_moment_id,
            workspace_item_ids=workspace_item_ids,
            defer_persist=defer_persist,
        )

        await finish_task_phase(
            self, record, predicted_outcome, goal=goal, pr_branch=pr_branch,
            head_before=remediation_head_before, defer_persist=defer_persist,
        )

    def _refuse_owned_push_standdown(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
        ahead_count: int,
    ) -> None:
        """Delegate to :meth:`PrRemediationController._refuse_owned_push_standdown`."""
        return self._pr_remediation._refuse_owned_push_standdown(pr_num, pr_branch, record, ahead_count)

    def _commit_all(self, message: str) -> None:
        """Delegate to :meth:`PrRemediationController._commit_all`."""
        return self._pr_remediation._commit_all(message)

    def _run_session_landing(
        self, pr_branch: str, pr_num: int | None,
    ) -> LandOutcome:
        """Delegate to :meth:`PrRemediationController._run_session_landing`."""
        return self._pr_remediation._run_session_landing(pr_branch, pr_num)

    def _push_owned_branch(self, branch: str) -> _OwnedPushResult:
        """Delegate to :meth:`PrRemediationController._push_owned_branch`."""
        return self._pr_remediation._push_owned_branch(branch)

    def _fetch_remote_branch_ok(self, branch: str) -> bool:
        """Delegate to :meth:`PrRemediationController._fetch_remote_branch_ok`."""
        return self._pr_remediation._fetch_remote_branch_ok(branch)

    def _record_owned_push_delivered(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
        settled: MergeTruth,
    ) -> None:
        """Delegate to :meth:`PrRemediationController._record_owned_push_delivered`."""
        return self._pr_remediation._record_owned_push_delivered(pr_num, pr_branch, record, settled)

    def _record_push_unconfirmed(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
    ) -> None:
        """Delegate to :meth:`PrRemediationController._record_push_unconfirmed`."""
        return self._pr_remediation._record_push_unconfirmed(pr_num, pr_branch, record)

    def _branch_has_uncommitted_owned_edit(self, branch: str) -> bool:
        """Delegate to :meth:`PrRemediationController._branch_has_uncommitted_owned_edit`."""
        return self._pr_remediation._branch_has_uncommitted_owned_edit(branch)

    def _is_checked_out(self, branch: str) -> bool:
        """Delegate to :meth:`PrRemediationController._is_checked_out`."""
        return self._pr_remediation._is_checked_out(branch)

    def _attribute_owned_gate_red(
        self, review: QualityGateResult,
    ) -> Attribution | None:
        """Delegate to :meth:`PrRemediationController._attribute_owned_gate_red`."""
        return self._pr_remediation._attribute_owned_gate_red(review)

    def _land_uncommitted_owned_edit(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
    ) -> None:
        """Delegate to :meth:`PrRemediationController._land_uncommitted_owned_edit`."""
        return self._pr_remediation._land_uncommitted_owned_edit(pr_num, pr_branch, record)

    def _branch_is_owned_writable(self, branch: str) -> bool:
        """Delegate to :meth:`PrRemediationController._branch_is_owned_writable`."""
        return self._pr_remediation._branch_is_owned_writable(branch)

    def _push_committed_unpushed_owned_head(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
    ) -> bool:
        """Delegate to :meth:`PrRemediationController._push_committed_unpushed_owned_head`."""
        return self._pr_remediation._push_committed_unpushed_owned_head(pr_num, pr_branch, record)

    def _settle_committed_unpushed_head(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
        blocker: str,
        ahead_count: int,
        review: QualityGateResult,
    ) -> bool:
        """Delegate to :meth:`PrRemediationController._settle_committed_unpushed_head`."""
        return self._pr_remediation._settle_committed_unpushed_head(pr_num, pr_branch, record, blocker, ahead_count, review)

    async def _record_remediation_outcome(
        self,
        *,
        goal: str,
        pr_branch: str | None,
        record: TaskRecord | None,
        head_before: str | None,
    ) -> None:
        """Delegate to :meth:`PrRemediationController._record_remediation_outcome`."""
        return await self._pr_remediation._record_remediation_outcome(goal=goal, pr_branch=pr_branch, record=record, head_before=head_before)

    # ------------------------------------------------------------------
    # Registered phase runners (proposal 106)
    #
    # Every ``_LOOP_PHASES`` member's runner, dispatched by one lookup in
    # the data-driven ``self._phase_runners`` table.
    # The persistent-session phases (``_phase_contemplate`` /
    # ``_phase_evolve`` / ``_phase_curate_pending_plans``) share the promoted
    # ``self._session_a`` / ``self._session_b`` the loop threads across
    # phases. Every runner shares the ``(self, *, iteration: int) ->
    # None`` signature so the dispatch can call any of them uniformly; only
    # ``_phase_tend_evolution_prs`` (its ``_reconcile_settled_tend_prs`` tail),
    # ``_phase_check_merged_plans`` (its ``_reconcile_settled_merge_check``
    # tail), ``_phase_audit_self_model`` (forwarded to ``_audit_self_model``),
    # and ``_phase_contemplate`` (forwarded to ``contemplate``) use
    # ``iteration``.
    # ``REGISTERED_PHASE_NAMES`` in ``maestro/loop/phases.py`` pins the set
    # and its order.
    # ------------------------------------------------------------------

    async def _phase_ensure_main_branch(self, *, iteration: int) -> None:
        # Ensure we're on main before normal operations —
        # guards against stale evolution branches from
        # crashes or suppressed checkout errors.
        self.evolution.ensure_main_branch()

    async def _phase_check_contributions(self, *, iteration: int) -> None:
        self.contributions.check_for_contributions(
            self.journal,
        )

    async def _phase_process_messages(self, *, iteration: int) -> None:
        self._iter_message_context = await self._process_messages()

    async def _phase_tend_evolution_prs(self, *, iteration: int) -> None:
        # PR tending is purely mechanical — fetch,
        # merge ``origin/main``, run subprocess
        # quality gates, send reminders, reconcile
        # orphan branches.  Conflicts and broken
        # post-merge gates route into
        # ``pending_pr_requests`` via
        # ``_route_pr_feedback_to_pending_requests``
        # so the remediation work runs through the
        # ``run_task`` phase on the next iteration.
        # No SDK session opens here; no burn-rate gate.
        tend_result = await self.evolution.tend_evolution_prs(
            settled_oracle=self.is_pr_settled,
        )
        # Only refresh the open-PR count when the tend
        # pass actually enumerated upstream PRs —
        # ``enumerated`` flips once the open-PR
        # listing succeeds.  Per-branch failures
        # (reminder / merge / push) append to
        # ``errors`` *after* counting, so they must
        # not discard a valid count; the
        # pre-enumeration short-circuits (fetch /
        # tamper / list-PR failures) leave ``tended``
        # at a transient 0 that would silently bypass
        # the integrate-before-create gate on the
        # very next iteration.
        if tend_result.enumerated:
            self._open_evolution_pr_count = (
                tend_result.tended
            )
        self._route_pr_feedback_to_pending_requests(
            tend_result,
        )
        # On-settlement record reconciliation (proposal
        # 103): a PR that left the open evolution-PR set
        # this lap — actively remediated, then settled
        # while its plan had already left ``implemented``
        # (so ``check_merged_plans`` and the audit-sweep
        # memo both miss it) — has all six of its PR-keyed
        # stores reconciled here through the single
        # flag-gated helper, matching how the
        # ``check_merged_plans`` phase drives
        # ``_reconcile_settled_merge_check``. Flag-gated
        # inside the helper, so a production no-op until
        # an operator enables it.
        self._reconcile_settled_tend_prs(
            tend_result, iteration,
        )

    async def _phase_attend(self, *, iteration: int) -> None:
        # Conscious-access workspace, observation only (proposal 130). A zero-SDK
        # phase gated on ``conscious_workspace_observe_enabled``: it gathers
        # candidates from facts this iteration has *already* materialised,
        # assembles exactly one immutable ``ConsciousMoment``, and appends one
        # episode to the ledger. It reaches no host, git or subprocess seam, and
        # injects the moment nowhere — no prompt changes, no decision changes, no
        # gate consults it. Attention is not authorization.
        now = datetime.now(UTC).isoformat()
        self_model = self.consciousness.load_self_model()
        # ``self._iter_diag_snapshot`` is built once per fresh iteration for the
        # status update and reused here; ``None`` on a cold mid-iteration resume,
        # in which case the diagnostic adapter is skipped and the gap is declared
        # a blind spot below.
        snapshot = self._iter_diag_snapshot
        audit_result = self.memory.get_most_recent_audit()
        task_records = self.memory.get_recent_tasks()
        live_plans = self.memory.get_live_plans()
        queued_tasks = self.task_queue.load()
        # The ninth candidate owner: reflection bets the calibration oracle has
        # already scored (the adapter admits only ``measured`` verdicts).
        scored_bets = workspace_candidates.load_prediction_verdict_inputs(
            self.predictions,
        )
        # What this observe-only pass did not sample, declared rather than
        # silently omitted; the vocabulary is owned by the adapter module.
        blind_spots = workspace_candidates.declare_blind_spots(
            snapshot=snapshot,
            audit_result=audit_result,
            scored_bets=scored_bets,
        )

        candidates = workspace_candidates.gather_candidates(
            self_model=self_model,
            snapshot=snapshot,
            audit_result=audit_result,
            task_records=task_records,
            now=now,
            task_focus=None,
            live_plans=live_plans,
            queued_tasks=queued_tasks,
            queue_cap=self.config.task_queue_max,
            burn_rate_throttle_active=self.burn_rate_throttle_active,
            maintenance_mode_active=self.maintenance_mode_active,
            throttle_entered_at=self.budget_throttle_entered_at,
            scored_bets=scored_bets,
        )

        # Predecessor link + continuity kind, classified honestly (step 7):
        # no predecessor -> genesis; else this process's startup reported a crash
        # -> crash_gap (one-shot); else the predecessor is the restored active
        # moment -> linked; else the loop ran without recording -> disabled_interval.
        recent = self.conscious_episodes.read_recent(1)
        predecessor = recent[0] if recent else None
        predecessor_id: str | None
        predecessor_source_refs: frozenset[tuple[str, str]]
        continuity: conscious_workspace.ContinuityKind
        if predecessor is None:
            predecessor_id = None
            predecessor_source_refs = frozenset()
            continuity = "genesis"
        else:
            predecessor_id = predecessor.moment.moment_id
            predecessor_source_refs = frozenset(
                (item.source_kind, item.source_ref)
                for item in predecessor.moment.items
            )
            if self._pending_crash_gap:
                continuity = "crash_gap"
            elif predecessor_id == self._active_conscious_moment_id:
                continuity = "linked"
            else:
                continuity = "disabled_interval"
        # Self-extinguishing: the crash marker is consumed on the first ``attend``
        # of the process regardless of branch, so it never re-fires.
        self._pending_crash_gap = False

        moment = conscious_workspace.assemble_moment(
            candidates,
            assembled_at=now,
            permission_summary=_CONSCIOUS_PERMISSION_SUMMARY,
            blind_spots=blind_spots,
            predecessor_id=predecessor_id,
            continuity=continuity,
            predecessor_source_refs=predecessor_source_refs,
        )
        # ``receipts`` / ``cited_task_ids`` / ``cited_plan_ids`` belong to later
        # wiring (broadcast, decision citation); they land empty now so the ledger
        # row shape is settled. One clock read serves both the moment's
        # ``assembled_at`` and the episode's ``recorded_at``.
        episode = conscious_workspace.ConsciousEpisode(
            moment=moment,
            receipts=(),
            cited_task_ids=(),
            cited_plan_ids=(),
            recorded_at=now,
        )
        self.conscious_episodes.append(episode)
        self._active_conscious_moment_id = moment.moment_id
        # Retain the full moment for this iteration's broadcast (proposal 130) and
        # reset the per-moment receipt list; ``_broadcast_moment_prefix`` renders
        # from this object and appends a receipt per delivered consumer.
        self._active_conscious_moment = moment
        self._active_broadcast_receipts = []
        # Reset the per-iteration cited-plan and cited-task accumulators
        # (proposal 130) alongside the receipt list, so a plan or task citing the
        # *previous* moment never leaks into this moment's finalized episode.
        self._active_cited_plan_ids = []
        self._active_cited_task_ids = []

    def _resolve_active_moment(
        self,
    ) -> conscious_workspace.ConsciousMoment | None:
        """Return this iteration's active moment, or ``None`` (proposal 130).

        Prefers the in-memory ``self._active_conscious_moment`` retained by
        ``_phase_attend``; falls back to reconstructing it from the episode ledger
        (``read_recent(1)``) when the in-memory moment is gone but its id was
        restored into ``self._active_conscious_moment_id`` on a cold mid-iteration
        resume, and only when the newest ledger row corroborates that id. Returns
        ``None`` when neither source yields a moment — and, when no id is restored,
        never touches the ledger. Shared by ``_broadcast_moment_prefix`` (per-consumer
        delivery) and ``_finalize_conscious_episode`` (end-of-iteration persistence).
        """
        moment = self._active_conscious_moment
        if moment is None and self._active_conscious_moment_id is not None:
            recent = self.conscious_episodes.read_recent(1)
            if recent and recent[0].moment.moment_id == self._active_conscious_moment_id:
                moment = recent[0].moment
        return moment

    def _deliver_moment_to_consumer(
        self, consumer: conscious_workspace.ConsumerName,
    ) -> tuple[str, conscious_workspace.ConsciousMoment, frozenset[str]] | None:
        """Render + deliver the active moment to *consumer*, or ``None`` (proposal 130).

        The sole reader of ``conscious_workspace_broadcast_enabled`` and the sole
        writer of ``self._active_broadcast_receipts`` this increment. Off, or with
        no active moment, returns ``None`` so the caller can fall back to its
        byte-identical no-broadcast path. On, it resolves the active moment via
        :meth:`_resolve_active_moment` (the in-memory moment, falling back to a
        ledger reconstruction on a cold mid-iteration resume), renders it via
        :func:`conscious_workspace.render_active_moment`, and records one
        :class:`~maestro.conscious_workspace.BroadcastReceipt` proving delivery.
        Returns ``(block, moment, visible_item_ids)`` where *visible_item_ids* is
        the admitted set the consumer actually saw — the moment's item ids **minus**
        the ids :func:`~maestro.conscious_workspace.render_active_moment` withheld —
        so a citation filter keyed off it can never accept a redacted id the
        consumer never saw. Advisory only: the block is prompt text and consults no
        gate.
        """
        if not self.config.conscious_workspace_broadcast_enabled:
            return None
        moment = self._resolve_active_moment()
        if moment is None:
            return None
        block, redacted_item_ids = conscious_workspace.render_active_moment(
            moment, consumer,
        )
        self._active_broadcast_receipts.append(
            conscious_workspace.build_broadcast_receipt(
                moment=moment,
                consumer=consumer,
                delivered_at=datetime.now(UTC).isoformat(),
                redacted_item_ids=redacted_item_ids,
            ),
        )
        # The consumer-visible admitted ids come from the single visibility owner
        # (proposal 130), the same rule ``validate_citation`` re-derives — never a
        # hand-rolled second copy — so the set handed to a producer and the set the
        # citation guard checks against can never drift.
        visible = conscious_workspace.visible_item_ids(moment, consumer)
        return block, moment, visible

    def _broadcast_moment_prefix(
        self, consumer: conscious_workspace.ConsumerName,
    ) -> str:
        """Return the active moment rendered for *consumer*, or ``""`` (proposal 130).

        Thin block-only wrapper over :meth:`_deliver_moment_to_consumer` for the
        callers that need only the prompt prefix (contemplation and task
        execution). Off, or with no active moment,
        returns the empty string so the caller's prompt is byte-identical to
        today's; on, returns the rendered block (and, as a side effect of the
        delegate, records the delivery receipt). Advisory only.
        """
        delivery = self._deliver_moment_to_consumer(consumer)
        return delivery[0] if delivery is not None else ""

    def _finalize_conscious_episode(self) -> None:
        """Persist this iteration's collected broadcast receipts (proposal 130).

        Zero-SDK, advisory/observation-only — consults no gate and grants no
        capability. Called once at the iteration-completion seam. No-op when the
        broadcast flag is off or no receipt was collected this iteration, so with
        broadcast off nothing new is written and every state write stays
        byte-identical to today's. Otherwise it resolves the active moment via
        :meth:`_resolve_active_moment` (returning if it is ``None``) and appends one
        :class:`~maestro.conscious_workspace.ConsciousEpisode` carrying that moment
        and the collected receipts, then clears the per-iteration receipt list. The
        finalized row is an append-only *supersede* of the ``attend``-time bootstrap
        row: it shares that row's ``moment_id``, so the next ``attend``'s predecessor
        read (``read_recent(1)``) still returns the correct moment. ``cited_plan_ids``
        lists the plans that cited this moment (proposal 130), order-preserved and
        de-duplicated from ``self._active_cited_plan_ids`` (empty when no plan cited
        it); ``cited_task_ids`` mirrors it for the tasks that cited this moment,
        drained the same way from ``self._active_cited_task_ids`` (empty when no
        task cited it).
        """
        if not self.config.conscious_workspace_broadcast_enabled:
            return
        if not self._active_broadcast_receipts:
            return
        moment = self._resolve_active_moment()
        if moment is None:
            return
        episode = conscious_workspace.ConsciousEpisode(
            moment=moment,
            receipts=tuple(self._active_broadcast_receipts),
            cited_task_ids=tuple(dict.fromkeys(self._active_cited_task_ids)),
            cited_plan_ids=tuple(dict.fromkeys(self._active_cited_plan_ids)),
            recorded_at=datetime.now(UTC).isoformat(),
        )
        self.conscious_episodes.append(episode)
        self._active_broadcast_receipts = []
        self._active_cited_plan_ids = []
        self._active_cited_task_ids = []

    async def _phase_respond_to_messages(self, *, iteration: int) -> None:
        # Replies MUST run at ``effort="low"``: never reuse the high-effort
        # Session A, whose ``effort="high"`` a follow-up turn inherits (fix
        # #1239); no session takes the low-effort one-shot branch. Pre-deploy
        # notices are never replied to.
        await self._respond_to_messages(
            self._iter_reply_context,
            self._iter_reflection,
        )

    async def _phase_execute_approved_publications(
        self, *, iteration: int,
    ) -> None:
        # Zero-SDK-cost executor for proposal 033.
        # Reuses the evolution engine's CodeHost
        # so ``gh`` calls share one configured
        # workspace and timeouts. Smoke + the
        # publication_enabled flag are already
        # gated by ``_phase_will_run``; if we got
        # here, it's safe to run the pipeline.
        from maestro.publication import Publisher

        publisher = Publisher(
            memory=self.memory,
            journal=self.journal,
            notifier=self.notifier,
            host=self.evolution.code_host,
            config=self.config,
            shutdown_event=self._ensure_shutdown_event(),
        )
        await publisher.tick()

    async def _phase_run_task(self, *, iteration: int) -> None:
        # No persistent SDK session here — ``run()``
        # creates its own run-scoped session at
        # effort=medium for the planner / executor /
        # distill pipeline. Loop-level running flag
        # restored after ``run()`` resets it.
        try:
            await self._run_task_phase()
        finally:
            self._running = True

    async def _phase_audit_self_model(self, *, iteration: int) -> None:
        # Zero-SDK-cost phase: runs deterministic
        # rules over (tasks, errors, self-model) and
        # persists the findings to ``audit_history.jsonl``
        # for the next contemplation to consume.
        # Failures here propagate to the iteration-level
        # handler — a rule bug or I/O error is a real
        # bug that must surface, not be swallowed.
        await self._audit_self_model(
            iteration=iteration,
        )

    async def _phase_check_merged_plans(self, *, iteration: int) -> None:
        # Offload to a worker thread: the method reads
        # merge state for N plans through the blocking
        # ``is_pr_settled`` chokepoint, and each PR not
        # already memoized this iteration costs a
        # ``git fetch origin`` plus serial ``gh`` reads
        # (merged / mergedAt / closed) — which would
        # otherwise block the event loop for the whole
        # phase (issue #197).
        check_result = await asyncio.to_thread(
            self.evolution.check_merged_plans,
            settled_oracle=self.is_pr_settled,
        )
        # Snapshot inputs are batch-stable across this
        # synchronous loop; compute once instead of N times.
        batch_snapshot: diagnostics.DiagnosticSnapshot | None = None
        batch_recent_tasks: list[TaskRecord] | None = None
        if check_result.merged and self.config.version_archive_enabled:
            try:
                batch_snapshot = diagnostics.build_diagnostic_snapshot(
                    self.config.app_log_path,
                    n_entries=500,
                    memory_dir=self.config.memory_dir,
                )
                batch_recent_tasks = self.memory.get_recent_tasks(
                    self.config.version_archive_window_iterations,
                )
            except Exception:
                logger.warning(
                    "Failed to pre-compute batch snapshot inputs; "
                    "falling back to per-plan computation",
                    exc_info=True,
                )
                batch_snapshot = None
                batch_recent_tasks = None
        for plan_id in check_result.merged:
            plan = self.memory.get_plan(plan_id)
            title = plan.title if plan else plan_id
            self.journal.record_event(
                content=f"Evolution merged: {title}",
                entry_type="milestone",
                triggered_by=f"evolution:{plan_id}",
            )
            # evolution_count tracks integrations, not
            # PRs opened: bump exactly once per plan that
            # transitions implemented -> merged. A plain
            # call (no broad except) — a local
            # read-modify-write failure is a real bug and
            # must propagate, matching the journal write.
            self.consciousness.increment_evolution()
            if plan is not None:
                self._snapshot_version(
                    plan_id,
                    snapshot=batch_snapshot,
                    recent_tasks=batch_recent_tasks,
                )
        if check_result.rejected:
            async with rejection_sessions(
                enabled=self.config.enable_session_reuse,
                identity=self._build_identity_context,
            ) as (rejection_session, self_model_session):
                for plan_id, reason in check_result.rejected:
                    plan = self.memory.get_plan(plan_id)
                    if plan:
                        await self.evolution.learn_from_rejection(
                            plan, reason,
                            session=rejection_session,
                            self_model_session=self_model_session,
                        )
                        self.memory.mark_rejection_learned(plan_id)
        # On-settlement record reconciliation (proposal
        # 103): the instant a plan's PR settles here,
        # drive the registered store reconcilers over it
        # so its residual traces are cleared at the merge
        # event rather than waiting for a later audit
        # cycle to warm-memo it. Flag-gated inside the
        # helper, so this is a production no-op until an
        # operator enables it.
        self._reconcile_settled_merge_check(
            check_result, iteration,
        )

    async def _phase_review_artifacts(self, *, iteration: int) -> None:
        # Zero-cost selection still runs under budget
        # throttle — only the LLM classification call
        # inside ``review()`` is gated via
        # ``budget_throttle_active`` so the candidate
        # list is always fresh for the next cycle.
        # ``host`` + ``notifier`` enable the 032
        # publication-proposal emission path; the
        # reviewer short-circuits cleanly when the
        # [publication] enabled flag is off.
        from maestro.artifact_review import ArtifactReviewer

        reviewer = ArtifactReviewer(
            self.memory,
            self.journal,
            self.config,
            host=self.evolution.code_host,
            notifier=self.notifier,
        )
        # Backfill fires on every tick — even
        # those with zero live candidates — so
        # flipping ``[publication] enabled``
        # retroactively drains historical publish
        # rows. No-op when publication is off or
        # not fully wired.
        await reviewer.backfill()
        candidates = reviewer.select_candidates(
            self.config.artifact_review_batch_size,
        )
        if candidates:
            review_session: Session | None = None
            try:
                if self.config.enable_session_reuse:
                    review_session = await create_session(
                        system=self._build_identity_context(),
                        options=QueryOptions(
                            effort="medium",
                            tools=[],
                            caller="artifact_review_session",
                        ),
                        phase="artifact_review",
                    )
                await reviewer.review(
                    candidates,
                    budget_throttle_active=(
                        self.budget_throttle_active
                    ),
                    session=review_session,
                )
            finally:
                if review_session is not None:
                    try:
                        await disconnect_session(review_session)
                    except Exception:
                        logger.debug(
                            "artifact_review_session disconnect failed",
                            exc_info=True,
                        )

    async def _phase_process_rejected_plans(self, *, iteration: int) -> None:
        # Learn from newly rejected plans (after
        # merged-PR check to avoid duplicate learning
        # for plans rejected via closed PRs).
        #
        # The classifier is independent across plans,
        # so one shared low-effort rejection session
        # for the whole cycle is acceptable — cross-
        # plan transcript bleed is benign. Open the
        # session only when there is at least one
        # plan to learn from so a clean iteration
        # pays zero connect cost; the phase itself
        # still always runs so failure semantics
        # (loud propagation) are preserved.
        has_pending_rejection = any(
            p.status == "rejected"
            and not p.rejection_learned
            for p in self.memory.get_all_plans()
        )
        async with rejection_sessions(
            enabled=(
                has_pending_rejection
                and self.config.enable_session_reuse
            ),
            identity=self._build_identity_context,
        ) as (process_rejected_session, self_model_session):
            await self._process_rejected_plans(
                session=process_rejected_session,
                self_model_session=self_model_session,
            )

    async def _phase_generate_task(self, *, iteration: int) -> None:
        # Session D — short-lived, scoped to this
        # phase. Created inline (not via
        # ``_get_or_create_session``) so it does
        # not persist across iterations or share
        # Session B's high-effort adaptive thinking
        # configuration. ``generate_task`` only
        # needs effort=low, and a per-turn effort
        # override on a session would be silently
        # dropped by the SDK. The phase no longer
        # chains into ``self.run()``: queue
        # consumption is handled by the
        # ``run_task`` phase that follows.
        task_session: Session | None = None
        try:
            if self.config.enable_session_reuse:
                task_session = await create_session(
                    system=self._build_identity_context(),
                    options=QueryOptions(
                        effort="low",
                        tools=[],
                        caller="task_generation_session",
                    ),
                    phase="task_generation",
                )
            await self.generate_task(
                session=task_session,
            )
        finally:
            if task_session is not None:
                try:
                    await disconnect_session(task_session)
                except Exception:
                    logger.debug(
                        "task_session disconnect failed",
                        exc_info=True,
                    )

    def _grounded_reflection_active(self) -> bool:
        """Whether the priced reflection gets its bounded read-only tool surface.

        The single source of the ``[loop] grounded_reflection_enabled`` flag read
        *and* of the throttle degrade (proposal 132): true only when the flag is
        on, the introspection registry is present (``cli._build_agent`` supplied
        it — absent in most test constructions), and the loop is **not**
        budget-throttled. While the loop deliberately slows (burn-rate or operator
        maintenance), the most expensive phase is never widened with tools.
        ``_session_a`` is reset to ``None`` at the top of every iteration, so this
        decision is re-taken fresh each cycle.
        """
        return (
            self.config.grounded_reflection_enabled
            and self._agent_tools is not None
            and not self.budget_throttle_active
        )

    def _build_contemplation_session_options(self) -> QueryOptions:
        """Build the slot-A contemplation session options.

        Inactive (the shipped default, or throttled, or no registry): exactly
        today's tool-free options — ``tools=[]`` and no
        ``mcp_servers``/``allowed_tools``/``max_turns``. Active: the same options
        plus the bounded read-only reflection surface, materialised through the
        provider tool adapter (the executor's proven registry→session shape).

        ``tools=[]`` is kept in **both** branches: the CLI's ``--tools ""``
        disables only the *built-in* tool set, while the in-process MCP servers
        ride ``--mcp-config`` and are admitted by ``--allowedTools`` — so the
        active session exposes exactly the ``READ_ONLY_REFLECTION_TOOLS`` members
        as ``mcp__<server>__<tool>`` names and no ``Bash``/``Read``/``Write``. The ``disallowed_tools=["Agent"]``
        default (applied in the query path) is untouched.
        """
        base_kwargs: dict[str, Any] = {
            "effort": "high",
            "tools": [],
            "caller": "contemplation_session",
        }
        if not self._grounded_reflection_active():
            return QueryOptions(**base_kwargs)
        assert self._agent_tools is not None  # narrowed by _grounded_reflection_active
        bounded = build_bounded_reflection_tool_set(self._agent_tools["registry"])
        allowed_tools, mcp_servers = AnthropicToolAdapter().materialize(bounded)
        return QueryOptions(
            **base_kwargs,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            max_turns=REFLECTION_SESSION_MAX_TURNS,
        )

    async def _phase_contemplate(self, *, iteration: int) -> None:
        # Persistent slot-A contemplation session (proposal 106):
        # get-or-create into ``self._session_a`` and thread it into
        # ``contemplate``. The ``finally`` restores the loop-level running
        # flag — ``contemplate`` resets it in its own ``finally``.
        try:
            self._session_a = await self._get_or_create_session(
                slot="a",
                existing=self._session_a,
                options=self._build_contemplation_session_options(),
                system=self._build_identity_context(),
            )
            self._iter_reflection = await self.contemplate(
                context=self._iter_message_context,
                session=self._session_a,
                iteration=iteration,
                diag_snapshot=self._iter_diag_snapshot,
            )
        finally:
            # Restore loop-level running flag; sub-methods
            # reset it in their own finally.
            self._running = True

    async def _phase_implement_approved_plans(
        self, *, iteration: int,
    ) -> None:
        # End contemplation session before
        # implementation — Session C will be created
        # per-plan inside implement_plan().
        if self._session_a is not None:
            with contextlib.suppress(Exception):
                await disconnect_session(self._session_a)
            self._session_a = None
        await self._implement_approved_plans(iteration=iteration)

    async def _phase_evolve(self, *, iteration: int) -> None:
        # Fuel first (proposal 155 step 4): no fuel opens no session. Slot B
        # (proposal 106) is shared with curate; unopened under the single-use
        # flag (139). ``finally`` undoes _evolve_with_fuel's reset.
        selection = self._admitted_evolution_fuel(datetime.now(UTC))
        await self_health.deliver_gap_decision_asks(self)
        if selection is None:
            return
        try:
            if self.config.sdk_single_use_evolution_calls_enabled:
                await self._evolve_with_fuel(selection, None)
                return
            self._session_b = await self._get_or_create_session(
                slot="b",
                existing=self._session_b,
                options=QueryOptions(
                    effort="high",
                    tools=[],
                    caller="evolution_session",
                ),
                system=self._build_identity_context(),
            )
            await self._evolve_with_fuel(selection, self._session_b)
        finally:
            self._running = True

    async def _phase_curate_pending_plans(
        self, *, iteration: int,
    ) -> None:
        # Proposal 060: refine or prune the oldest
        # unrefined pending plan on slot B (if open); no
        # SDK call when no pending plan exists.
        self._session_b = await self._curate_pending_plans(
            session=self._session_b,
        )

    async def _phase_dream(self, *, iteration: int) -> None:
        # The priced digest is deleted; the phase runs only its zero-SDK
        # passes on cadence.
        refresh_phase_cost_report(
            self.cost_samples, self.config.artifacts_dir / "phase_cost_report.md")
        if self.config.archive_threshold_days <= 0:
            return
        ids = open_evolution_plan_ids(
            lambda: self._open_owned_pr_states(strict=True))
        # Best-effort: cold tier is operator-run.
        try:
            self.memory.archive(
                self.config.archive_threshold_days,
                retry_budget=self.config.max_implementation_attempts,
                open_pr_plan_ids=ids)
        except Exception:
            logger.warning("archive failed", exc_info=True)

    async def _run_warning_hold_probe(self) -> None:
        """Probe the API until the warning hold clears or shutdown fires.

        Extracted from ``run_loop``'s warning-hold arm so the
        session-attachment fence exempts only this probe rather than the
        whole loop (#2957): the bare ``query()`` below is deliberately a
        fresh one-shot client, not a main-phase session call, and the
        allowlist entry blessing it must not reach the phase dispatch.
        Exponential-backoff sleep between probes
        (``get_warning_hold_probe_interval() * 2**attempt``, capped at
        ``_WARNING_HOLD_MAX_PROBE_INTERVAL``); any SDK error counts as a
        failed probe and continues, so only shutdown or a cleared hold
        exits. A future warned reset replaces the backoff (#4514).
        """
        probe_attempt = 0
        while is_warning_hold_active():
            if self._ensure_shutdown_event().is_set():
                return
            reset_wait = get_warning_hold_reset_wait()
            probe_sleep = reset_wait or min(
                get_warning_hold_probe_interval() * (2**probe_attempt),
                _WARNING_HOLD_MAX_PROBE_INTERVAL,
            )
            logger.info(
                "Warning hold probe %d: sleeping %ds", probe_attempt + 1, probe_sleep
            )
            if await self._heartbeat_wait(probe_sleep):
                return
            try:
                await query(
                    "Reply with OK.",
                    options=QueryOptions(
                        effort="low",
                        max_turns=1,
                        caller="warning_hold_probe",
                        bypass_warning_throttle=True,
                    ),
                )
            except SDKError as exc:
                if exc.category == ErrorCategory.RATE_LIMIT:
                    logger.warning(
                        "Warning hold probe %d: hard rate limit",
                        probe_attempt + 1,
                    )
                else:
                    logger.warning(
                        "Warning hold probe %d: %s",
                        probe_attempt + 1,
                        exc,
                    )
            probe_attempt += 1

    def _raise_pending_poll_failure(self) -> None:
        """Surface a dead Telegram poll task by crashing the loop loudly.

        The approval/message poll loop runs fire-and-forget; its done-callback
        (``TelegramNotifier._on_poll_task_done``) stores any non-cancellation
        handler exception on ``notifier.poll_failure`` instead of leaving it
        unretrieved on the task. This is the "classify or crash" consumer the
        poll loop's fail-loud contract promises: probed at the top of every
        iteration, a stored failure is re-raised here so it propagates past the
        per-phase guards to ``run_loop``'s crash boundary (``crashed_unwind`` →
        ``shutdown(crashed=True)``), and the next startup runs crash detection.
        Without this the channel that carries approvals, rejections,
        ``/maintenance``, and inbound messages would die silently for the rest
        of the run. The loop is meant to die hard, not auto-restart polling.
        """
        from maestro.notifications.telegram import TelegramNotifier

        if isinstance(self.notifier, TelegramNotifier):
            failure = self.notifier.poll_failure
            if failure is not None:
                # Consume the signal so it is reported once, then crash.
                self.notifier.poll_failure = None
                raise failure

    async def run_loop(
        self,
        interval: int = 60,
        max_iters: int = 0,
        evolve_every: int = 5,
        task_every: int = 3,
        dream_every: int = 10,
        audit_self_every: int = 10,
        contemplate_every: int = 1,
        curate_every: int = 0,
        curate_enabled: bool = False,
        smoke: bool = False,
    ) -> bool:
        """Delegate to :func:`maestro.loop.iteration.run_loop`."""
        return await iteration.run_loop(self, interval, max_iters, evolve_every, task_every, dream_every, audit_self_every, contemplate_every, curate_every, curate_enabled, smoke)
