"""Caller-level wiring of the PR-churn block's settlement oracle (#4596).

``format_pr_churn_summary``'s own truth table lives in
``tests/test_no_progress_laps.py``; these tests pin that both call sites
(``contemplate`` and ``generate_task``) pass an ``is_pr_settled`` that
delegates to ``agent.is_pr_settled(n).settled`` — the merge-truth chokepoint.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import MockQueryResult


def _assert_delegates(oracle, agent_mock: MagicMock) -> None:  # type: ignore[no-untyped-def]
    agent_mock.is_pr_settled.return_value.settled = True
    assert oracle(4596) is True
    agent_mock.is_pr_settled.assert_called_with(4596)
    agent_mock.is_pr_settled.return_value.settled = False
    assert oracle(4597) is False
    agent_mock.is_pr_settled.assert_called_with(4597)


@pytest.mark.asyncio
async def test_contemplate_wires_is_pr_settled_to_chokepoint(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    settled = MagicMock()
    with patch(
        "maestro.loop.contemplation.format_pr_churn_summary", return_value="",
    ) as mock_churn, patch(
        "maestro.consciousness.query", new_callable=AsyncMock,
    ) as mock_q:
        mock_q.side_effect = [
            MockQueryResult(response="reflection"),
            MockQueryResult(response=json.dumps({
                "identity_statements": ["I am"], "values": ["growth"],
            })),
        ]
        await agent.contemplate()

    oracle = mock_churn.call_args.kwargs["is_pr_settled"]
    with patch.object(agent, "is_pr_settled", settled.is_pr_settled):
        _assert_delegates(oracle, settled)


@pytest.mark.asyncio
async def test_generate_task_wires_is_pr_settled_to_chokepoint(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    settled = MagicMock()
    result = MockQueryResult(
        response=json.dumps({"prune": [], "edit": [], "enqueue": []}),
    )
    with patch(
        "maestro.loop.task_generation.format_pr_churn_summary", return_value="",
    ) as mock_churn, patch(
        "maestro.loop.task_generation.query", AsyncMock(return_value=result),
    ):
        await agent.generate_task()

    oracle = mock_churn.call_args.kwargs["is_pr_settled"]
    with patch.object(agent, "is_pr_settled", settled.is_pr_settled):
        _assert_delegates(oracle, settled)
