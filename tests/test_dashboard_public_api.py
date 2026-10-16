"""Tests for the public `/api/public/identity` endpoint.

These tests guard the curated-subset contract: only the intended whitelist of
SelfModel fields is exposed, and no authentication is required.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.config import Config
from maestro.consciousness import Consciousness, SelfModel
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner

FAKE_DIR = Path("/fake/pubapi")

EXPECTED_KEYS = {
    "framing",
    "awakened_at",
    "evolution_count",
    "identity_statements",
    "values",
    "capabilities_known",
    "limitations_known",
    "current_phase",
}


def _make_app(mem_fs: dict[str, str], *, token: str | None = None):  # type: ignore[no-untyped-def]
    from maestro.agent import MaestroAgent
    from maestro.dashboard.app import create_app

    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR, dashboard_token=token)
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    executor = Executor()
    planner = Planner()
    evolution = EvolutionEngine(
        memory, consciousness, journal, notifier, config=config,
    )

    agent = MaestroAgent(
        config=config, consciousness=consciousness, journal=journal,
        memory=memory, executor=executor, planner=planner,
        evolution=evolution, notifier=notifier,
    )
    return create_app(agent), agent


@pytest.mark.asyncio
async def test_public_identity_returns_200_without_auth(
    mem_fs: dict[str, str],
) -> None:
    """Public endpoint must return 200 even when a dashboard token is set."""
    app, _ = _make_app(mem_fs, token="test-secret-token")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_public_identity_payload_keys_are_exact(
    mem_fs: dict[str, str],
) -> None:
    """Response must contain exactly the whitelisted keys — no more, no less."""
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    data = r.json()
    assert set(data.keys()) == EXPECTED_KEYS


@pytest.mark.asyncio
async def test_public_identity_excludes_private_fields(
    mem_fs: dict[str, str],
) -> None:
    """Open questions and other private SelfModel fields must never leak."""
    app, agent = _make_app(mem_fs)
    # Populate the self-model with a non-default open_questions list so we
    # would notice if it slipped through.
    model = agent.consciousness.load_self_model()
    model.open_questions = ["secret question?"]
    agent.consciousness.save_self_model(model)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    data = r.json()
    assert "open_questions" not in data
    assert "current_task" not in data
    assert "secret question?" not in r.text


@pytest.mark.asyncio
async def test_public_identity_empty_state_valid(mem_fs: dict[str, str]) -> None:
    """Fresh SelfModel should return a valid payload with empty lists."""
    app, _ = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    data = r.json()
    assert data["identity_statements"] == []
    assert data["values"] == []
    assert data["capabilities_known"] == []
    assert data["limitations_known"] == []
    assert data["evolution_count"] == 0


@pytest.mark.asyncio
async def test_public_identity_caps_list_counts_at_5(
    mem_fs: dict[str, str],
) -> None:
    """Identity/values/capabilities/limitations lists are truncated to 5 items."""
    app, agent = _make_app(mem_fs)
    many = [f"item {i}" for i in range(10)]
    # Consciousness caps list fields to MAX_LIST_ITEMS (10) on save, so we
    # assign 10 items and expect exactly 5 to come back from the public API.
    agent.consciousness.save_self_model(SelfModel(
        identity_statements=many,
        values=many,
        capabilities_known=many,
        limitations_known=many,
    ))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    data = r.json()
    assert len(data["identity_statements"]) == 5
    assert len(data["values"]) == 5
    assert len(data["capabilities_known"]) == 5
    assert len(data["limitations_known"]) == 5


@pytest.mark.asyncio
async def test_public_identity_string_lengths_bounded(
    mem_fs: dict[str, str],
) -> None:
    """Each list string is <= 200 chars.

    Consciousness enforces these caps on save/load, so the public API can
    rely on them without re-truncating.
    """
    app, agent = _make_app(mem_fs)
    long_item = "x" * 500  # over the 200-char item cap
    agent.consciousness.save_self_model(SelfModel(
        identity_statements=[long_item],
        values=[long_item],
        capabilities_known=[long_item],
        limitations_known=[long_item],
    ))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    data = r.json()
    for key in ("identity_statements", "values", "capabilities_known", "limitations_known"):
        for s in data[key]:
            assert len(s) <= 200, f"{key} item exceeds 200-char cap"


@pytest.mark.asyncio
async def test_private_consciousness_endpoint_still_requires_auth(
    mem_fs: dict[str, str],
) -> None:
    """Adding a public subset must not weaken /api/consciousness."""
    app, _ = _make_app(mem_fs, token="test-secret-token")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/consciousness")
    assert r.status_code == 401


def _write_heartbeat(mem_fs: dict[str, str], age_seconds: float = 0.0) -> None:
    """Write a heartbeat file ``age_seconds`` in the past."""
    ts = datetime.now(UTC) - timedelta(seconds=age_seconds)
    mem_fs[str(FAKE_DIR / "loop.heartbeat")] = ts.isoformat()


def _write_lifecycle(mem_fs: dict[str, str], current_phase: str | None) -> None:
    """Write a lifecycle.json with the given ``current_phase``."""
    mem_fs[str(FAKE_DIR / "lifecycle.json")] = json.dumps({
        "iteration": 1,
        "next_phase": current_phase,
        "current_phase": current_phase,
    })


@pytest.mark.asyncio
async def test_public_identity_reports_none_phase_when_no_heartbeat(
    mem_fs: dict[str, str],
) -> None:
    """Missing heartbeat (loop never ran / loop dead) → ``current_phase=None``.

    The in-memory ``agent._current_phase`` is irrelevant: the dashboard and
    loop run as separate containers in production, so the endpoint must
    source the phase from shared disk state only.
    """
    app, agent = _make_app(mem_fs)
    # Simulate a bogus in-memory override to prove it's ignored.
    agent._current_phase = "dream"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.json()["current_phase"] is None


@pytest.mark.asyncio
async def test_public_identity_reads_phase_from_lifecycle_json(
    mem_fs: dict[str, str],
) -> None:
    """Fresh heartbeat + lifecycle.json.current_phase → endpoint returns it."""
    app, _ = _make_app(mem_fs)
    _write_heartbeat(mem_fs)
    _write_lifecycle(mem_fs, "dream")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.json()["current_phase"] == "dream"


@pytest.mark.asyncio
async def test_public_identity_reflects_budget_throttle_override(
    mem_fs: dict[str, str],
) -> None:
    """The budget-throttle override is faithfully carried through lifecycle.json."""
    app, _ = _make_app(mem_fs)
    _write_heartbeat(mem_fs)
    _write_lifecycle(mem_fs, "budget_throttle")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.json()["current_phase"] == "budget_throttle"


@pytest.mark.asyncio
async def test_public_identity_returns_none_when_heartbeat_is_stale(
    mem_fs: dict[str, str],
) -> None:
    """Stale heartbeat (>600s old) → loop is dead, visualizer goes idle."""
    app, _ = _make_app(mem_fs)
    _write_heartbeat(mem_fs, age_seconds=601)
    _write_lifecycle(mem_fs, "evolve")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.json()["current_phase"] is None


@pytest.mark.asyncio
async def test_public_identity_returns_none_when_lifecycle_missing(
    mem_fs: dict[str, str],
) -> None:
    """Heartbeat present but lifecycle.json missing → None (loop starting up)."""
    app, _ = _make_app(mem_fs)
    _write_heartbeat(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.json()["current_phase"] is None


@pytest.mark.asyncio
async def test_public_identity_returns_none_when_lifecycle_malformed(
    mem_fs: dict[str, str],
) -> None:
    """Malformed lifecycle.json → None, not a 500."""
    app, _ = _make_app(mem_fs)
    _write_heartbeat(mem_fs)
    mem_fs[str(FAKE_DIR / "lifecycle.json")] = "not json {{{"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.status_code == 200
    assert r.json()["current_phase"] is None


@pytest.mark.asyncio
async def test_public_identity_returns_none_when_lifecycle_omits_phase(
    mem_fs: dict[str, str],
) -> None:
    """lifecycle.json without ``current_phase`` (legacy payload) → None."""
    app, _ = _make_app(mem_fs)
    _write_heartbeat(mem_fs)
    mem_fs[str(FAKE_DIR / "lifecycle.json")] = json.dumps({
        "iteration": 1, "next_phase": "evolve",
    })
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.json()["current_phase"] is None


@pytest.mark.asyncio
async def test_public_identity_accepts_naive_heartbeat_timestamp(
    mem_fs: dict[str, str],
) -> None:
    """A naive (offset-less) heartbeat is interpreted as UTC, not rejected.

    Guards against a scenario where an external script (or a future refactor)
    writes the heartbeat without a ``+00:00`` suffix — we prefer tolerance
    over a false 'loop dead' report.
    """
    app, _ = _make_app(mem_fs)
    # A naive timestamp in the recent past (assumed UTC).
    mem_fs[str(FAKE_DIR / "loop.heartbeat")] = (
        datetime.now(UTC).replace(tzinfo=None).isoformat()
    )
    _write_lifecycle(mem_fs, "contemplate")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.json()["current_phase"] == "contemplate"


@pytest.mark.asyncio
async def test_public_identity_returns_none_when_lifecycle_not_dict(
    mem_fs: dict[str, str],
) -> None:
    """lifecycle.json that parses as non-dict JSON → None, not a crash."""
    app, _ = _make_app(mem_fs)
    _write_heartbeat(mem_fs)
    mem_fs[str(FAKE_DIR / "lifecycle.json")] = json.dumps(["not", "a", "dict"])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.status_code == 200
    assert r.json()["current_phase"] is None


@pytest.mark.asyncio
async def test_public_identity_cross_container_writer_reader(
    mem_fs: dict[str, str],
) -> None:
    """A dashboard agent reads phase written by a *different* loop agent.

    This simulates the production topology: the dashboard container and the
    loop container hold separate ``MaestroAgent`` instances. The dashboard
    never enters ``run_loop``, so its in-memory ``_current_phase`` stays
    ``None`` forever. The endpoint must nonetheless surface the loop's live
    phase via shared on-disk state.
    """
    # The "loop" agent writes lifecycle.json via the real ``_persist_progress``
    # path. Its in-memory phase is set the way ``run_loop`` sets it.
    _, loop_agent = _make_app(mem_fs)
    loop_agent._current_phase = "evolve"
    loop_agent._persist_progress(iteration=7, next_phase="evolve")

    # A *separate* "dashboard" agent serves the endpoint. Its in-memory
    # attribute remains None — proving disk state is the sole source.
    dashboard_app, dashboard_agent = _make_app(mem_fs)
    assert dashboard_agent is not loop_agent
    assert dashboard_agent.current_phase is None

    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.json()["current_phase"] == "evolve"

    # Loop advances: throttle override during stretched inter-iteration sleep.
    loop_agent._current_phase = "budget_throttle"
    loop_agent._persist_progress(iteration=7, next_phase="contemplate")
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.json()["current_phase"] == "budget_throttle"

    # Loop clears phase at iteration end (simulates idle-between-iterations).
    loop_agent._current_phase = None
    loop_agent._persist_progress(iteration=7, next_phase=None)
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.json()["current_phase"] is None
