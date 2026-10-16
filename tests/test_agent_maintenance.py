"""Tests for operator-initiated maintenance mode (Telegram /maintenance)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# ── Command handler ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maintenance_command_on_toggles_state(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """`/maintenance on` flips the flag, persists it, replies, and gates phases."""
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    consumed = await agent._handle_maintenance_command("/maintenance on")

    assert consumed is True
    assert agent.maintenance_mode_active is True
    assert agent.budget_throttle_active is True
    notify_mock.assert_awaited_once()
    assert "ENGAGED" in notify_mock.await_args[0][0]

    sidecar = json.loads(mem_fs[str(agent.config.maintenance_state_path)])
    assert sidecar["active"] is True
    assert sidecar["set_by"] == "telegram"
    assert isinstance(sidecar["since"], str)


@pytest.mark.asyncio
async def test_maintenance_command_off_toggles_state(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """`/maintenance off` clears the flag and rewrites the sidecar."""
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    await agent._handle_maintenance_command("/maintenance on")
    await agent._handle_maintenance_command("/maintenance off")

    assert agent.maintenance_mode_active is False
    assert agent.budget_throttle_active is False
    sidecar = json.loads(mem_fs[str(agent.config.maintenance_state_path)])
    assert sidecar["active"] is False
    assert sidecar["since"] is None


@pytest.mark.asyncio
async def test_maintenance_command_idempotent_on(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Toggling to the current state replies "already X" without rewriting."""
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    await agent._handle_maintenance_command("/maintenance on")
    first_payload = mem_fs[str(agent.config.maintenance_state_path)]

    notify_mock.reset_mock()
    await agent._handle_maintenance_command("/maintenance on")

    assert mem_fs[str(agent.config.maintenance_state_path)] == first_payload
    notify_mock.assert_awaited_once()
    reply = notify_mock.await_args[0][0]
    assert "already" in reply.lower()
    assert "ON" in reply


@pytest.mark.asyncio
async def test_maintenance_command_idempotent_off(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """`/maintenance off` while already OFF replies "already OFF" without writing."""
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    await agent._handle_maintenance_command("/maintenance off")

    assert str(agent.config.maintenance_state_path) not in mem_fs
    notify_mock.assert_awaited_once()
    reply = notify_mock.await_args[0][0]
    assert "already" in reply.lower()
    assert "OFF" in reply


@pytest.mark.asyncio
async def test_maintenance_command_status(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Bare `/maintenance` and `/maintenance status` both report current state."""
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    await agent._handle_maintenance_command("/maintenance")
    await agent._handle_maintenance_command("/maintenance status")

    assert agent.maintenance_mode_active is False
    assert notify_mock.await_count == 2
    for call in notify_mock.await_args_list:
        assert "currently OFF" in call.args[0]


@pytest.mark.asyncio
async def test_maintenance_command_case_insensitive(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Case and surrounding whitespace are normalised away."""
    agent = make_agent()
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]

    await agent._handle_maintenance_command("/Maintenance ON")
    assert agent.maintenance_mode_active is True

    await agent._handle_maintenance_command("  /MAINTENANCE  off  ")
    assert agent.maintenance_mode_active is False


@pytest.mark.asyncio
async def test_maintenance_command_malformed(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Unknown subcommands produce a usage reply and leave state untouched."""
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    consumed_one = await agent._handle_maintenance_command("/maintenance foo")
    consumed_two = await agent._handle_maintenance_command("/maintenance on extra")

    assert consumed_one is True
    assert consumed_two is True
    assert agent.maintenance_mode_active is False
    assert str(agent.config.maintenance_state_path) not in mem_fs
    assert notify_mock.await_count == 2
    for call in notify_mock.await_args_list:
        assert "Usage:" in call.args[0]


@pytest.mark.asyncio
async def test_maintenance_command_not_recognized_falls_through(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Plain text that does not start with `/maintenance` returns False."""
    agent = make_agent()
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]

    consumed = await agent._handle_maintenance_command("hello there")

    assert consumed is False
    assert agent.maintenance_mode_active is False


@pytest.mark.asyncio
async def test_maintenance_command_arbitrary_suffix_falls_through(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """A `/maintenance`-prefixed typo (`/maintenancex`) is not consumed.

    Regression lock for #1995: the old substring `startswith` guard let
    `/maintenancex` reach the bare-status branch and silently reply. The
    exact-token guard now returns False so the message is stored as a normal
    instruction — no state change, no sidecar write, no notify.
    """
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    consumed = await agent._handle_maintenance_command("/maintenancex")

    assert consumed is False
    assert agent.maintenance_mode_active is False
    assert str(agent.config.maintenance_state_path) not in mem_fs
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_maintenance_command_underscore_suffix_falls_through(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """An underscore-suffixed token (`/maintenance_foo`) also falls through."""
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    consumed = await agent._handle_maintenance_command("/maintenance_foo")

    assert consumed is False
    assert agent.maintenance_mode_active is False
    assert str(agent.config.maintenance_state_path) not in mem_fs
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_maintenance_command_works_without_message_store(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """The handler itself does not depend on MessageStore being configured."""
    agent = make_agent()
    agent.message_store = None
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]

    consumed = await agent._handle_maintenance_command("/maintenance on")

    assert consumed is True
    assert agent.maintenance_mode_active is True


# ── Sidecar persistence ───────────────────────────────────────────────


def test_maintenance_state_restored_from_sidecar(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """A pre-written sidecar with active=true restores via the read helper."""
    agent = make_agent()
    mem_fs[str(agent.config.maintenance_state_path)] = json.dumps({
        "active": True,
        "since": "2026-05-13T00:00:00+00:00",
        "set_by": "telegram",
    })

    assert agent._read_maintenance_state() is True


def test_maintenance_state_corrupt_file_defaults_to_false(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Corrupt JSON in the sidecar defaults to False rather than raising."""
    agent = make_agent()
    mem_fs[str(agent.config.maintenance_state_path)] = "not json"

    assert agent._read_maintenance_state() is False


def test_maintenance_state_missing_file_defaults_to_false(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A missing sidecar file (cold start) is treated as OFF."""
    agent = make_agent()

    assert agent._read_maintenance_state() is False


def test_maintenance_state_non_bool_active_defaults_to_false(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Non-bool ``active`` defaults to False without raising."""
    agent = make_agent()
    mem_fs[str(agent.config.maintenance_state_path)] = json.dumps({
        "active": "yes",
    })

    assert agent._read_maintenance_state() is False


def test_maintenance_state_non_dict_payload_defaults_to_false(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """A JSON payload that parses but isn't a dict (e.g. a list) defaults to False."""
    agent = make_agent()
    mem_fs[str(agent.config.maintenance_state_path)] = json.dumps([1, 2, 3])

    assert agent._read_maintenance_state() is False


# ── Phase gating ──────────────────────────────────────────────────────


def test_maintenance_gates_budget_phases(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A budget-gated phase skips when only the maintenance input is active."""
    agent = make_agent()
    agent._maintenance_mode_active = True
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=False,
    ):
        will_run = agent._phase_will_run(
            "contemplate",
            iteration=1, smoke=False, first_iteration_after_restart=True,
            task_every=3, evolve_every=5, dream_every=10, audit_self_every=10,
        )
    assert will_run is False


def test_maintenance_does_not_gate_process_messages(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``process_messages`` stays alive so the operator can release the gate."""
    agent = make_agent()
    agent._maintenance_mode_active = True
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=False,
    ):
        will_run = agent._phase_will_run(
            "process_messages",
            iteration=1, smoke=False, first_iteration_after_restart=True,
            task_every=3, evolve_every=5, dream_every=10, audit_self_every=10,
        )
    assert will_run is True


def test_maintenance_keeps_tend_prs_llm_steps_alive(make_agent) -> None:  # type: ignore[no-untyped-def]
    """PR tending is top priority — operator-initiated maintenance does not
    disable conflict resolution or the post-merge quality-fix. The gate is
    keyed on ``burn_rate_throttle_active`` only, so maintenance leaves both
    LLM flags at True.
    """
    agent = make_agent()
    agent._maintenance_mode_active = True
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=False,
    ):
        will_run = agent._phase_will_run(
            "tend_evolution_prs",
            iteration=1, smoke=False, first_iteration_after_restart=True,
            task_every=3, evolve_every=5, dream_every=10, audit_self_every=10,
        )
    # Phase itself runs (it is not in ``_BUDGET_GATED_PHASES``).
    assert will_run is True
    # Maintenance is the only throttle input — burn-rate is not engaged,
    # so the per-call gate that the loop dispatches with stays ON.
    assert agent.budget_throttle_active is True
    assert agent.burn_rate_throttle_active is False


def test_maintenance_does_not_stretch_sleep(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When only maintenance is active and burn rate is zero, multiplier is 1.0."""
    from maestro.cost_tracking import CostRateSnapshot

    agent = make_agent()
    agent._maintenance_mode_active = True
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.budget_rate_min_cost_usd = 0.10

    zero_snapshot = CostRateSnapshot(
        window_start="2026-05-13T00:00:00+00:00",
        window_end="2026-05-13T01:00:00+00:00",
        window_seconds=3600.0,
        total_cost_usd=0.0,
        total_input_tokens=0,
        total_output_tokens=0,
        total_cache_read_tokens=0,
        total_calls=0,
        cost_per_hour=0.0,
        cost_per_day=0.0,
        cost_per_month=0.0,
        tokens_in_per_hour=0.0,
        tokens_out_per_hour=0.0,
        by_caller={},
    )
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=zero_snapshot,
    ):
        gating, sleep, _snapshot = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    assert gating == 1.0
    assert sleep == 1.0


# ── Edge-detector milestones ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_maintenance_edge_detector_fires_enter_milestone_once(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Crossing into maintenance fires one notify + journal milestone."""
    agent = make_agent()
    agent._maintenance_mode_active = True
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    await agent._maybe_notify_maintenance_transition()
    # Sustained: no re-notify on the second tick.
    await agent._maybe_notify_maintenance_transition()

    assert agent._maintenance_milestone_persisted is True
    notify_mock.assert_awaited_once()
    entries = agent.journal.read_recent_events(n=10)
    enter = [e for e in entries if e.triggered_by == "maintenance_enter"]
    assert len(enter) == 1


@pytest.mark.asyncio
async def test_maintenance_edge_detector_fires_exit_milestone(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Releasing maintenance fires one exit notify + journal milestone."""
    agent = make_agent()
    agent._maintenance_milestone_persisted = True
    agent._maintenance_mode_active = False
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    await agent._maybe_notify_maintenance_transition()

    assert agent._maintenance_milestone_persisted is False
    notify_mock.assert_awaited_once()
    assert "released" in notify_mock.await_args[0][0].lower()
    entries = agent.journal.read_recent_events(n=10)
    exit_entries = [
        e for e in entries if e.triggered_by == "maintenance_exit"
    ]
    assert len(exit_entries) == 1


@pytest.mark.asyncio
async def test_maintenance_edge_detector_no_notify_when_idle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """No transition → no notify, no state change."""
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    await agent._maybe_notify_maintenance_transition()

    notify_mock.assert_not_awaited()
    assert agent._maintenance_milestone_persisted is False


@pytest.mark.asyncio
async def test_maintenance_edge_detector_swallows_journal_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Journal failure during entry is logged, not raised — state still set."""
    agent = make_agent()
    agent._maintenance_mode_active = True
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("journal down"),
    )

    await agent._maybe_notify_maintenance_transition()

    assert agent._maintenance_milestone_persisted is True


@pytest.mark.asyncio
async def test_maintenance_edge_detector_exit_swallows_journal_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Journal failure during exit is logged, not raised — state still cleared."""
    agent = make_agent()
    agent._maintenance_milestone_persisted = True
    agent._maintenance_mode_active = False
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("journal down"),
    )

    await agent._maybe_notify_maintenance_transition()

    assert agent._maintenance_milestone_persisted is False


# ── Lifecycle round-trip ──────────────────────────────────────────────


def test_maintenance_milestone_persisted_round_trip(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """The edge-flag mirror round-trips through lifecycle.json."""
    agent = make_agent()
    agent._maintenance_milestone_persisted = True
    agent._persist_progress(5, "evolve")

    data = json.loads(agent.config.lifecycle_path.read_text())
    assert data["maintenance_milestone_persisted"] is True

    fresh = make_agent()
    result = fresh._read_lifecycle_progress()
    assert result[-1] is True


def test_maintenance_milestone_legacy_file_defaults_to_false(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """A lifecycle.json missing the new key defaults to False, no warning."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-05-13T00:00:00+00:00",
        "iteration": 1,
        "next_phase": None,
    })

    result = agent._read_lifecycle_progress()
    assert result[-1] is False


def test_maintenance_milestone_non_bool_defaults_to_false(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Corrupt non-bool maintenance flag defaults to False."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-05-13T00:00:00+00:00",
        "iteration": 1,
        "next_phase": None,
        "maintenance_milestone_persisted": "yes",
    })

    result = agent._read_lifecycle_progress()
    assert result[-1] is False


# ── _on_message wiring ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_message_routes_maintenance_command_to_handler(make_agent) -> None:  # type: ignore[no-untyped-def]
    """`_on_message` short-circuits on `/maintenance`, never touches MessageStore."""
    from maestro.messaging import MessageStore
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]

    captured: dict[str, object] = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured["on_message"] = kwargs.get("on_message")

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock(side_effect=fake_start_polling)
    mock_notifier.stop_approval_polling = AsyncMock()
    mock_notifier.start_message_polling = AsyncMock()
    mock_notifier.stop_message_polling = AsyncMock()
    mock_notifier.resolve_plan = AsyncMock()
    mock_notifier.__class__ = TelegramNotifier
    mock_notifier.poll_failure = None
    agent.notifier = mock_notifier

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_message = captured.get("on_message")
    assert on_message is not None
    await on_message("/maintenance on")  # type: ignore[misc]

    assert agent.maintenance_mode_active is True
    assert len(agent.message_store.get_recent(10)) == 0


@pytest.mark.asyncio
async def test_on_message_works_without_message_store(make_agent) -> None:  # type: ignore[no-untyped-def]
    """`_on_message` falls through cleanly when MessageStore is disabled."""
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    agent.message_store = None
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]

    captured: dict[str, object] = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured["on_message"] = kwargs.get("on_message")

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock(side_effect=fake_start_polling)
    mock_notifier.stop_approval_polling = AsyncMock()
    mock_notifier.start_message_polling = AsyncMock()
    mock_notifier.stop_message_polling = AsyncMock()
    mock_notifier.resolve_plan = AsyncMock()
    mock_notifier.__class__ = TelegramNotifier
    mock_notifier.poll_failure = None
    agent.notifier = mock_notifier

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_message = captured.get("on_message")
    assert on_message is not None
    # Plain text with no message store: should not raise.
    await on_message("hello there")  # type: ignore[misc]
    # /maintenance command still works.
    await on_message("/maintenance on")  # type: ignore[misc]
    assert agent.maintenance_mode_active is True


# ── Dashboard exposure ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_diagnostics_exposes_maintenance_flag(mem_fs: dict[str, str]) -> None:
    """`/api/diagnostics` includes ``maintenance_mode_active`` from the sidecar."""
    from pathlib import Path

    from maestro.agent import MaestroAgent
    from maestro.config import Config
    from maestro.consciousness import Consciousness
    from maestro.dashboard.app import create_app
    from maestro.evolution import EvolutionEngine
    from maestro.executor import Executor
    from maestro.journal import Journal
    from maestro.memory import Memory
    from maestro.notifications.base import NullNotifier
    from maestro.planner import Planner

    fake_dir = Path("/fake/dash-maint")
    config = Config(memory_dir=fake_dir, workspace=fake_dir)
    memory = Memory(fake_dir)
    consciousness = Consciousness(fake_dir)
    journal = Journal(fake_dir)
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
    app = create_app(agent)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r_off = await client.get("/api/diagnostics")
    data_off = r_off.json()
    assert data_off["maintenance_mode_active"] is False

    # The dashboard reads the loop's persisted sidecar, not the serve-side
    # agent's in-memory flag (#4075).
    mem_fs[str(config.maintenance_state_path)] = '{"active": true}'
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r_on = await client.get("/api/diagnostics")
    data_on = r_on.json()
    assert data_on["maintenance_mode_active"] is True
    assert data_on["budget_throttle_active"] is True
