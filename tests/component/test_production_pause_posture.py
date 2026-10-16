"""Proposal 149 step 0 — the production overlay pauses contemplation.

``maestro.production.toml`` sets ``contemplate_every`` to ``0`` while keeping
``curate_enabled`` and ``plan_feedback_enabled`` on. ``curate_every`` is back to
``5`` (proposal 155 step 6 skips unchanged plans), and evolve is unpaused
(proposal 155 step 4 gates it on gap-issue fuel instead). These tests pin that
posture through the single phase gate (``LifecycleController.phase_will_run``):
contemplation never fires, evolve fires on the base cadence, curation fires on
its 5-iteration cadence, and reviewer-feedback curation still fires off-cadence.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from maestro import config_settings
from maestro.config import Config
from maestro.memory import EvolutionPlan
from maestro.plan_feedback import PlanFeedback
from tests.component.conftest import CONFIG_DIR, apply_test_overlay, build_base_config

pytestmark = pytest.mark.component

_PAUSED_PHASES = ("contemplate",)
_WINDOW = range(1, 61)


@pytest.fixture
def production_config(memory_dir, workspace_dir) -> Config:  # type: ignore[no-untyped-def]
    """The shipped production ``Config`` with only per-test paths overlaid."""
    cfg = config_settings.build_config_authoritative(CONFIG_DIR, "production")
    return apply_test_overlay(cfg, memory_dir, workspace_dir)


def _feedback_plan() -> EvolutionPlan:
    """A ``pending_review`` plan carrying one unaddressed reviewer note."""
    plan = EvolutionPlan(
        plan_id="plan-1",
        title="Stale plan",
        category="capability",
        rationale="because",
        proposed_changes=["change one"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="pending_review",
        created_at="2025-01-01T00:00:00Z",
    )
    plan.reviewer_feedback = [
        PlanFeedback(feedback_id="fb-1", text="narrow the scope", addressed_at=None),
    ]
    return plan


def _will_run(agent: Any, cfg: Config, phase: str, iteration: int, *, smoke: bool = False) -> bool:
    """Call the phase gate with cadence kwargs read from *cfg*, as ``cmd_run`` does."""
    return agent._phase_will_run(
        phase,
        iteration=iteration,
        smoke=smoke,
        first_iteration_after_restart=False,
        task_every=cfg.loop_task_every,
        evolve_every=cfg.loop_evolve_every,
        dream_every=cfg.loop_dream_every,
        audit_self_every=cfg.loop_audit_self_every,
        contemplate_every=cfg.loop_contemplate_every,
        curate_every=cfg.loop_curate_every,
        curate_enabled=cfg.loop_curate_enabled,
        body_frozen=False,
    )


def test_production_overlay_pauses_the_cadences(production_config) -> None:  # type: ignore[no-untyped-def]
    """A16: evolve keeps the base cadence; only contemplation is paused."""
    assert production_config.loop_evolve_every == build_base_config().loop_evolve_every
    assert production_config.loop_evolve_every > 0
    assert production_config.loop_contemplate_every == 0
    assert production_config.loop_curate_every == 5
    assert production_config.loop_curate_enabled is True
    assert production_config.loop_plan_feedback_enabled is True


def test_base_config_keeps_the_cadences_on() -> None:
    """Non-vacuity anchor: the overlay, not the base, is what pauses them."""
    base = build_base_config()
    assert base.loop_evolve_every > 0
    assert base.loop_contemplate_every > 0


def test_paused_phases_never_run_without_feedback(make_agent, production_config) -> None:  # type: ignore[no-untyped-def]
    agent, _, _ = make_agent(config_override=production_config)
    agent.memory.get_pending_plans = MagicMock(return_value=[])
    for iteration in _WINDOW:
        for phase in _PAUSED_PHASES:
            assert _will_run(agent, production_config, phase, iteration) is False, (phase, iteration)


def test_curation_fires_on_its_cadence(make_agent, production_config) -> None:  # type: ignore[no-untyped-def]
    agent, _, _ = make_agent(config_override=production_config)
    agent.memory.get_pending_plans = MagicMock(return_value=[])
    fired = [
        i for i in _WINDOW
        if _will_run(agent, production_config, "curate_pending_plans", i)
    ]
    assert fired == [i for i in _WINDOW if i % 5 == 0]


def test_feedback_curation_still_fires_off_cadence(make_agent, production_config) -> None:  # type: ignore[no-untyped-def]
    agent, _, _ = make_agent(config_override=production_config)
    agent.memory.get_pending_plans = MagicMock(return_value=[_feedback_plan()])
    for iteration in _WINDOW:
        assert _will_run(agent, production_config, "curate_pending_plans", iteration) is True
        assert _will_run(agent, production_config, "contemplate", iteration) is False


def test_evolve_runs_on_the_base_cadence(make_agent, production_config) -> None:  # type: ignore[no-untyped-def]
    """Unpaused evolve (155 step 4): the phase fires; fuel decides the spend."""
    agent, _, _ = make_agent(config_override=production_config)
    agent.memory.get_pending_plans = MagicMock(return_value=[])
    every = production_config.loop_evolve_every
    fired = [i for i in _WINDOW if _will_run(agent, production_config, "evolve", i)]
    assert fired == [i for i in _WINDOW if i % every == 0]


def test_smoke_mode_still_runs_contemplate(make_agent, production_config) -> None:  # type: ignore[no-untyped-def]
    """Documented carve-out: smoke mode always runs ``contemplate``."""
    agent, _, _ = make_agent(config_override=production_config)
    agent.memory.get_pending_plans = MagicMock(return_value=[])
    assert _will_run(agent, production_config, "contemplate", 1, smoke=True) is True
