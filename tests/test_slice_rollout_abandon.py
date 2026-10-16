"""Regression tests for #4568: a proposal-118 rollout whose eligible slice ends
without a PR (rejected / obsolete / failed past its retry budget) must move to
``abandoned`` and retire its dormant ``sliced`` successors, rather than stay
``rolling_out`` forever."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from maestro.memory import (
    EvolutionPlan,
    EvolutionProposal,
    slice_ended_without_pr,
)
from maestro.pr_facts import MergeTruth
from maestro.types import PlanStatus

_PR0 = "https://github.com/o/r/pull/100"


def _seed_proposal(agent: Any) -> None:
    agent.memory.write_evolution_proposal(EvolutionProposal(
        proposal_id="prop", title="Multi-slice initiative",
        goal="Land it across slices", status="rolling_out",
        created_at="2025-01-01T00:00:00Z",
    ))


def _plan(
    plan_id: str,
    *,
    phase_index: int = 0,
    status: PlanStatus = "sliced",
    pr_url: str | None = None,
    failure_count: int = 0,
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id, title=f"Slice {phase_index}",
        category="capability", rationale="slice work",
        proposed_changes=["change"], files_affected=["file.py"],
        expected_impact="better", status=status,
        parent_plan_id="prop", phase_index=phase_index, pr_url=pr_url,
        failure_count=failure_count, created_at="2025-01-01T00:00:00Z",
    )


def _seed(agent: Any, plan_id: str, **kwargs: Any) -> None:
    agent.memory.save_evolution_plan(_plan(plan_id, **kwargs))


def _agent(make_agent: Any) -> Any:
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    agent.is_pr_settled = MagicMock(return_value=MergeTruth(
        pr_number=100, settled=True, state="MERGED", branch_landed=True,
        source="host_api", observed_at=0.0,
    ))
    _seed_proposal(agent)
    return agent


def _proposal_status(agent: Any) -> str:
    (proposal,) = agent.memory.get_evolution_proposals()
    return str(proposal.status)


@pytest.mark.asyncio
async def test_advance_slice_rollouts_abandons_on_rejected_middle_slice(
    make_agent: Any,
) -> None:
    agent = _agent(make_agent)
    _seed(agent, "s0", phase_index=0, status="implemented", pr_url=_PR0)
    _seed(agent, "s1", phase_index=1, status="rejected")
    _seed(agent, "s2", phase_index=2, status="sliced")
    agent.journal.record_event = MagicMock()
    agent.journal.write_thought = MagicMock()

    await agent._advance_slice_rollouts()

    assert _proposal_status(agent) == "abandoned"
    s2 = agent.memory.get_plan("s2")
    assert s2.status == "obsolete"
    assert "s1" in s2.obsolete_reason
    assert agent.memory.get_plan("s1").status == "rejected"
    agent.journal.record_event.assert_called_once()
    content = agent.journal.record_event.call_args.kwargs["content"]
    assert "s1" in content
    assert "rejected" in content
    assert agent.journal.record_event.call_args.kwargs["triggered_by"] == (
        "slice_rollout_abandoned"
    )
    agent.journal.write_thought.assert_not_called()
    # The dormant successor is obsolete, so the ordering gate defers it (the
    # abandoned proposal names no eligible slice) — nothing is implemented.
    assert agent._slice_defers_implementation(s2) is True

    # Idempotent: the abandoned proposal is skipped without a settlement read.
    agent.is_pr_settled.reset_mock()
    await agent._advance_slice_rollouts()
    agent.is_pr_settled.assert_not_called()
    agent.journal.record_event.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("dead", ["obsolete", "failed"])
async def test_advance_slice_rollouts_abandons_on_dead_head(
    make_agent: Any, dead: PlanStatus,
) -> None:
    agent = _agent(make_agent)
    budget = agent.config.max_implementation_attempts
    _seed(agent, "s0", phase_index=0, status=dead, failure_count=budget)
    _seed(agent, "s1", phase_index=1, status="sliced")
    _seed(agent, "s2", phase_index=2, status="sliced")

    await agent._advance_slice_rollouts()

    assert _proposal_status(agent) == "abandoned"
    assert agent.memory.get_plan("s1").status == "obsolete"
    assert agent.memory.get_plan("s2").status == "obsolete"


@pytest.mark.asyncio
async def test_advance_slice_rollouts_abandon_self_heals_on_retire_failure(
    make_agent: Any,
) -> None:
    agent = _agent(make_agent)
    _seed(agent, "s0", phase_index=0, status="rejected")
    _seed(agent, "s1", phase_index=1, status="sliced")
    real = agent.memory.mark_plan_obsolete
    agent.memory.mark_plan_obsolete = MagicMock(side_effect=OSError("disk"))

    with pytest.raises(OSError):
        await agent._advance_slice_rollouts()

    # Proposal not yet abandoned, so the next cycle re-fires the cascade.
    assert _proposal_status(agent) == "rolling_out"
    agent.memory.mark_plan_obsolete = real
    await agent._advance_slice_rollouts()
    assert _proposal_status(agent) == "abandoned"
    assert agent.memory.get_plan("s1").status == "obsolete"


@pytest.mark.asyncio
async def test_advance_slice_rollouts_keeps_retryable_failed_slice(
    make_agent: Any,
) -> None:
    agent = _agent(make_agent)
    budget = agent.config.max_implementation_attempts
    _seed(agent, "s0", phase_index=0, status="failed", failure_count=budget - 1)
    _seed(agent, "s1", phase_index=1, status="sliced")

    await agent._advance_slice_rollouts()

    assert _proposal_status(agent) == "rolling_out"
    assert agent.memory.get_plan("s1").status == "sliced"


@pytest.mark.asyncio
@pytest.mark.parametrize("live", ["approved", "pending_review", "implemented"])
async def test_advance_slice_rollouts_keeps_in_flight_slice(
    make_agent: Any, live: PlanStatus,
) -> None:
    agent = _agent(make_agent)
    _seed(agent, "s0", phase_index=0, status=live)
    _seed(agent, "s1", phase_index=1, status="sliced")

    await agent._advance_slice_rollouts()

    assert _proposal_status(agent) == "rolling_out"
    assert agent.memory.get_plan("s1").status == "sliced"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("rejected", True),
        ("obsolete", True),
        ("sliced", False),
        ("pending_review", False),
        ("approved", False),
        ("implemented", False),
    ],
)
def test_slice_ended_without_pr_by_status(
    status: PlanStatus, expected: bool,
) -> None:
    assert slice_ended_without_pr(_plan("p", status=status), 3) is expected


def test_slice_ended_without_pr_failed_budget() -> None:
    assert slice_ended_without_pr(
        _plan("p", status="failed", failure_count=3), 3,
    ) is True
    assert slice_ended_without_pr(
        _plan("p", status="failed", failure_count=2), 3,
    ) is False


def test_slice_ended_without_pr_with_pr_url_never_dead() -> None:
    plan = _plan("p", status="rejected", pr_url=_PR0)
    assert slice_ended_without_pr(plan, 3) is False


def test_slice_ended_without_pr_non_int_failure_count_not_spent() -> None:
    plan = _plan("p", status="failed")
    plan.failure_count = True
    assert slice_ended_without_pr(plan, 1) is False


def test_mark_plan_obsolete_sliced_requires_opt_in(make_agent: Any) -> None:
    agent = make_agent()
    _seed(agent, "s0", status="sliced")
    assert agent.memory.mark_plan_obsolete("s0", reason="r") is False
    assert agent.memory.mark_plan_obsolete(
        "s0", reason="r", allow_sliced=True,
    ) is True
    assert agent.memory.get_plan("s0").status == "obsolete"
