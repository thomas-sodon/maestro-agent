"""Task-generation cluster carved out of ``maestro.agent`` (proposal 068, step 2).

Holds the candidate-task-queue curator — ``generate_task`` with its
pre-call gate chain (the declarative :data:`maestro.task_gates.GATE_CHAIN`
registry, proposal 069), prompt assembly, and the post-call content gates
:func:`_vet_goal_content` applies (their ordered membership lives in
:func:`maestro.task_gates.content_gate_order`) — behind
:class:`TaskGenerator`. ``MaestroAgent.generate_task`` is a one-line
delegating shim. The SDK seams (``query`` / ``query_on_session``) are imported
from :mod:`maestro.sdk`; tests patch them on this module.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from maestro import audit_checks_calibration, audit_checks_settlement, conscious_workspace
from maestro.audit_checks import (
    check_phantom_premise,
    check_task_concreteness,
    compute_investigation_rate,
    count_unintegrated_tasks,
    extract_issue_numbers,
    format_integration_backlog,
    format_investigation_rate_context,
    format_known_state_summary,
    format_promotable_summary,
    format_task_mode_summary,
    format_task_shape_summary,
    goal_opens_or_merges_pr,
    is_compound_scope,
    log_gate_activation,
)
from maestro.audit_checks_calibration import (
    format_data_sufficiency_context,
    log_premise_observation,
    measure_prediction_accuracy,
)
from maestro.consciousness import match_open_question
from maestro.escalation_bridge import active_standdown_prs
from maestro.evolution import (
    ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
    ACCEPTANCE_CRITERIA_MAX_ITEMS,
)
from maestro.prompts.defaults import (
    select_task_generation_prompt,
)
from maestro.remediation_tracker import (
    _UNLANDED_REMEDIATION_CYCLES,
    format_forced_vs_chosen_quiet,
    format_pr_churn_summary,
    format_pr_lap_summary,
    format_pr_readmission_combined,
    format_standdown_production,
    format_unlanded_remediations_line,
    measure_no_progress_laps,
    measure_unlanded_remediations,
)
from maestro.sdk import QueryOptions, json_complete, query, query_on_session
from maestro.stats import format_confirmed_landing_summary
from maestro.task_gates import (
    FAILED_BRANCH_WINDOW,
    GATE_CHAIN,
    GateContext,
    failed_branch_target_named,
    investigation_observable_gate,
    run_gate_chain,
)
from maestro.task_queue import QueuedTask, sort_queue_oldest_bugs_first

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.consciousness import SelfModel
    from maestro.memory import TaskRecord
    from maestro.providers.base import Session

logger = logging.getLogger("maestro")


# Regex matching Python file paths with at least one directory component.
# Captures paths like ``maestro/agent.py``, ``tests/test_foo.py``,
# ``src/lib/bar.py``. Ignores bare filenames without a slash — those
# are ambiguous and cannot be reliably located on disk.
_FILE_PATH_RE = re.compile(r"(?:[\w.-]+/)+[\w.-]+\.py")


def _extract_file_paths(text: str) -> list[str]:
    """Extract Python file paths from *text* using :data:`_FILE_PATH_RE`.

    Returns de-duplicated paths in discovery order.
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in _FILE_PATH_RE.findall(text):
        if match not in seen:
            seen.add(match)
            result.append(match)
    return result


_PRIOR_TASK_CONTEXT_CAP = 500

# Prompt-boundary ceilings for the candidate-queue snapshot. These are
# deliberately independent of ``task_queue_max``: that cap is
# admission-only (it stops new enqueues but never evicts), so a queue
# persisted under an older, larger cap — or hand-edited — reaches the
# prompt at full size (#3596).
_QUEUE_SNAPSHOT_TASK_ID_CAP = 60
_QUEUE_SNAPSHOT_GOAL_CAP = 160
_QUEUE_SNAPSHOT_RATIONALE_CAP = 120
_QUEUE_SNAPSHOT_LINKED_QUESTION_CAP = 120
_QUEUE_SNAPSHOT_MAX_ITEMS = 20
_QUEUE_SNAPSHOT_TOTAL_CAP = 4000


def _queue_snapshot_overflow_marker(omitted: int) -> str:
    """The single trailing line naming *omitted* unrendered queue entries."""
    return f"- \u2026 {omitted} further queued task(s) omitted for prompt length"


def _format_queue_snapshot_line(item: QueuedTask) -> str:
    """Render one queue entry with every interpolated field sliced.

    ``task_id`` is capped alongside the free-text fields: a hand-edited
    ``task_queue.json`` — the very state this bound exists for — can carry
    an arbitrarily long id, and an uncapped one would consume the whole
    block budget on its own.
    """
    created = item.created_at[:10] if item.created_at else "?"
    line = (
        f"- task_id={item.task_id[:_QUEUE_SNAPSHOT_TASK_ID_CAP]} "
        f"queued: {created} "
        f"goal: {item.goal[:_QUEUE_SNAPSHOT_GOAL_CAP]}"
    )
    if item.linked_question:
        capped = item.linked_question[:_QUEUE_SNAPSHOT_LINKED_QUESTION_CAP]
        line += f" | linked_question: {capped}"
    if item.rationale:
        line += f" | rationale: {item.rationale[:_QUEUE_SNAPSHOT_RATIONALE_CAP]}"
    if item.acceptance_criteria:
        line += f" | criteria: {len(item.acceptance_criteria)}"
    return line


def _render_queue_snapshot_lines(
    queue: Sequence[QueuedTask], budget: int,
) -> list[str]:
    """Render entries of *queue* while the joined block fits within *budget*."""
    lines: list[str] = []
    used = 0
    for item in queue[:_QUEUE_SNAPSHOT_MAX_ITEMS]:
        line = _format_queue_snapshot_line(item)
        # +1 for the joining newline once at least one line is present.
        cost = len(line) + (1 if lines else 0)
        if used + cost > budget:
            break
        lines.append(line)
        used += cost
    return lines


def _format_queue_snapshot(queue: Sequence[QueuedTask]) -> str:
    """Render *queue* as the bounded ``queue_snapshot`` prompt block.

    Every list entering a prompt carries a load-bearing cap. Three
    ceilings bind here — per-field slices, an item count
    (:data:`_QUEUE_SNAPSHOT_MAX_ITEMS`), and a total character budget
    (:data:`_QUEUE_SNAPSHOT_TOTAL_CAP`) — and whichever binds first stops
    the render. Omitted entries are named in a single trailing overflow
    marker rather than dropped silently, so the model knows the block is
    a window onto a longer queue; the marker's worst-case length is
    reserved against the budget so the returned block honours the total
    cap including it. When the budget admits no entry at all the marker
    alone is returned — a non-empty queue never renders as ``(empty)``.

    This is a rendering bound only: the persisted queue is never mutated
    and no entry is evicted. An empty queue renders ``(empty)``.
    """
    if not queue:
        return "(empty)"
    lines = _render_queue_snapshot_lines(queue, _QUEUE_SNAPSHOT_TOTAL_CAP)
    if len(lines) == len(queue):
        return "\n".join(lines)
    reserve = len(_queue_snapshot_overflow_marker(len(queue))) + 1
    lines = _render_queue_snapshot_lines(
        queue, _QUEUE_SNAPSHOT_TOTAL_CAP - reserve,
    )
    marker = _queue_snapshot_overflow_marker(len(queue) - len(lines))
    return "\n".join([*lines, marker])


_PRIOR_TASK_OUTCOME_LABELS: dict[str, str] = {
    "success": "LAST TASK (completed)",
    "partial": "LAST TASK (partial — unfinished, continue from here)",
    "failure": "LAST TASK (failed — use a different approach)",
}

_PRIOR_TASK_ELIGIBLE_OUTCOMES = frozenset(_PRIOR_TASK_OUTCOME_LABELS)


#: The advisory conscious-access citation instruction (proposal 130), mirroring
#: :data:`maestro.evolution._WORKSPACE_CITATION_PROMPT` for the task-generation
#: enqueue schema. Appended to ``generate_task``'s prompt only when a broadcast
#: moment is delivered, so the flag-off prompt stays byte-identical. It asks the
#: model to declare, per ``enqueue`` entry, which admitted item ids (rendered as
#: ``wi-...`` handles in the moment block) informed that task; a cheap
#: in-producer resolve filter drops any id not in the admitted set, so a
#: hallucinated id is never persisted. The citation grants no capability, alters
#: no task priority, and gates nothing.
_WORKSPACE_CITATION_PROMPT = """

CONSCIOUS-ACCESS CITATION: The conscious-moment block above lists admitted items,
each line prefixed with its `wi-...` id. If any of those items informed a task you
enqueue, add a "workspace_item_ids" array to that enqueue entry naming ONLY the
ids you actually used, copied exactly. Cite nothing you did not use; use an empty
array (or omit the field) if no admitted item informed the task. Never invent an id."""


def _format_prior_task_context(
    tasks: list[TaskRecord],
    enabled: bool,
) -> str | None:
    """Return a '## Prior Task Context' block for the most recent task.

    Extracts ``key_findings`` from the most recent task with an eligible
    outcome (success, partial, or failure). Falls back to
    ``result_summary`` when ``key_findings`` is empty or absent. Returns
    ``None`` when disabled, when no eligible task exists, or when the
    extracted text is empty.

    The heading carries an outcome-appropriate label so the task generator
    can distinguish completed work from partial/failed attempts.

    Output is hard-capped at :data:`_PRIOR_TASK_CONTEXT_CAP` characters
    (truncated with '…') to prevent prompt bloat.
    """
    if not enabled:
        return None
    for task in reversed(tasks):
        if task.outcome not in _PRIOR_TASK_ELIGIBLE_OUTCOMES:
            continue
        text = (task.key_findings or "").strip() or (task.result_summary or "").strip()
        if not text:
            return None
        if len(text) > _PRIOR_TASK_CONTEXT_CAP:
            text = text[:_PRIOR_TASK_CONTEXT_CAP] + "…"
        label = _PRIOR_TASK_OUTCOME_LABELS[task.outcome]
        return (
            "## Prior Task Context\n"
            f"{label}: The most recent task produced the following "
            "findings. Consider whether your next task can build on, "
            "extend, or validate this work.\n"
            f"{text}"
        )
    return None


def _parse_acceptance_criteria(
    agent: MaestroAgent, raw: object,
) -> list[str] | None:
    """Clean and validate a curation entry's ``acceptance_criteria`` list.

    Shared by the enqueue and edit arms so a goal rewrite's replacement
    criteria pass the same bar as a fresh task's. Keep only non-empty
    strings, then filter and cap, so plan-level and task-level acceptance
    criteria stay in lockstep with EvolutionEngine.propose_plan(); see
    issue #1955. Returns ``None`` when nothing usable survives.
    """
    if not isinstance(raw, list):
        return None
    cleaned = [
        c.strip()[:ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS]
        for c in raw
        if isinstance(c, str) and c.strip()
    ]
    if not cleaned:
        return None
    # Apply the same vacuousness heuristics used by EvolutionEngine for
    # plan acceptance_criteria.
    validated = agent.evolution.validate_success_criteria(
        cleaned,
    )[:ACCEPTANCE_CRITERIA_MAX_ITEMS]
    return validated or None


def _vet_goal_content(
    agent: MaestroAgent,
    goal: str,
    *,
    rationale: str | None = None,
    recent_records: Sequence[TaskRecord] | None = None,
) -> str | None:
    """Return the name of the first content gate *goal* fails, else ``None``.

    The six per-goal content gates that police what may enter
    ``task_queue.json``, in the order the enqueue pass applies them: the
    #2280 PR-mutation filter, terrain-exhaustion, compound-scope,
    concreteness, phantom-premise, and failed-branch-target. Each rejection
    logs and — so every drop leaves a trace — appends a ``rejected``
    :func:`log_gate_activation` row.

    The failed-branch-target gate (issue #4049) reads *rationale* as well as
    *goal*, matching the generation-time gate's text; *recent_records* lets a
    curation pass load the history tail once instead of per goal, falling back
    to a :data:`FAILED_BRANCH_WINDOW` read when omitted.

    Shared by both queue-mutation sites. The enqueue pass vets a *new* goal
    before admitting it; the edit pass vets a *rewrite* before it replaces a
    vetted entry, so a rewritten goal cannot smuggle past these six content
    gates (issue #2939). It is not the enqueue path's *whole* admission
    check: the ``existing_goal_norms`` duplicate filter stays enqueue-only,
    so an edit can still rewrite one entry's goal into a duplicate of
    another's — deliberately out of scope for #2939. Only the enqueue pass
    acts on the returned name (re-arming the concreteness steering
    directive); the edit pass just declines the rewrite.

    Zero SDK cost apart from the terrain gate's on-disk file probes.
    """
    # --- PR-mutation goal filter ---
    # Belt-and-suspenders to the structural ``tool_gating`` Bash guard: a
    # goal whose objective is to open or merge a pull request would, if
    # executed, try to bypass the human-in-the-loop approval gate (issue
    # #2280). Drop it before it ever enters the queue; the only sanctioned
    # route to main for a task branch is nominating it via
    # ``EvolutionPlan.source_task_id``.
    if goal_opens_or_merges_pr(goal):
        logger.info(
            "generate_task: dropping PR-opening/merging goal %r — "
            "promotion must route through source_task_id approval",
            goal,
        )
        log_gate_activation(
            agent.gate_activations,
            gate="pr_mutation_goal",
            action="rejected",
            detail=goal[:200],
        )
        return "pr_mutation_goal"

    # --- Terrain-exhaustion precondition check ---
    candidate_issues = extract_issue_numbers(goal)
    if candidate_issues and not agent._check_issue_preconditions(goal):
        logger.debug(
            "generate_task: dropping entry — stale issue terrain for %s",
            candidate_issues,
        )
        log_gate_activation(
            agent.gate_activations,
            gate="terrain_exhaustion",
            action="rejected",
            detail=f"stale issue terrain for {candidate_issues}"[:200],
        )
        return "terrain_exhaustion"

    # --- Compound-scope gate ---
    if agent.config.compound_scope_gate_enabled and is_compound_scope(goal):
        logger.info(
            "generate_task: compound-scope gate dropped %r — "
            "split into separate creation and wiring tasks",
            goal,
        )
        log_gate_activation(
            agent.gate_activations,
            gate="compound_scope",
            action="rejected",
            detail=goal[:200],
        )
        return "compound_scope"

    # --- Concreteness gate ---
    # Reject pure-analysis tasks: no concrete file target, no implementation
    # verb, only analysis-only verbs. Such tasks pass every other gate,
    # consume a full implementation cycle, and produce only gitignored
    # markdown — the analysis monoculture entering at its source. Tagged
    # experiment: revert if the implementation-task proportion does not
    # shift within 10 iterations.
    concrete_ok, concrete_reason = check_task_concreteness(goal)
    if not concrete_ok:
        logger.info(
            "generate_task: concreteness gate dropped %r — %s",
            goal, concrete_reason,
        )
        log_gate_activation(
            agent.gate_activations,
            gate="concreteness",
            action="rejected",
            detail=goal[:200],
        )
        return "concreteness"

    # --- Phantom-premise gate ---
    # Refuse a goal that asserts an *existing* defect in a ``.py`` path that
    # is not in the tree (issue #3666). Such a goal passes every gate above,
    # pops, opens a task branch, and burns a full implementation cycle before
    # the executor discovers the file it was told to fix is absent. The
    # missing-path reading comes from ``_missing_file_references`` — the same
    # predicate the advisory premise report uses, so the #1882 containment
    # guard is shared, not duplicated — narrowed here to the paths that are
    # genuinely *absent* from disk. That narrowing is load-bearing: the
    # advisory reading also reports a path that exists but fails ``ast.parse``,
    # and "fix the syntax error in <that file>" is a true premise, not a
    # phantom one, so refusing it would be exactly backwards. A goal whose
    # framing is to *create* the path stays admitted. Default-off experiment.
    if agent.config.phantom_premise_gate_enabled:
        missing_paths = [
            rel_path
            for rel_path in agent._missing_file_references(goal)
            if not agent._file_reference_exists(rel_path)
        ]
        premise_ok, premise_reason = check_phantom_premise(goal, missing_paths)
        if not premise_ok:
            logger.info(
                "generate_task: phantom-premise gate dropped %r — %s",
                goal, premise_reason,
            )
            log_gate_activation(
                agent.gate_activations,
                gate="phantom_premise",
                action="rejected",
                detail=goal[:200],
            )
            return "phantom_premise"

    # --- Failed-branch-target gate ---
    # Refuse a goal that re-attacks a ``maestro/task/<hash>`` branch whose
    # recent task run recorded a non-success outcome (issue #4049). The
    # generation-time gate only skips curation; without this admission check
    # (and the pop-time twin in ``_run_task_phase``) the re-attempt still runs.
    records = (
        recent_records
        if recent_records is not None
        else agent.memory.get_recent_tasks(FAILED_BRANCH_WINDOW)
    )
    token = failed_branch_target_named(f"{goal} {rationale or ''}", records)
    if token is not None:
        logger.info(
            "generate_task: failed-branch-target gate dropped %r — "
            "re-attacks failed task branch %s",
            goal, token,
        )
        log_gate_activation(
            agent.gate_activations,
            gate="failed_branch_target",
            action="rejected",
            detail=f"{token}: {goal}"[:200],
        )
        return "failed_branch_target"

    return None


def rebase_curated_queue(
    live: list[QueuedTask],
    *,
    snapshot_ids: set[str],
    survivors_by_id: dict[str, QueuedTask],
    incoming: list[QueuedTask],
    queue_cap: int,
) -> tuple[list[QueuedTask], list[QueuedTask]]:
    """Rebase a curation decision onto the *live* task queue (#3561).

    ``generate_task`` curates a snapshot loaded before a multi-minute priced
    SDK call while ``_run_task_phase`` pops through the same backend, so the
    persistence step must apply a *delta* rather than replace the whole queue
    with the stale snapshot. Given *live* (the queue re-read under the
    backend's lock):

    * a live entry the curator kept is replaced by its ``survivors_by_id``
      counterpart, so the curator's in-place edits land;
    * a live entry the snapshot contained but the curator pruned drops;
    * a live entry the snapshot never saw — a concurrent enqueue — carries
      forward untouched;
    * an entry popped mid-call is simply absent from *live*, so it stays
      popped along with any edit the curator made to it.

    *incoming* (the vetted new entries) is appended, re-trimmed against
    ``queue_cap`` over the *rebased* length so a concurrent enqueue cannot
    push the queue past the cap; existing entries are never evicted. The
    result is then stable-sorted oldest-bug-first
    (:func:`~maestro.task_queue.sort_queue_oldest_bugs_first`), which bubbles
    ``#NNN`` issue tasks to the front by lowest issue number — counteracting
    recency-novelty bias without LLM judgment.

    Returns ``(rebased_queue, admitted_incoming)``. Callers must key every
    downstream ledger write off the second element, never off *incoming*, so a
    recorded task id always resolves to a really-queued task (#2304). Pure:
    it neither mutates *live* nor touches the backend.
    """
    rebased: list[QueuedTask] = []
    for entry in live:
        survivor = survivors_by_id.get(entry.task_id)
        if survivor is not None:
            rebased.append(survivor)
        elif entry.task_id not in snapshot_ids:
            rebased.append(entry)
    admitted = incoming[: max(0, queue_cap - len(rebased))]
    return sort_queue_oldest_bugs_first(rebased + admitted), admitted


class TaskGenerator:
    """Task-generation collaborator for :class:`~maestro.agent.MaestroAgent`.

    Constructed with the agent and dispatching through its attributes at
    call time: the cluster reads ``consciousness`` / ``config`` /
    ``task_queue`` / ``memory`` / ``journal`` / ``evolution`` and the
    agent-level helpers (``_check_issue_preconditions``,
    ``_build_identity_context``), several of which the test suite
    monkey-patches on the agent instance after construction.
    """

    def __init__(self, agent: MaestroAgent) -> None:
        self._agent = agent

    async def generate_task(
        self,
        session: Session | None = None,
    ) -> None:
        """Maintain the candidate-task queue (prune / edit / enqueue).

        No longer executes anything: the freshly maintained queue is
        consumed by ``_run_task_phase`` (which runs every iteration and
        first drains ``pending_user_requests`` / ``pending_pr_requests``
        before falling back to the queue head). All pre-call short-circuit
        gates (audit-stall, cost-risk) and post-call content
        gates (see :func:`maestro.task_gates.content_gate_order`) still fire at
        idea time so the queue is pre-vetted.

        When *session* is provided, ``effort`` has no effect — the
        session-level effort configured at creation applies instead.
        """
        agent = self._agent
        model = agent.consciousness.load_self_model()

        # --- Mechanical pending-plan cluster sweep (zero SDK cost) ---
        # Before any remediation/task pop, drain redundant ``pending_review``
        # backlog: the submit-time duplicate detectors, applied periodically,
        # mark all-but-the-newest member of each qualifying cluster
        # ``obsolete`` via the owned terminal act — no approval, no LLM call.
        # A no-op when no cluster meets threshold or the flag is off.
        if agent.config.loop_pending_cluster_sweep_enabled:
            swept = agent.memory.sweep_clustered_pending_plans(
                threshold=agent.config.loop_pending_cluster_threshold,
                min_cluster_size=agent.config.loop_pending_cluster_max,
                lever_attempts=agent.lever_attempts,
            )
            if swept:
                agent.journal.record_event(
                    "Pending-plan cluster sweep obsoleted "
                    f"{len(swept)} redundant plan(s): {', '.join(swept)}",
                    entry_type="reflection",
                    triggered_by="pending_cluster_sweep",
                )

        # --- Mechanical pending-plan restatement sweep (zero SDK cost) ---
        # The pairwise complement of the cluster sweep above: retire every
        # ``pending_review`` plan that restates a strictly-newer surviving
        # pending plan (keeping the newest of each chain), capturing each
        # owned ``mark_plan_obsolete`` fire (its return AND a
        # ``verify_obsolete_persisted`` re-read) to the lever-attempts backend
        # and reporting only fires recorded ``True``. Reuses the duplicate-guard Jaccard cutoff.
        if agent.config.loop_restatement_sweep_enabled:
            restated = agent.memory.sweep_restatement_pending_plans(
                threshold=agent.config.loop_duplicate_pending_plan_threshold,
                lever_attempts=agent.lever_attempts,
            )
            if restated:
                agent.journal.record_event(
                    "Pending-plan restatement sweep obsoleted "
                    f"{len(restated)} restated plan(s): {', '.join(restated)}",
                    entry_type="reflection",
                    triggered_by="pending_restatement_sweep",
                )

        # --- Mechanical pending-plan expiry sweep (zero SDK cost) ---
        # The age-based complement of the content sweeps above: auto-obsolete
        # every ``pending_review`` plan older than the review horizon (disposal,
        # not rejection — an approval moves a plan out of ``pending_review`` and
        # is the explicit hold), capturing each owned ``mark_plan_obsolete``
        # fire (its return AND a ``verify_obsolete_persisted`` re-read) to the
        # lever-attempts backend. A no-op when the
        # horizon is ``<= 0`` or no pending plan has aged out.
        if agent.config.loop_plan_review_expiry_days > 0:
            expired = agent.memory.sweep_expired_pending_plans(
                max_age_days=agent.config.loop_plan_review_expiry_days,
                lever_attempts=agent.lever_attempts,
            )
            if expired:
                agent.journal.record_event(
                    "Pending-plan expiry sweep obsoleted "
                    f"{len(expired)} stale plan(s): {', '.join(expired)}",
                    entry_type="reflection",
                    triggered_by="pending_expiry_sweep",
                )

        # --- Pre-call gate chain (zero SDK cost) ---
        # Blocking gates and directive bridges are declarative
        # ``TaskGate`` entries in ``maestro.task_gates.GATE_CHAIN``;
        # precedence is list order, a block short-circuits the SDK call,
        # and the first bridge to fire wins the empty
        # ``next_task_directive`` slot (a pre-set directive wins over
        # all of them). ``maestro list-gates`` prints the chain.
        ctx = GateContext(agent=agent, config=agent.config, model=model)
        if run_gate_chain(GATE_CHAIN, ctx):
            return
        # A firing directive gate rebinds ``ctx.model`` to the live persisted
        # self-model (#4302); read that copy so the consumed directive below is
        # what is actually on disk, not the pre-chain snapshot.
        model = ctx.model

        # The window size (10) bounds the recent-task context fed into the
        # prompt below: the known-state goals, the task-shape and task-mode
        # summaries, and the prior-task context block. Widening/narrowing
        # here shifts what counts as "recent" for those consumers. (The
        # pre-call terrain-exhaustion and compound-scope gates do NOT read
        # this window — they inspect only the candidate goal string.) The
        # chain loads the same window lazily, so this read reuses its cache.
        recent = ctx.recent()

        # No brace escaping on any of the values below. ``str.format`` only
        # processes braces in the TASK_GENERATION_PROMPT template literal — it
        # inserts substituted values verbatim and never re-scans them, so a
        # value containing literal braces neither raises nor needs doubling.
        # Escaping here doubled braces in the prompt actually sent (#2092).

        # Load the current candidate-task queue. ``generate_task`` is a
        # curator over this queue; ``_run_task_phase`` is the consumer.
        current_queue = agent.task_queue.load()
        queue_cap = agent.config.task_queue_max
        free_slots = max(0, queue_cap - len(current_queue))

        task_history = (
            "\n".join(
                f"- {(t.goal or '')[:80]} -> {t.outcome}: {(t.key_findings or t.result_summary or '')[:150]}"
                for t in recent
            )
            or "(none)"
        )
        questions_text = (
            "\n".join(f"- {q}" for q in model.open_questions)
            if model.open_questions
            else "(none)"
        )
        # Read the one-shot contemplation directive (may be None).
        consumed_directive = model.next_task_directive
        directive_text = consumed_directive or "(none)"

        shape_summary = format_task_shape_summary(recent) or "(none)"

        # --- Known-state context injection (zero SDK cost) ---
        # Surface gate enable/disable states and recent task goals so the
        # LLM can see what the system already knows and avoid proposing
        # redundant investigation of already-confirmed state.
        known_state_gates: dict[str, bool] = {
            "stale_entry_enabled": agent.config.audit_stale_entry_enabled,
            "stall_block_enabled": agent.config.audit_stall_block_enabled,
        }
        known_state_goals = [t.goal or "" for t in recent if t.goal]

        # Investigation-rate metric from the falsification predicate
        _inv_rate, _inv_count, _inv_window = compute_investigation_rate(
            recent, window=len(recent),
        )
        inv_rate_ctx = format_investigation_rate_context(_inv_count, _inv_window)

        # --- Prediction accuracy context (zero SDK cost) ---
        pred_accuracy = measure_prediction_accuracy(
            agent.predictions,
            [asdict(t) for t in recent],
        )
        pred_accuracy_ctx = ""
        pred_total = pred_accuracy.get("total", 0)
        if isinstance(pred_total, int) and pred_total >= audit_checks_calibration._PREDICTION_MIN_MATCHED:
            pred_acc = pred_accuracy.get("accuracy", 0.0)
            if isinstance(pred_acc, (int, float)):
                pred_accuracy_ctx = (
                    f"Prediction accuracy: {round(pred_acc * 100)}% "
                    f"({pred_total} predictions)"
                )

        # --- Data-sufficiency context (zero SDK cost) ---
        data_suff_ctx = format_data_sufficiency_context(
            agent.config.memory_dir,
            gate_activation_backend=agent.gate_activations,
            prediction_backend=agent.predictions,
        )

        # --- Unlanded-remediation ground-truth + PR-LAPS sensors (zero SDK
        # cost) — Host-API-first merge-truth oracle (proposal 080 Phase 1): the
        # host verdict wins over a ``git log`` subject name-drop, with the git
        # oracle consulted only when the host call raises. Measure the per-PR
        # lap dict once and derive *both* the UNLANDED-REMEDIATION line and the
        # PR-LAPS line (080 step 4) from it: the measurement re-runs the host
        # oracle (a network call memoized only within one ``measure`` call), so
        # a single measure keeps it at one call per distinct PR per iteration
        # instead of two. ---
        unlanded_laps = measure_unlanded_remediations(
            agent.config.memory_dir, _UNLANDED_REMEDIATION_CYCLES,
            is_pr_merged=agent._host_or_git_pr_merged,
        )
        unlanded_ctx = format_unlanded_remediations_line(
            unlanded_laps, _UNLANDED_REMEDIATION_CYCLES,
        )

        # Per-PR remediation lap counts and any active handoff standdowns folded
        # into one line so a pileup is observable here before it reaches the
        # budget. Caller does the I/O; the formatter is pure (zero SDK cost).
        pr_lap_ctx = format_pr_lap_summary(
            unlanded_laps,
            active_standdown_prs(agent.escalation_standdowns),
            agent.config.pr_remediation_lap_budget,
        )

        # --- PR-readmission combined sensor — admission-channel count and
        # merged-re-serviced rate folded into one line; git-merge-truth oracle
        # runs caller-side so format_known_state_summary stays pure (zero SDK
        # cost) ---
        known_state_history = [asdict(t) for t in recent]
        # --- PR-READY / PR-STUCK churn block (089 step 6): done vs stuck vs
        # healthily-churning, from the consecutive-no-progress sensor and the
        # latest readiness snapshot per PR. Caller reads task history;
        # settled PRs drop via the ledger, then the memoized is_pr_settled door. ---
        pr_churn_ctx = format_pr_churn_summary(
            known_state_history,
            measure_no_progress_laps(
                agent.config.memory_dir, _UNLANDED_REMEDIATION_CYCLES,
            ),
            agent.config.pr_remediation_lap_budget,
            agent.config.memory_dir,
            settled_prs=lambda: audit_checks_settlement.load_settled_prs(
                agent.settled_verdicts,
            ),
            is_pr_settled=lambda n: agent.is_pr_settled(n).settled,
        )
        pr_readmission_ctx = format_pr_readmission_combined(
            agent.pr_provenance, known_state_history,
        )

        # --- Forced-vs-chosen-quiet mirror — classifies every remediation
        # quiet as externally forced vs self-initiated [stood-down]; reads
        # 100%-forced until a chosen stand-down lands (zero SDK cost) ---
        forced_quiet_ctx = format_forced_vs_chosen_quiet(known_state_history)

        # --- STAND-DOWN-PRODUCTION counter (#2865) — chosen-stand-down verdicts
        # on the ledger vs the stalled externally-blocked lineages that qualify
        # now, so a starved eviction gate is visible before it silently regresses;
        # two zero-SDK reads, the formatter is pure ---
        standdown_production_ctx = format_standdown_production(
            known_state_history, agent.stand_down_verdicts,
        )

        # --- Confirmed-vs-caused landing mirror — names the share of cures
        # stamped success that were merely confirmed already-landed (another's
        # merge) rather than caused to land; suppressed below threshold (zero
        # SDK cost) ---
        confirmed_landing_ctx = format_confirmed_landing_summary(
            agent.config.memory_dir,
        )

        known_state_section = format_known_state_summary(
            known_state_gates, known_state_goals,
            investigation_rate_context=inv_rate_ctx,
            prediction_accuracy_context=pred_accuracy_ctx,
            data_sufficiency_context=data_suff_ctx,
            unlanded_remediation_context=unlanded_ctx,
            pr_lap_context=pr_lap_ctx,
            pr_churn_context=pr_churn_ctx,
            pr_readmission_context=pr_readmission_ctx,
            forced_quiet_context=forced_quiet_ctx,
            standdown_production_context=standdown_production_ctx,
            confirmed_landing_context=confirmed_landing_ctx,
            task_history=known_state_history,
            data_dir=agent.config.memory_dir,
            pr_remediation_max_attempts_per_head=(
                agent.config.pr_remediation_max_attempts_per_head
            ),
            gate_activation_backend=agent.gate_activations,
            pr_ledger_backend=agent.pr_ledger_backend,
        )

        queue_snapshot = _format_queue_snapshot(current_queue)

        # --- Promotable-backlog pressure (zero SDK cost) ---
        # Cross-reference successful task branches against plans that
        # cite them via source_task_id. When the uncited count exceeds
        # the threshold, inject a prompt section listing top-3
        # candidates by recency so the LLM sees integration pressure
        # at the exact decision point where it can influence action.
        # Uses a wider window (50 tasks) than the rest of generate_task
        # (10) because the backlog accumulates over the full history;
        # a 10-task window would cap the count at 10 and understate
        # the real backlog size.
        # Gated by the branch-promotion-pressure master switch (proposal 116,
        # step 6): with the flag off this hint and the integration-backlog hint
        # below are both withheld — an empty ``promotable_section`` here.
        promotable_section = ""
        if agent.config.branch_promotion_pressure_enabled:
            promotable_tasks = agent.memory.get_recent_tasks(50)
            promotable_section = format_promotable_summary(
                promotable_tasks,
                agent.memory.get_cited_source_task_ids(),
                agent.config.promotable_backlog_threshold,
            )

        # Select the curator prompt by the resolved work-target framing
        # (proposal 107). ``str.format`` silently ignores the surplus
        # ``target_repo`` kwarg on the self template, so the default
        # ``self_evolution`` path is byte-identical to today; the external
        # template consumes it. ``github_repo`` may be ``None`` — fall back to
        # a generic phrase.
        wt = agent.config.work_target()
        base_prompt = select_task_generation_prompt(wt.task_framing).format(
            open_questions=questions_text,
            recent_tasks=task_history,
            next_task_directive=directive_text,
            task_shape_summary=shape_summary,
            queue_snapshot=queue_snapshot,
            free_slots=free_slots,
            target_repo=wt.github_repo or "the target repository",
        )

        if promotable_section:
            base_prompt += "\n\n" + promotable_section

        # --- Integration-backlog prompt hint (zero SDK cost) ---
        # Surface unmerged successful branches so the LLM can consider
        # branch promotion instead of starting fresh exploratory work.
        # Suppressed when the more detailed promotable-backlog section
        # already fired (same signal, richer candidate list), and withheld
        # entirely when the branch-promotion-pressure master switch is off
        # (proposal 116, step 6).
        if agent.config.branch_promotion_pressure_enabled and not promotable_section:
            backlog_count, _backlog_ids = count_unintegrated_tasks(
                [asdict(t) for t in recent],
                [
                    {"source_task_id": sid}
                    for sid in agent.memory.get_cited_source_task_ids()
                ],
            )
            backlog_note = format_integration_backlog(backlog_count)
            if backlog_note:
                base_prompt += "\n\n" + backlog_note

        # --- Task-mode balance hint (zero SDK cost context injection) ---
        # Surface the maintenance-vs-generative ratio so the LLM can
        # see portfolio balance and naturally steer toward the
        # underrepresented mode.  Suppressed for <3 recent tasks.
        mode_summary = format_task_mode_summary(
            [asdict(t) for t in recent],
        )
        if mode_summary:
            base_prompt += "\n\n" + mode_summary

        # --- Promotable-branch hint (zero SDK cost context injection) ---
        # When enabled, surface successful task branches that have not
        # yet been cited by any evolution plan.  The LLM can then
        # propose a branch-promotion task (whose deliverable is a PR),
        # routing toward integration of accumulated value instead of
        # always creating new analytical work. Withheld when the
        # branch-promotion-pressure master switch is off (proposal 116,
        # step 6) — the new flag is an additional gate above the existing
        # ``promotable_branch_hint_enabled`` sub-flag.
        if (
            agent.config.branch_promotion_pressure_enabled
            and agent.config.promotable_branch_hint_enabled
        ):
            promotable = agent.memory.get_promotable_branches(
                max_results=agent.config.promotable_branch_max_shown,
            )
            if promotable:
                # Appended after the single TASK_GENERATION_PROMPT.format()
                # call above and sent to the SDK verbatim, so braces here are
                # never reinterpreted — no escaping needed (#2092).
                branch_lines = "\n".join(
                    f"- task_id={b['task_id']} "
                    f"branch={b['branch']} "
                    f"goal: {b['goal']}"
                    for b in promotable
                )
                base_prompt += (
                    "\n\n## Promotable branches\n"
                    "The following successful task branches have not yet "
                    "been promoted into an evolution plan. Consider "
                    "proposing a task that promotes one of these branches "
                    "into main via a PR — integration work is as valuable "
                    "as new investigation.\n"
                    f"{branch_lines}"
                )

        # --- Prior-task context injection (zero SDK cost) ---
        # Surface key_findings (or result_summary fallback) from the most
        # recent task (any outcome) so the LLM can compound on prior work
        # or avoid re-diagnosing already-identified failures.
        prior_ctx = _format_prior_task_context(
            recent, agent.config.prior_task_context_enabled,
        )
        # These appends and base_prompt itself go to the SDK verbatim — no
        # second .format() pass — so braces are never reinterpreted and need
        # no escaping (#2092).
        if prior_ctx:
            base_prompt += "\n\n" + prior_ctx

        if known_state_section:
            base_prompt += "\n\n" + known_state_section

        # --- Experiment cadence (zero SDK cost decision) ---
        # After every N recorded tasks, mechanically inject experiment-
        # mode language requiring a hypothesis-driven task.  The counter
        # reads task_count() (fast non-empty-line count, no JSON parsing).
        # task_count() counts tasks recorded before this generation, so
        # the experiment lands on the next task produced once the count
        # reaches a multiple of N (offset by one from a literal "task N").
        # task_count() is hot-file-relative: a tiered-memory archive
        # (Memory.archive) moves older records to cold storage and
        # shrinks the hot count, so the cadence baseline resets after
        # each archive.  The trigger is therefore "approximately after
        # every N recorded tasks since the last archive," not absolute
        # lifetime cadence.
        # Existing short-circuit gates (audit-stall, cost-risk) fire
        # first and take precedence.
        task_total = agent.memory.task_count()
        if (
            task_total > 0
            and task_total % agent.config.experiment_cadence == 0
        ):
            base_prompt += (
                "\n\nEXPERIMENT MODE (triggered mechanically after every "
                f"{agent.config.experiment_cadence} recorded tasks): "
                "This task MUST be a hypothesis-driven experiment. "
                "It must contain all three of: "
                "(a) a falsifiable hypothesis about a specific system "
                "behavior, "
                "(b) named metrics and a concrete measurement procedure, "
                "(c) success defined as 'data was collected and analyzed "
                "— confirmation or disconfirmation are equally valid "
                "outcomes.' "
                "Do NOT dress routine engineering work as an experiment. "
                "The hypothesis must be genuinely uncertain — something "
                "whose outcome you cannot predict with confidence."
            )
            logger.info(
                "generate_task: experiment cadence triggered "
                "(task_count=%d, cadence=%d)",
                task_total, agent.config.experiment_cadence,
            )

        # --- Conscious-access broadcast + citation (proposal 130) ---
        # Deliver the active moment to the task-generation decision, mirroring
        # ``evolve``'s plan-proposal seam. ``_deliver_moment_to_consumer`` is the
        # sole reader of ``conscious_workspace_broadcast_enabled`` and records
        # the one per-consumer receipt; it returns a triple only when broadcast
        # is on and a moment is active — the exact condition under which an
        # enqueued task should cite it — resolving the moment exactly once. Off,
        # it returns ``None``: no block is prepended, no citation instruction is
        # appended, nothing is stamped, and the prompt/queue writes stay byte-
        # identical to today's. On, the moment block frames the decision as
        # ambient "what is present now" context (prepended, like ``introspect``)
        # and the advisory citation instruction is appended so the model can name
        # the admitted ids it used. ``admitted_visible_ids`` is the consumer-
        # *visible* item set (redacted ids removed), so the in-producer resolve
        # filter below can never accept an id the consumer never saw. Both SDK
        # branches send ``base_prompt`` verbatim (no second .format() pass), so
        # assembling it once here covers the session and one-shot paths alike and
        # needs no brace escaping (#2092). Advisory only — grants no capability,
        # alters no priority, and consults no gate.
        delivery = agent._deliver_moment_to_consumer("task_generation")
        workspace_moment_id: str | None = None
        admitted_visible_ids: frozenset[str] = frozenset()
        # Retained for the post-cap fail-closed citation guard (proposal 130):
        # the exact moment delivered to this decision, or ``None`` when broadcast
        # is off / no moment was active. Initialized here so the guard below can
        # reference it unconditionally, mirroring ``evolve``'s ``cited_moment``.
        cited_moment: conscious_workspace.ConsciousMoment | None = None
        if delivery is not None:
            moment_block, cited_moment, admitted_visible_ids = delivery
            workspace_moment_id = cited_moment.moment_id
            base_prompt = base_prompt + _WORKSPACE_CITATION_PROMPT
            base_prompt = f"{moment_block}\n\n{base_prompt}"

        # --- SDK call (single shot — no terrain-exhaustion retry loop) ---
        # The post-call gates below drop individual new entries instead of
        # rejecting the whole response. At queue scale the LLM can pick a
        # different idea next cycle, so we don't pay for a re-query here.
        if session is not None:
            result = await query_on_session(
                session, base_prompt, caller="generate_task",
                complete_on=json_complete,
            )
        else:
            result = await query(
                prompt=base_prompt,
                system=agent._build_identity_context(),
                options=QueryOptions(
                    effort="low", caller="generate_task",
                ),
                complete_on=json_complete,
            )

        try:
            payload = result.parse_json()
        except json.JSONDecodeError:
            logger.debug(
                "generate_task: response did not parse as JSON: %r",
                result.response[:200],
            )
            return

        if not isinstance(payload, dict):
            return

        prune_ids_raw = payload.get("prune") or []
        edit_entries_raw = payload.get("edit") or []
        enqueue_entries_raw = payload.get("enqueue") or []
        if not isinstance(prune_ids_raw, list):
            prune_ids_raw = []
        if not isinstance(edit_entries_raw, list):
            edit_entries_raw = []
        if not isinstance(enqueue_entries_raw, list):
            enqueue_entries_raw = []

        # --- Apply prune (remove named entries by task_id) ---
        prune_ids = {
            p for p in prune_ids_raw if isinstance(p, str) and p
        }
        next_queue: list[QueuedTask] = [
            q for q in current_queue if q.task_id not in prune_ids
        ]

        # History tail for the failed-branch-target content gate, read once
        # per curation pass rather than once per vetted goal.
        failed_branch_records = agent.memory.get_recent_tasks(
            FAILED_BRANCH_WINDOW,
        )

        # --- Apply edit (in-place goal/rationale rewrite) ---
        edits_by_id: dict[str, dict[str, Any]] = {}
        for entry in edit_entries_raw:
            if not isinstance(entry, dict):
                continue
            tid = entry.get("task_id")
            if not isinstance(tid, str) or not tid:
                continue
            edits_by_id[tid] = entry
        for q in next_queue:
            entry = edits_by_id.get(q.task_id)
            if entry is None:
                continue
            new_goal = entry.get("goal")
            new_rationale = entry.get("rationale")
            mutated = False
            if isinstance(new_goal, str) and new_goal.strip():
                # A rewrite replaces a *vetted* queue entry, and pop time
                # re-checks only failed branches — so route it through the
                # same content gates the enqueue path enforces, or an edit
                # smuggles a PR-mutating / stale-terrain / compound-scope /
                # pure-analysis goal into the pre-vetted queue (issue #2939).
                # On rejection the goal is left as-is; the helper's
                # activation row is the trace. Steering stays an enqueue-pass
                # contract, so ``concreteness_steered`` is not set here.
                #
                # An *echoed* goal is a no-op, not a rewrite: the curation
                # schema lists ``goal`` beside ``rationale``, so a
                # rationale-only edit typically repeats the existing goal
                # verbatim. Vetting that would stamp a fresh ``rejected`` row
                # every curation pass for any already-queued goal whose
                # terrain has since drifted, diluting the tail-windowed gate
                # sensors that read the trace. Skip it.
                #
                # Goal and acceptance criteria move as one unit (issue
                # #3963): a rewrite of a task carrying binding criteria must
                # bring a valid replacement list, or the old contract would
                # grade the new work. Without one the rewrite is refused and
                # the original pair stands; criteria are never cleared.
                stripped_goal = new_goal.strip()
                if (
                    stripped_goal != q.goal
                    and _vet_goal_content(
                        agent,
                        stripped_goal,
                        rationale=(
                            new_rationale
                            if isinstance(new_rationale, str)
                            else q.rationale
                        ),
                        recent_records=failed_branch_records,
                    ) is None
                ):
                    new_criteria = _parse_acceptance_criteria(
                        agent, entry.get("acceptance_criteria"),
                    )
                    if q.acceptance_criteria and new_criteria is None:
                        logger.info(
                            "Refusing goal edit for queued task %s: it "
                            "carries acceptance criteria and the edit "
                            "supplied no valid replacement",
                            q.task_id,
                        )
                    else:
                        q.goal = stripped_goal
                        if new_criteria is not None:
                            q.acceptance_criteria = new_criteria
                        mutated = True
            # Only an explicit ``null`` clears; an absent key keeps the
            # stored rationale like every other omitted field (issue #4394).
            if "rationale" in entry and new_rationale is None:
                if q.rationale is not None:
                    q.rationale = None
                    mutated = True
            elif isinstance(new_rationale, str):
                trimmed = new_rationale.strip()[:200] or None
                # A rationale rewrite is admission too (issue #4049): one
                # naming a failed task branch would make a clean queued task
                # re-attack it, so the old rationale stands.
                token = (
                    failed_branch_target_named(trimmed, failed_branch_records)
                    if trimmed is not None and trimmed != q.rationale
                    else None
                )
                if token is not None:
                    logger.info(
                        "Refusing rationale edit for queued task %s: it "
                        "names failed task branch %s",
                        q.task_id, token,
                    )
                    log_gate_activation(
                        agent.gate_activations,
                        gate="failed_branch_target",
                        action="rejected",
                        detail=f"{token}: rationale edit {q.task_id}"[:200],
                    )
                elif trimmed != q.rationale:
                    q.rationale = trimmed
                    mutated = True
            if mutated:
                q.last_touched_at = datetime.now(UTC).isoformat()

        # --- Apply enqueue (post-call gates per new entry) ---
        existing_goal_norms = {q.goal.strip().lower() for q in next_queue}

        accepted_new: list[QueuedTask] = []
        # Premise measurements (issue #2304), keyed by task_id, captured during
        # the accept loop but *logged* only after the queue-cap truncation below
        # so the trace never records an observation for a task that the cap
        # drops before it reaches the persisted queue.
        premise_by_task_id: dict[str, tuple[int, int]] = {}
        # Set when the concreteness gate drops a pure-analysis entry; steers
        # the next curation pass toward concrete-target tasks (applied after
        # the directive-clearing block below).
        concreteness_steered = False
        for entry in enqueue_entries_raw:
            if not isinstance(entry, dict):
                continue
            raw_goal = entry.get("goal")
            goal = raw_goal.strip() if isinstance(raw_goal, str) else ""
            if not goal:
                continue
            norm = goal.lower()
            if norm in existing_goal_norms:
                logger.debug(
                    "generate_task: dropping duplicate enqueue goal %r", goal,
                )
                continue

            # --- Per-goal content gates (shared with the edit path) ---
            # PR-mutation, terrain-exhaustion, compound-scope, concreteness,
            # phantom-premise, failed-branch-target — see
            # :func:`_vet_goal_content`, which owns the logging and the
            # rejected gate-activation rows.
            rationale_raw = entry.get("rationale")
            rationale = None
            if isinstance(rationale_raw, str):
                trimmed = rationale_raw.strip()[:200]
                rationale = trimmed or None

            rejecting_gate = _vet_goal_content(
                agent,
                goal,
                rationale=rationale,
                recent_records=failed_branch_records,
            )
            if rejecting_gate is not None:
                if rejecting_gate == "concreteness":
                    concreteness_steered = True
                continue

            raw_question = entry.get("linked_question")
            linked: str | None = None
            if isinstance(raw_question, str) and raw_question.strip():
                linked = match_open_question(
                    raw_question, model.open_questions,
                )

            # --- Extract and validate acceptance_criteria ---
            criteria = _parse_acceptance_criteria(
                agent, entry.get("acceptance_criteria"),
            )

            # --- Resolve-filter the workspace citation (proposal 130) ---
            # Mirror EvolutionEngine._plan_from_spec's declared-then-resolve
            # filter: keep only declared ids that are strings AND in the
            # delivered admitted (consumer-visible) set, order-preserving and
            # de-duplicated via ``dict.fromkeys`` (which also bounds the list to
            # |admitted_visible_ids|). Stamped only when a moment was delivered
            # (``workspace_moment_id`` set); then ``workspace_item_ids`` is a
            # list (``[]`` when nothing valid was declared) so the citation-
            # provenance ``None`` (never cited) vs ``[]`` (saw the moment, cited
            # no item) distinction is preserved. With broadcast off both stay
            # ``None`` and the queue write is byte-identical. Advisory only.
            cited_item_ids: list[str] | None = None
            if workspace_moment_id is not None:
                declared = entry.get("workspace_item_ids")
                declared_list = declared if isinstance(declared, list) else []
                cited_item_ids = list(
                    dict.fromkeys(
                        c
                        for c in declared_list
                        if isinstance(c, str) and c in admitted_visible_ids
                    ),
                )

            new_task = QueuedTask(
                goal=goal,
                linked_question=linked,
                rationale=rationale,
                acceptance_criteria=criteria,
                workspace_moment_id=workspace_moment_id,
                workspace_item_ids=cited_item_ids,
            )
            # --- Premise-accuracy leading indicator (issue #2304, zero SDK) ---
            # Measure, at the moment a task is accepted, how many of the ``.py``
            # paths its goal references actually exist on disk. Advisory and
            # measurement-only: nothing is injected into any prompt and no task
            # is dropped — this sits alongside the ``log_gate_activation``
            # writes in ``_vet_goal_content``, which police; this only records.
            # Each path ``_extract_file_paths`` finds is counted against the shared
            # ``_file_reference_exists`` predicate
            # (on-disk existence only — parse-failure is a separate quality axis
            # deliberately out of scope for this leading indicator, #2304).
            # The measurement is banked here but persisted only after the
            # queue-cap truncation below, over the tasks that actually survive.
            referenced_paths = _extract_file_paths(goal)
            missing_count = sum(
                1 for rel_path in referenced_paths
                if not agent._file_reference_exists(rel_path)
            )
            premise_by_task_id[new_task.task_id] = (
                len(referenced_paths), missing_count,
            )
            accepted_new.append(new_task)
            existing_goal_norms.add(norm)

        # --- Enforce queue cap on new entries only (never evict existing) ---
        room = max(0, queue_cap - len(next_queue))
        if len(accepted_new) > room:
            dropped = len(accepted_new) - room
            logger.info(
                "generate_task: queue at cap (%d) — dropping %d new entries",
                queue_cap, dropped,
            )
            accepted_new = accepted_new[:room]

        # --- Fail-closed citation guard (proposal 130) ---
        # Before a new task's citation is persisted, re-check it against the
        # moment the decision actually saw. ``conscious_workspace.validate_citation``
        # is the single authority. On an invalid verdict — cross-moment,
        # unresolved, or an unknown/redacted id — null the task's provenance
        # (direct mutation of the mutable ``QueuedTask``, the same object the
        # rebase below persists, so the persisted queue carries the nulled
        # citation) and log the rejection, while the task itself stays queued:
        # citations are strictly advisory. Only tasks stamped with a delivered
        # moment id (broadcast on) qualify. This runs *before* the rebase
        # because it edits the objects the rebase persists; the ledger writes it
        # feeds — the premise observation and the episode's ``cited_task_ids`` —
        # run after it, over the tasks the rebase actually admitted.
        for task in accepted_new:
            if task.workspace_moment_id is not None:
                verdict = conscious_workspace.validate_citation(
                    moment_id=task.workspace_moment_id,
                    item_ids=task.workspace_item_ids,
                    moment=cited_moment,
                    consumer="task_generation",
                )
                if not verdict.valid:
                    logger.warning(
                        "Nulling invalid conscious-access citation on task %s "
                        "(reason=%s); task queued uncited",
                        task.task_id, verdict.reason,
                    )
                    task.workspace_moment_id = None
                    task.workspace_item_ids = None

        # --- Persist the curation as a delta against the *live* queue ---
        # Everything above ran against ``current_queue``, the snapshot loaded
        # before the priced SDK call. ``_run_task_phase`` pops through the same
        # backend meanwhile, so a whole-queue ``save(next_queue + accepted_new)``
        # would erase any pop that landed inside that window, resurrecting an
        # already-dispatched task (#3561). Instead re-read the queue under the
        # backend's lock and rebase: the snapshot survivors (carrying their
        # curation edits) apply by ``task_id`` onto the live entries, entries the
        # snapshot pruned drop, and entries the snapshot never saw — a concurrent
        # enqueue — carry forward untouched. An edit to a task popped mid-call
        # simply evaporates with it. The SDK call itself stays well outside the
        # lock; only this rebase is serialised.
        snapshot_ids = {q.task_id for q in current_queue}
        survivors_by_id = {q.task_id: q for q in next_queue}
        #: The ``accepted_new`` entries the rebase actually admitted — the cap
        #: is re-applied against the *live* length, so a concurrent enqueue can
        #: shrink this below ``accepted_new``. Every downstream ledger write
        #: keys off it, never off ``accepted_new``, so a recorded task id always
        #: resolves to a really-queued task (#2304).
        admitted: list[QueuedTask] = []

        def _rebase(live: list[QueuedTask]) -> list[QueuedTask]:
            rebased, kept = rebase_curated_queue(
                live,
                snapshot_ids=snapshot_ids,
                survivors_by_id=survivors_by_id,
                incoming=accepted_new,
                queue_cap=queue_cap,
            )
            admitted[:] = kept
            if len(kept) < len(accepted_new):
                logger.info(
                    "generate_task: live queue at cap (%d) — dropping %d new "
                    "entries the pre-call trim had room for",
                    queue_cap, len(accepted_new) - len(kept),
                )
            live[:] = rebased
            return rebased

        agent.task_queue.mutate(_rebase)

        # --- Persist premise observations for the admitted tasks (#2304) ---
        # Keyed off ``admitted`` rather than ``accepted_new`` so a recent-miss
        # task id always resolves to a really-queued task. The episode's
        # ``cited_task_ids`` append (mirroring ``evolve``'s
        # ``_active_cited_plan_ids``) rides the same list, so the episode's
        # causal reach never lists a task the rebase dropped. A surviving
        # ``workspace_moment_id`` is exactly the valid-verdict set: the guard
        # above nulled every invalid one.
        for task in admitted:
            referenced, missing = premise_by_task_id[task.task_id]
            log_premise_observation(
                agent.premise_observations,
                task_id=task.task_id,
                referenced=referenced,
                missing=missing,
            )
            if task.workspace_moment_id is not None:
                agent._active_cited_task_ids.append(task.task_id)

        # Clear the directive when the LLM produced a parseable response —
        # independent of whether any *new* entry survived the post-call
        # gates (terrain). At queue scale this is
        # the right contract: the directive's job was to steer one
        # curation pass; if every proposed entry fell out of a gate, the
        # next pass should fire fresh with whatever the current state of
        # the world is, not keep replaying the same stale steering. The
        # directive still survives SDK failures (exception before this
        # point) and parse errors (returned earlier) so the next iteration
        # can retry with the same guidance after a transient blip.
        # Applied to the live model (#4040): only the directive this pass
        # consumed is cleared — one a concurrent writer set during the SDK
        # await is kept — and no other field is replayed from the snapshot.
        if consumed_directive is not None or concreteness_steered:
            def _consume_directive(live: SelfModel) -> None:
                if (
                    consumed_directive is not None
                    and live.next_task_directive == consumed_directive
                ):
                    live.next_task_directive = None
                # A concreteness rejection re-arms the directive (after the
                # clear above) to steer the next pass toward concrete targets.
                if concreteness_steered:
                    live.next_task_directive = (
                        "Next task must target specific files with a binary "
                        "done condition"
                    )

            agent.consciousness.mutate_self_model(_consume_directive)
        # --- Investigation-observable gate (post-call task-selection actuator) ---
        # With the steering directive that drove this pass cleared above, steer
        # the NEXT pass off the investigation→new-artifact reflex on two arms:
        # (1) a surviving entry that is investigation-framed and commits to a new
        # artifact file while naming no observable signal, or (2) no such goal but
        # the most recent completed task was an owner-stamped COSTUME. Either fills
        # the empty directive slot with a reframe demand and logs one
        # gate_activations row. Empty-slot only — a concreteness re-arm above keeps
        # precedence. The discharge-evidence arm keys only on the tail record, so
        # the single most-recent completed task is all it needs. Zero SDK cost.
        investigation_observable_gate(
            [q.goal for q in admitted],
            agent.config,
            agent.consciousness,
            agent.memory.get_recent_tasks(1),
            agent.gate_activations,
        )
        return
