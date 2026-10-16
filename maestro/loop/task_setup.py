"""Pre-execution staging stages carved out of ``maestro.agent`` (proposal 106).

Holds the four stages ``MaestroAgent.run`` executes back-to-back between the
lifecycle entry trio and step execution, and that between them produce every
local :func:`maestro.loop.step_execution.execute_step_groups` consumes:
resume-checkpoint resolution
(``# 0.``), plan resolution (``# 1.``), working-branch selection (``# 2.``),
and workspace preparation (``# 2b.``). They share one concern — getting the run
staged before any step executes — and they run as one contiguous straight-line
stretch of ``run``.

The extraction is behaviour-preserving: the same bodies, the same call order,
the same effects — only the receiver changed from ``self`` to a first
positional ``agent`` argument, matching
:func:`maestro.loop.lifecycle.teardown_task_run` and
:func:`maestro.loop.record_binds.apply_remediation_narration_framing`. ``VCSError``
and ``task_branch`` are imported directly; the logger keeps the ``"maestro"``
name so existing ``caplog`` assertions match unchanged.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from maestro.vcs import VCSError, task_branch

if TYPE_CHECKING:
    from pathlib import Path

    from maestro.agent import MaestroAgent
    from maestro.memory import PlanStep, TaskCheckpoint
    from maestro.providers.base import Session

logger = logging.getLogger("maestro")


def resolve_resume_checkpoint(
    agent: MaestroAgent, *, pr_branch: str | None, task_id: str,
) -> tuple[TaskCheckpoint | None, str | None]:
    """Decide whether to resume from a checkpoint — ``run``'s ``# 0.`` block
    (extracted).

    A behaviour-preserving lift of the inline ``# 0.`` resume block: owned
    task branches only (``pr_branch is None`` — ``pr_branch`` runs are
    owned/pushed by ``_tend_single_pr`` and skip checkpoint loading
    entirely), it loads any surviving checkpoint and honours it only while
    its recorded branch still resolves (``rev_parse`` non-``None``),
    otherwise discarding the orphan via ``clear_checkpoint`` so the task
    starts cleanly. It reassigns nothing in the caller's scope and returns
    the two locals that scope reads back — ``(resume_checkpoint,
    resume_branch)`` — which flow unchanged into ``resolve_task_plan`` and
    ``prepare_task_branch``. Behaviour-preservation of the whole block is
    proven by the full-``run`` regression harness (``tests/test_checkpoint.py``
    for the resume/checkpoint lifecycle).
    """
    # 0. Resume from a surviving checkpoint. Owned task branches
    # only — ``pr_branch`` runs are owned/pushed by
    # ``_tend_single_pr`` and are out of scope. A checkpoint is only
    # honoured when its branch still resolves; otherwise it is an
    # orphan and is cleared so the task starts cleanly.
    # Recovery is forward-only for *uncommitted* work: the checkpoint
    # restores which steps already ran (so completed groups are
    # skipped), and the entry-time reset below discards any dirty
    # working tree, so a resume never replays earlier steps' unstaged
    # edits. Edits those steps *committed* need no replaying — Bash
    # permits ``git commit``, and the resume checks the recorded
    # branch back out, so they are simply already in the tree.
    # A checkpoint outlives its run whenever an exception escapes
    # before ``clear_checkpoint`` (which runs right after
    # ``record_task``), but it is *honoured* only while its branch
    # still resolves — and the outer ``finally`` force-deletes any
    # branch step 5 did not keep, so an escape before step 5 leaves an
    # orphan that is cleared here rather than resumed.
    resume_checkpoint: TaskCheckpoint | None = None
    resume_branch: str | None = None
    if pr_branch is None:
        loaded = agent.memory.load_checkpoint(task_id)
        if (
            loaded is not None
            and loaded.branch is not None
            and agent._vcs.rev_parse(loaded.branch) is not None
        ):
            resume_checkpoint = loaded
            resume_branch = loaded.branch
            logger.info(
                "Resuming task %s from checkpoint: %d/%d steps done, "
                "replans=%d, branch=%s",
                task_id,
                sum(1 for s in loaded.steps if s.status == "done"),
                len(loaded.steps),
                loaded.replans,
                loaded.branch,
            )
        elif loaded is not None:
            logger.info(
                "Discarding orphaned checkpoint for %s (branch %r "
                "missing); starting fresh", task_id, loaded.branch,
            )
            agent.memory.clear_checkpoint(task_id)
    return resume_checkpoint, resume_branch


async def resolve_task_plan(
    agent: MaestroAgent,
    *,
    resume_checkpoint: TaskCheckpoint | None,
    goal: str,
    identity_context: str,
    context: str,
    run_session: Session | None,
) -> tuple[list[PlanStep], list[str], list[str], list[str]]:
    """Produce the initial plan and accumulators — ``run``'s ``# 1.`` block
    (extracted).

    A behaviour-preserving lift of the inline ``# 1.`` Plan block: on resume
    it restores the surviving checkpoint's ``steps`` and returns fresh
    ``list(...)`` copies of its three accumulators (so the caller cannot
    mutate the checkpoint's own list objects); otherwise it decomposes the
    goal through ``planner.decompose`` on the shared run session and returns
    three empty accumulators. It reassigns nothing in the caller's scope and
    returns exactly the four locals that scope reads back —
    ``(steps, errors, all_output, executed_commands)`` — which flow unchanged
    into :func:`maestro.loop.step_execution.execute_step_groups`.
    Behaviour-preservation of the whole block is proven by the full-``run``
    regression harness
    (``tests/test_checkpoint.py`` for the resume/checkpoint lifecycle,
    ``tests/test_agent.py`` / ``tests/test_run_task_phase.py`` for full-run).
    """
    # 1. Plan (skipped on resume — the checkpoint carries the steps)
    errors: list[str]
    all_output: list[str]
    # The executed-command record (Bash tool inputs), threaded alongside
    # ``all_output`` (assistant prose) so the red-read bind keys on what
    # actually ran, not what the model narrated.
    executed_commands: list[str]
    if resume_checkpoint is not None:
        steps = resume_checkpoint.steps
        errors = list(resume_checkpoint.errors)
        all_output = list(resume_checkpoint.all_output)
        executed_commands = list(resume_checkpoint.executed_commands)
    else:
        steps = await agent.planner.decompose(
            goal, identity_context, context, session=run_session,
            require_claude_md_update=agent.config.require_claude_md_update,
            workspace_moment=agent._broadcast_moment_prefix("task_execution"),
        )
        errors = []
        all_output = []
        executed_commands = []
    return steps, errors, all_output, executed_commands


def prepare_task_branch(
    agent: MaestroAgent,
    *,
    pr_branch: str | None,
    resume_branch: str | None,
    task_id: str,
) -> tuple[str | None, str | None, tuple[str, ...] | None]:
    """Select the working branch — ``run``'s ``# 2.`` block (extracted).

    A behaviour-preserving lift of the inline block: it performs the
    entry-time working-tree reset (discard changes, return to ``main``,
    warn if the tree is still dirty) and then the three-way branch
    selection — check out an existing PR head (remediation lap), check out
    a surviving checkpoint branch (resume), or cut a fresh
    ``maestro/task/<id>`` branch and stamp its creation-head SHA (normal
    path). It reassigns nothing in the caller's scope and returns the three
    locals that scope reads back: ``branch_name`` (the branch the task runs
    on, or ``None`` to fall back to the current branch when selection
    fails) and ``branch_base_sha`` (the fresh branch's creation-head
    watermark, set only on the fresh-branch success path; ``None``
    otherwise — resume, PR branch, failed creation, or failed probe), plus
    ``pr_head_watermark`` (the local and ``origin/`` PR head SHAs at
    checkout — whichever resolved — set only on the
    PR-branch arm once the checkout succeeded; ``None`` otherwise) — the
    lap-entry head the CI red-read bind measures lap-authored commits from.
    """
    branch_name: str | None = None
    branch_base_sha: str | None = None
    pr_head_watermark: tuple[str, ...] | None = None
    # Entry-time defense: ensure_main_branch() is best-effort and
    # can leave the tree on a stale task branch or dirty on main.
    # Reset before branching so leftover diff from a prior run()
    # cannot migrate onto this task branch.
    with contextlib.suppress(Exception):
        agent._vcs.discard_changes()
    with contextlib.suppress(VCSError):
        agent._vcs.checkout("main")
    try:
        if agent._vcs.has_changes():
            logger.warning("Tree still dirty after entry-time reset")
    except Exception:
        logger.warning(
            "has_changes failed during entry-time reset", exc_info=True,
        )
    if pr_branch is not None:
        # PR-remediation path: commit on the PR's head ref so
        # ``_tend_single_pr`` can deliver the local commits via
        # its trailing ``push``. Mirrors the same try/origin/
        # checkout_new_branch fallback Tend PR uses.
        branch_name = pr_branch
        try:
            agent._vcs.checkout(pr_branch)
        except VCSError:
            try:
                agent._vcs.checkout(f"origin/{pr_branch}")
                agent._vcs.checkout_new_branch(pr_branch)
            except VCSError:
                logger.warning(
                    "Failed to switch to PR branch %s", pr_branch,
                    exc_info=True,
                )
                branch_name = None  # fall back to current branch
        if branch_name is not None:
            # Lap-entry head watermark: commits the agent authors on the PR
            # branch this lap are counted from here. Kept out of
            # ``branch_base_sha`` so the #2938 keep scoping is unchanged.
            # Both the local and ``origin/`` heads are stamped: a stale local
            # ref fast-forwarded onto upstream this lap must not count those
            # upstream commits as lap-authored. No fetch precedes this
            # read, so that protection is only as fresh as the last fetch:
            # a non-merge commit pushed upstream since then and pulled this
            # lap is still counted.
            heads = (
                agent._vcs.rev_parse(pr_branch),
                agent._vcs.rev_parse(f"origin/{pr_branch}"),
            )
            pr_head_watermark = (
                tuple(dict.fromkeys(h for h in heads if h is not None)) or None
            )
    elif resume_branch is not None:
        # Resume: switch onto the checkpoint's existing branch
        # instead of minting a fresh one, so completed commits and
        # working-tree edits are preserved.
        branch_name = resume_branch
        try:
            agent._vcs.checkout(resume_branch)
        except VCSError:
            logger.warning(
                "Failed to switch to checkpoint branch %s",
                resume_branch, exc_info=True,
            )
            branch_name = None  # fall back to current branch
    else:
        _branch = task_branch(task_id)
        branch_name = _branch
        try:
            agent._vcs.checkout_new_branch(_branch)
        except VCSError:
            logger.warning(
                "Failed to create task branch %s", branch_name,
                exc_info=True,
            )
            branch_name = None  # fall back to current branch
        else:
            # Stamp the creation head. The entry-time ``checkout("main")``
            # above is best-effort, so a fresh task branch can be cut
            # from a stale branch that is *already* ahead of ``main`` —
            # step 5's commit count alone would then read inherited
            # commits as this task's work. ``rev_parse`` is best-effort
            # (``None`` on failure), which degrades to that count.
            branch_base_sha = agent._vcs.rev_parse(_branch)
    return branch_name, branch_base_sha, pr_head_watermark


def prepare_task_workspace(
    agent: MaestroAgent, *, branch_name: str | None, task_id: str,
) -> tuple[frozenset[str] | None, Path]:
    """Prepare the execution workspace — ``run``'s ``# 2b.`` block (extracted).

    A behaviour-preserving lift of the two inline workspace-preparation
    steps that sit between :func:`prepare_task_branch` and
    :func:`maestro.loop.step_execution.execute_step_groups`. It reassigns
    nothing in the caller's scope and returns the two locals that scope reads
    back: ``allowed_branches``
    (the task executor's action-gate owned-writable branch set) and
    ``artifacts_path`` (the per-task artifacts directory, created here).

    Owned-writable branch set for the task executor's action gate:
    flag-off / branchless collapses to today's ``{branch_name}`` / ``None``;
    flag-on widens to own open evolution-PR heads. The same ``branch_name``
    is threaded to the executor as the gate's ``current_branch``
    (proposal 099) so a bare ``HEAD`` / refspec-less push resolves to it —
    always already a member of ``allowed_branches`` and never
    ``main``/``master``; ``None`` (the branch-fallback path) keeps the gate
    fail-closed.
    """
    allowed_branches = agent._gate_allowed_branches(branch_name)
    # Create task-specific artifacts directory.
    artifacts_path = agent.config.artifacts_dir / task_id
    artifacts_path.mkdir(parents=True, exist_ok=True)
    return allowed_branches, artifacts_path
