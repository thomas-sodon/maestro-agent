"""Tests for ``POST /api/plans/{plan_id}/feedback`` (proposal 145)."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory, compute_plan_content_hash
from maestro.messaging import DASHBOARD_FEEDBACK_MESSAGE_ID_PREFIX, MessageStore
from maestro.notifications.base import NullNotifier
from maestro.plan_feedback import FEEDBACK_TEXT_MAX_CHARS, truncate_hash_prefix
from maestro.planner import Planner

FAKE_DIR = Path("/fake/dash")


def _make_app(
    *, with_store: bool = True, **overrides: Any,
):  # type: ignore[no-untyped-def]
    from maestro.agent import MaestroAgent
    from maestro.dashboard.app import create_app

    settings: dict[str, Any] = {
        "loop_plan_feedback_enabled": True, "loop_curate_enabled": True,
    }
    settings.update(overrides)
    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR, **settings)
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    evolution = EvolutionEngine(memory, consciousness, journal, notifier,
                                config=config)
    agent = MaestroAgent(
        config=config, consciousness=consciousness, journal=journal,
        memory=memory, executor=Executor(), planner=Planner(),
        evolution=evolution, notifier=notifier,
        message_store=MessageStore(FAKE_DIR) if with_store else None,
    )
    return create_app(agent), agent


def _seed(agent: Any, status: str = "pending_review") -> EvolutionPlan:
    plan = EvolutionPlan(
        plan_id="p1", title="Wire a sensor",
        category="capability", rationale="r", proposed_changes=["c"],
        files_affected=[], expected_impact="",
        status=status,  # type: ignore[arg-type]
        created_at="2026-10-01T00:00:00Z",
    )
    plan.plan_content_hash = compute_plan_content_hash(plan)
    agent.memory.save_evolution_plan(plan)
    return plan


async def _post(app: Any, body: Any, plan_id: str = "p1") -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(f"/api/plans/{plan_id}/feedback", json=body)


@pytest.mark.asyncio
async def test_feedback_queues_one_message_and_leaves_plan(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app()
    plan = _seed(agent)
    before = asdict(agent.memory.get_plan("p1"))

    r = await _post(app, {"content_hash": plan.plan_content_hash, "text": "  narrow it  "})

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "queued"
    assert data["plan_id"] == "p1"
    msgs = agent.message_store.get_recent(10)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.message_id == data["message_id"]
    assert msg.message_id.startswith(DASHBOARD_FEEDBACK_MESSAGE_ID_PREFIX)
    assert msg.message_type == "plan_feedback"
    assert msg.sender == "thomas"
    assert msg.content == "narrow it"
    assert msg.reply_to == f"p1@{truncate_hash_prefix(plan.plan_content_hash)}"
    assert asdict(agent.memory.get_plan("p1")) == before


@pytest.mark.asyncio
async def test_feedback_stale_hash_accepted(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app()
    _seed(agent)
    stale = "f" * 64

    r = await _post(app, {"content_hash": stale, "text": "revise"})

    assert r.status_code == 200
    assert agent.message_store.get_recent(10)[0].reply_to == f"p1@{truncate_hash_prefix(stale)}"


@pytest.mark.asyncio
async def test_feedback_unknown_plan_404(mem_fs: dict[str, str]) -> None:
    app, _ = _make_app()
    r = await _post(app, {"content_hash": "ab", "text": "t"}, plan_id="nope")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_feedback_non_pending_plan_400(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app()
    _seed(agent, status="approved")
    r = await _post(app, {"content_hash": "ab", "text": "t"})
    assert r.status_code == 400
    assert r.json()["error"] == "Plan is not pending review"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"content_hash": "ab", "text": "   "},
        {"content_hash": "ab", "text": "a" * (FEEDBACK_TEXT_MAX_CHARS + 1)},
        {"content_hash": "ab", "text": "t", "extra": 1},
        {"text": "t"},
        {"content_hash": "ab@cd", "text": "t"},
        {"content_hash": "a" * 65, "text": "t"},
        ["not", "a", "dict"],
    ],
)
async def test_feedback_bad_body_400(mem_fs: dict[str, str], body: Any) -> None:
    app, agent = _make_app()
    _seed(agent)
    r = await _post(app, body)
    assert r.status_code == 400
    assert agent.message_store.get_recent(10) == []


@pytest.mark.asyncio
async def test_feedback_invalid_json_400(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app()
    _seed(agent)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post(
            "/api/plans/p1/feedback", content=b"not json",
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"loop_plan_feedback_enabled": False},
        {"loop_curate_enabled": False},
        {"work_focus_profile": "external_delivery"},
    ],
)
async def test_feedback_unserviceable_409(
    mem_fs: dict[str, str], overrides: dict[str, Any],
) -> None:
    app, agent = _make_app(**overrides)
    _seed(agent)
    r = await _post(app, {"content_hash": "ab", "text": "t"})
    assert r.status_code == 409
    assert "not being serviced" in r.json()["error"]
    assert agent.message_store.get_recent(10) == []


@pytest.mark.asyncio
async def test_feedback_no_store_501(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(with_store=False)
    _seed(agent)
    r = await _post(app, {"content_hash": "ab", "text": "t"})
    assert r.status_code == 501


@pytest.mark.asyncio
async def test_feedback_full_ledger_409(mem_fs: dict[str, str]) -> None:
    from maestro.plan_feedback import MAX_UNADDRESSED_FEEDBACK

    app, agent = _make_app()
    plan = _seed(agent)
    for i in range(MAX_UNADDRESSED_FEEDBACK):
        assert agent.memory.add_plan_feedback(
            "p1", f"t{i}", source="telegram", content_hash_seen="ab",
            feedback_id=f"f{i}",
        )
    r = await _post(app, {"content_hash": plan.plan_content_hash, "text": "more"})
    assert r.status_code == 409
    assert "unaddressed" in r.json()["error"]
    assert agent.message_store.get_recent(10) == []
