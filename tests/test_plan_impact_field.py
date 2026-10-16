"""Proposal 148 step 4: the outcome-framed ``expected_impact`` plan field."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier

FAKE_DIR = Path("/fake/impact")


def _make_engine() -> EvolutionEngine:
    m = Memory(FAKE_DIR)
    c = Consciousness(FAKE_DIR)
    c.load_self_model()
    j = Journal(FAKE_DIR)
    return EvolutionEngine(
        m, c, j, NullNotifier(),
        issues=IssueTracker(github_repo=None), config=Config(),
    )


def test_plan_from_spec_ignores_legacy_impact_key(mem_fs: dict[str, str]) -> None:
    """A model reply using the legacy key no longer lands it (proposal 148 Step 9)."""
    spec = {"expected_stage_impact": "observer sees faster loops"}

    plan = _make_engine()._plan_from_spec(
        spec, default_rationale="fallback",
    )

    assert plan.expected_impact == ""
    assert "expected_impact" not in spec


def test_plan_from_spec_new_impact_key_lands(mem_fs: dict[str, str]) -> None:
    """A reply carrying the outcome-framed key lands it."""
    spec = {"expected_stage_impact": "legacy", "expected_impact": "current"}

    plan = _make_engine()._plan_from_spec(
        spec, default_rationale="fallback",
    )

    assert plan.expected_impact == "current"


@pytest.mark.parametrize("method", ["propose_plan", "propose_plan_chain"])
def test_planner_schema_is_outcome_framed(method: str) -> None:
    """Neither planner prompt nor schema asks for the stage-framed impact."""
    source = inspect.getsource(getattr(EvolutionEngine, method))

    assert "expected_stage_impact" not in source
    assert "moves you forward" not in source
    assert '"expected_impact"' in source
