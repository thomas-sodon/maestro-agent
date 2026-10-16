"""``MaestroAgent._escalate_stuck_pr`` items-driven vs git-driven latches (#4579)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro import escalation_bridge
from maestro.pr_facts import MergeTruth
from maestro.vcs import PrMergeState


def _pr_state(branch: str, *, mergeable: str, status: str) -> PrMergeState:
    """Build a minimal ``PrMergeState`` for a host-probe stub."""
    return PrMergeState(
        number=9, head_ref=branch, title="t", body="b", state="OPEN",
        mergeable=mergeable, merge_state_status=status,
        review_decision="", created_at="", updated_at="", url="",
    )


def _truth(pr: int, *, settled: bool) -> MergeTruth:
    return MergeTruth(
        pr_number=pr, settled=settled,
        state="MERGED" if settled else "OPEN",
        branch_landed=settled, source="host_api", observed_at=0.0,
    )


def _clean_agent(make_agent, *, settled: bool = False):  # type: ignore[no-untyped-def]
    """An agent whose git probe would report the PR cleanly mergeable."""
    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "success"
    host.get_open_evolution_pr_states.return_value = {
        "maestro/evolve/p9": _pr_state(
            "maestro/evolve/p9", mergeable="MERGEABLE", status="CLEAN",
        ),
    }
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent._vcs.commits_ahead_behind.return_value = (0, 0)
    agent._vcs.rev_parse.return_value = "sha9"
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_truth(9, settled=settled),
    )
    agent.pr_ledger.observe_head(9, "maestro/evolve/p9", "sha1")
    return agent


def _assert_items_escalated(agent) -> None:  # type: ignore[no-untyped-def]
    agent._notify_best_effort.assert_awaited_once()
    message = agent._notify_best_effort.await_args.args[0]
    assert "2 unresolved review item(s)" in message
    assert "Merge blocker" not in message
    entry = agent.pr_ledger.get(9)
    assert entry is not None
    assert entry.escalated_at is not None


@pytest.mark.asyncio
async def test_escalate_stuck_pr_items_driven_clean_pr_still_escalates(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A cleanly mergeable but unsettled PR latched on review items is still
    escalated — the git probe's clean verdict does not silence it (#4579)."""
    agent = _clean_agent(make_agent)
    await agent._escalate_stuck_pr(9, "maestro/evolve/p9", items_remaining=2)
    _assert_items_escalated(agent)
    agent.is_pr_settled.assert_called_once_with(9)


@pytest.mark.asyncio
async def test_escalate_stuck_pr_items_driven_clean_pr_without_branch_still_escalates(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The branch-less shape (no state-map entry) escalates too (#4579)."""
    agent = _clean_agent(make_agent)
    await agent._escalate_stuck_pr(9, None, items_remaining=2)
    _assert_items_escalated(agent)


@pytest.mark.asyncio
async def test_escalate_stuck_pr_items_driven_settled_returns_silently(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A settled PR needs no hand-up, even with review items counted."""
    agent = _clean_agent(make_agent, settled=True)
    await agent._escalate_stuck_pr(9, "maestro/evolve/p9", items_remaining=2)
    agent._notify_best_effort.assert_not_awaited()
    entry = agent.pr_ledger.get(9)
    assert entry is not None
    assert entry.escalated_at is None


@pytest.mark.asyncio
async def test_escalate_stuck_pr_items_driven_handoff_delivered_on_clean_pr(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The durable stand-down is recorded for the items-driven clean PR."""
    agent = _clean_agent(make_agent)
    agent.config.pr_remediation_lap_budget = 3
    await agent._escalate_stuck_pr(9, "maestro/evolve/p9", items_remaining=2)
    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 9) is True


@pytest.mark.asyncio
async def test_escalate_stuck_pr_zero_items_clean_pr_still_returns_silently(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A git-driven latch keeps the clean-mergeable short-circuit and never
    consults the merge-truth chokepoint."""
    agent = _clean_agent(make_agent)
    await agent._escalate_stuck_pr(9, "maestro/evolve/p9", items_remaining=0)
    agent._notify_best_effort.assert_not_awaited()
    agent.is_pr_settled.assert_not_called()


@pytest.mark.asyncio
async def test_escalate_stuck_pr_items_driven_names_review_items(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An items-driven latch names the real review-items blocker rather than
    the transient git merge-block snapshot (#2744)."""
    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"
    agent.evolution._host = host  # type: ignore[attr-defined]
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_truth(9, settled=False),
    )
    await agent._escalate_stuck_pr(9, None, items_remaining=2)
    agent._notify_best_effort.assert_awaited_once()
    message = agent._notify_best_effort.await_args.args[0]
    assert "2 unresolved review item(s)" in message
    assert "CI_FAILING" not in message
    assert "Merge blocker" not in message


@pytest.mark.asyncio
async def test_escalate_stuck_pr_zero_items_uses_merge_blocker(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A zero outstanding-items count is not items-driven, so the escalation
    keeps the git merge-block CTA — guarding the ``> 0`` discriminator (#2744)."""
    agent = make_agent()
    agent._notify_best_effort = AsyncMock()  # type: ignore[method-assign]
    host = MagicMock()
    host.is_pr_merged.return_value = False
    host.get_pr_check_conclusion.return_value = "failure"
    agent.evolution._host = host  # type: ignore[attr-defined]
    await agent._escalate_stuck_pr(10, None, items_remaining=0)
    agent._notify_best_effort.assert_awaited_once()
    message = agent._notify_best_effort.await_args.args[0]
    assert "CI_FAILING" in message
    assert "Merge blocker" in message
    assert "unresolved review item" not in message
