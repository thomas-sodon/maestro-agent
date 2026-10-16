"""Tests for ``maestro.loop.contemplation`` — the contemplation phase carved
out of ``MaestroAgent.contemplate`` (proposal 147).

The phase's sensor-by-sensor behaviour stays covered by the existing
contemplate tests, which reach it through the agent delegator; these pin the
module's own entry point and the delegator's forwarding.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.consciousness import SelfModel
from maestro.loop import contemplation


@pytest.mark.asyncio
async def test_contemplate_returns_reflection_and_resets_running(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A direct call returns the introspection text and clears ``_running``."""
    agent = make_agent()
    seen: list[tuple[bool, str | None]] = []

    async def _introspect(*_a: object, **_k: object) -> str:
        seen.append((agent._running, agent._current_task))
        return "a reflection"

    introspect = AsyncMock(side_effect=_introspect)

    with patch.object(agent.consciousness, "introspect", introspect), \
         patch.object(
             agent.consciousness, "update_self_model",
             AsyncMock(return_value=SelfModel()),
         ), \
         patch.object(
             agent.consciousness, "load_self_model",
             return_value=MagicMock(),
         ), \
         patch.object(agent.journal, "write_thought"):
        reflection = await contemplation.contemplate(agent, iteration=4)

    assert reflection == "a reflection"
    introspect.assert_awaited_once()
    assert seen == [(True, "contemplation")]
    assert agent._running is False
    assert agent._current_task is None


@pytest.mark.asyncio
async def test_contemplate_resets_running_when_introspect_raises(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """The ``finally`` clears ``_running``/``_current_task`` on a raise."""
    agent = make_agent()

    with patch.object(
             agent.consciousness, "introspect",
             AsyncMock(side_effect=RuntimeError("boom")),
         ), \
         patch.object(
             agent.consciousness, "load_self_model",
             return_value=MagicMock(),
         ), \
         patch.object(agent.journal, "write_thought"), \
         pytest.raises(RuntimeError, match="boom"):
        await contemplation.contemplate(agent, iteration=4)

    assert agent._running is False
    assert agent._current_task is None


@pytest.mark.asyncio
async def test_agent_contemplate_delegates_to_module(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """``MaestroAgent.contemplate`` forwards every argument to the module."""
    agent = make_agent()
    fake = AsyncMock(return_value="forwarded")
    session = MagicMock()
    snapshot = MagicMock()

    with patch("maestro.loop.contemplation.contemplate", fake):
        result = await agent.contemplate(
            "ctx", session=session, iteration=9, diag_snapshot=snapshot,
        )

    assert result == "forwarded"
    fake.assert_awaited_once_with(
        agent, "ctx", session=session, iteration=9, diag_snapshot=snapshot,
    )
