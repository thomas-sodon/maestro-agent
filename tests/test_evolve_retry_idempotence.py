"""``_phase_evolve`` selects fuel before it opens a session (proposal 155 step 4).

A transient ``SDKError`` re-runs the failed phase from the top at the same
iteration; the phase re-reads fuel each time, so no once-per-cycle write keys on
the iteration. Lives apart from ``tests/test_agent.py``, which sits near
SPLIT_BUDGET.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.support.gap_fuel import stub_evolve_phase


@pytest.mark.asyncio
async def test_phase_evolve_hands_the_selection_to_the_body(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    body = stub_evolve_phase(agent, AsyncMock())
    with patch.object(agent, "_get_or_create_session", AsyncMock(return_value=None)):
        await agent._phase_evolve(iteration=9)

    selection = agent._admitted_evolution_fuel.return_value
    assert body.await_args.args == (selection, None)


@pytest.mark.asyncio
async def test_phase_evolve_without_fuel_opens_no_session(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent._admitted_evolution_fuel = MagicMock(return_value=None)  # type: ignore[method-assign]
    opener = AsyncMock()
    with patch.object(agent, "_get_or_create_session", opener):
        await agent._phase_evolve(iteration=9)
    opener.assert_not_awaited()
