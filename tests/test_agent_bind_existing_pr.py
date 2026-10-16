"""Failed-plan retry binds an existing PR instead of re-implementing (#4553)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _bind_plan(plan_id: str, status: str = "failed"):  # type: ignore[no-untyped-def]
    from maestro.memory import EvolutionPlan

    return EvolutionPlan(
        plan_id=plan_id,
        title="Retry Bind Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status=status,
        failure_count=1 if status == "failed" else 0,
        created_at="2025-01-01T00:00:00Z",
    )


def _bind_pr_state(state: str):  # type: ignore[no-untyped-def]
    from maestro.vcs import PrMergeState

    return PrMergeState(
        number=4545, head_ref="maestro/evolve/p-bind", title="t", body="",
        state=state, mergeable="", merge_state_status="", review_decision="",
        created_at="", updated_at="",
        url="https://github.com/owner/repo/pull/4545",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_state", ["OPEN", "MERGED"])
async def test_implement_plan_failed_plan_binds_existing_pr(make_agent, pr_state) -> None:  # type: ignore[no-untyped-def]
    """A failed plan whose branch already carries a PR is bound, not re-implemented (#4553)."""
    agent = make_agent()
    agent.memory.save_evolution_plan(_bind_plan("p-bind"))
    implement_mock = AsyncMock()
    create_pr_mock = AsyncMock()
    record_event_mock = MagicMock()
    with (
        patch.object(agent.journal, "record_event", record_event_mock),
        patch.object(agent.evolution, "find_existing_pr", return_value=_bind_pr_state(pr_state)),
        patch.object(agent.evolution, "implement", implement_mock),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
    ):
        await agent.implement_plan("p-bind")

    plan = agent.memory.get_plan("p-bind")
    assert plan is not None
    assert plan.status == "implemented"
    assert plan.pr_url == "https://github.com/owner/repo/pull/4545"
    assert plan.failure_count == 1
    implement_mock.assert_not_called()
    create_pr_mock.assert_not_called()
    record_event_mock.assert_called_once()
    kwargs = record_event_mock.call_args.kwargs
    assert kwargs["entry_type"] == "milestone"
    assert kwargs["triggered_by"] == "bind_existing_pr:p-bind"


@pytest.mark.asyncio
async def test_implement_plan_failed_plan_lookup_failure_defers(make_agent) -> None:  # type: ignore[no-untyped-def]
    from maestro.vcs import VCSError

    agent = make_agent()
    agent.memory.save_evolution_plan(_bind_plan("p-bind"))
    implement_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "find_existing_pr", side_effect=VCSError("boom")),
        patch.object(agent.evolution, "implement", implement_mock),
    ):
        await agent.implement_plan("p-bind")

    plan = agent.memory.get_plan("p-bind")
    assert plan is not None
    assert plan.status == "failed"
    assert plan.failure_count == 1
    implement_mock.assert_not_called()


@pytest.mark.asyncio
async def test_implement_plan_approved_plan_skips_lookup(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.memory.save_evolution_plan(_bind_plan("p-bind", status="approved"))
    lookup = MagicMock()
    with (
        patch.object(agent.evolution, "find_existing_pr", lookup),
        patch.object(agent.evolution, "implement", AsyncMock(side_effect=RuntimeError("stop"))),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        pytest.raises(RuntimeError),
    ):
        await agent.implement_plan("p-bind")
    lookup.assert_not_called()


def test_find_existing_pr_queries_evolve_branch(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    host = MagicMock()
    host.find_evolution_pr_for_branch.return_value = None
    agent.evolution._host = host
    from maestro.evolution import EvolutionEngine

    assert EvolutionEngine.find_existing_pr(agent.evolution, _bind_plan("p-bind")) is None
    host.find_evolution_pr_for_branch.assert_called_once_with("maestro/evolve/p-bind")
