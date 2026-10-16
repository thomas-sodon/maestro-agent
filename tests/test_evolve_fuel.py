"""Fuel-gated evolve (proposal 155 step 4): no fuel, no SDK call."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.evolution import ProposalParseError, pin_gap_plan
from maestro.evolution_fuel import (
    GAP_LABELS,
    EvolutionFuel,
    FuelSelection,
    parse_owner_id,
)
from maestro.generation_gate import GENERATION_STANDDOWN_DIRECTIVE
from maestro.issues import Issue
from maestro.loop import self_health
from maestro.memory import EvolutionPlan


def _fuel(owner_id: str = "maestro:gate", number: int = 11) -> EvolutionFuel:
    owner = parse_owner_id(owner_id)
    return EvolutionFuel(
        issue=Issue(number=number, title=f"{owner_id} — x", labels=["gap:maestro"],
                    body="b", created_at="2026-10-01"),
        owner=owner, label="gap:maestro",
        fuel_class=1 if owner_id == "maestro:gate" else 4, trust="rederived",
    )


def _selection(fuel: EvolutionFuel | None) -> FuelSelection:
    return FuelSelection(selected=fuel)


def _plan(plan_id: str = "p1") -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id, title="t", category="capability", rationale="r",
        proposed_changes=["c"], files_affected=[], expected_impact="e",
        problem_ref="audit:x",
    )


@pytest.fixture
def sdk_calls() -> Iterator[list[str]]:
    """Record every SDK seam and subprocess launch an evolve tick reaches."""
    calls: list[str] = []

    def rec(name: str):  # type: ignore[no-untyped-def]
        async def _call(*_a: object, **_k: object) -> None:
            calls.append(name)
        return _call

    with ExitStack() as stack:
        for target in (
            "maestro.evolution.query", "maestro.evolution.single_use_call",
            "maestro.evolution.query_on_session",
        ):
            stack.enter_context(patch(target, side_effect=rec(target)))
        stack.enter_context(patch(
            "subprocess.run", side_effect=lambda *a, **k: calls.append("subprocess"),
        ))
        yield calls


def _no_fuel(agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(self_health, "run_evolve_prestep", MagicMock(return_value=None))
    agent.evolution.run_quality_gates = MagicMock()  # type: ignore[method-assign]
    agent._get_or_create_session = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
@pytest.mark.parametrize("single_use", [False, True])
async def test_zero_fuel_phase_makes_no_sdk_call(
    make_agent, monkeypatch, sdk_calls, single_use: bool,  # type: ignore[no-untyped-def]
) -> None:
    """A1 golden: a zero-fuel tick yields an empty SDK-call list, no session, no gate."""
    agent = make_agent()
    agent.config.sdk_single_use_evolution_calls_enabled = single_use
    _no_fuel(agent, monkeypatch)

    await agent._phase_evolve(iteration=3)

    assert sdk_calls == []
    agent._get_or_create_session.assert_not_awaited()
    agent.evolution.run_quality_gates.assert_not_called()


@pytest.mark.asyncio
async def test_zero_fuel_evolve_makes_no_sdk_call(
    make_agent, monkeypatch, sdk_calls,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    _no_fuel(agent, monkeypatch)
    agent.evolution.propose_plan = AsyncMock()  # type: ignore[method-assign]
    await agent.evolve()
    assert sdk_calls == []
    agent.evolution.propose_plan.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_plan", [False, True])
async def test_configured_repo_without_admissible_gap_makes_no_sdk_call(
    make_agent, monkeypatch, sdk_calls, live_plan: bool,  # type: ignore[no-untyped-def]
) -> None:
    """A1 golden on a configured repo: open issues but no admissible gap."""
    agent = make_agent()
    report = self_health.SelfHealthReport(
        red_main=self_health.RedMainReading("green"), outcomes=(),
    )
    monkeypatch.setattr(self_health, "run_evolve_prestep", MagicMock(return_value=report))
    monkeypatch.setattr(self_health, "build_gap_store", MagicMock())
    monkeypatch.setattr(self_health, "effective_operator_logins", MagicMock(return_value=()))
    labels = ["gap:maestro"] if live_plan else ["bug"]
    issue = Issue(number=11, title="maestro:gate — x", labels=labels, body="b",
                  created_at="2026-10-01")
    tracker = MagicMock()
    tracker.list_open_or_none.return_value = [issue]
    tracker.meta.return_value = None
    agent.issues = tracker
    monkeypatch.setattr(type(agent.evolution), "code_host", MagicMock())
    monkeypatch.setattr(
        self_health, "select_evolution_fuel",
        _wrap_trusting(self_health.select_evolution_fuel),
    )
    if live_plan:
        plan = _plan()
        plan.issue_number = 11
        plan.status = "pending_review"
        agent.memory.get_all_plans = MagicMock(return_value=[plan])  # type: ignore[method-assign]
    agent.evolution.run_quality_gates = MagicMock()  # type: ignore[method-assign]
    agent._get_or_create_session = AsyncMock()  # type: ignore[method-assign]

    await agent._phase_evolve(iteration=3)

    assert sdk_calls == []
    agent._get_or_create_session.assert_not_awaited()
    agent.evolution.run_quality_gates.assert_not_called()
    assert tracker.list_open_or_none.call_count == len(GAP_LABELS)


def _wrap_trusting(real):  # type: ignore[no-untyped-def]
    """Admit every listed gap as trusted so only the live-plan skip refuses it."""
    def _call(*a, **k):  # type: ignore[no-untyped-def]
        with patch(
            "maestro.evolution_fuel._admit_with_trust",
            side_effect=lambda cands, **_k: [(c, "rederived") for c in cands],
        ):
            return real(*a, **k)
    return _call


@pytest.mark.asyncio
async def test_fuelled_phase_opens_slot_b(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.sdk_single_use_evolution_calls_enabled = False
    selection = _selection(_fuel())
    monkeypatch.setattr(self_health, "admitted_evolution_fuel", MagicMock(return_value=selection))
    session = object()
    agent._get_or_create_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    run = AsyncMock()
    monkeypatch.setattr(self_health, "evolve_with_fuel", run)
    agent.evolution.run_quality_gates = MagicMock()  # type: ignore[method-assign]

    await agent._phase_evolve(iteration=1)

    run.assert_awaited_once_with(agent, selection, session)
    agent.evolution.run_quality_gates.assert_not_called()
    assert agent._running is True


@pytest.mark.asyncio
async def test_fuelled_single_use_phase_opens_no_session(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.sdk_single_use_evolution_calls_enabled = True
    selection = _selection(_fuel())
    monkeypatch.setattr(self_health, "admitted_evolution_fuel", MagicMock(return_value=selection))
    agent._get_or_create_session = AsyncMock()  # type: ignore[method-assign]
    run = AsyncMock()
    monkeypatch.setattr(self_health, "evolve_with_fuel", run)

    await agent._phase_evolve(iteration=1)

    run.assert_awaited_once_with(agent, selection, None)
    agent._get_or_create_session.assert_not_awaited()


def _admit(agent, monkeypatch, fuel: EvolutionFuel | None):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(self_health, "run_evolve_prestep", MagicMock(return_value=None))
    monkeypatch.setattr(self_health, "select_fuel", MagicMock(return_value=_selection(fuel)))
    return self_health.admitted_evolution_fuel(agent, datetime.now(UTC))


def test_gate_fuel_bypasses_wip_and_pre_mint(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A3: a maestro:gate issue is planned while WIP owns the slot."""
    agent = make_agent()
    agent._wip_owns_slot = MagicMock(return_value=True)  # type: ignore[method-assign]
    agent._pre_mint_generation_verdict = MagicMock()  # type: ignore[method-assign]
    selection = _admit(agent, monkeypatch, _fuel())
    assert selection is not None
    agent._wip_owns_slot.assert_not_called()
    agent._pre_mint_generation_verdict.assert_not_called()


def test_source_budget_fuel_stands_down_for_wip(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._wip_owns_slot = MagicMock(return_value=True)  # type: ignore[method-assign]
    agent._current_task_focus = MagicMock(return_value="pr")  # type: ignore[method-assign]
    assert _admit(agent, monkeypatch, _fuel("maestro:source_budget:maestro/a.py")) is None


def test_pre_mint_refusal_writes_stand_down_directive(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._wip_owns_slot = MagicMock(return_value=False)  # type: ignore[method-assign]
    agent._pre_mint_generation_verdict = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(admit=False, reason="saturated"),
    )
    assert _admit(agent, monkeypatch, _fuel("maestro:source_budget:maestro/a.py")) is None
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == GENERATION_STANDDOWN_DIRECTIVE


def test_pre_mint_refusal_keeps_an_occupied_directive(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "keep me"
    agent.consciousness.save_self_model(model)
    agent._wip_owns_slot = MagicMock(return_value=False)  # type: ignore[method-assign]
    agent._pre_mint_generation_verdict = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(admit=False, reason="saturated"),
    )
    assert _admit(agent, monkeypatch, _fuel("maestro:source_budget:maestro/a.py")) is None
    assert agent.consciousness.load_self_model().next_task_directive == "keep me"


def test_admitted_when_gates_pass(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._wip_owns_slot = MagicMock(return_value=False)  # type: ignore[method-assign]
    agent._pre_mint_generation_verdict = MagicMock(return_value=None)  # type: ignore[method-assign]
    assert _admit(agent, monkeypatch, _fuel("maestro:source_budget:maestro/a.py")) is not None


@pytest.mark.parametrize(("cap", "admitted"), [(2, False), (0, True), (3, True)])
def test_saturated_backlog_defers_before_the_priced_call(  # type: ignore[no-untyped-def]
    make_agent, monkeypatch, cap: int, admitted: bool,
) -> None:
    """A saturated live backlog stands down at zero SDK cost (a priced mint would be refused)."""
    agent = make_agent()
    agent.config.loop_pending_plan_submit_block_threshold = cap
    monkeypatch.setattr(
        agent.memory, "get_live_plan_records", MagicMock(return_value=[_plan("a"), _plan("b")]),
    )
    agent._wip_owns_slot = MagicMock(return_value=False)  # type: ignore[method-assign]
    agent._pre_mint_generation_verdict = MagicMock(return_value=None)  # type: ignore[method-assign]
    selection = _admit(agent, monkeypatch, _fuel("maestro:source_budget:maestro/a.py"))
    assert (selection is not None) == admitted


def test_real_saturated_backlog_with_pre_mint_on_writes_directive(  # type: ignore[no-untyped-def]
    make_agent, monkeypatch,
) -> None:
    """A real saturated backlog under the pre-mint gate still arms the stand-down directive."""
    agent = make_agent()
    agent.config.pre_mint_generation_gate_enabled = True
    agent.config.loop_pending_plan_submit_block_threshold = 2
    plans = [_plan("a"), _plan("b")]
    for plan in plans:
        plan.status = "pending_review"
    monkeypatch.setattr(agent.memory, "get_all_plans", MagicMock(return_value=plans))
    monkeypatch.setattr(agent.memory, "get_live_plan_records", MagicMock(return_value=plans))
    agent._wip_owns_slot = MagicMock(return_value=False)  # type: ignore[method-assign]
    assert _admit(agent, monkeypatch, _fuel("maestro:source_budget:maestro/a.py")) is None
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == GENERATION_STANDDOWN_DIRECTIVE


def test_no_fuel_admits_nothing(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._wip_owns_slot = MagicMock()  # type: ignore[method-assign]
    assert _admit(agent, monkeypatch, None) is None
    agent._wip_owns_slot.assert_not_called()


@pytest.mark.asyncio
async def test_evolve_with_fuel_pins_and_submits(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A10: the submitted plan is pinned to the gap, claim admitted by the open set."""
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = False
    agent._deliver_moment_to_consumer = MagicMock(return_value=None)  # type: ignore[method-assign]
    agent.evolution.propose_plan = AsyncMock(return_value=_plan())  # type: ignore[method-assign]
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]

    await self_health.evolve_with_fuel(agent, _selection(_fuel()), None)

    plan = agent.evolution.submit_for_review.call_args.args[0]
    assert plan.issue_number == 11
    assert plan.problem_ref is None
    assert plan.category == "maintenance"
    assert agent.evolution.submit_for_review.call_args.kwargs == {
        "self_initiated": True, "exempt_saturation": True,
    }


@pytest.mark.asyncio
async def test_evolve_with_fuel_without_selection_is_noop(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.evolution.propose_plan = AsyncMock()  # type: ignore[method-assign]
    await self_health.evolve_with_fuel(agent, FuelSelection(), None)
    agent.evolution.propose_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_evolve_with_fuel_chain_pins_every_slice(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    slices = [_plan("s1"), _plan("s2")]
    agent.evolution.propose_plan_chain = AsyncMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), slices),
    )
    agent.evolution.submit_proposal_chain = AsyncMock()  # type: ignore[method-assign]

    await self_health.evolve_with_fuel(agent, _selection(_fuel()), None)

    prompt = agent.evolution.propose_plan_chain.call_args.args[0]
    assert prompt.startswith("Close this capability gap:")
    pinned = agent.evolution.submit_proposal_chain.call_args.args[1]
    assert [s.issue_number for s in pinned] == [11, 11]
    kwargs = agent.evolution.submit_proposal_chain.call_args.kwargs
    assert "open_gap_issues" not in kwargs


@pytest.mark.asyncio
async def test_evolve_with_fuel_chain_lone_plan_is_pinned(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    agent.evolution.propose_plan_chain = AsyncMock(return_value=_plan())  # type: ignore[method-assign]
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]
    await self_health.evolve_with_fuel(agent, _selection(_fuel()), None)
    assert agent.evolution.submit_for_review.call_args.args[0].issue_number == 11


@pytest.mark.asyncio
async def test_evolve_with_fuel_parse_error_records_event(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = False
    agent._deliver_moment_to_consumer = MagicMock(return_value=None)  # type: ignore[method-assign]
    agent.evolution.propose_plan = AsyncMock(  # type: ignore[method-assign]
        side_effect=ProposalParseError("bad json"),
    )
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]
    agent.journal.record_event = MagicMock()  # type: ignore[method-assign]

    await self_health.evolve_with_fuel(agent, _selection(_fuel()), None)

    agent.evolution.submit_for_review.assert_not_awaited()
    assert agent.journal.record_event.call_args.kwargs["triggered_by"] == "evolution_skip"


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_evolve_with_fuel_citation_guard(make_agent, monkeypatch, valid: bool) -> None:  # type: ignore[no-untyped-def]
    from maestro import conscious_workspace

    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = False
    moment = MagicMock(moment_id="m1")
    agent._deliver_moment_to_consumer = MagicMock(  # type: ignore[method-assign]
        return_value=("block", moment, frozenset({"wi-1"})),
    )
    plan = _plan()
    plan.workspace_moment_id = "m1"
    plan.workspace_item_ids = ["wi-1"]
    agent.evolution.propose_plan = AsyncMock(return_value=plan)  # type: ignore[method-assign]
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        conscious_workspace, "validate_citation",
        MagicMock(return_value=MagicMock(valid=valid, reason="r")),
    )
    agent._active_cited_plan_ids = []

    await self_health.evolve_with_fuel(agent, _selection(_fuel()), None)

    kwargs = agent.evolution.propose_plan.call_args.kwargs
    assert kwargs["workspace_moment_id"] == "m1"
    submitted = agent.evolution.submit_for_review.call_args.args[0]
    if valid:
        assert agent._active_cited_plan_ids == ["p1"]
        assert submitted.workspace_moment_id == "m1"
    else:
        assert agent._active_cited_plan_ids == []
        assert submitted.workspace_moment_id is None


def test_pin_gap_plan_sets_maintenance_category() -> None:
    capability = EvolutionFuel(
        issue=Issue(number=4, title="x", labels=["gap:capability"], body="",
                    created_at=""),
        owner=None, label="gap:capability", fuel_class=2, trust="operator",
    )
    plan = pin_gap_plan(_plan(), capability)
    assert plan.category == "maintenance"
    assert plan.issue_number == 4


@pytest.mark.asyncio
async def test_approved_plans_implement_without_fuel(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A13: implementing approved plans never consults evolve's fuel."""
    agent = make_agent()
    fuel_read = MagicMock(side_effect=AssertionError("fuel consulted"))
    monkeypatch.setattr(self_health, "admitted_evolution_fuel", fuel_read)
    monkeypatch.setattr(self_health, "select_fuel", fuel_read)
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    plan = _plan("ap1")
    plan.status = "approved"
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("ap1", "approved")

    await agent._implement_approved_plans()

    fuel_read.assert_not_called()
    agent.implement_plan.assert_awaited()
