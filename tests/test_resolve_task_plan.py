"""Unit tests for ``maestro.loop.task_setup.resolve_task_plan``.

The stage is the verbatim lift of ``run``'s ``# 1.`` Plan block (proposal 106):
it either restores the initial step list and accumulators from a surviving
resume checkpoint, or decomposes the goal through ``planner.decompose`` on the
shared run session. These tests call the function directly with keyword args and
pin both arms; the planner seam is an ``AsyncMock`` and ``_broadcast_moment_prefix``
is stubbed, so nothing shells out or reaches an SDK.

Behaviour-preservation of the *whole* block is proven by the full-``run``
regression harness (``tests/test_checkpoint.py`` for the resume/checkpoint
lifecycle, ``tests/test_agent.py`` / ``tests/test_run_task_phase.py`` for
full-run mechanics), which now flows through this function unchanged; these unit
tests exercise the extracted stage in isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.loop.task_setup import resolve_task_plan
from maestro.memory import PlanStep, TaskCheckpoint

_TASK_BRANCH = "maestro/task/tid"


@pytest.mark.asyncio
async def test_resume_path_restores_steps_and_copies_accumulators(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) resume arm: a checkpoint restores ``steps`` verbatim and returns fresh
    ``list(...)`` copies of the three accumulators — equal by value but distinct
    objects, so the caller cannot mutate the checkpoint's own lists — and fresh
    planning is skipped entirely (``decompose`` never awaited)."""
    agent = make_agent()
    agent.planner.decompose = AsyncMock()
    cp = TaskCheckpoint(
        task_id="tid",
        goal="g",
        branch=_TASK_BRANCH,
        steps=[PlanStep("s1", "restored", status="done")],
        errors=["boom"],
        all_output=["prior output"],
        executed_commands=["echo prior"],
    )

    steps, errors, all_output, executed_commands = await resolve_task_plan(agent,
        resume_checkpoint=cp,
        goal="the goal",
        identity_context="id-ctx",
        context="ctx",
        run_session=None,
    )

    # ``steps`` is restored verbatim — the checkpoint's own list object.
    assert steps is cp.steps
    # The three accumulators are fresh copies: equal by value, distinct objects.
    assert errors == ["boom"]
    assert errors is not cp.errors
    assert all_output == ["prior output"]
    assert all_output is not cp.all_output
    assert executed_commands == ["echo prior"]
    assert executed_commands is not cp.executed_commands
    # Fresh planning is skipped entirely on resume.
    agent.planner.decompose.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_path_decomposes_and_returns_empty_accumulators(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) fresh arm: with ``resume_checkpoint=None`` the goal is decomposed once
    with the expected positional/keyword args — the run session, the
    ``require_claude_md_update`` config flag, and the ``task_execution`` broadcast
    moment are all threaded through — and the return is
    ``(decomposed_steps, [], [], [])``."""
    agent = make_agent()
    decomposed = [PlanStep("s1", "planned step")]
    agent.planner.decompose = AsyncMock(return_value=decomposed)
    # Stub the broadcast prefix so the ``workspace_moment`` arg is a known,
    # config-independent sentinel; assert it is derived from the task-execution
    # consumer exactly as the extracted block computes it.
    agent._broadcast_moment_prefix = MagicMock(return_value="moment-prefix")
    agent.config.require_claude_md_update = True
    # Opaque session sentinel — mypy does not type-check ``tests/`` (the core
    # gate runs ``mypy maestro/`` only), so no cast is needed to assert it
    # threads through unchanged as the ``session`` kwarg.
    run_session = MagicMock(name="run_session")

    steps, errors, all_output, executed_commands = await resolve_task_plan(agent,
        resume_checkpoint=None,
        goal="the goal",
        identity_context="id-ctx",
        context="ctx",
        run_session=run_session,
    )

    assert steps is decomposed
    assert errors == []
    assert all_output == []
    assert executed_commands == []
    agent.planner.decompose.assert_awaited_once_with(
        "the goal",
        "id-ctx",
        "ctx",
        session=run_session,
        require_claude_md_update=True,
        workspace_moment="moment-prefix",
    )
    agent._broadcast_moment_prefix.assert_called_once_with("task_execution")
