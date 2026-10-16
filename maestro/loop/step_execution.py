"""Step-group execution carved out of ``maestro.agent`` (proposal 106).

Holds the single stage ``MaestroAgent.run`` executes between the
pre-execution staging of :mod:`maestro.loop.task_setup` and the outcome
classification of :mod:`maestro.loop.task_outcome`: the step-group
execution engine (``# 3.``). That is the ``while group_idx < len(groups)``
loop with its two closures, the single-step fast path, the concurrent
``asyncio.gather`` path, the adaptive replan/splice, the session-reuse
lifecycle, and the trailing executor-session teardown. ``run`` is its sole
caller.

The extraction is behaviour-preserving: the same body, the same effects,
the same five-tuple return — only the receiver changed from ``self`` to a
first positional ``agent`` argument, matching
:func:`maestro.loop.task_delivery.commit_task_artifacts` and
:func:`maestro.loop.task_outcome.classify_task_outcome`. The three
helpers the body reads — ``_build_execution_groups``, ``_should_use_session``,
and the ``_LOOP_ABORT_WARNING`` format string — live here, beside their sole
caller.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from maestro.memory import TaskCheckpoint
from maestro.sdk import SDKError

if TYPE_CHECKING:
    from pathlib import Path

    from maestro.agent import MaestroAgent
    from maestro.executor import StepResult
    from maestro.memory import PlanStep
    from maestro.providers.base import Session
    from maestro.types import TaskSource

logger = logging.getLogger("maestro")


#: WARNING emitted at both ``run()`` step-failure sites (serial + concurrent
#: group) when a step aborts on a repeated tool-call loop
#: (``StepResult.loop_detected``). Shared so the two surfaces stay in sync.
#: Worded to stay accurate whether or not a replan follows — "any replan" is
#: conditional, so it never promises replanning when the ``loop_max_replans``
#: budget is exhausted. ``%s`` is the failed step's description.
_LOOP_ABORT_WARNING = (
    "Step aborted on tool loop; any replan will take a different approach: %s"
)


def _build_execution_groups(
    steps: list[PlanStep],
    completed_ids: set[str] | None = None,
) -> list[list[PlanStep]]:
    """Group steps into execution tiers based on dependency graph.

    Steps within each tier have all their dependencies satisfied by
    completed tiers (or by *completed_ids*).  Steps in a single tier
    can execute concurrently.  Only a ``done`` prerequisite satisfies a
    dependency: a pending step depending — directly or transitively —
    on a ``failed`` or ``running`` step is *blocked*, is left out of
    every tier (and out of the sequential fallback), and stays
    ``pending`` (#3959).

    Falls back to one-step-per-group (sequential) when a cycle or
    broken reference is detected.  If no step declares any dependency
    edges the function also returns one-step-per-group to preserve
    backward-compatible sequential execution.
    """
    ids = [s.step_id for s in steps]
    if len(ids) != len(set(ids)):
        # A duplicate ID would let a historical step satisfy a new step's
        # dependency (#3960); splice sites must remap via ``_splice_replan``.
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate step_id in plan: {dupes}")
    pending = [s for s in steps if s.status == "pending"]

    # No dependency edges declared → preserve sequential execution.
    if all(not s.depends_on for s in pending):
        return [[s] for s in pending]

    done: set[str] = set(completed_ids) if completed_ids else set()
    done.update(s.step_id for s in steps if s.status == "done")
    blocked: set[str] = {
        s.step_id for s in steps
        if s.status not in ("pending", "done") and s.step_id not in done
    }
    grew = True
    while grew:
        grew = False
        for s in pending:
            if s.step_id not in blocked and any(d in blocked for d in s.depends_on):
                blocked.add(s.step_id)
                grew = True
    blocked_pending = [s.step_id for s in pending if s.step_id in blocked]
    if blocked_pending:
        logger.info(
            "Unsatisfied prerequisites block %d pending step(s): %s",
            len(blocked_pending), ", ".join(blocked_pending),
        )
    remaining = [s for s in pending if s.step_id not in blocked]
    groups: list[list[PlanStep]] = []

    while remaining:
        ready = [s for s in remaining if all(d in done for d in s.depends_on)]
        if not ready:
            # Cycle or broken refs — fall back to sequential for safety.
            logger.warning(
                "Dependency cycle or broken refs detected; "
                "falling back to sequential execution for %d remaining steps",
                len(remaining),
            )
            for s in remaining:
                groups.append([s])
            break
        groups.append(ready)
        done.update(s.step_id for s in ready)
        remaining = [s for s in remaining if s not in ready]

    return groups


def _should_use_session(
    groups: list[list[PlanStep]],
    enable_session_reuse: bool,
) -> bool:
    """Whether executor session reuse pays off for *groups*.

    Reuse only helps when at least two single-step tiers share a
    conversation prefix.  Recomputed after every ``_build_execution_groups``
    rebuild so the decision tracks the plan currently being executed, not the
    initial plan's tier shape (#1213).
    """
    return enable_session_reuse and sum(1 for g in groups if len(g) == 1) >= 2


def _splice_replan(
    steps: list[PlanStep], new_steps: list[PlanStep],
) -> list[PlanStep]:
    """Retain the non-pending *steps* and append *new_steps* with unique IDs.

    ``Planner.replan`` only makes IDs unique within one response, so a
    second replan may reuse an ID a retained step already holds (#3960).
    ``_build_execution_groups`` keys readiness on bare ``step_id``, so the
    historical step would then satisfy a dependency meant for the new one.
    A colliding new step is renamed to a fresh ID and that response's own
    ``depends_on`` edges are rewritten through the same map; non-colliding
    IDs are left unchanged.
    """
    executed = [s for s in steps if s.status != "pending"]
    taken = {s.step_id for s in executed}
    renames: dict[str, str] = {}
    for step in new_steps:
        if step.step_id in taken:
            n = 2
            while f"{step.step_id}_r{n}" in taken:
                n += 1
            renames[step.step_id] = f"{step.step_id}_r{n}"
        taken.add(renames.get(step.step_id, step.step_id))
    spliced = [
        dataclasses.replace(
            step,
            step_id=renames.get(step.step_id, step.step_id),
            depends_on=[renames.get(d, d) for d in step.depends_on],
        )
        for step in new_steps
    ]
    return executed + spliced


async def execute_step_groups(
    agent: MaestroAgent,
    *,
    steps: list[PlanStep],
    resume_checkpoint: TaskCheckpoint | None,
    errors: list[str],
    all_output: list[str],
    executed_commands: list[str],
    identity_context: str,
    context: str,
    goal: str,
    run_session: Session | None,
    artifacts_path: Path,
    allowed_branches: frozenset[str] | None,
    branch_name: str | None,
    pr_branch: str | None,
    task_id: str,
    source: TaskSource,
    linked_question: str | None,
    acceptance_criteria: list[str] | None,
    workspace_moment_id: str | None,
    workspace_item_ids: list[str] | None,
    allowed_push_remotes: frozenset[str] | None = None,
) -> tuple[list[PlanStep], list[str], list[str], list[str], int]:
    """Drive the plan to completion — ``run``'s ``# 3.`` block (extracted).

    The step-group execution engine: the ``while group_idx <
    len(groups)`` loop with its two nested closures
    (``_checkpoint_progress``, which snapshots step-group progress for
    crash recovery, and ``_run_step``, the concurrent-tier coroutine
    wrapper), the single-step fast path, the concurrent
    ``asyncio.gather`` path, the adaptive replanning that splices new
    steps and rebuilds the execution groups, the session-reuse
    lifecycle, and the trailing ``finally`` that ends the executor
    session.

    A behaviour-preserving lift of the inline block: it reassigns
    nothing in the caller's scope and returns exactly the five values
    that scope reads back — ``steps``, ``errors``, ``all_output``,
    ``executed_commands``, and ``replans``. The loop-local state it
    owns (``groups``, ``completed_ids``, ``group_idx``,
    ``use_session``, ``session_active``) never escapes. Both closures
    are declared once, above the loop, so ``_run_step`` is not rebuilt
    per concurrent group (#1174).

    *allowed_push_remotes* scopes the action gate's push remotes.
    """
    exec_workspace = str(agent.config.workspace)
    # 3. Execute step groups (with adaptive re-planning).
    # ``replans`` and ``completed_ids`` are derived from the
    # checkpoint on resume (steps already marked ``done`` are skipped
    # by ``_build_execution_groups``); both reduce to the fresh-start
    # values when there is no checkpoint.
    replans = resume_checkpoint.replans if resume_checkpoint else 0
    if len({s.step_id for s in steps}) != len(steps):
        # A legacy checkpoint written before #3960 may hold a replan that
        # reused a retained ID; remap its pending steps before tiering.
        # Settled duplicates are renamed first (nothing pending can name
        # them any more), then the pending tail is spliced as a replan.
        settled: list[PlanStep] = []
        for s in steps:
            if s.status != "pending":
                settled = _splice_replan(settled, [s])
        steps[:] = _splice_replan(
            settled, [s for s in steps if s.status == "pending"],
        )
    completed_ids: set[str] = {
        s.step_id for s in steps if s.status == "done"
    }
    groups = _build_execution_groups(steps, completed_ids)
    group_idx = 0

    def _checkpoint_progress() -> None:
        """Persist step-group progress for crash recovery.

        Owned task branches only: ``pr_branch`` runs and
        branch-fallback runs (``branch_name is None``) are skipped.
        Reads the live loop state by closure so the latest step
        statuses, errors, output, and replan count are captured.
        """
        branch = branch_name
        if pr_branch is not None or branch is None:
            return
        agent.memory.save_checkpoint(
            TaskCheckpoint(
                task_id=task_id,
                goal=goal,
                source=source,
                branch=branch,
                steps=list(steps),
                errors=list(errors),
                all_output=list(all_output),
                executed_commands=list(executed_commands),
                artifacts_dir=str(artifacts_path),
                replans=replans,
                timestamp=datetime.now(UTC).isoformat(),
                linked_question=linked_question,
                acceptance_criteria=list(acceptance_criteria or []),
                workspace_moment_id=workspace_moment_id,
                workspace_item_ids=(
                    list(workspace_item_ids)
                    if workspace_item_ids is not None
                    else None
                ),
            )
        )

    async def _run_step(
        s: PlanStep,
        prior: list[PlanStep] | None,
    ) -> tuple[PlanStep, StepResult]:
        """Execute one step of a concurrent group.

        Declared once here rather than inside the group loop: every
        value it closes over (``identity_context``,
        ``artifacts_path``, ``allowed_branches``, ``branch_name``) is
        bound before the loop opens and never reassigned, and the two
        that do vary per call are explicit parameters. ``prior``
        deliberately shadows the loop-local of the same name — the call
        site passes the live value, so capturing it instead would freeze
        it (#1174).
        """
        return s, await agent.executor.execute(
            s, exec_workspace, identity_context,
            artifacts_dir=str(artifacts_path),
            prior_steps=prior,
            allowed_branches=allowed_branches,
            current_branch=branch_name,
            allowed_push_remotes=allowed_push_remotes,
        )

    # Session reuse: share an opaque session handle across sequential tiers
    # for prompt cache reuse on the full conversation prefix.
    use_session = _should_use_session(
        groups, agent.config.enable_session_reuse
    )
    session_active = False

    def _reschedule() -> None:
        """Rebuild the tiers from the live step statuses and restart.

        Shared by the replan-splice arms and the unreplaced-failure arm:
        recomputes session reuse (a new schedule can change the tier
        shape, #1213), rewinds ``group_idx``, and snapshots the revised
        schedule so a crash resumes from it.
        """
        nonlocal groups, use_session, group_idx
        groups = _build_execution_groups(steps, completed_ids)
        use_session = _should_use_session(
            groups, agent.config.enable_session_reuse
        )
        group_idx = 0
        _checkpoint_progress()

    try:
        while group_idx < len(groups):
            # Readiness backstop: dispatch only ``pending`` steps whose
            # declared prerequisites (when present in the plan) are all
            # ``done`` — a blocked step stays ``pending`` and is counted
            # skipped by the outcome (#3959).
            status_by_id = {s.step_id: s.status for s in steps}
            group = [
                s for s in groups[group_idx]
                if s.status == "pending" and all(
                    status_by_id.get(d, "done") == "done" for d in s.depends_on
                )
            ]
            if not group:
                group_idx += 1
                continue
            prior = [s for s in steps if s.status == "done"] or None

            if len(group) == 1:
                # Single step — run directly (common path)
                step = group[0]
                step.status = "running"

                if use_session and not session_active:
                    await agent.executor.start_session(
                        exec_workspace,
                        identity_context,
                        allowed_branches=allowed_branches,
                        current_branch=branch_name,
                        allowed_push_remotes=allowed_push_remotes,
                    )
                    session_active = True

                result = await agent.executor.execute(
                    step, exec_workspace, identity_context,
                    artifacts_dir=str(artifacts_path),
                    prior_steps=prior,
                    use_session=use_session and session_active,
                    allowed_branches=allowed_branches,
                    current_branch=branch_name,
                    allowed_push_remotes=allowed_push_remotes,
                )
                if result.success:
                    step.status = "done"
                    step.result = result.output
                    all_output.append(result.output)
                    executed_commands.extend(result.bash_commands)
                    completed_ids.add(step.step_id)
                else:
                    step.status = "failed"
                    error_msg = result.error or "Unknown error"
                    step.result = error_msg
                    errors.append(error_msg)
                    if result.loop_detected:
                        logger.warning(
                            _LOOP_ABORT_WARNING, step.description
                        )
                    if replans < agent.config.loop_max_replans:
                        await agent.executor.end_session()
                        session_active = False
                        done_list = [s for s in steps if s.status == "done"]
                        new_steps = await agent.planner.replan(
                            goal=goal,
                            identity_context=identity_context,
                            completed_steps=done_list,
                            failed_step=step,
                            context=context,
                            session=run_session,
                            loop_detected=result.loop_detected,
                        )
                        # Debit the budget on every replan *attempt*,
                        # not only a non-empty reply — an unrecoverable
                        # or unparseable ``replan()`` still spent an LLM
                        # call and must count against ``max_replans``
                        # (#451).
                        replans += 1
                        if new_steps:
                            steps[:] = _splice_replan(steps, new_steps)
                            # Session already torn down above.
                            _reschedule()
                            continue
            else:
                # Multiple independent steps — run concurrently
                # End session before concurrent tier to prevent contamination.
                if session_active:
                    await agent.executor.end_session()
                    session_active = False

                for s in group:
                    s.status = "running"

                raw_results = await asyncio.gather(
                    *[_run_step(s, prior) for s in group],
                    return_exceptions=True,
                )

                # Check for control flow and infrastructure errors first.
                # This pre-scan is a deliberately separate first pass: a
                # cancellation (or any other non-``Exception``
                # ``BaseException``) or an SDKError anywhere in the group
                # must raise *before* any step status, ``all_output``
                # entry, or ``errors`` entry is mutated. Folding it into
                # the attribution loop below would let steps at lower
                # indices commit partial state before the raise, so keep
                # the two-pass shape. Control flow outranks
                # infrastructure failure, so it is scanned first: a
                # child's ``CancelledError`` propagates exactly as it
                # does from a single-step tier (#3969).
                outcomes: list[Exception | tuple[PlanStep, StepResult]] = []
                for item in raw_results:
                    if isinstance(item, BaseException) and not isinstance(
                        item, Exception,
                    ):
                        raise item
                    outcomes.append(item)
                for item in raw_results:
                    if isinstance(item, SDKError):
                        raise item

                failed_in_group: PlanStep | None = None
                failed_loop_detected = False
                # ``asyncio.gather`` preserves positional order, so
                # ``outcomes[i]`` is ``group[i]``'s outcome. Consume
                # that pairing directly rather than re-deriving the
                # blamed step from mutable status state (#1173).
                for step, outcome in zip(group, outcomes, strict=True):
                    if isinstance(outcome, Exception):
                        step.status = "failed"
                        step.result = str(outcome)
                        errors.append(str(outcome))
                        if failed_in_group is None:
                            failed_in_group = step
                        continue
                    # ``_run_step`` also echoes back the step it ran, but
                    # the positional pairing above is the authority and
                    # the echo cannot diverge from it: ``_run_step(s)``
                    # closes over its own ``s``. Discard the echo rather
                    # than guarding it — an ``assert`` here would be
                    # stripped under ``python -O``, and a ``raise`` would
                    # be an unreachable, uncoverable branch.
                    _, result = outcome
                    if result.success:
                        step.status = "done"
                        step.result = result.output
                        all_output.append(result.output)
                        executed_commands.extend(result.bash_commands)
                        completed_ids.add(step.step_id)
                    else:
                        step.status = "failed"
                        error_msg = result.error or "Unknown error"
                        step.result = error_msg
                        errors.append(error_msg)
                        if failed_in_group is None:
                            failed_in_group = step
                            failed_loop_detected = result.loop_detected

                if failed_loop_detected and failed_in_group is not None:
                    logger.warning(
                        _LOOP_ABORT_WARNING, failed_in_group.description
                    )
                if failed_in_group and replans < agent.config.loop_max_replans:
                    done_list = [s for s in steps if s.status == "done"]
                    new_steps = await agent.planner.replan(
                        goal=goal,
                        identity_context=identity_context,
                        completed_steps=done_list,
                        failed_step=failed_in_group,
                        context=context,
                        session=run_session,
                        loop_detected=failed_loop_detected,
                    )
                    # Debit the budget on every replan *attempt*, not
                    # only a non-empty reply — an unrecoverable or
                    # unparseable ``replan()`` still spent an LLM call
                    # and must count against ``max_replans`` (#451).
                    replans += 1
                    if new_steps:
                        steps[:] = _splice_replan(steps, new_steps)
                        # Session already torn down before this tier.
                        _reschedule()
                        continue

            if any(s.status == "failed" for s in group):
                # An unreplaced failure: the precomputed tiers may hold
                # dependents of the failed step, so rebuild rather than
                # advance — blocked descendants drop out while
                # independent ready work keeps running (#3959). Tear a
                # live session down first: the rebuilt schedule may
                # turn session reuse off, which would strand it open.
                if session_active:
                    await agent.executor.end_session()
                    session_active = False
                _reschedule()
                continue
            # Checkpoint after each completed group so a crash costs
            # at most the in-flight group, not the whole task.
            _checkpoint_progress()
            group_idx += 1
    finally:
        await agent.executor.end_session()
    return steps, errors, all_output, executed_commands, replans
