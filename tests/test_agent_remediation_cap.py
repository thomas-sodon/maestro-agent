"""Consecutive-remediation cap with an empty task queue (#4047).

The cap forces task-shape diversity by diverting a slot to the queue head;
with no head there is nothing to divert to, so remediation stays eligible.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.consciousness import PendingPRRequest


@pytest.mark.asyncio
async def test_run_task_phase_capped_streak_with_empty_queue_still_serves_remediation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4047: an engaged cap with an empty queue has nothing to divert to, so
    the slot still serves remediation instead of silently doing nothing."""
    agent = make_agent()
    agent.config.max_consecutive_remediation = 2
    agent._consecutive_remediation = 2
    agent._wip_owns_slot = MagicMock(return_value=False)  # type: ignore[method-assign]
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=f"pr goal {i}", pr_branch=f"maestro/evolve/p{i}",
        )
        for i in range(2)
    ]
    agent.consciousness.save_self_model(model)

    goals: list[tuple[str, str]] = []

    async def _capture(goal, **kwargs):  # type: ignore[no-untyped-def]
        goals.append((goal, kwargs["source"]))
        return MagicMock(task_id=None)

    agent.run = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

    await agent._run_task_phase()

    assert goals == [("pr goal 0", "external")]
    after = agent.consciousness.load_self_model()
    assert [e.directive for e in after.pending_pr_requests] == ["pr goal 1"]


@pytest.mark.asyncio
async def test_run_task_phase_capped_streak_diverts_once_queue_refills(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#4047: the cap still forces diversity as soon as a queue head exists."""
    from maestro.task_queue import QueuedTask

    agent = make_agent()
    agent.config.max_consecutive_remediation = 2
    agent._consecutive_remediation = 2
    agent._wip_owns_slot = MagicMock(return_value=False)  # type: ignore[method-assign]
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=f"pr goal {i}", pr_branch=f"maestro/evolve/p{i}",
        )
        for i in range(2)
    ]
    agent.consciousness.save_self_model(model)
    agent.task_queue.save([QueuedTask(task_id="q1", goal="queue goal")])

    goals: list[tuple[str, str]] = []

    async def _capture(goal, **kwargs):  # type: ignore[no-untyped-def]
        goals.append((goal, kwargs["source"]))
        return MagicMock(task_id=None)

    agent.run = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

    await agent._run_task_phase()

    assert goals == [("queue goal", "self_directed")]
    assert agent._consecutive_remediation == 0
    after = agent.consciousness.load_self_model()
    assert [e.directive for e in after.pending_pr_requests] == [
        "pr goal 0", "pr goal 1",
    ]
