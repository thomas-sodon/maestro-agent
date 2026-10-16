"""Record-assembly stages carved out of ``maestro.agent`` (proposal 106).

Holds the five stages ``MaestroAgent.run`` executes once every step group has
run and the branch is committed — the tail that turns a finished lap into a
persisted ``TaskRecord`` and its aftermath: the capped ``result_summary``
composition, the best-effort findings distillation (``# 5b.``), the
``TaskRecord`` assembly, the clean-branch promotion directive (``# 5g.``), and
the post-task reflection journal entry plus completion notice (``# 6.``). They
form one concern — summary → findings → record → post-record steering →
reflection — and ``run`` is their sole caller.

The extraction is behaviour-preserving: the same bodies, the same call order,
the same effects — only the receiver changed from ``self`` to a first
positional ``agent: MaestroAgent`` argument, matching
:func:`maestro.loop.task_delivery.commit_task_artifacts` and
:func:`maestro.loop.step_execution.execute_step_groups`.
``_cap_task_summary`` is reached through the instance
(``agent._cap_task_summary``) rather than the class, since
``tests/test_compose_task_summary.py`` patches it as an instance attribute.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from maestro.consciousness import Consciousness
from maestro.memory import PlanStep, TaskRecord
from maestro.sdk import SDKError

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.providers.base import Session
    from maestro.types import TaskOutcome, TaskSource

logger = logging.getLogger("maestro")


def compose_task_summary(
    agent: MaestroAgent,
    *,
    all_output: list[str],
    kept_branch: str | None,
    remediation_note: str | None,
) -> str:
    """Compose the task ``result_summary`` — ``run``'s inline summary-building
    block (proposal 106), extracted verbatim.

    Joins the step outputs, appends the kept-branch suffix, reserves room for
    any remediation note the later ``apply_remediation_narration_framing``
    prepend will consume, and delegates to the shared static
    ``_cap_task_summary`` for the 500-char budget. A pure/sync
    behaviour-preserving lift — reassigns nothing in the caller's scope — so
    the budget arithmetic is now unit-testable in isolation
    (``tests/test_compose_task_summary.py``) rather than only end-to-end. The
    full-``run`` regression harness (``tests/test_agent_task_branch.py``'s
    ``test_run_summary_*`` suite, which now flows through this stage) proves
    the block's behaviour is unchanged.
    """
    joined = "\n".join(all_output).strip() if all_output else ""
    branch_suffix = f"\n\nBranch: {kept_branch}" if kept_branch else ""
    # When ``apply_remediation_narration_framing`` will prepend a
    # remediation note, reserve room for it so the trailing branch
    # suffix survives the 500-char cap instead of being truncated off
    # the end by ``_frame_remediation_note`` (#2457).
    note_reserve = len(f"{remediation_note} ") if remediation_note else 0
    return agent._cap_task_summary(joined, branch_suffix, note_reserve)


async def distill_task_findings(
    agent: MaestroAgent,
    *,
    goal: str,
    steps: list[PlanStep],
    outcome: TaskOutcome,
    run_session: Session | None,
) -> str | None:
    """Distill key findings best-effort — the verbatim lift of ``run``'s
    ``# 5b.`` block (proposal 106).

    A behaviour-preserving delegate: it reads every run-scoped value as a
    keyword param, reassigns none of the caller's locals, and returns the
    ``key_findings`` value (or ``None``) the enclosing scope reads back.

    Enrichment runs after every step has executed and the branch is
    committed, so ANY SDK failure here must not abort record-construction
    downstream — that would permanently discard the completed task's
    outcome, errors, duration, and artifacts along with its already-popped
    queue slot. The catch is deliberately category-blind (not just the
    transient categories): the record must survive regardless of why the
    cosmetic enrichment failed.
    ``_distill_findings`` keeps its own ``except SDKError: raise``
    (fail-loud for its direct callers); this caller degrades to ``None``,
    the documented default. Nothing is swallowed for good — a persistent
    fault re-surfaces at the next SDK call, where the loop's
    resume-in-place and crash machinery handles it on its merits.

    The ``session=run_session`` forwarding is preserved exactly, since it
    is asserted by the session-lifecycle regression tests.
    """
    try:
        return await agent._distill_findings(
            goal, steps, outcome, session=run_session,
        )
    except SDKError:
        logger.warning(
            "Findings distillation failed; recording task without "
            "key_findings", exc_info=True,
        )
        return None


def assemble_task_record(
    agent: MaestroAgent,
    *,
    task_id: str,
    goal: str,
    steps: list[PlanStep],
    outcome: TaskOutcome,
    summary: str,
    errors: list[str],
    duration: float,
    source: TaskSource,
    kept_branch: str | None,
    key_findings: str | None,
    artifacts_path: Path,
    replans: int,
    linked_question: str | None,
    criteria_verdicts: list[dict[str, str]] | None,
    pr_head_sha_stamp: str | None,
    base_head_sha_stamp: str | None,
    merge_clean: bool | None,
    pr_readiness_snapshot_stamp: dict[str, object] | None,
    push_delivered: bool | None,
    push_error: str | None,
    workspace_moment_id: str | None,
    workspace_item_ids: list[str] | None,
) -> TaskRecord:
    """Assemble the completed run's ``TaskRecord`` — ``run``'s inline
    ``record = TaskRecord(...)`` block, extracted verbatim (proposal 106).

    The final glue block ``run`` still built inline: it maps the locals every
    prior stage produced onto the ``TaskRecord`` fields (``steps`` → ``plan``,
    ``summary`` → ``result_summary``, ``kept_branch`` → ``branch``,
    ``artifacts_path`` → ``artifacts_dir``, and the ``*_stamp`` →
    ``pr_head_sha`` / ``base_head_sha`` / ``pr_readiness_snapshot`` renames),
    computing the three derived fields inside so the mapping is unchanged:
    ``timestamp`` (the record's mint moment), ``duration_secs`` (the passed-in
    *duration* rounded to 2 dp), and ``artifacts_dir`` (``str(artifacts_path)``).

    A pure/sync behaviour-preserving lift — it reassigns nothing in the
    caller's scope and returns exactly the record ``run`` reads back — so the
    assembled record is byte-for-byte identical to the inline construction and
    the field mapping is now unit-testable in isolation
    (``tests/test_assemble_task_record.py``) rather than only end-to-end
    through a full ``run``. The full-``run`` regression harness
    (``tests/test_agent_task_branch.py``'s ``record.*`` assertions) proves the block's
    behaviour is unchanged.

    Precondition: *duration* is captured by the caller before the distillation
    LLM call and the record-time actuators, so ``duration_secs`` measures the
    task's work, not the post-run bookkeeping — capturing it here would fold
    that tail into the figure, a behaviour change.
    """
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=steps,
        outcome=outcome,
        result_summary=summary,
        errors=errors,
        duration_secs=round(duration, 2),
        timestamp=datetime.now(UTC).isoformat(),
        source=source,
        branch=kept_branch,
        key_findings=key_findings,
        artifacts_dir=str(artifacts_path),
        replans=replans,
        linked_question=linked_question,
        criteria_verdicts=criteria_verdicts,
        pr_head_sha=pr_head_sha_stamp,
        base_head_sha=base_head_sha_stamp,
        merge_clean=merge_clean,
        pr_readiness_snapshot=pr_readiness_snapshot_stamp,
        push_delivered=push_delivered,
        push_error=push_error,
        workspace_moment_id=workspace_moment_id,
        workspace_item_ids=workspace_item_ids,
    )


def apply_merge_clean_promotion_directive(
    agent: MaestroAgent,
    *,
    merge_clean: bool | None,
    kept_branch: str | None,
    task_id: str,
) -> None:
    """Steer the next ``generate_task`` to promote a cleanly-mergeable kept
    branch — ``run``'s ``# 5g.`` block, extracted verbatim.

    Runs after the ``TaskRecord`` is persisted, so a self-model write can
    never drop the task record. It reads run-scoped values only (all passed
    in as keyword params), reassigns none of them, and touches no checkpoint
    or outcome machinery — a behaviour-preserving delegate, not a new stage.
    Gated by the branch-promotion-pressure master switch, it fills *only* an
    empty ``next_task_directive`` slot, so it defers to any directive another
    bridge already queued (e.g. the ``wip_primacy`` finish/hand-off
    directive for an open own PR). ``merge_clean`` is clean-mergeability,
    NOT landing.
    """
    if merge_clean is True and agent.config.branch_promotion_pressure_enabled:
        model = agent.consciousness.load_self_model()
        if not model.next_task_directive:
            model.next_task_directive = (
                f"Branch {kept_branch} merges cleanly — "
                f"promote via source_task_id={task_id}"
            )
            agent.consciousness.save_self_model(model)


# Producer-side caps on the reflection entry (Context bloat caps).
_REFLECTION_GOAL_CHARS = 200
_REFLECTION_ERRORS_CHARS = 300


async def run_post_task_reflection(
    agent: MaestroAgent,
    *,
    goal: str,
    task_id: str,
    outcome: TaskOutcome,
    steps: list[PlanStep],
    errors: list[str],
    failed: int,
    key_findings: str | None,
    summary: str,
    linked_question: str | None,
    source: TaskSource,
) -> None:
    """Write the post-task reflection journal entry and fire the completion
    notice — ``run``'s ``# 6.`` tail.

    Runs after the outcome is classified and the ``TaskRecord`` is built.
    It reads run-scoped values only (all passed in as keyword params),
    reassigns none of them, and touches no checkpoint, branch, or
    outcome machinery — it is a delegate, not a new stage in the control
    flow. The journal entry carries a capped first-line goal label and a
    capped errors rendering (#4372); the completion notice still uses the
    full goal. The journal write is guarded so a journal failure never
    blocks the completion notice.
    """
    target_prefix = (
        f"[Targeting: {linked_question}] " if linked_question else ""
    )
    # A remediation lap's ``goal`` is the full multi-KB directive; the
    # event record carries only its first-line label, so one entry stays
    # bounded in ``events.jsonl`` (#4372).
    goal_label = Consciousness._truncate_item(
        goal.strip().splitlines()[0] if goal.strip() else goal,
        _REFLECTION_GOAL_CHARS,
    )
    errors_text = Consciousness._truncate_item(
        str(errors), _REFLECTION_ERRORS_CHARS
    )
    try:
        if outcome == "failure":
            agent.journal.record_event(
                content=f"{target_prefix}Task failed: {goal_label}. Errors: {errors_text}. What went wrong?",
                entry_type="reflection",
                triggered_by=f"task:{task_id}",
            )
        elif outcome == "partial":
            findings_text = (key_findings or summary)[:300]
            agent.journal.record_event(
                content=f"{target_prefix}Partial success: {goal_label}. {failed}/{len(steps)} steps failed. Findings: {findings_text}. Errors: {errors_text}. What can I learn?",
                entry_type="reflection",
                triggered_by=f"task:{task_id}",
            )
        elif outcome == "success":
            findings_text = (key_findings or summary)[:500]
            agent.journal.record_event(
                content=f"{target_prefix}Completed: {goal_label}. Findings: {findings_text}",
                entry_type="reflection",
                triggered_by=f"task:{task_id}",
            )
    except Exception:
        logger.warning("Failed to write post-task journal entry", exc_info=True)

    # Notify
    if outcome == "failure":
        message = f"Task failed: {goal}"
    else:
        message = f"Task completed ({outcome}): {goal}"
    if source == "self_directed" and linked_question:
        message += f"\nTargeting: {linked_question}"
        if key_findings:
            message += f"\nFinding: {key_findings[:500]}"
    await agent._notify_best_effort(
        message,
        failure_log="Notification failed",
    )
