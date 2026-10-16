"""A shared evolution-fuel fixture for the planner tests (proposal 155 step 4)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from maestro.evolution_fuel import EvolutionFuel, FuelSelection, parse_owner_id
from maestro.issues import Issue


def make_fuel(number: int = 42, owner_id: str = "maestro:gate") -> EvolutionFuel:
    """A re-derived ``gap:maestro`` issue numbered *number* (a red gate by default)."""
    return EvolutionFuel(
        issue=Issue(
            number=number, title=f"{owner_id} — gap",
            labels=["gap:maestro"], body="origin/main is red on ruff",
            created_at="2026-10-01T00:00:00Z",
        ),
        owner=parse_owner_id(owner_id),
        label="gap:maestro",
        fuel_class=1 if owner_id == "maestro:gate" else 4,
        trust="rederived",
    )


GAP = make_fuel()


def stub_evolve_phase(agent: Any, body: Any) -> Any:
    """Fuel *agent*'s evolve phase and route its planning body to *body*.

    ``_phase_evolve`` selects fuel before it opens a session; a loop test that
    only needs the phase body to run stubs both seams here.
    """
    agent._admitted_evolution_fuel = MagicMock(
        return_value=FuelSelection(selected=GAP),
    )
    agent._evolve_with_fuel = body
    return body


def fuel_patch(number: int = 42) -> Any:
    """Patch evolve's fuel selection to one trusted gap issue numbered *number*."""
    from unittest.mock import patch

    return patch(
        "maestro.loop.self_health.select_fuel",
        return_value=FuelSelection(selected=make_fuel(number)),
    )
