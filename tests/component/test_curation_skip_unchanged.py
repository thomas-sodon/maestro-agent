"""Proposal 155 step 6 acceptance — unchanged pending plans cost no curation.

Under the shipped production posture, two kept ``pending_review`` plans make
zero curate calls over 100 iterations once each carries a curation stamp, and
one new reviewer note buys exactly one call.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from maestro import config_settings
from maestro.config import Config
from maestro.memory import EvolutionPlan
from maestro.sdk import QueryResult
from tests.component.conftest import CONFIG_DIR, apply_test_overlay

pytestmark = pytest.mark.component


@pytest.fixture
def production_config(memory_dir, workspace_dir) -> Config:  # type: ignore[no-untyped-def]
    cfg = config_settings.build_config_authoritative(CONFIG_DIR, "production")
    return apply_test_overlay(cfg, memory_dir, workspace_dir)


def _plan(plan_id: str) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=f"Plan {plan_id}",
        category="capability",
        rationale="because",
        proposed_changes=["change one"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        acceptance_criteria=["a test pins it"],
        status="pending_review",
        created_at="2025-01-01T00:00:00Z",
    )


async def _drive(agent: Any, cfg: Config, iterations: range) -> None:
    for i in iterations:
        if agent._phase_will_run(
            "curate_pending_plans",
            iteration=i,
            smoke=False,
            first_iteration_after_restart=False,
            task_every=cfg.loop_task_every,
            evolve_every=cfg.loop_evolve_every,
            dream_every=cfg.loop_dream_every,
            audit_self_every=cfg.loop_audit_self_every,
            contemplate_every=cfg.loop_contemplate_every,
            curate_every=cfg.loop_curate_every,
            curate_enabled=cfg.loop_curate_enabled,
            body_frozen=False,
        ):
            await agent._curate_pending_plans(session=None)


@pytest.mark.asyncio
async def test_unchanged_plans_cost_no_curation(make_agent, production_config) -> None:  # type: ignore[no-untyped-def]
    agent, _, _ = make_agent(config_override=production_config)
    for pid in ("p1", "p2"):
        agent.memory.save_evolution_plan(_plan(pid))

    def _verdict(*_a: Any, **_k: Any) -> QueryResult:
        return QueryResult(response=json.dumps({"action": "keep", "response": "ok"}))

    calls = AsyncMock(side_effect=_verdict)
    with patch("maestro.evolution.single_use_call", calls), patch(
        "maestro.evolution.query", calls,
    ):
        # Warm-up: each plan's first keep stamps its fingerprint.
        await agent._curate_pending_plans(session=None)
        await agent._curate_pending_plans(session=None)
        assert calls.await_count == 2

        calls.reset_mock()
        await _drive(agent, production_config, range(1, 101))
        assert calls.await_count == 0

        plan = agent.memory.get_plan("p1")
        assert plan is not None
        assert agent.memory.add_plan_feedback(
            "p1", "tighten the criteria", source="dashboard",
            content_hash_seen=plan.plan_content_hash,
        )
        await _drive(agent, production_config, range(101, 201))
        assert calls.await_count == 1
