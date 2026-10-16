"""evolve() spends nothing until a gap issue fuels it (proposal 155 step 4).

The zero-SDK self-health detectors run first, then fuel selection, then — for
any fuel but a red gate — the WIP-primacy and pre-mint gates. A refused or
fuel-less tick awaits neither ``introspect`` nor the priced planner, and no
tick runs the quality-gate subprocess (its only reader, reflect, is deleted).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.evolution_fuel import FuelSelection
from maestro.loop import self_health
from tests.support.gap_fuel import make_fuel

_BUDGET = "maestro:source_budget:maestro/agent.py"


def _stub_common(agent, monkeypatch, fuel=None) -> None:  # type: ignore[no-untyped-def]
    """Fuel the tick with *fuel* and mock the priced and gate seams."""
    monkeypatch.setattr(self_health, "run_evolve_prestep", MagicMock(return_value=None))
    monkeypatch.setattr(
        self_health, "select_fuel", MagicMock(return_value=FuelSelection(selected=fuel)),
    )
    agent.evolution.run_quality_gates = MagicMock()  # type: ignore[method-assign]
    agent.consciousness.introspect = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.propose_plan = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_pre_mint_refusal_skips_the_planner(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    _stub_common(agent, monkeypatch, make_fuel(owner_id=_BUDGET))
    agent._wip_owns_slot = MagicMock(return_value=False)  # type: ignore[method-assign]
    agent._pre_mint_generation_verdict = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(admit=False, reason="saturated"),
    )

    await agent.evolve()

    agent.consciousness.introspect.assert_not_awaited()
    agent.evolution.propose_plan.assert_not_awaited()
    agent.evolution.run_quality_gates.assert_not_called()


@pytest.mark.asyncio
async def test_wip_owned_slot_skips_the_planner(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    _stub_common(agent, monkeypatch, make_fuel(owner_id=_BUDGET))
    agent._wip_owns_slot = MagicMock(return_value=True)  # type: ignore[method-assign]
    agent._pre_mint_generation_verdict = MagicMock(return_value=None)  # type: ignore[method-assign]

    await agent.evolve()

    agent.evolution.propose_plan.assert_not_awaited()
    agent._pre_mint_generation_verdict.assert_not_called()


@pytest.mark.asyncio
async def test_gate_fuel_is_planned_while_wip_owns_the_slot(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A3: a maestro:gate issue bypasses WIP primacy and the pre-mint gate."""
    agent = make_agent()
    _stub_common(agent, monkeypatch, make_fuel())
    agent._wip_owns_slot = MagicMock(return_value=True)  # type: ignore[method-assign]
    agent._pre_mint_generation_verdict = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(admit=False, reason="saturated"),
    )

    await agent.evolve()

    agent.evolution.propose_plan.assert_awaited_once()
    agent.evolution.run_quality_gates.assert_not_called()
    agent.consciousness.introspect.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_fuel_runs_nothing(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    _stub_common(agent, monkeypatch, None)
    agent._wip_owns_slot = MagicMock()  # type: ignore[method-assign]

    await agent.evolve()

    agent._wip_owns_slot.assert_not_called()
    agent.evolution.propose_plan.assert_not_awaited()
    agent.evolution.run_quality_gates.assert_not_called()


@pytest.mark.asyncio
async def test_self_health_prestep_runs_before_fuel(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Proposal 155 step 2: the detectors run first, then fuel selection."""
    agent = make_agent()
    order: list[str] = []
    report = MagicMock()
    monkeypatch.setattr(
        self_health, "run_evolve_prestep",
        MagicMock(side_effect=lambda *_a: order.append("prestep") or report),
    )
    monkeypatch.setattr(
        self_health, "select_fuel",
        MagicMock(side_effect=lambda *_a: order.append("fuel") or FuelSelection()),
    )

    await agent.evolve()

    assert order == ["prestep", "fuel"]
    assert self_health.run_evolve_prestep.call_args.args[0] is agent
    assert self_health.select_fuel.call_args.args[1] is report


@pytest.mark.asyncio
async def test_self_health_store_error_propagates(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    agent = make_agent()
    _stub_common(agent, monkeypatch, make_fuel())
    monkeypatch.setattr(
        self_health, "run_evolve_prestep",
        MagicMock(side_effect=sqlite3.OperationalError("disk I/O error")),
    )
    with pytest.raises(sqlite3.OperationalError):
        await agent.evolve()
    agent.evolution.propose_plan.assert_not_awaited()


def test_make_agent_has_no_repo(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The prestep's no-repo return keeps make_agent evolve tests off the host."""
    from datetime import UTC, datetime

    agent = make_agent()
    assert self_health.run_evolve_prestep(agent, datetime.now(UTC)) is None
    assert self_health.select_fuel(agent, None, datetime.now(UTC)) == FuelSelection()


@pytest.mark.asyncio
async def test_fuelless_tick_still_delivers_a_pending_gap_ask(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Proposal 155 step 5: a tripped gap is asked even when no fuel remains."""
    from maestro.evolution_fuel import TrippedGap

    agent = make_agent()
    agent.config.github_repo = "o/r"
    _stub_common(agent, monkeypatch, None)
    agent.ask_thomas = AsyncMock(return_value="q")  # type: ignore[method-assign]
    agent._pending_gap_asks = [TrippedGap(7, 2)]

    await agent.evolve()
    await agent._phase_evolve(iteration=1)

    agent.ask_thomas.assert_awaited_once()
    assert agent.ask_thomas.await_args.args[0].startswith("[gap-decision #7]")
    agent.evolution.propose_plan.assert_not_awaited()
    assert agent._pending_gap_asks == []
