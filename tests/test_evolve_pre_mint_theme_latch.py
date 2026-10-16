"""Regression tests for #4151: evolve()'s pre-mint gate must not latch off.

The pre-mint generation gate runs before any candidate plan exists, so it may
refuse on backlog saturation only; theme concentration is judged per-candidate
at the submission door. A theme-dominated obsolete graveyard with a drained live
backlog must therefore still let a fuelled evolve() reach propose_plan.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.evolution_fuel import FuelSelection
from maestro.loop import self_health
from maestro.memory import EvolutionPlan
from tests.support.gap_fuel import make_fuel


def _plan(plan_id: str, title: str, status: str, created_at: str) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status=status,
        created_at=created_at,
    )


def _themed_obsolete_plan(i: int) -> EvolutionPlan:
    """An ``obsolete`` self-measurement-themed plan for a theme-dominated window."""
    return _plan(
        f"obs{i}", f"Add a sensor for signal {i}", "obsolete",
        f"2025-03-{i + 1:02d}T00:00:00Z",
    )


def _pending_review_plan(i: int) -> EvolutionPlan:
    return _plan(
        f"p{i}", "Ship a live capability", "pending_review",
        "2025-01-01T00:00:00Z",
    )


def _stub_evolve_phases(agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Fuel evolve with a source_budget gap (proposal 155 step 4) and mock
    its non-minting seams so only the pre-mint gate decides."""
    monkeypatch.setattr(
        self_health, "select_fuel",
        MagicMock(return_value=FuelSelection(
            selected=make_fuel(owner_id="maestro:source_budget:maestro/a.py"),
        )),
    )
    agent.config.pre_mint_generation_gate_enabled = True
    agent.config.wip_primacy_enabled = False
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(raw_output="health"),
    )
    agent.consciousness.introspect = AsyncMock(  # type: ignore[method-assign]
        return_value="reflection",
    )


@pytest.mark.asyncio
async def test_evolve_pre_mint_gate_admits_theme_dominated_graveyard_with_drained_backlog(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """#4151: a wholly themed obsolete graveyard with zero live plans must not
    latch evolve() off — no candidate exists pre-mint, so the gate refuses on
    backlog saturation alone and the theme arm is judged at the door."""
    from maestro.generation_gate import GENERATION_STANDDOWN_DIRECTIVE

    agent = make_agent()
    agent.config.generation_admission_window = 20
    agent.config.generation_admission_theme_fraction = 0.75
    _stub_evolve_phases(agent, monkeypatch)
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_themed_obsolete_plan(i) for i in range(20)],
    )
    agent.evolution.propose_plan = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(),
    )
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]

    await agent.evolve()

    agent.evolution.propose_plan.assert_awaited_once()
    directive = agent.consciousness.load_self_model().next_task_directive
    assert directive != GENERATION_STANDDOWN_DIRECTIVE


@pytest.mark.asyncio
async def test_evolve_pre_mint_gate_themed_graveyard_still_refuses_saturation(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """#4151: the saturation arm is untouched — the same themed graveyard plus a
    live backlog at the submit-block threshold still refuses pre-mint."""
    agent = make_agent()
    agent.config.loop_pending_plan_submit_block_threshold = 5
    _stub_evolve_phases(agent, monkeypatch)
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_themed_obsolete_plan(i) for i in range(20)]
        + [_pending_review_plan(i) for i in range(5)],
    )
    agent.evolution.propose_plan = AsyncMock()  # type: ignore[method-assign]

    await agent.evolve()

    agent.evolution.propose_plan.assert_not_awaited()
