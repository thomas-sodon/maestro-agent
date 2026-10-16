"""Tests for MaestroAgent rate-limit backoff, loop-level recovery and the
startup probe — split out of tests/test_agent.py (#3875). Fully mocked I/O."""

from __future__ import annotations

import ast
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.agent import (
    MaestroAgent,
)
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine, QualityGateResult
from maestro.evolution_tend import TendPRsResult
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.loop import assembly as assembly_module
from maestro.loop.iteration import _is_in_standby_window
from maestro.loop.lifecycle import _is_in_peak_throttle_window
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner
from maestro.remediation_tracker import (
    TrackerState,
)
from maestro.sdk import ErrorCategory, SDKError
from tests.conftest import MockQueryResult
from tests.support.gap_fuel import stub_evolve_phase

FAKE_DIR = Path("/fake/agent_test")

_OK_REVIEW = QualityGateResult(
    passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
    raw_output="Ruff: clean\n\nMypy: clean\n\nTests: clean",
)


def _build_agent(
    mem_fs: dict[str, str], config: Config | None = None,
) -> MaestroAgent:
    if config is None:
        config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    config.ensure_dirs()
    if isinstance(assembly_module.StateStore, MagicMock):
        # ``@patch("maestro.loop.assembly.StateStore")`` hands every SQLite adapter a
        # MagicMock. All but one are lazy, but the remediation tracker loads its
        # state eagerly at construction and unpacks it (proposal 106), so the
        # mocked store must hand back a real, empty ``TrackerState`` rather than
        # a MagicMock that iterates empty.
        mocked = assembly_module.StateStore.return_value
        mocked.load_remediation_tracker.return_value = TrackerState(
            {}, {}, {}, {}, 0,
        )
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    executor = Executor()
    planner = Planner()
    evolution = EvolutionEngine(memory, consciousness, journal, notifier,
                                config=config)
    vcs = MagicMock()
    vcs.has_changes.return_value = False

    return MaestroAgent(
        config=config, consciousness=consciousness, journal=journal,
        memory=memory, executor=executor, planner=planner,
        evolution=evolution, notifier=notifier, vcs=vcs,
    )

# --- Rate limit backoff tests ---


@pytest.mark.asyncio
async def test_run_loop_crashes_on_sdk_error_in_contemplate(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() crashes when contemplate raises SDKError (no more backoff)."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=SDKError("rate limited"),
    )

    with pytest.raises(SDKError, match="rate limited"):
        await agent.run_loop(interval=60, max_iters=3, task_every=0, evolve_every=999)

    agent.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_run_loop_sleeps_base_interval_on_success(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() sleeps the base interval between successful iterations."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]

    wait_timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        wait_timeouts.append(timeout)
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=60, max_iters=3, task_every=0, evolve_every=999)

    # 2 sleeps (last iteration doesn't sleep)
    assert wait_timeouts == [60, 60]


@pytest.mark.asyncio
async def test_run_loop_base_backoff_ignores_rate_limit_warning_flag(  # type: ignore[no-untyped-def]
    make_agent, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the loop's base-case backoff is just ``interval``.

    The sticky ``allowed_warning`` throttle now lives in
    :mod:`maestro.sdk` as a per-``query()`` pre-call sleep.  The loop
    must no longer apply a multiplier to its own end-of-iteration sleep
    when the throttle state is active: doing so would double-count the
    throttle (once per call + once per iteration).  This test pins that
    invariant so a future refactor cannot silently re-introduce
    loop-level throttling.
    """
    import maestro.sdk as sdk_module

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]

    wait_timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        wait_timeouts.append(timeout)
        raise TimeoutError

    # Prime the SDK-level throttle state to a non-baseline value.  The
    # loop must ignore it completely and still sleep ``interval``.
    monkeypatch.setattr(sdk_module, "_warning_throttle_multiplier", 8)
    monkeypatch.setattr(sdk_module, "_warning_throttle_clean_calls", 0)
    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=60, max_iters=2, task_every=0, evolve_every=999,
        )

    # Exactly one end-of-iteration sleep (iter2 is last), and it must be
    # the plain interval — no 8x multiplier.  The loop no longer touches
    # the throttle state at all.
    assert wait_timeouts == [60]


@pytest.mark.asyncio
async def test_run_loop_phase_error_crashes_loop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Any phase error crashes the loop — no backoff, just crash detection."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=SDKError("model overloaded"),
    )

    with pytest.raises(SDKError, match="model overloaded"):
        await agent.run_loop(interval=120, max_iters=7, task_every=0, evolve_every=999)

    # Shutdown must still be called for cleanup
    agent.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_run_loop_crashes_on_respond_to_messages_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates _respond_to_messages() errors."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]

    # Inject message context so _respond_to_messages is called
    async def _drain() -> str:
        agent._iter_reply_context = "Thomas said hello"
        return "Thomas said hello"

    agent._process_messages = AsyncMock(side_effect=_drain)  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="boom"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    agent._respond_to_messages.assert_called_once()
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_phase_crash_invokes_crash_handler_on_restart(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """End-to-end: a phase crash must reach the crash-detection path.

    Pins the phase-crash -> crash-handler wiring so a future refactor
    can't silently re-introduce phase-level error suppression. Verifies:

    1. A ``RuntimeError`` raised from ``contemplate`` propagates out of
       ``run_loop`` (instead of being swallowed by an ``except Exception:``).
    2. The outer ``finally`` calls ``shutdown(crashed=True)`` (derived via
       ``sys.exc_info()``).
    3. On the *next* ``run_loop`` invocation, ``handle_crash`` is actually
       called and the loop continues normally — confirming
       ``_read_lifecycle_progress`` sees ``crashed=True`` when
       ``last_shutdown`` is absent from ``lifecycle.json``.
    """
    from unittest.mock import ANY

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("phase crash"),
    )

    # Run #1: contemplate raises, outer finally calls the real shutdown()
    # which honours crashed=True by skipping last_shutdown write.
    with patch.object(agent.evolution, "ensure_main_branch"), \
         pytest.raises(RuntimeError, match="phase crash"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # lifecycle.json should exist but have no last_shutdown key — this is
    # the signal that _read_lifecycle_progress uses to set crashed=True.
    lifecycle_path = agent.config.lifecycle_path
    assert lifecycle_path.exists()
    lifecycle_data = json.loads(lifecycle_path.read_text())
    assert "last_shutdown" not in lifecycle_data

    # Run #2: fresh agent reading the same lifecycle.json must invoke
    # handle_crash via the startup detection block and then continue.
    agent2 = make_agent()
    agent2._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent2._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent2.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent2.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent2._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent2.evolve = AsyncMock()  # type: ignore[method-assign]

    with patch("maestro.crash.handle_crash", new=AsyncMock()) as mock_handle_crash, \
         patch("maestro.crash.build_crash_event") as mock_build_event, \
         patch.object(agent2.evolution, "ensure_main_branch"):
        mock_build_event.return_value = MagicMock()
        result = await agent2.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    mock_handle_crash.assert_called_once()
    mock_build_event.assert_called_once_with(
        last_iteration=ANY,
        last_phase=ANY,
        log_path=ANY,
        exit_code=ANY,
        sdk_error=ANY,
    )
    # Loop continues after crash detection
    assert result is True
    agent2.contemplate.assert_awaited()


# =====================================================================
# Standby window
# =====================================================================


def test_is_in_standby_window_disabled() -> None:
    """When disabled, helper always returns False even mid-window."""
    config = Config(
        standby_enabled=False,
        standby_start_hour_utc=13,
        standby_end_hour_utc=19,
    )
    now = datetime(2025, 6, 15, 15, 0, tzinfo=UTC)
    assert _is_in_standby_window(config, now) is False


def test_is_in_standby_window_inside() -> None:
    config = Config(
        standby_enabled=True,
        standby_start_hour_utc=13,
        standby_end_hour_utc=19,
    )
    now = datetime(2025, 6, 15, 15, 0, tzinfo=UTC)
    assert _is_in_standby_window(config, now) is True


def test_is_in_standby_window_outside() -> None:
    config = Config(
        standby_enabled=True,
        standby_start_hour_utc=13,
        standby_end_hour_utc=19,
    )
    now = datetime(2025, 6, 15, 20, 0, tzinfo=UTC)
    assert _is_in_standby_window(config, now) is False


def test_is_in_standby_window_start_inclusive() -> None:
    """Exactly the start hour is inside the window."""
    config = Config(
        standby_enabled=True,
        standby_start_hour_utc=13,
        standby_end_hour_utc=19,
    )
    now = datetime(2025, 6, 15, 13, 0, tzinfo=UTC)
    assert _is_in_standby_window(config, now) is True


def test_is_in_standby_window_end_exclusive() -> None:
    """Exactly the end hour is outside the window."""
    config = Config(
        standby_enabled=True,
        standby_start_hour_utc=13,
        standby_end_hour_utc=19,
    )
    now = datetime(2025, 6, 15, 19, 0, tzinfo=UTC)
    assert _is_in_standby_window(config, now) is False


def test_is_in_standby_window_wraparound_inside() -> None:
    config = Config(
        standby_enabled=True,
        standby_start_hour_utc=22,
        standby_end_hour_utc=2,
    )
    # 23:00 — past start, before midnight
    assert _is_in_standby_window(
        config, datetime(2025, 6, 15, 23, 0, tzinfo=UTC),
    ) is True
    # 01:00 — after midnight, before end
    assert _is_in_standby_window(
        config, datetime(2025, 6, 15, 1, 0, tzinfo=UTC),
    ) is True


def test_is_in_standby_window_wraparound_outside() -> None:
    config = Config(
        standby_enabled=True,
        standby_start_hour_utc=22,
        standby_end_hour_utc=2,
    )
    assert _is_in_standby_window(
        config, datetime(2025, 6, 15, 12, 0, tzinfo=UTC),
    ) is False


def test_is_in_standby_window_converts_other_zone_to_utc() -> None:
    """A datetime in another zone is converted to UTC before comparison."""
    from zoneinfo import ZoneInfo

    config = Config(
        standby_enabled=True,
        standby_start_hour_utc=13,
        standby_end_hour_utc=19,
    )
    # 10:00 America/New_York EST == 15:00 UTC -> inside window
    et_now = datetime(2025, 1, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert _is_in_standby_window(config, et_now) is True


@pytest.mark.asyncio
async def test_run_loop_skips_iteration_during_standby(make_agent) -> None:  # type: ignore[no-untyped-def]
    """While standby is active no SDK-bearing phase runs; once it ends the
    loop resumes and completes a normal iteration."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Standby returns True for the first two checks, then False so the
    # iteration body runs once and the loop exits via max_iters.
    standby_calls = [True, True, False]

    def fake_standby(_config: Config, now: object = None) -> bool:
        return standby_calls.pop(0) if standby_calls else False

    with patch("maestro.loop.iteration._is_in_standby_window", side_effect=fake_standby):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # Standby was checked at least three times (twice paused, once cleared)
    # and contemplate fired exactly once after standby ended.
    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]
    assert agent.evolve.call_count == 0  # type: ignore[attr-defined]
    assert agent._implement_approved_plans.call_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_standby_responds_to_shutdown(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Setting the shutdown event during standby exits cleanly without
    running any phases."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    def fake_standby(_config: Config, now: object = None) -> bool:
        # Trigger shutdown after the first standby check so the bounded
        # sleep returns immediately and the outer loop exits.
        agent._ensure_shutdown_event().set()
        return True

    with patch("maestro.loop.iteration._is_in_standby_window", side_effect=fake_standby):
        await agent.run_loop(
            interval=0, max_iters=5, task_every=0, evolve_every=999,
        )

    agent.contemplate.assert_not_called()  # type: ignore[attr-defined]
    agent.evolve.assert_not_called()  # type: ignore[attr-defined]


# =====================================================================
# Peak-hours scheduled budget throttle
# =====================================================================


def test_is_in_peak_throttle_window_disabled() -> None:
    """When disabled, helper always returns False even mid-window."""
    config = Config(
        peak_throttle_enabled=False,
        peak_throttle_timezone="UTC",
        peak_throttle_start_hour=13,
        peak_throttle_end_hour=19,
        peak_throttle_weekdays_only=False,
    )
    now = datetime(2025, 6, 16, 15, 0, tzinfo=UTC)  # Monday 15:00 UTC
    assert _is_in_peak_throttle_window(config, now) is False


def test_is_in_peak_throttle_window_inside() -> None:
    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="UTC",
        peak_throttle_start_hour=13,
        peak_throttle_end_hour=19,
        peak_throttle_weekdays_only=True,
    )
    now = datetime(2025, 6, 16, 15, 0, tzinfo=UTC)  # Monday
    assert _is_in_peak_throttle_window(config, now) is True


def test_is_in_peak_throttle_window_outside() -> None:
    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="UTC",
        peak_throttle_start_hour=13,
        peak_throttle_end_hour=19,
        peak_throttle_weekdays_only=True,
    )
    now = datetime(2025, 6, 16, 20, 0, tzinfo=UTC)
    assert _is_in_peak_throttle_window(config, now) is False


def test_is_in_peak_throttle_window_start_inclusive() -> None:
    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="UTC",
        peak_throttle_start_hour=13,
        peak_throttle_end_hour=19,
        peak_throttle_weekdays_only=True,
    )
    now = datetime(2025, 6, 16, 13, 0, tzinfo=UTC)
    assert _is_in_peak_throttle_window(config, now) is True


def test_is_in_peak_throttle_window_end_exclusive() -> None:
    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="UTC",
        peak_throttle_start_hour=13,
        peak_throttle_end_hour=19,
        peak_throttle_weekdays_only=True,
    )
    now = datetime(2025, 6, 16, 19, 0, tzinfo=UTC)
    assert _is_in_peak_throttle_window(config, now) is False


def test_is_in_peak_throttle_window_wraparound() -> None:
    """``start > end`` (window crossing midnight in zone) is supported."""
    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="UTC",
        peak_throttle_start_hour=22,
        peak_throttle_end_hour=2,
        peak_throttle_weekdays_only=False,
    )
    assert _is_in_peak_throttle_window(
        config, datetime(2025, 6, 16, 23, 0, tzinfo=UTC),
    ) is True
    assert _is_in_peak_throttle_window(
        config, datetime(2025, 6, 16, 1, 0, tzinfo=UTC),
    ) is True
    assert _is_in_peak_throttle_window(
        config, datetime(2025, 6, 16, 12, 0, tzinfo=UTC),
    ) is False


def test_is_in_peak_throttle_window_weekday_skip() -> None:
    """``weekdays_only=True`` excludes Saturday and Sunday in the zone."""
    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="UTC",
        peak_throttle_start_hour=13,
        peak_throttle_end_hour=19,
        peak_throttle_weekdays_only=True,
    )
    # Saturday 15:00 UTC — would be inside but for the weekday gate.
    saturday = datetime(2025, 6, 21, 15, 0, tzinfo=UTC)
    sunday = datetime(2025, 6, 22, 15, 0, tzinfo=UTC)
    assert _is_in_peak_throttle_window(config, saturday) is False
    assert _is_in_peak_throttle_window(config, sunday) is False


def test_is_in_peak_throttle_window_weekday_disabled_runs_on_weekend() -> None:
    """``weekdays_only=False`` lets the window fire any day."""
    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="UTC",
        peak_throttle_start_hour=13,
        peak_throttle_end_hour=19,
        peak_throttle_weekdays_only=False,
    )
    saturday = datetime(2025, 6, 21, 15, 0, tzinfo=UTC)
    assert _is_in_peak_throttle_window(config, saturday) is True


def test_is_in_peak_throttle_window_converts_other_zone_to_utc() -> None:
    """A datetime in another zone is normalized before comparison."""
    from zoneinfo import ZoneInfo

    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="UTC",
        peak_throttle_start_hour=13,
        peak_throttle_end_hour=19,
        peak_throttle_weekdays_only=True,
    )
    # 10:00 New York (EST) on a Wednesday == 15:00 UTC.
    et_now = datetime(2025, 1, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert _is_in_peak_throttle_window(config, et_now) is True


def test_is_in_peak_throttle_window_dst_aware_pdt() -> None:
    """5-11 wall-clock in America/Los_Angeles fires correctly during PDT.

    During Pacific Daylight Time (UTC-7), 5 AM Pacific == 12 UTC.
    """
    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="America/Los_Angeles",
        peak_throttle_start_hour=5,
        peak_throttle_end_hour=11,
        peak_throttle_weekdays_only=True,
    )
    # 2025-06-16 is a Monday in PDT.
    inside = datetime(2025, 6, 16, 12, 0, tzinfo=UTC)   # 5 AM PDT
    edge = datetime(2025, 6, 16, 18, 0, tzinfo=UTC)     # 11 AM PDT (exclusive)
    before = datetime(2025, 6, 16, 11, 0, tzinfo=UTC)   # 4 AM PDT
    assert _is_in_peak_throttle_window(config, inside) is True
    assert _is_in_peak_throttle_window(config, edge) is False
    assert _is_in_peak_throttle_window(config, before) is False


def test_is_in_peak_throttle_window_dst_aware_pst() -> None:
    """5-11 wall-clock in America/Los_Angeles fires correctly during PST.

    During Pacific Standard Time (UTC-8), 5 AM Pacific == 13 UTC.
    """
    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="America/Los_Angeles",
        peak_throttle_start_hour=5,
        peak_throttle_end_hour=11,
        peak_throttle_weekdays_only=True,
    )
    # 2025-01-13 is a Monday in PST.
    inside = datetime(2025, 1, 13, 13, 0, tzinfo=UTC)   # 5 AM PST
    edge = datetime(2025, 1, 13, 19, 0, tzinfo=UTC)     # 11 AM PST (exclusive)
    before = datetime(2025, 1, 13, 12, 0, tzinfo=UTC)   # 4 AM PST
    assert _is_in_peak_throttle_window(config, inside) is True
    assert _is_in_peak_throttle_window(config, edge) is False
    assert _is_in_peak_throttle_window(config, before) is False


def test_is_in_peak_throttle_window_weekday_in_configured_zone() -> None:
    """Weekday gate is evaluated in ``peak_throttle_timezone``, not UTC.

    Sunday 23:00 PT == Monday 06:00-07:00 UTC. The weekday filter must say
    Sunday (and skip), not Monday (and fire).
    """
    config = Config(
        peak_throttle_enabled=True,
        peak_throttle_timezone="America/Los_Angeles",
        peak_throttle_start_hour=22,
        peak_throttle_end_hour=23,
        peak_throttle_weekdays_only=True,
    )
    # 2025-06-15 23:00 PDT == 2025-06-16 06:00 UTC. Sunday in PT, Monday in UTC.
    sun_pt_mon_utc = datetime(2025, 6, 16, 6, 0, tzinfo=UTC)
    assert _is_in_peak_throttle_window(config, sun_pt_mon_utc) is False


def test_budget_throttle_active_combines_inputs(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Combined gate fires on burn-rate OR maintenance; peak does NOT gate."""
    agent = make_agent()
    # Both inputs off → False.
    assert agent.budget_throttle_active is False
    # Only burn-rate side → True.
    agent._budget_throttle_entered_at = "2026-05-01T00:00:00+00:00"
    assert agent.burn_rate_throttle_active is True
    assert agent.budget_throttle_active is True
    # Only maintenance side → True (burn-rate cleared).
    agent._budget_throttle_entered_at = None
    agent._maintenance_mode_active = True
    assert agent.burn_rate_throttle_active is False
    assert agent.budget_throttle_active is True
    # Peak-throttle alone no longer flips the gate — it only slows the loop.
    agent._maintenance_mode_active = False
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        assert agent.burn_rate_throttle_active is False
        assert agent.budget_throttle_active is False


@pytest.mark.asyncio
async def test_burn_rate_transition_uses_burn_rate_property_not_combined(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A burn-rate transition while peak-throttle is on still fires entry.

    Regression: before the split, ``budget_throttle_active`` was the only
    signal — so peak-throttle being on would suppress the burn-rate entry
    notification because the combined gate was already True before the
    multiplier crossed.
    """
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    snapshot = _mk_snapshot(cost_per_hour=3.0, total_cost=1.0)
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        await agent._maybe_notify_throttle_transition(
            multiplier=3.0, snapshot=snapshot,
        )
    assert agent.burn_rate_throttle_active is True
    notify_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_throttle_entry_reports_sleep_multiplier_not_gating(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """When peak-throttle is also active, the entry message quotes the sleep
    multiplier (tightened limit), not the smaller gating multiplier.

    Regression: the notifier received only the gating multiplier and reported
    it verbatim as the 'sleep stretched Nx' factor, so operators reading the
    Telegram message during a simultaneous peak window under-estimated the
    slowdown.
    """
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 10.0
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    snapshot = _mk_snapshot(cost_per_hour=12.0, total_cost=12.0)
    # gating = max(1.0, 12/10) = 1.2 ; sleep (limit tightened x0.5) = 12/5 = 2.4
    await agent._maybe_notify_throttle_transition(
        multiplier=1.2, snapshot=snapshot, sleep_multiplier=2.4,
    )
    notify_mock.assert_awaited_once()
    sent = notify_mock.await_args[0][0]
    assert "2.40x" in sent
    assert "1.20x" not in sent


@pytest.mark.asyncio
async def test_throttle_entry_sleep_multiplier_defaults_to_gating(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Off-peak (no sleep_multiplier passed) the message quotes the gating
    multiplier — the two are equal, so the omitted arg falls back to it."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 10.0
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    snapshot = _mk_snapshot(cost_per_hour=30.0, total_cost=30.0)
    await agent._maybe_notify_throttle_transition(multiplier=3.0, snapshot=snapshot)
    notify_mock.assert_awaited_once()
    assert "3.00x" in notify_mock.await_args[0][0]


@pytest.mark.asyncio
async def test_throttle_entry_reports_plan_headroom_when_present(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A cli_paced /usage probe reading rides the entry message read-only
    (proposal 062 Step 5) — awareness, not gating."""
    from maestro.cost_tracking import PlanHeadroom

    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    snapshot = _mk_snapshot(cost_per_hour=3.0, total_cost=1.0)
    with patch(
        "maestro.loop.lifecycle.latest_plan_headroom",
        return_value=PlanHeadroom(percent_used=45.0, label="Weekly"),
    ):
        await agent._maybe_notify_throttle_transition(
            multiplier=3.0, snapshot=snapshot,
        )
    notify_mock.assert_awaited_once()
    sent = notify_mock.await_args[0][0]
    assert "Plan headroom (Weekly): 45% used." in sent


@pytest.mark.asyncio
async def test_throttle_entry_headroom_label_falls_back_to_plan(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A percent-bearing reading with no plan label still reports, labelled
    generically rather than dropping the signal."""
    from maestro.cost_tracking import PlanHeadroom

    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    snapshot = _mk_snapshot(cost_per_hour=3.0, total_cost=1.0)
    with patch(
        "maestro.loop.lifecycle.latest_plan_headroom",
        return_value=PlanHeadroom(percent_used=80.0, label=None),
    ):
        await agent._maybe_notify_throttle_transition(
            multiplier=3.0, snapshot=snapshot,
        )
    assert "Plan headroom (plan): 80% used." in notify_mock.await_args[0][0]


@pytest.mark.asyncio
async def test_throttle_entry_omits_headroom_without_percent(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A reading whose ``/usage`` percent missed (and the default-provider
    ``None``) leaves the entry message unchanged — no fabricated headroom."""
    from maestro.cost_tracking import PlanHeadroom

    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    snapshot = _mk_snapshot(cost_per_hour=3.0, total_cost=1.0)
    with patch(
        "maestro.loop.lifecycle.latest_plan_headroom",
        return_value=PlanHeadroom(percent_used=None, label="Weekly"),
    ):
        await agent._maybe_notify_throttle_transition(
            multiplier=3.0, snapshot=snapshot,
        )
    assert "Plan headroom" not in notify_mock.await_args[0][0]


@pytest.mark.asyncio
async def test_peak_throttle_transition_entry_notifies_once(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Crossing into peak window emits one notify + journal entry.

    With a budget limit set, the entry message describes the slowdown
    (tightened budget rate), not phase gating.
    """
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.peak_throttle_rate_multiplier = 0.5
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        await agent._maybe_notify_peak_throttle_transition()
        # Sustained: no re-notify.
        await agent._maybe_notify_peak_throttle_transition()
    assert agent._peak_throttle_active_persisted is True
    notify_mock.assert_awaited_once()
    msg = notify_mock.await_args[0][0].lower()
    assert "peak" in msg
    assert "tightened" in msg
    entries = agent.journal.read_recent_events(n=10)
    enter = [e for e in entries if e.triggered_by == "peak_throttle_enter"]
    assert len(enter) == 1


@pytest.mark.asyncio
async def test_peak_throttle_entry_warns_when_budget_disabled(make_agent, caplog) -> None:  # type: ignore[no-untyped-def]
    """Entering the window with no budget limit warns that no slowdown applies."""

    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 0.0
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ), caplog.at_level(logging.WARNING, logger="maestro.agent"):
        await agent._maybe_notify_peak_throttle_transition()
    assert agent._peak_throttle_active_persisted is True
    notify_mock.assert_awaited_once()
    msg = notify_mock.await_args[0][0].lower()
    assert "no budget limit" in msg
    assert any(
        "no slowdown will apply" in r.message.lower() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_peak_throttle_transition_exit_notifies_once(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Leaving the window emits one exit notify + journal entry."""
    agent = make_agent()
    agent._peak_throttle_active_persisted = True
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=False,
    ):
        await agent._maybe_notify_peak_throttle_transition()
    assert agent._peak_throttle_active_persisted is False
    notify_mock.assert_awaited_once()
    assert "released" in notify_mock.await_args[0][0].lower()
    entries = agent.journal.read_recent_events(n=10)
    exit_ = [e for e in entries if e.triggered_by == "peak_throttle_exit"]
    assert len(exit_) == 1


@pytest.mark.asyncio
async def test_peak_throttle_transition_no_notify_outside_window(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Window inactive + flag already cleared → no notify, no state change."""
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=False,
    ):
        await agent._maybe_notify_peak_throttle_transition()
    notify_mock.assert_not_awaited()
    assert agent._peak_throttle_active_persisted is False


@pytest.mark.asyncio
async def test_peak_throttle_transition_swallows_journal_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Journal failure during entry is logged, not raised — state still set."""
    agent = make_agent()
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("journal down"),
    )
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        await agent._maybe_notify_peak_throttle_transition()
    assert agent._peak_throttle_active_persisted is True


@pytest.mark.asyncio
async def test_peak_throttle_transition_exit_swallows_journal_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Journal failure during exit is logged, not raised — state still cleared."""
    agent = make_agent()
    agent._peak_throttle_active_persisted = True
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("journal down"),
    )
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=False,
    ):
        await agent._maybe_notify_peak_throttle_transition()
    assert agent._peak_throttle_active_persisted is False


@pytest.mark.asyncio
async def test_peak_throttle_transition_persisted_flag_round_trips(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """The persisted flag round-trips through lifecycle.json so a restart inside
    the window does not re-notify entry."""
    agent = make_agent()
    agent._peak_throttle_active_persisted = True
    agent._persist_progress(5, "evolve")
    data = json.loads(agent.config.lifecycle_path.read_text())
    assert data["peak_throttle_active_persisted"] is True

    # On the next startup, _read_lifecycle_progress restores the flag.
    fresh = make_agent()
    (*_, peak_persisted, _rl_idle, _rl, _cc, _ss, _sss, _maint) = (
        fresh._read_lifecycle_progress()
    )
    assert peak_persisted is True


def test_peak_throttle_persisted_flag_non_bool_defaults_to_false(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Corrupt non-bool ``peak_throttle_active_persisted`` defaults to False."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-05-01T00:00:00+00:00",
        "iteration": 1,
        "next_phase": None,
        "peak_throttle_active_persisted": "yes",
    })
    (*_, peak_persisted, _rl_idle, _rl, _cc, _ss, _sss, _maint) = (
        agent._read_lifecycle_progress()
    )
    assert peak_persisted is False


def test_phase_will_run_not_gated_under_peak_throttle_only(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A budget-gated phase keeps running when only the schedule input is on.

    Peak hours slow the loop (longer sleep) rather than gating phases, so a
    heavy phase like ``contemplate`` must still run inside the window when the
    burn-rate side has not breached.
    """
    agent = make_agent()
    # Burn-rate side off; peak side on.
    agent._budget_throttle_entered_at = None
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        will_run = agent._phase_will_run(
            "contemplate",
            iteration=1, smoke=False, first_iteration_after_restart=True,
            task_every=3, evolve_every=5, dream_every=10, audit_self_every=10,
        )
    assert will_run is True


def test_respond_to_messages_runs_under_peak_throttle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``respond_to_messages`` is excluded from the budget-gated set even
    when the peak throttle is on (responsiveness preserved)."""
    agent = make_agent()
    agent._budget_throttle_entered_at = None
    agent._iter_message_context = "ping"
    agent._iter_reply_context = "ping"
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        will_run = agent._phase_will_run(
            "respond_to_messages",
            iteration=1, smoke=False, first_iteration_after_restart=True,
            task_every=3, evolve_every=5, dream_every=10, audit_self_every=10,
        )
    assert will_run is True


def test_execute_approved_publications_runs_under_peak_throttle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``execute_approved_publications`` is not budget-gated; the peak
    throttle does not strand the publication queue."""
    agent = make_agent()
    agent._budget_throttle_entered_at = None
    agent.config.publication_enabled = True
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        will_run = agent._phase_will_run(
            "execute_approved_publications",
            iteration=1, smoke=False, first_iteration_after_restart=True,
            task_every=3, evolve_every=5, dream_every=10, audit_self_every=10,
        )
    assert will_run is True


def test_peak_throttle_below_floor_does_not_stretch_sleep(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Inside the peak window, spend below the floor still leaves both at 1.0.

    The minimum-cost floor suppresses any stretch — peak tightening only
    applies once there is enough spend in the window to act on.
    """
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.peak_throttle_enabled = True
    agent.config.peak_throttle_rate_multiplier = 0.5
    agent._budget_throttle_entered_at = None
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=0.0, total_cost=0.0),
    ), patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        gating, sleep, _snapshot = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    assert gating == 1.0
    assert sleep == 1.0


def test_peak_throttle_tightens_sleep_multiplier(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Inside the window the sleep multiplier exceeds the gating multiplier.

    With the limit halved by ``peak_throttle_rate_multiplier``, a rate at the
    untightened limit leaves the gating multiplier at 1.0 (no phase gating)
    while the sleep multiplier doubles (loop slows).
    """
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 2.0
    agent.config.budget_rate_min_cost_usd = 0.0
    agent.config.budget_throttle_max_multiplier = 60.0
    agent.config.peak_throttle_enabled = True
    agent.config.peak_throttle_rate_multiplier = 0.5
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=2.0, total_cost=1.0),
    ), patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        gating, sleep, _snapshot = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    # rate/limit = 1.0 → gating clamps to 1.0; rate/(limit*0.5) = 2.0 → sleep.
    assert gating == 1.0
    assert sleep == pytest.approx(2.0)


def test_peak_throttle_sleep_respects_max_multiplier_cap(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The peak-tightened sleep multiplier is still capped at the max."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.budget_rate_min_cost_usd = 0.0
    agent.config.budget_throttle_max_multiplier = 10.0
    agent.config.peak_throttle_enabled = True
    agent.config.peak_throttle_rate_multiplier = 0.1
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=50.0, total_cost=10.0),
    ), patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        gating, sleep, _snapshot = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    assert gating == 10.0
    assert sleep == 10.0


def test_peak_throttle_disabled_flag_leaves_multipliers_equal(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``peak_throttle_enabled=False`` keeps gating and sleep multipliers equal
    even when the window predicate would otherwise return True."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 2.0
    agent.config.budget_rate_min_cost_usd = 0.0
    agent.config.peak_throttle_enabled = False
    agent.config.peak_throttle_rate_multiplier = 0.5
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=5.0, total_cost=1.0),
    ), patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        gating, sleep, _snapshot = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    assert gating == pytest.approx(2.5)
    assert sleep == pytest.approx(2.5)


def test_peak_throttle_factor_one_leaves_multipliers_equal(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A ``peak_throttle_rate_multiplier`` of 1.0 is a no-op slowdown."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 2.0
    agent.config.budget_rate_min_cost_usd = 0.0
    agent.config.peak_throttle_enabled = True
    agent.config.peak_throttle_rate_multiplier = 1.0
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=5.0, total_cost=1.0),
    ), patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ):
        gating, sleep, _snapshot = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    assert gating == pytest.approx(2.5)
    assert sleep == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# Smoke mode — fast deploy-validation path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_smoke_runs_contemplate_without_smoke_override(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop(smoke=True) calls contemplate() with the same arguments it
    would in production — no per-call effort override."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999, smoke=True,
    )

    agent.contemplate.assert_called_once()  # type: ignore[attr-defined]
    _, kwargs = agent.contemplate.call_args  # type: ignore[attr-defined]
    assert "smoke" not in kwargs


@pytest.mark.asyncio
async def test_run_loop_smoke_skips_check_merged_plans(make_agent) -> None:  # type: ignore[no-untyped-def]
    """smoke=True bypasses first-iteration check_merged_plans entirely."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999, smoke=True,
    )

    agent.evolution.check_merged_plans.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_smoke_skips_evolve_dream_task_and_rejected(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """smoke=True skips evolve, dream, generate_task, and rejected-plan
    processing even when their modulo gates would otherwise fire."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=("should not run", None))  # type: ignore[method-assign]
    agent.run = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    dream = AsyncMock()
    agent._phase_runners["dream"] = dream

    # Every modulo gate would match on iteration 1 if smoke were off.
    await agent.run_loop(
        interval=0, max_iters=1, task_every=1, evolve_every=1, dream_every=1,
        smoke=True,
    )

    agent.evolve.assert_not_called()  # type: ignore[attr-defined]
    agent.generate_task.assert_not_called()  # type: ignore[attr-defined]
    agent.run.assert_not_called()  # type: ignore[attr-defined]
    agent._process_rejected_plans.assert_not_called()  # type: ignore[attr-defined]
    dream.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_smoke_skips_implement_approved_plans(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """smoke=True must not run _implement_approved_plans — implementation
    work against the throwaway snapshot is wasted effort."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999, smoke=True,
    )

    agent._implement_approved_plans.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_smoke_skips_respond_to_messages(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """smoke=True must not run _respond_to_messages — sending Telegram
    from the throwaway snapshot would duplicate the live container's
    response after the deploy completes."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._process_messages = AsyncMock(return_value="msg from thomas")  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999, smoke=True,
    )

    agent._respond_to_messages.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_contemplate_runs_introspect_and_update_self_model(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """contemplate() runs introspect at medium effort and then calls
    update_self_model. The same code path runs in smoke mode (smoke
    no longer overrides per-call effort)."""
    agent = make_agent()

    captured_options: list[object] = []

    async def fake_query(**kwargs: object) -> MockQueryResult:
        captured_options.append(kwargs.get("options"))
        # Second call is update_self_model — return valid JSON.
        if len(captured_options) == 1:
            return MockQueryResult(response="reflection text")
        return MockQueryResult(response=json.dumps({"values": ["growth"]}))

    with patch("maestro.consciousness.query", side_effect=fake_query):
        result = await agent.contemplate()

    assert result == "reflection text"
    assert len(captured_options) == 2  # introspect + update_self_model
    introspect_options = captured_options[0]
    assert getattr(introspect_options, "effort", None) == "medium"


# --- Fix 2: Loop-level rate-limit recovery ---


@pytest.mark.asyncio
async def test_run_loop_survives_rate_limit_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() retries the rate-limited iteration in place, then continues.

    The first contemplate call is rate-limited. The retry re-enters the
    same iteration and succeeds, then two more iterations run normally —
    4 contemplate calls across 3 successful iterations (retries don't
    count toward ``max_iters``).
    """
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "Rate limited after 6 attempts",
                category=ErrorCategory.RATE_LIMIT,
                retries_exhausted=True,
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=3, task_every=0, evolve_every=999)

    # 1 failed + 1 retry + 2 more normal iterations = 4 total calls
    assert agent.contemplate.call_count == 4  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_rate_limit_cooldown_duration(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() sleeps _RATE_LIMIT_COOLDOWN_SECONDS (300) on rate-limit error."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "Rate limited",
                category=ErrorCategory.RATE_LIMIT,
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    wait_timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        wait_timeouts.append(timeout)
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=60, max_iters=2, task_every=0, evolve_every=999)

    assert 300 in wait_timeouts


@pytest.mark.asyncio
async def test_run_loop_rate_limit_journals_milestone(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() journals a milestone with triggered_by='rate_limit' on rate-limit."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "Rate limited",
                category=ErrorCategory.RATE_LIMIT,
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    journal_calls: list[dict[str, Any]] = []
    original_write = agent.journal.record_event

    def capture_write(**kwargs: Any) -> None:
        journal_calls.append(kwargs)
        original_write(**kwargs)

    agent.journal.record_event = capture_write  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=2, task_every=0, evolve_every=999)

    rate_limit_entries = [
        c for c in journal_calls if c.get("triggered_by") == "rate_limit"
    ]
    assert len(rate_limit_entries) == 1
    assert rate_limit_entries[0]["entry_type"] == "milestone"


@pytest.mark.asyncio
async def test_run_loop_non_recoverable_error_crashes(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() re-raises SDKError categories without a resume-in-place arm."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=SDKError(
            "boom",
            category=ErrorCategory.UNKNOWN,
        ),
    )

    with pytest.raises(SDKError, match="boom"):
        await agent.run_loop(interval=0, max_iters=3, task_every=0, evolve_every=999)


@pytest.mark.asyncio
async def test_run_loop_sdk_error_persists_exit_code(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An unwinding SDKError writes its exit_code into the lifecycle cursor.

    Pins the wiring from the finally block's ``_persist_crash_exit_code``
    through to ``_read_lifecycle_progress``: the next startup must see the
    code so crash reporting can surface it.
    """
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=SDKError(
            "oom killed",
            category=ErrorCategory.UNKNOWN,
            exit_code=137,
        ),
    )

    with patch.object(agent.evolution, "ensure_main_branch"), \
         pytest.raises(SDKError):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    lifecycle_data = json.loads(agent.config.lifecycle_path.read_text())
    assert lifecycle_data["last_exit_code"] == 137
    assert "last_shutdown" not in lifecycle_data
    # Phase 3: structured SDK context rides through the same lifecycle
    # persistence so the next startup can reconstitute an SDKError and
    # feed it into build_crash_event.
    assert lifecycle_data["last_sdk_error"]["category"] == "unknown"
    assert lifecycle_data["last_sdk_error"]["exit_code"] == 137
    assert lifecycle_data["last_sdk_error"]["message"] == "oom killed"


@pytest.mark.asyncio
async def test_run_loop_rate_limit_cooldown_respects_shutdown(make_agent) -> None:  # type: ignore[no-untyped-def]
    """rate-limit cooldown is interruptible via shutdown event."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=SDKError(
            "Rate limited",
            category=ErrorCategory.RATE_LIMIT,
        ),
    )

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        agent._ensure_shutdown_event().set()
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=60, max_iters=5, task_every=0, evolve_every=999)

    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]


# --- Fix 4: Startup rate-limit probe ---


@pytest.mark.asyncio
async def test_startup_probe_succeeds(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_probe_rate_limit() returns normally when SDK responds OK."""
    agent = make_agent()
    agent._probe_rate_limit = MaestroAgent._probe_rate_limit.__get__(agent)  # type: ignore[attr-defined]

    with patch("maestro.agent.query", AsyncMock(return_value=MockQueryResult(response="OK"))):
        await agent._probe_rate_limit()


@pytest.mark.asyncio
async def test_probe_rate_limit_enters_idle_on_rate_limit(make_agent) -> None:  # type: ignore[no-untyped-def]
    """RATE_LIMIT on the startup probe funnels into the extended-idle path."""
    agent = make_agent()
    agent._probe_rate_limit = MaestroAgent._probe_rate_limit.__get__(agent)  # type: ignore[attr-defined]

    call_log: list[tuple[str, int]] = []

    async def fake_enter(iteration: int, phase: str) -> None:
        call_log.append(("enter", agent._consecutive_rate_limits))
        agent._rate_limit_idle_entered_at = "2026-04-23T00:00:00+00:00"

    async def fake_probe() -> None:
        call_log.append(("probe", agent._consecutive_rate_limits))

    async def fake_exit() -> None:
        call_log.append(("exit", agent._consecutive_rate_limits))
        agent._rate_limit_idle_entered_at = None
        agent._consecutive_rate_limits = 0

    agent._enter_rate_limit_idle = fake_enter  # type: ignore[method-assign]
    agent._run_rate_limit_idle_probe = fake_probe  # type: ignore[method-assign]
    agent._exit_rate_limit_idle = fake_exit  # type: ignore[method-assign]

    with patch(
        "maestro.agent.query",
        AsyncMock(
            side_effect=SDKError("rate limited", category=ErrorCategory.RATE_LIMIT),
        ),
    ):
        await agent._probe_rate_limit()

    # Counter is incremented before enter, then cleared by exit.
    assert [name for name, _ in call_log] == ["enter", "probe", "exit"]
    assert call_log[0][1] == 1  # incremented before enter
    assert agent._consecutive_rate_limits == 0  # cleared by exit


@pytest.mark.asyncio
async def test_probe_rate_limit_idle_skips_exit_on_shutdown(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When shutdown fires during the idle probe, exit is not called."""
    agent = make_agent()
    agent._probe_rate_limit = MaestroAgent._probe_rate_limit.__get__(agent)  # type: ignore[attr-defined]

    exit_called = False

    async def fake_enter(iteration: int, phase: str) -> None:
        agent._rate_limit_idle_entered_at = "2026-04-23T00:00:00+00:00"

    async def fake_probe() -> None:
        agent._ensure_shutdown_event().set()

    async def fake_exit() -> None:
        nonlocal exit_called
        exit_called = True

    agent._enter_rate_limit_idle = fake_enter  # type: ignore[method-assign]
    agent._run_rate_limit_idle_probe = fake_probe  # type: ignore[method-assign]
    agent._exit_rate_limit_idle = fake_exit  # type: ignore[method-assign]

    with patch(
        "maestro.agent.query",
        AsyncMock(
            side_effect=SDKError("rate limited", category=ErrorCategory.RATE_LIMIT),
        ),
    ):
        await agent._probe_rate_limit()

    assert exit_called is False


@pytest.mark.asyncio
async def test_startup_probe_non_rate_limit_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_probe_rate_limit() swallows non-rate-limit errors without cooldown."""
    agent = make_agent()
    agent._probe_rate_limit = MaestroAgent._probe_rate_limit.__get__(agent)  # type: ignore[attr-defined]

    with patch(
        "maestro.agent.query",
        AsyncMock(
            side_effect=SDKError("CLI crashed", category=ErrorCategory.CLI_CRASH),
        ),
    ):
        await agent._probe_rate_limit()


@pytest.mark.asyncio
async def test_startup_probe_unexpected_exception(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_probe_rate_limit() swallows unexpected (non-SDKError) exceptions."""
    agent = make_agent()
    agent._probe_rate_limit = MaestroAgent._probe_rate_limit.__get__(agent)  # type: ignore[attr-defined]

    with patch(
        "maestro.agent.query",
        AsyncMock(side_effect=ValueError("totally unexpected")),
    ):
        await agent._probe_rate_limit()


@pytest.mark.asyncio
async def test_run_loop_rate_limit_journal_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() tolerates journal write failure during rate-limit cooldown."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "Rate limited",
                category=ErrorCategory.RATE_LIMIT,
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]
    agent.journal.record_event = MagicMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=2, task_every=0, evolve_every=999)

    # 1 rate-limited + 1 successful retry (same iteration) + 1 second
    # iteration = 3 calls. Retries-in-place don't count toward max_iters.
    assert call_count == 3


# ── Burn-rate standby gate ────────────────────────────────────────────


def _mk_snapshot(
    cost_per_hour: float = 0.0, total_cost: float = 0.0,
):  # type: ignore[no-untyped-def]
    from maestro.cost_tracking import CostRateSnapshot
    return CostRateSnapshot(
        window_start="2026-04-14T00:00:00+00:00",
        window_end="2026-04-14T01:00:00+00:00",
        window_seconds=3600.0,
        total_cost_usd=total_cost,
        total_input_tokens=0,
        total_output_tokens=0,
        total_cache_read_tokens=0,
        total_calls=1,
        cost_per_hour=cost_per_hour,
        cost_per_day=cost_per_hour * 24,
        cost_per_month=cost_per_hour * 24 * 30,
        tokens_in_per_hour=0.0,
        tokens_out_per_hour=0.0,
        by_caller={},
    )


def test_current_burn_rate_reads_cost_samples(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch(
        "maestro.agent.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=2.75, total_cost=1.0),
    ):
        assert agent.current_burn_rate_usd_per_hour() == 2.75


def test_current_burn_rate_returns_zero_on_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch(
        "maestro.agent.build_cost_rates",
        side_effect=RuntimeError("boom"),
    ):
        assert agent.current_burn_rate_usd_per_hour() == 0.0


def test_throttle_inactive_by_default(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert agent.budget_throttle_active is False
    assert agent.budget_throttle_entered_at is None


def test_throttle_multiplier_returns_one_when_disabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``max_loop_budget_usd_per_hour == 0`` disables the throttle."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 0.0
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=1000.0, total_cost=1000.0),
    ):
        gating, sleep, snapshot = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    assert gating == 1.0
    assert sleep == 1.0
    assert snapshot is None


def test_throttle_multiplier_returns_one_in_smoke(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Smoke mode bypasses the throttle entirely."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    with patch("maestro.loop.lifecycle.build_cost_rates") as builder:
        builder.side_effect = AssertionError("smoke must not consult cost rates")
        gating, sleep, snapshot = agent._compute_budget_throttle_multiplier(
            smoke=True,
        )
    assert gating == 1.0
    assert sleep == 1.0
    assert snapshot is None


def test_throttle_multiplier_returns_one_below_min_spend(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Total spend below floor suppresses the throttle, snapshot still returned."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.budget_rate_min_cost_usd = 1.0
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=100.0, total_cost=0.05),
    ):
        gating, sleep, snapshot = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    assert gating == 1.0
    assert sleep == 1.0
    assert snapshot is not None
    assert snapshot.cost_per_hour == 100.0


def test_throttle_multiplier_floor_is_one_when_rate_below_limit(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Rate below limit → multiplier clamped at 1.0 (not less)."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 10.0
    agent.config.budget_rate_min_cost_usd = 0.0
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=2.0, total_cost=1.0),
    ):
        gating, sleep, _ = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    assert gating == 1.0
    assert sleep == 1.0


def test_throttle_multiplier_scales_with_ratio(make_agent) -> None:  # type: ignore[no-untyped-def]
    """rate/limit is the multiplier when above limit and under cap."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 2.0
    agent.config.budget_rate_min_cost_usd = 0.0
    agent.config.budget_throttle_max_multiplier = 60.0
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=5.0, total_cost=1.0),
    ):
        gating, sleep, _ = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    assert gating == pytest.approx(2.5)
    assert sleep == pytest.approx(2.5)


def test_throttle_multiplier_capped_at_max(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Runaway rate saturates at budget_throttle_max_multiplier."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.budget_rate_min_cost_usd = 0.0
    agent.config.budget_throttle_max_multiplier = 60.0
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=1_000.0, total_cost=100.0),
    ):
        gating, sleep, _ = agent._compute_budget_throttle_multiplier(
            smoke=False,
        )
    assert gating == 60.0
    assert sleep == 60.0


def test_throttle_multiplier_propagates_rate_build_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A cost-rate build failure is a defect: it propagates, never neutralises."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    with patch(
        "maestro.loop.lifecycle.build_cost_rates", side_effect=RuntimeError("boom"),
    ), pytest.raises(RuntimeError, match="boom"):
        agent._compute_budget_throttle_multiplier(smoke=False)


def test_active_throttle_survives_rate_build_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An engaged throttle is not released by a measurement fault."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent._budget_throttle_entered_at = "2026-04-14T00:30:00+00:00"
    with patch(
        "maestro.loop.lifecycle.build_cost_rates", side_effect=RuntimeError("boom"),
    ), pytest.raises(RuntimeError, match="boom"):
        agent._compute_budget_throttle_multiplier(smoke=False)
    assert agent._budget_throttle_entered_at == "2026-04-14T00:30:00+00:00"
    assert agent.burn_rate_throttle_active is True


# ---------------------------------------------------------------------------
# Proposal 049 net-burn-aware throttle — spec validation (issue #1689)
#
# These tests codify the invariant stated in proposal 049's Description:
#   "the operator's effective budget is gross + income; ignoring the income
#    side means the loop throttles harder than the budget warrants"
#
# The _proposed_formula helper implements the corrected formula from the
# proposal's Implementation Plan Step 1: income reduces the numerator
# (net cost), not the denominator (budget limit).  The invariant is:
# income present → multiplier DECREASES (throttle relaxes).
# ---------------------------------------------------------------------------


def _proposed_formula(
    cost_per_hour: float,
    limit: float,
    income_rate: float,
    max_multiplier: float = 60.0,
) -> float:
    """Proposal 049 Step 1 formula — net cost vs gross limit.

    ``net_cost = max(0.0, cost_per_hour - income_rate)``
    ``raw = net_cost / limit``
    ``capped = min(max_multiplier, max(1.0, raw))``
    """
    net_cost = max(0.0, cost_per_hour - income_rate)
    raw = net_cost / limit
    return min(max_multiplier, max(1.0, raw))


def test_net_burn_income_reduces_throttle_multiplier() -> None:
    """Income arriving must reduce the throttle multiplier, not increase it.

    Proposal 049 Description (lines 20-24): 'the operator's effective budget
    is gross + income; ignoring the income side means the loop throttles
    harder than the budget warrants.'

    This test uses the worked example from the proposal (lines 122-124):
    cost=$2/hr, limit=$1/hr, income=$0.5/hr.

    Without income: multiplier = 2.0 / 1.0 = 2.0.
    With income: multiplier must be LESS than 2.0 (throttle relaxes).

    Income reduces the net-cost numerator, so ``_proposed_formula`` yields
    ``max(0, 2.0 - 0.5) / 1.0 = 1.5 < 2.0``. (Historical note: the *rejected*
    original proposal reduced the denominator — ``2.0 / (1.0 - 0.5) = 4.0``,
    doubling the throttle — which is the regression this test guards against.)
    """
    cost, limit, income = 2.0, 1.0, 0.5
    without_income = cost / limit  # 2.0

    with_income = _proposed_formula(cost, limit, income)

    # The proposal's own intent: income → less aggressive throttle.
    assert with_income < without_income, (
        f"Income must REDUCE the throttle multiplier: got {with_income} "
        f">= {without_income} (without income). Income should shrink the "
        f"net-cost numerator; the rejected denominator formula "
        f"(max(0, limit - income)) would instead make the throttle harsher."
    )


def test_net_burn_surplus_income_yields_no_throttle() -> None:
    """When income fully covers the budget, the throttle must be a no-op.

    Proposal 049 Step 2 (lines 86-88): 'When income_rate >= limit,
    effective_limit is 0.0; the throttle becomes a no-op (multiplier = 1.0)
    because income alone covers the entire budget.'

    Surplus income drives the net cost to zero, so ``_proposed_formula``
    yields ``max(0, 1.0 - 2.0) / 1.0 = 0.0``, clamped up to the no-op
    multiplier of 1.0. (Historical note: the *rejected* original proposal
    routed effective_limit=0 through to budget_throttle_max_multiplier
    (default 60.0) — maximum throttle — which is what this test guards against.)
    """
    cost, limit, income = 1.0, 1.0, 2.0

    multiplier = _proposed_formula(cost, limit, income, max_multiplier=60.0)

    assert multiplier == pytest.approx(1.0), (
        f"Surplus income (income >= limit) should yield multiplier=1.0 "
        f"(no throttle), but proposed formula yields {multiplier}. "
        f"The effective_limit=0 branch falls through to max_multiplier."
    )


def test_net_burn_correct_formula_reduces_multiplier() -> None:
    """Validate the exact multiplier value with fresh income.

    net_cost = max(0, 2.0 - 0.5) = 1.5; raw = 1.5 / 1.0 = 1.5
    """
    cost, limit, income = 2.0, 1.0, 0.5
    without_income = cost / limit  # 2.0

    with_income = _proposed_formula(cost, limit, income)

    assert with_income < without_income, (
        f"Formula should reduce multiplier from {without_income} "
        f"to {with_income}."
    )
    assert with_income == pytest.approx(1.5)


def test_net_burn_correct_formula_surplus_yields_one() -> None:
    """Surplus income (income >= cost) yields multiplier = 1.0.

    net_cost = max(0, 1.0 - 2.0) = 0.0; raw = 0.0 / 1.0 = 0.0;
    clamped to 1.0.
    """
    cost, limit, income = 1.0, 1.0, 2.0

    multiplier = _proposed_formula(cost, limit, income, max_multiplier=60.0)

    assert multiplier == pytest.approx(1.0)


def test_net_burn_no_income_unchanged() -> None:
    """With zero income the formula is identical to the gross-only path."""
    cost, limit, income = 2.0, 1.0, 0.0
    expected = cost / limit  # 2.0

    result = _proposed_formula(cost, limit, income)

    assert result == pytest.approx(expected)


@pytest.mark.asyncio
async def test_throttle_transition_entry_notifies_once(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Crossing from base pace into throttled mode emits one notify + journal."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    snapshot = _mk_snapshot(cost_per_hour=3.0, total_cost=1.0)
    await agent._maybe_notify_throttle_transition(multiplier=3.0, snapshot=snapshot)
    assert agent.budget_throttle_active is True
    assert agent.budget_throttle_entered_at is not None
    notify_mock.assert_awaited_once()
    assert "throttle" in notify_mock.await_args[0][0].lower()
    entries = agent.journal.read_recent_events(n=10)
    enter = [e for e in entries if e.triggered_by == "budget_throttle_enter"]
    assert len(enter) == 1


@pytest.mark.asyncio
async def test_throttle_transition_sustained_does_not_renotify(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Already throttled + still throttled → no re-notify, entered_at preserved."""
    agent = make_agent()
    agent._budget_throttle_entered_at = "2026-04-14T00:00:00+00:00"
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    snapshot = _mk_snapshot(cost_per_hour=10.0, total_cost=10.0)
    await agent._maybe_notify_throttle_transition(multiplier=10.0, snapshot=snapshot)
    notify_mock.assert_not_awaited()
    assert agent.budget_throttle_entered_at == "2026-04-14T00:00:00+00:00"


@pytest.mark.asyncio
async def test_throttle_transition_exit_notifies_once(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Crossing from throttled back to base pace emits one exit notify + journal."""
    agent = make_agent()
    agent._budget_throttle_entered_at = "2026-04-14T00:00:00+00:00"
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    await agent._maybe_notify_throttle_transition(multiplier=1.0, snapshot=None)
    assert agent.budget_throttle_active is False
    assert agent.budget_throttle_entered_at is None
    notify_mock.assert_awaited_once()
    assert "released" in notify_mock.await_args[0][0].lower()
    entries = agent.journal.read_recent_events(n=10)
    exit_ = [e for e in entries if e.triggered_by == "budget_throttle_exit"]
    assert len(exit_) == 1


@pytest.mark.asyncio
async def test_throttle_transition_stable_base_pace_is_noop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Not throttled + multiplier stays at 1.0 → no notify, state unchanged."""
    agent = make_agent()
    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]
    await agent._maybe_notify_throttle_transition(multiplier=1.0, snapshot=None)
    notify_mock.assert_not_awaited()
    assert agent.budget_throttle_active is False


@pytest.mark.asyncio
async def test_throttle_transition_propagates_notify_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A real Telegram exception during throttle entry propagates.

    State mutation happens before the notify call, so the throttle is
    still recorded as active even when the notification raises.
    Best-effort notification suppresses only ``CancelledError``; a real
    bug must surface for RCA.
    """
    agent = make_agent()
    agent.notifier = AsyncMock()
    agent.notifier.notify.side_effect = RuntimeError("telegram down")
    snapshot = _mk_snapshot(cost_per_hour=10.0, total_cost=10.0)
    with pytest.raises(RuntimeError, match="telegram down"):
        await agent._maybe_notify_throttle_transition(
            multiplier=10.0, snapshot=snapshot,
        )
    assert agent.budget_throttle_active is True


@pytest.mark.asyncio
async def test_budget_throttle_entry_journal_failure_propagates(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Journal failure during throttle entry propagates.

    Fail-loud: a journal write failure is a real defect — the loop's
    iteration handler classifies it. State mutation happens before the
    journal call, so the throttle is still recorded as active.
    """
    agent = make_agent()
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("journal down"),
    )
    snapshot = _mk_snapshot(cost_per_hour=10.0, total_cost=10.0)
    with pytest.raises(RuntimeError, match="journal down"):
        await agent._maybe_notify_throttle_transition(
            multiplier=10.0, snapshot=snapshot,
        )
    assert agent.budget_throttle_active is True


@pytest.mark.asyncio
async def test_throttle_transition_exit_propagates_notify_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A real Telegram exception during throttle exit propagates.

    State clearing happens before the notify call, so the throttle is
    correctly cleared even though the notification raises.
    """
    agent = make_agent()
    agent._budget_throttle_entered_at = "2026-04-14T00:00:00+00:00"
    agent.notifier = AsyncMock()
    agent.notifier.notify.side_effect = RuntimeError("telegram down")
    with pytest.raises(RuntimeError, match="telegram down"):
        await agent._maybe_notify_throttle_transition(
            multiplier=1.0, snapshot=None,
        )
    assert agent.budget_throttle_active is False
    assert agent.budget_throttle_entered_at is None


@pytest.mark.asyncio
async def test_budget_throttle_exit_journal_failure_propagates(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Journal failure during throttle exit propagates.

    Fail-loud: a journal write failure is a real defect — the loop's
    iteration handler classifies it. State clearing happens before the
    journal call, so the throttle is still recorded as cleared.
    """
    agent = make_agent()
    agent._budget_throttle_entered_at = "2026-04-14T00:00:00+00:00"
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("journal down"),
    )
    with pytest.raises(RuntimeError, match="journal down"):
        await agent._maybe_notify_throttle_transition(
            multiplier=1.0, snapshot=None,
        )
    assert agent.budget_throttle_active is False
    assert agent.budget_throttle_entered_at is None


def test_lifecycle_persists_throttle_state(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._budget_throttle_entered_at = "2026-04-14T01:23:45+00:00"
    agent._persist_progress(5, "evolve")
    data = json.loads(agent.config.lifecycle_path.read_text())
    assert data["budget_throttle_entered_at"] == "2026-04-14T01:23:45+00:00"
    assert "budget_standby_entered_at" not in data
    assert "budget_standby_anchor_message_id" not in data


def test_lifecycle_shutdown_persists_throttle_state(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._budget_throttle_entered_at = "2026-04-14T01:23:45+00:00"
    agent._write_lifecycle_shutdown()
    data = json.loads(agent.config.lifecycle_path.read_text())
    assert data["budget_throttle_entered_at"] == "2026-04-14T01:23:45+00:00"
    assert "budget_standby_entered_at" not in data


def test_lifecycle_rehydrates_throttle_state(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 3,
        "next_phase": "contemplate",
        "budget_throttle_entered_at": "2026-04-14T01:00:00+00:00",
    })
    (*_, throttle_entered, peak_persisted, rate_limit_idle,
     _rl, _cc, _ss, _sss, _maint) = (
        agent._read_lifecycle_progress()
    )
    assert throttle_entered == "2026-04-14T01:00:00+00:00"
    assert peak_persisted is False
    assert rate_limit_idle is None


def test_lifecycle_backward_compat_no_throttle_fields(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 3,
        "next_phase": None,
    })
    (*_, throttle_entered, peak_persisted, rate_limit_idle,
     _rl, _cc, _ss, _sss, _maint) = (
        agent._read_lifecycle_progress()
    )
    assert throttle_entered is None
    assert peak_persisted is False
    assert rate_limit_idle is None


def test_lifecycle_ignores_legacy_standby_fields(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Legacy ``budget_standby_*`` keys left over from a prior deploy don't crash startup."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 2,
        "next_phase": None,
        "budget_standby_entered_at": "2026-04-14T00:10:00+00:00",
        "budget_standby_anchor_message_id": "anchor",
    })
    (*_, throttle_entered, peak_persisted, rate_limit_idle,
     _rl, _cc, _ss, _sss, _maint) = (
        agent._read_lifecycle_progress()
    )
    assert throttle_entered is None
    assert peak_persisted is False
    assert rate_limit_idle is None


def test_lifecycle_corrupt_throttle_fields(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Non-string throttle fields in lifecycle file default to None."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 2,
        "next_phase": None,
        "budget_throttle_entered_at": 123,
        "rate_limit_idle_entered_at": 456,
    })
    (*_, throttle_entered, peak_persisted, rate_limit_idle,
     _rl, _cc, _ss, _sss, _maint) = (
        agent._read_lifecycle_progress()
    )
    assert throttle_entered is None
    assert peak_persisted is False
    assert rate_limit_idle is None


@pytest.mark.asyncio
async def test_run_loop_gates_heavy_llm_phases_when_throttle_active(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """During a burn-rate throttle, the heavy LLM phases (including
    ``contemplate``) are silently skipped while zero-cost phases still run."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.budget_rate_min_cost_usd = 0.0
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 0,
        "next_phase": None,
        "budget_throttle_entered_at": "2026-04-14T00:10:00+00:00",
    })

    executed: list[str] = []

    def mk_async(name: str):  # type: ignore[no-untyped-def]
        async def _impl(*_a: object, **_kw: object) -> str:
            executed.append(name)
            return ""
        return _impl

    agent.evolution.ensure_main_branch = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *a, **kw: executed.append("ensure_main_branch"),
    )
    agent.contributions.check_for_contributions = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *a, **kw: executed.append("check_contributions"),
    )
    agent.contemplate = mk_async("contemplate")  # type: ignore[method-assign]
    agent.evolve = mk_async("evolve")  # type: ignore[method-assign]
    agent._implement_approved_plans = mk_async(  # type: ignore[method-assign]
        "implement_approved_plans",
    )

    # Keep burn rate high — throttle stays active and gated phases skip.
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=100.0, total_cost=100.0),
    ):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=1,
        )

    # Zero-cost phases still run.
    assert "ensure_main_branch" in executed
    assert "check_contributions" in executed
    # Gated LLM phases — including contemplate — are silently skipped.
    assert "contemplate" not in executed
    assert "evolve" not in executed
    assert "implement_approved_plans" not in executed


@pytest.mark.asyncio
async def test_run_loop_gates_contemplate_when_throttle_active(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """``contemplate`` is silently skipped under an active burn-rate throttle."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.budget_rate_min_cost_usd = 0.0
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 0,
        "next_phase": None,
        "budget_throttle_entered_at": "2026-04-14T00:10:00+00:00",
    })

    executed: list[str] = []

    async def _contemplate(*_a: object, **_kw: object) -> str:
        executed.append("contemplate")
        return ""

    agent.evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = _contemplate  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]

    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=100.0, total_cost=100.0),
    ):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=1,
        )

    assert agent.budget_throttle_active is True
    assert "contemplate" not in executed


@pytest.mark.asyncio
async def test_run_loop_releases_throttle_when_rate_falls(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """When rate drops below limit between iterations, throttle releases on its own."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 10.0
    agent.config.budget_rate_min_cost_usd = 0.0
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:15:00+00:00",
        "iteration": 0,
        "next_phase": None,
        "budget_throttle_entered_at": "2026-04-14T00:10:00+00:00",
    })

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]

    # Rate has fallen below limit since entry.
    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=1.0, total_cost=1.0),
    ):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert agent.budget_throttle_active is False


@pytest.mark.asyncio
async def test_smoke_mode_bypasses_throttle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Under --smoke the throttle never engages even with rate well above limit."""
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 0.01
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]

    def fail_build(*_a: object, **_kw: object) -> None:
        raise AssertionError("smoke must not consult cost rates")

    with patch("maestro.loop.lifecycle.build_cost_rates", side_effect=fail_build):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
            smoke=True,
        )

    assert agent.budget_throttle_active is False


# ---------------------------------------------------------------------------
# Iteration-counter freeze during budget throttle
# ---------------------------------------------------------------------------


def _persisted_iteration(mem_fs: dict[str, str], path: Path) -> int:
    """Read the ``iteration`` field out of the persisted lifecycle.json."""
    return int(json.loads(mem_fs[str(path)])["iteration"])


def _stub_run_loop_phases(agent: MaestroAgent) -> None:
    """Neutralize phase bodies so freeze tests focus on the iteration cursor."""
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    stub_evolve_phase(agent, AsyncMock())
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.tend_evolution_prs = AsyncMock(  # type: ignore[method-assign]
        return_value=TendPRsResult(),
    )
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(merged=[], rejected=[]),
    )


@pytest.mark.asyncio
async def test_run_loop_freezes_iteration_during_burn_rate_throttle(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """During a burn-rate throttle the iteration cursor stays put across bodies.

    Today the body still runs (so always-on phases stay live) but the
    `iteration` value persisted to lifecycle.json is the same on body 1 and
    body 2 — phase cadence is preserved across the throttle window instead
    of being stretched by its duration.
    """
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.budget_rate_min_cost_usd = 0.0
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 7,
        "next_phase": None,
        "budget_throttle_entered_at": "2026-04-14T00:10:00+00:00",
    })
    _stub_run_loop_phases(agent)

    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=100.0, total_cost=100.0),
    ):
        await agent.run_loop(
            interval=0, max_iters=2, task_every=0, evolve_every=999,
        )

    # Throttle held throughout; iteration never bumped past the persisted 7.
    assert agent.budget_throttle_active is True
    assert _persisted_iteration(mem_fs, agent.config.lifecycle_path) == 7


@pytest.mark.asyncio
async def test_run_loop_mid_body_maintenance_release_does_not_refire_gated_cadence(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A mid-body ``/maintenance off`` takes effect on the next body (#4150).

    The body starts frozen under maintenance; an always-on phase early in the
    body releases the flag (as the concurrent Telegram poll task would). The
    budget-gated cadence phases must stay skipped for the rest of that frozen
    body — they have no dedup, so re-firing would re-run an already-fired
    ``iteration % N == 0`` tick. The next body bumps iteration exactly once
    and fires them there.
    """
    agent = make_agent()
    mem_fs[str(agent.config.maintenance_state_path)] = json.dumps(
        {"active": True, "since": None, "set_by": "telegram"},
    )
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 10,
        "next_phase": None,
    })
    _stub_run_loop_phases(agent)
    gated = ("generate_task",)
    for name in gated:
        agent._phase_runners[name] = AsyncMock()
    calls_at_release: dict[str, int] = {}

    def _release() -> None:
        if agent._maintenance_mode_active:
            agent._maintenance_mode_active = False
            calls_at_release["evolve"] = agent._evolve_with_fuel.call_count  # type: ignore[attr-defined]
            for name in gated:
                calls_at_release[name] = agent._phase_runners[name].call_count  # type: ignore[attr-defined]

    agent.evolution.ensure_main_branch = MagicMock(  # type: ignore[method-assign]
        side_effect=_release,
    )

    with patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=0.0, total_cost=0.0),
    ):
        await agent.run_loop(
            interval=0, max_iters=2, task_every=1, evolve_every=1,
            dream_every=1,
        )

    # Body 1 (frozen at 10): released mid-body, yet no gated cadence phase
    # ran. Body 2 bumps to 11 and fires each exactly once.
    assert calls_at_release == {
        "evolve": 0, "generate_task": 0,
    }
    assert agent._evolve_with_fuel.call_count == 1  # type: ignore[attr-defined]
    for name in gated:
        assert agent._phase_runners[name].call_count == 1  # type: ignore[attr-defined]
    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]
    assert _persisted_iteration(mem_fs, agent.config.lifecycle_path) == 11


@pytest.mark.asyncio
async def test_run_loop_does_not_freeze_iteration_during_peak_throttle(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """Peak hours slow the loop but do NOT freeze iteration — phases run.

    The schedule window no longer gates phases, so the iteration counter
    advances normally; only a genuine burn-rate breach (in the union) freezes
    cadence. Here burn rate stays 0, so both bodies bump the counter.
    """
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 4,
        "next_phase": None,
    })
    _stub_run_loop_phases(agent)

    # Burn rate stays 0; only the peak-window predicate fires, exercising
    # the schedule-side input independently of the burn-rate side.
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=True,
    ), patch(
        "maestro.loop.lifecycle.build_cost_rates",
        return_value=_mk_snapshot(cost_per_hour=0.0, total_cost=0.0),
    ):
        await agent.run_loop(
            interval=0, max_iters=2, task_every=0, evolve_every=999,
        )
        assert agent.budget_throttle_active is False

    # Iteration advances across both bodies (4 → 5 → 6) — not frozen.
    assert _persisted_iteration(mem_fs, agent.config.lifecycle_path) == 6


@pytest.mark.asyncio
async def test_run_loop_resumes_iteration_increment_after_throttle_clears(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """When the throttle clears, the next body bumps iteration exactly once.

    Three bodies are driven: throttled, throttled, clear. Iteration starts
    at the persisted value, stays frozen for two bodies, then bumps by one
    on the body that runs without the throttle gate.
    """
    agent = make_agent()
    agent.config.max_loop_budget_usd_per_hour = 10.0
    agent.config.budget_rate_min_cost_usd = 0.0
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "last_shutdown": "2026-04-14T00:00:00+00:00",
        "iteration": 12,
        "next_phase": None,
        "budget_throttle_entered_at": "2026-04-14T00:10:00+00:00",
    })
    _stub_run_loop_phases(agent)

    # Exactly five reads are consumed, in order: the iteration-12 status
    # report and body 1's end-of-iteration throttle check (both high, so the
    # throttle stays engaged), body 2's check (below limit — releases), the
    # iteration-13 status report, and body 3's check. The sequence stays
    # bounded so an unexpected *extra* read raises StopIteration instead of
    # being absorbed by a repeating tail, and the exhaustion assertion below
    # catches a *missing* read.
    rates = iter([
        _mk_snapshot(cost_per_hour=100.0, total_cost=100.0),
        _mk_snapshot(cost_per_hour=100.0, total_cost=100.0),
        _mk_snapshot(cost_per_hour=0.5, total_cost=0.5),
        _mk_snapshot(cost_per_hour=0.5, total_cost=0.5),
        _mk_snapshot(cost_per_hour=0.5, total_cost=0.5),
    ])

    def _next_rate(*_a: object, **_kw: object):  # type: ignore[no-untyped-def]
        return next(rates)

    with patch("maestro.loop.lifecycle.build_cost_rates", side_effect=_next_rate):
        await agent.run_loop(
            interval=0, max_iters=3, task_every=0, evolve_every=999,
        )

    assert next(rates, None) is None

    # Frozen for body 1 + body 2 (still 12), bumped once on body 3 (→ 13).
    assert agent.budget_throttle_active is False
    assert _persisted_iteration(mem_fs, agent.config.lifecycle_path) == 13


# ---------------------------------------------------------------------------
# _phase_will_run dedup for non-budget-gated cadence phases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase",
    ["review_artifacts", "tend_evolution_prs", "audit_self_model"],
)
def test_phase_will_run_dedups_non_gated_cadence_under_freeze(  # type: ignore[no-untyped-def]
    make_agent, phase,
) -> None:
    """A frozen ``iteration % N == 0`` value cannot re-fire a non-gated cadence
    phase: once recorded in ``_last_phase_iteration``, the next call at the
    same iteration value short-circuits to False.
    """
    agent: MaestroAgent = make_agent()
    agent.config.artifact_review_enabled = True
    agent.config.tend_prs_enabled = True
    agent.config.artifact_review_every = 5
    agent.config.tend_prs_every = 5
    agent._last_phase_iteration[phase] = 10
    # ``first_iteration_after_restart=False`` so the bypass that this phase
    # honors (review_artifacts/tend_evolution_prs) does not mask the dedup.
    assert agent._phase_will_run(
        phase,
        iteration=10, smoke=False, first_iteration_after_restart=False,
        task_every=1, evolve_every=1, dream_every=1,
        audit_self_every=5,
    ) is False


@pytest.mark.parametrize(
    "phase",
    ["review_artifacts", "tend_evolution_prs", "audit_self_model"],
)
def test_phase_will_run_allows_non_gated_cadence_on_first_visit(  # type: ignore[no-untyped-def]
    make_agent, phase,
) -> None:
    """Empty ``_last_phase_iteration`` lets a cadence-aligned phase fire.

    Proves the dedup short-circuit is keyed on a recorded prior fire, not a
    blanket off-switch.
    """
    agent: MaestroAgent = make_agent()
    agent.config.artifact_review_enabled = True
    agent.config.tend_prs_enabled = True
    agent.config.artifact_review_every = 5
    agent.config.tend_prs_every = 5
    assert agent._last_phase_iteration == {}
    assert agent._phase_will_run(
        phase,
        iteration=10, smoke=False, first_iteration_after_restart=False,
        task_every=1, evolve_every=1, dream_every=1,
        audit_self_every=5,
    ) is True


# ---------------------------------------------------------------------------
# _LOOP_PHASES ordering: tend_evolution_prs is top priority
# ---------------------------------------------------------------------------


def test_tend_evolution_prs_runs_before_contemplate() -> None:
    """Tending is top priority: it must appear in the loop ahead of the
    LLM-heavy phases so PR-conflict resolution is not delayed by them or
    by a mid-iteration crash."""
    from maestro.agent import _LOOP_PHASES

    tend_index = _LOOP_PHASES.index("tend_evolution_prs")
    contemplate_index = _LOOP_PHASES.index("contemplate")
    assert tend_index < contemplate_index
    # And tending follows ``process_messages`` so a fresh operator command
    # is read first, then PRs are tended before any heavier phase runs.
    process_messages_index = _LOOP_PHASES.index("process_messages")
    assert tend_index == process_messages_index + 1


def test_tend_prs_every_default_is_one() -> None:
    """Default cadence is every iteration. Locked in by a test so a future
    bump is intentional, not accidental — Thomas requires PR tending to
    run on every iteration."""
    from maestro.config import Config

    config = Config()
    assert config.tend_prs_every == 1


def test_contemplate_every_default_is_one() -> None:
    """Default contemplate cadence is every iteration (proposal 113). Locked in
    by a test so the priced-introspection knob ships behavior-preserving — a
    production bump to a higher cadence is a later, intentional config flip."""
    from maestro.config import Config

    config = Config()
    assert config.loop_contemplate_every == 1


# ---------------------------------------------------------------------------
# Session reuse in run()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_session_reuse_enabled(make_agent):  # type: ignore[no-untyped-def]
    """When enable_session_reuse=True and >=2 sequential tiers, session is used."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    plan_response = json.dumps([{"description": "S1"}, {"description": "S2"}])

    from maestro.executor import StepResult

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.planner.query_on_session", new_callable=AsyncMock) as mock_plan_sess, \
         patch.object(agent.executor, "start_session", new_callable=AsyncMock) as mock_start, \
         patch.object(agent.executor, "end_session", new_callable=AsyncMock) as mock_end, \
         patch.object(agent.executor, "execute", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.lifecycle.create_session", new_callable=AsyncMock), \
         patch("maestro.agent.query_on_session", new_callable=AsyncMock), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_plan_sess.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = StepResult(success=True, output="done")

        record = await agent.run("Session goal")

    assert record.outcome == "success"
    mock_start.assert_awaited_once()
    # start_session receives workspace and identity_context (no step)
    start_args = mock_start.call_args
    assert isinstance(start_args[0][0], str)  # workspace
    assert isinstance(start_args[0][1], str)  # identity_context
    # end_session called in the finally block
    assert mock_end.await_count >= 1
    # execute was called with use_session=True
    for call in mock_exec.call_args_list:
        assert call.kwargs.get("use_session") is True


@pytest.mark.asyncio
async def test_run_session_reuse_disabled_stateless_fallback(make_agent):  # type: ignore[no-untyped-def]
    """When session reuse is explicitly disabled, start_session is never called."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    plan_response = json.dumps([{"description": "S1"}, {"description": "S2"}])

    from maestro.executor import StepResult

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch.object(agent.executor, "start_session", new_callable=AsyncMock) as mock_start, \
         patch.object(agent.executor, "end_session", new_callable=AsyncMock), \
         patch.object(agent.executor, "execute", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = StepResult(success=True, output="done")

        record = await agent.run("Goal")

    assert record.outcome == "success"
    mock_start.assert_not_awaited()
    for call in mock_exec.call_args_list:
        assert call.kwargs.get("use_session") is False


@pytest.mark.asyncio
async def test_run_session_reuse_min_tiers(make_agent):  # type: ignore[no-untyped-def]
    """Single-step plans don't trigger session reuse (not worth the overhead)."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    plan_response = json.dumps([{"description": "Only one step"}])

    from maestro.executor import StepResult

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.planner.query_on_session", new_callable=AsyncMock) as mock_plan_sess, \
         patch.object(agent.executor, "start_session", new_callable=AsyncMock) as mock_start, \
         patch.object(agent.executor, "end_session", new_callable=AsyncMock), \
         patch.object(agent.executor, "execute", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.lifecycle.create_session", new_callable=AsyncMock), \
         patch("maestro.agent.query_on_session", new_callable=AsyncMock), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_plan_sess.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = StepResult(success=True, output="done")

        await agent.run("Goal")

    mock_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_session_reuse_replan_ends_session(make_agent):  # type: ignore[no-untyped-def]
    """Replan triggers session teardown to get a fresh context."""
    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.loop_max_replans = 1

    plan_response = json.dumps([{"description": "S1"}, {"description": "S2"}])

    from maestro.executor import StepResult

    exec_calls = 0

    async def exec_side_effect(*args: object, **kwargs: object) -> StepResult:
        nonlocal exec_calls
        exec_calls += 1
        if exec_calls == 1:
            return StepResult(success=False, output="", error="failed")
        return StepResult(success=True, output="done")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.planner.query_on_session", new_callable=AsyncMock) as mock_plan_sess, \
         patch.object(agent.executor, "start_session", new_callable=AsyncMock), \
         patch.object(agent.executor, "end_session", new_callable=AsyncMock) as mock_end, \
         patch.object(agent.executor, "execute", side_effect=exec_side_effect), \
         patch("maestro.loop.lifecycle.create_session", new_callable=AsyncMock), \
         patch("maestro.agent.query_on_session", new_callable=AsyncMock), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_plan_sess.return_value = MockQueryResult(response=plan_response)
        agent.planner.replan = AsyncMock()  # type: ignore[method-assign]
        from maestro.memory import PlanStep
        agent.planner.replan.return_value = [
            PlanStep(step_id="new-1", description="S3"),
        ]

        await agent.run("Replan goal")

    # end_session called at least twice: once on failure (before replan),
    # and once in the finally block
    assert mock_end.await_count >= 2


@pytest.mark.asyncio
async def test_run_session_reuse_recomputed_after_replan_collapse(make_agent):  # type: ignore[no-untyped-def]
    """A replan that collapses to <2 single-step tiers drops session reuse.

    The initial two-step plan qualifies for reuse (two single-step tiers), so
    a session opens for the first step. When that step fails and the replan
    yields a single step, the replanned plan no longer warrants a session:
    ``use_session`` must be recomputed so the executor does not re-open a
    session it can never reuse (#1213).
    """
    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.loop_max_replans = 1

    # Two dependency-free steps → two single-step tiers → use_session True.
    plan_response = json.dumps([{"description": "S1"}, {"description": "S2"}])

    from maestro.executor import StepResult

    exec_calls = 0

    async def exec_side_effect(*args: object, **kwargs: object) -> StepResult:
        nonlocal exec_calls
        exec_calls += 1
        if exec_calls == 1:
            return StepResult(success=False, output="", error="failed")
        return StepResult(success=True, output="done")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.planner.query_on_session", new_callable=AsyncMock) as mock_plan_sess, \
         patch.object(agent.executor, "start_session", new_callable=AsyncMock) as mock_start, \
         patch.object(agent.executor, "end_session", new_callable=AsyncMock), \
         patch.object(agent.executor, "execute", side_effect=exec_side_effect) as mock_exec, \
         patch("maestro.loop.lifecycle.create_session", new_callable=AsyncMock), \
         patch("maestro.agent.query_on_session", new_callable=AsyncMock), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_plan_sess.return_value = MockQueryResult(response=plan_response)
        agent.planner.replan = AsyncMock()  # type: ignore[method-assign]
        from maestro.memory import PlanStep
        # Replan collapses to a single step → one single-step tier.
        agent.planner.replan.return_value = [
            PlanStep(step_id="new-1", description="S3"),
        ]

        await agent.run("Replan collapse goal")

    # Session opened once for the original two-tier plan and NOT re-opened for
    # the single-tier replanned plan.
    assert mock_start.await_count == 1
    # The replanned step executed statelessly: use_session recomputed to False.
    assert mock_exec.call_args_list[1].kwargs.get("use_session") is False


@pytest.mark.asyncio
async def test_run_session_reuse_concurrent_tier_boundary(make_agent):  # type: ignore[no-untyped-def]
    """Session ends before a concurrent tier."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    # Create steps with dependencies so we get:
    # tier 1: [s1] (sequential), tier 2: [s2, s3] (concurrent), tier 3: [s4] (sequential)
    plan_response = json.dumps([
        {"id": "a", "description": "S1"},
        {"id": "b", "description": "S2", "depends_on": ["a"]},
        {"id": "c", "description": "S3", "depends_on": ["a"]},
        {"id": "d", "description": "S4", "depends_on": ["b", "c"]},
    ])

    from maestro.executor import StepResult

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.planner.query_on_session", new_callable=AsyncMock) as mock_plan_sess, \
         patch.object(agent.executor, "start_session", new_callable=AsyncMock) as mock_start, \
         patch.object(agent.executor, "end_session", new_callable=AsyncMock) as mock_end, \
         patch.object(agent.executor, "execute", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.lifecycle.create_session", new_callable=AsyncMock), \
         patch("maestro.agent.query_on_session", new_callable=AsyncMock), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_plan_sess.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = StepResult(success=True, output="done")

        record = await agent.run("Mixed tiers goal")

    assert record.outcome == "success"
    # Session started for first sequential tier and restarted after concurrent tier
    assert mock_start.await_count >= 1
    # Session ended before concurrent tier, and in the finally block
    assert mock_end.await_count >= 2


def _run_step_nested_in_loop(tree: ast.AST) -> bool:
    """True when an ``_run_step`` definition sits inside any loop.

    Walks every ``while`` / ``for`` / ``async for`` in the tree and asks
    whether it transitively contains an ``_run_step`` function definition.
    The regression class is *per-iteration re-declaration*, not the ``while``
    keyword specifically — so a group loop refactored into a ``for`` must trip
    this too. Shared by the source assertion and the synthetic fixtures below
    so both exercise the same predicate.
    """
    return any(
        isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
        and inner.name == "_run_step"
        for loop in ast.walk(tree)
        if isinstance(loop, ast.While | ast.For | ast.AsyncFor)
        for inner in ast.walk(loop)
    )


def test_run_step_closure_declared_once_outside_group_loop() -> None:
    """``_run_step`` is declared once in ``execute_step_groups()``, never inside
    the group loop.

    Regression guard for #1174: the concurrent-tier coroutine wrapper used to
    live inside the ``else`` branch of ``while group_idx < len(groups):``, so
    Python rebuilt the function object on every concurrent group. Nothing it
    captures varies per iteration (``identity_context``, ``allowed_branches``,
    and ``artifacts_path`` are all bound before the loop opens), and the two
    values that do vary are explicit parameters — so the declaration belongs
    beside ``_checkpoint_progress`` in the ``execute_step_groups()`` body
    (proposal 106 lifted the group loop out of ``run()``, first into a
    ``MaestroAgent`` method and then into the module-level
    ``maestro/loop/step_execution.py`` sibling this guard now reads). Pure
    ``ast`` over the source text — no mem_fs, subprocess, network, or sleep.
    """
    source = (
        Path(__file__).resolve().parent.parent
        / "maestro" / "loop" / "step_execution.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Scope to the module-level ``execute_step_groups`` rather than asserting
    # module-wide uniqueness of the name: an unrelated nested
    # ``async def execute_step_groups`` added elsewhere in the module must not
    # fail a #1174 test with a misleading message.
    stage_defs = [
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "execute_step_groups"
    ]
    assert len(stage_defs) == 1, (
        "expected exactly one module-level `async def execute_step_groups` in "
        f"step_execution.py, found {len(stage_defs)}"
    )
    stage_def = stage_defs[0]

    # (a) The closure exists exactly once inside ``execute_step_groups()``.
    declarations = [
        node.lineno for node in ast.walk(stage_def)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_step"
    ]
    assert len(declarations) == 1, (
        "expected exactly one `_run_step` declaration inside "
        f"execute_step_groups(), found {len(declarations)} at line(s) "
        f"{sorted(declarations)}"
    )

    # (b) It is not nested inside any loop in ``execute_step_groups()``.
    assert not _run_step_nested_in_loop(stage_def), (
        f"`_run_step` (line {declarations[0]}) is declared inside a loop in "
        "execute_step_groups(); hoist it to the function body beside "
        "_checkpoint_progress so the function object is built once, not once "
        "per concurrent group"
    )


@pytest.mark.parametrize(
    "header",
    [
        "    while True:\n",
        "    for group in groups:\n",
        "    async for group in groups:\n",
    ],
    ids=["while", "for", "async-for"],
)
def test_run_step_nesting_predicate_catches_a_nested_closure(header: str) -> None:
    """The nesting predicate has teeth — it flags a closure inside any loop.

    Without this, ``test_run_step_closure_declared_once_outside_group_loop``
    could pass vacuously if the walk ever stopped finding the node (a rename,
    a change from ``async def`` to ``def``, a restructured ``run()``). The
    synthetic sources below pin the predicate's behaviour independently of
    ``agent.py``'s current shape, across all three loop forms — the defect is
    per-iteration re-declaration, not the ``while`` keyword.
    """
    bad_source = (
        "async def run(self):\n"
        f"{header}"
        "        async def _run_step(s, prior):\n"
        "            return s\n"
    )
    assert _run_step_nested_in_loop(ast.parse(bad_source))

    # The hoisted shape — same closure, declared above the loop — must pass.
    good_source = (
        "async def run(self):\n"
        "    async def _run_step(s, prior):\n"
        "        return s\n"
        f"{header}"
        "        await _run_step(1, None)\n"
    )
    assert not _run_step_nested_in_loop(ast.parse(good_source))


# ---------------------------------------------------------------------------
# Warning hold gate — sustained API pressure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_warning_hold_blocks_iteration(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When warning hold is active, the loop probes until it clears, then
    runs the iteration normally."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    hold_calls = [True, True, False, False]

    def fake_hold() -> bool:
        return hold_calls.pop(0) if hold_calls else False

    probe_mock = AsyncMock()

    with patch("maestro.agent.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.loop.iteration.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.agent.get_warning_hold_probe_interval", return_value=1), \
         patch("maestro.agent.query", probe_mock), \
         patch.object(agent, "_safe_shutdown_wait", new_callable=AsyncMock,
                      return_value=False):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    probe_mock.assert_called_once()
    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_warning_hold_probe_opts_out_of_throttle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The warning-hold probe must pass ``bypass_warning_throttle=True``.

    Pins #1203: without the bypass the probe would inherit the per-call
    throttle sleep inside ``query()`` (160s with multiplier pinned at 16),
    compounding the exponential probe-interval wait and ignoring the
    shutdown event during the throttle sleep. The bypass keeps the probe
    cadence at exactly the configured probe interval and shutdown-aware.
    """
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    hold_calls = [True, True, False, False]

    def fake_hold() -> bool:
        return hold_calls.pop(0) if hold_calls else False

    probe_mock = AsyncMock()

    with patch("maestro.agent.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.loop.iteration.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.agent.get_warning_hold_probe_interval", return_value=1), \
         patch("maestro.agent.query", probe_mock), \
         patch.object(agent, "_safe_shutdown_wait", new_callable=AsyncMock,
                      return_value=False):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    probe_mock.assert_called_once()
    call = probe_mock.await_args
    assert call is not None
    opts = call.kwargs["options"]
    assert opts.caller == "warning_hold_probe"
    assert opts.bypass_warning_throttle is True


@pytest.mark.asyncio
async def test_run_rate_limit_idle_probe_opts_out_of_throttle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The rate-limit idle probe must pass ``bypass_warning_throttle=True``.

    Same contract as the warning-hold probe (#1203): the probe is the
    signal that detects when the SDK is healthy again, so gating it on
    the soft-warning throttle compounds the wait and silences the
    shutdown event.
    """
    agent = make_agent()
    agent._rate_limit_idle_entered_at = "2026-04-22T12:00:00+00:00"

    probe_mock = AsyncMock(return_value=MockQueryResult(response="OK"))

    with patch("maestro.agent.query", probe_mock), \
         patch.object(agent, "_safe_shutdown_wait", new_callable=AsyncMock,
                      return_value=False), \
         patch.object(agent, "_write_heartbeat"):
        await agent._run_rate_limit_idle_probe()

    probe_mock.assert_called_once()
    call = probe_mock.await_args
    assert call is not None
    opts = call.kwargs["options"]
    assert opts.caller == "rate_limit_idle_probe"
    assert opts.bypass_warning_throttle is True


@pytest.mark.asyncio
async def test_run_loop_warning_hold_probe_rate_limit(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A probe that receives SDKError(RATE_LIMIT) does not break the hold
    loop; it continues probing."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    hold_calls = [True, True, True, False, False]

    def fake_hold() -> bool:
        return hold_calls.pop(0) if hold_calls else False

    probe_call_count = 0

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        nonlocal probe_call_count
        probe_call_count += 1
        if probe_call_count == 1:
            raise SDKError("rate limited", category=ErrorCategory.RATE_LIMIT)
        return MockQueryResult(response="OK")

    with patch("maestro.agent.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.loop.iteration.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.agent.get_warning_hold_probe_interval", return_value=1), \
         patch("maestro.agent.query", side_effect=probe_side_effect), \
         patch("asyncio.wait_for", new_callable=AsyncMock):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert probe_call_count == 2
    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_warning_hold_respects_shutdown(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Setting the shutdown event during warning hold exits cleanly."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    def fake_hold() -> bool:
        agent._ensure_shutdown_event().set()
        return True

    with patch("maestro.agent.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.loop.iteration.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.agent.get_warning_hold_probe_interval", return_value=1):
        await agent.run_loop(
            interval=0, max_iters=5, task_every=0, evolve_every=999,
        )

    agent.contemplate.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_warning_hold_journals_milestone(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A journal milestone entry is written when warning hold activates."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.journal.record_event = MagicMock()  # type: ignore[method-assign]

    hold_calls = [True, False, False]

    def fake_hold() -> bool:
        return hold_calls.pop(0) if hold_calls else False

    with patch("maestro.agent.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.loop.iteration.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.agent.get_warning_hold_probe_interval", return_value=1), \
         patch("maestro.agent.query", AsyncMock()):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    agent.journal.record_event.assert_called_once()  # type: ignore[attr-defined]
    call_kwargs = agent.journal.record_event.call_args  # type: ignore[attr-defined]
    assert call_kwargs.kwargs["entry_type"] == "milestone"
    assert call_kwargs.kwargs["triggered_by"] == "warning_hold"
    assert "Warning hold activated" in call_kwargs.kwargs["content"]


@pytest.mark.asyncio
async def test_run_loop_warning_hold_probe_backoff(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Probe sleep intervals follow exponential backoff."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Hold active for 4 checks (3 probes), then clears.
    hold_calls = [True, True, True, True, False, False]

    def fake_hold() -> bool:
        return hold_calls.pop(0) if hold_calls else False

    wait_timeouts: list[float | None] = []

    async def capture_wait_for(
        coro: Any, *, timeout: float | None = None,
    ) -> None:
        wait_timeouts.append(timeout)
        raise TimeoutError

    with patch("maestro.agent.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.loop.iteration.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.agent.get_warning_hold_probe_interval", return_value=10), \
         patch("maestro.agent.query", AsyncMock()), \
         patch("asyncio.wait_for", side_effect=capture_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # Probe sleeps: 10*2^0=10, 10*2^1=20, 10*2^2=40
    assert wait_timeouts[:3] == [10, 20, 40]


@pytest.mark.asyncio
async def test_warning_hold_sleeps_until_resets_at(make_agent) -> None:  # type: ignore[no-untyped-def]
    """#4514: with a future resets_at the hold waits for the window reset
    and probes once, instead of probing every 60s."""
    agent = make_agent()
    hold_calls = [True, False]
    waits: list[float] = []

    async def fake_wait(timeout: float) -> bool:
        waits.append(timeout)
        return False

    probe = AsyncMock()
    with patch("maestro.agent.is_warning_hold_active",
               side_effect=lambda: hold_calls.pop(0)), \
         patch("maestro.agent.get_warning_hold_reset_wait", return_value=610), \
         patch.object(agent, "_safe_shutdown_wait", side_effect=fake_wait), \
         patch("maestro.agent.query", probe):
        await agent._run_warning_hold_probe()

    assert waits == [300, 300, 10]
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_warning_hold_reset_wait_refreshes_heartbeat(make_agent) -> None:  # type: ignore[no-untyped-def]
    """#4514: the multi-hour reset wait refreshes the heartbeat per chunk
    and exits promptly on shutdown mid-wait."""
    agent = make_agent()
    waits = iter([False, True])
    heartbeat = MagicMock()
    probe = AsyncMock()
    with patch("maestro.agent.is_warning_hold_active", return_value=True), \
         patch("maestro.loop.iteration.is_warning_hold_active", return_value=True), \
         patch("maestro.agent.get_warning_hold_reset_wait", return_value=3600), \
         patch.object(agent, "_safe_shutdown_wait",
                      side_effect=lambda _t: next(waits)), \
         patch.object(agent, "_write_heartbeat", heartbeat), \
         patch("maestro.agent.query", probe):
        await agent._run_warning_hold_probe()

    assert heartbeat.call_count == 1
    assert probe.await_count == 0


@pytest.mark.asyncio
async def test_warning_hold_backoff_when_no_reset_wait(make_agent) -> None:  # type: ignore[no-untyped-def]
    """No future resets_at keeps the exponential-backoff probe."""
    agent = make_agent()
    hold_calls = [True, True, False]
    waits: list[float] = []

    async def fake_wait(timeout: float) -> bool:
        waits.append(timeout)
        return False

    with patch("maestro.agent.is_warning_hold_active",
               side_effect=lambda: hold_calls.pop(0)), \
         patch("maestro.agent.get_warning_hold_reset_wait", return_value=None), \
         patch("maestro.agent.get_warning_hold_probe_interval", return_value=60), \
         patch.object(agent, "_safe_shutdown_wait", side_effect=fake_wait), \
         patch("maestro.agent.query", AsyncMock()):
        await agent._run_warning_hold_probe()

    assert waits == [60, 120]


@pytest.mark.asyncio
async def test_run_loop_warning_hold_journal_failure_resilient(make_agent) -> None:  # type: ignore[no-untyped-def]
    """If journaling the hold entry fails, the hold loop still runs."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    original_record = agent.journal.record_event
    record_call_count = 0

    def failing_record(*args: Any, **kwargs: Any) -> Any:
        nonlocal record_call_count
        record_call_count += 1
        if record_call_count == 1:
            raise OSError("disk full")
        return original_record(*args, **kwargs)

    agent.journal.record_event = failing_record  # type: ignore[method-assign]

    hold_calls = [True, False, False]

    def fake_hold() -> bool:
        return hold_calls.pop(0) if hold_calls else False

    with patch("maestro.agent.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.loop.iteration.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.agent.get_warning_hold_probe_interval", return_value=1), \
         patch("maestro.agent.query", AsyncMock()):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_warning_hold_shutdown_during_probe_sleep(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Shutdown set after probe sleep but before query exits cleanly."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    async def fake_wait_for(
        coro: Any, *, timeout: float | None = None,
    ) -> None:
        agent._ensure_shutdown_event().set()

    with patch("maestro.agent.is_warning_hold_active", return_value=True), \
         patch("maestro.loop.iteration.is_warning_hold_active", return_value=True), \
         patch("maestro.agent.get_warning_hold_probe_interval", return_value=1), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=5, task_every=0, evolve_every=999,
        )

    agent.contemplate.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_warning_hold_probe_non_rate_limit_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-rate-limit SDKError during probe is logged and retried."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    hold_calls = [True, True, False, False]

    def fake_hold() -> bool:
        return hold_calls.pop(0) if hold_calls else False

    probe_mock = AsyncMock(
        side_effect=SDKError("cli crash", category=ErrorCategory.CLI_CRASH),
    )

    with patch("maestro.agent.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.loop.iteration.is_warning_hold_active", side_effect=fake_hold), \
         patch("maestro.agent.get_warning_hold_probe_interval", return_value=1), \
         patch("maestro.agent.query", probe_mock), \
         patch("asyncio.wait_for", new_callable=AsyncMock):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    probe_mock.assert_called_once()
    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_rate_limit_retry_preserves_iteration_number(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A rate-limit retry re-enters the same iteration, does not bump iterations_run."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    observed_iterations: list[int] = []
    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        observed_iterations.append(agent._resume_iteration)
        if call_count == 1:
            raise SDKError(
                "rate limited", category=ErrorCategory.RATE_LIMIT,
            )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # Both calls observe the same iteration number; retry did not advance it.
    assert len(observed_iterations) == 2
    assert observed_iterations[0] == observed_iterations[1]


@pytest.mark.asyncio
async def test_run_loop_rate_limit_ceiling_enters_extended_idle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Past the ceiling, the loop enters extended idle instead of crashing."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    contemplate_count = 0
    entered_at_snapshot: list[str | None] = []

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal contemplate_count
        contemplate_count += 1
        if contemplate_count <= _MAX_CONSECUTIVE_RATE_LIMITS + 1:
            raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)
        entered_at_snapshot.append(agent._rate_limit_idle_entered_at)
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    probe_calls = 0

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        nonlocal probe_calls
        probe_calls += 1
        return MockQueryResult(response="OK")

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("maestro.agent.query", side_effect=probe_side_effect), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # The 6th rate-limit triggered idle entry; the probe recovered and the
    # loop resumed at the failed phase, running contemplate a 7th time.
    assert contemplate_count == _MAX_CONSECUTIVE_RATE_LIMITS + 2
    assert probe_calls == 1
    # At the point contemplate ran successfully, idle had already been
    # exited (the exit handler clears the flag before the loop re-enters
    # the phases).
    assert entered_at_snapshot == [None]
    # Counter reset on successful probe.
    assert agent._consecutive_rate_limits == 0
    assert agent._rate_limit_idle_entered_at is None


@pytest.mark.asyncio
async def test_run_loop_rate_limit_idle_probe_exponential_backoff(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Probe sleep intervals follow exponential backoff capped at the max."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Shrink intervals so the test covers multiple attempts without
    # exploding sleep math. base=10, cap=40 -> 10, 20, 40, 40, 40
    agent.config.sdk_rate_limit_idle_probe_interval = 10
    agent.config.sdk_rate_limit_idle_max_probe_interval = 40

    contemplate_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal contemplate_count
        contemplate_count += 1
        if contemplate_count <= _MAX_CONSECUTIVE_RATE_LIMITS + 1:
            raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    probe_calls = 0

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls < 5:
            raise SDKError("still rate limited", category=ErrorCategory.RATE_LIMIT)
        return MockQueryResult(response="OK")

    wait_timeouts: list[float | None] = []

    async def capture_wait_for(
        coro: Any, *, timeout: float | None = None,
    ) -> None:
        # Only capture sleeps that happen while idle is active so the
        # cooldown sleeps before idle entry don't pollute the assertion.
        if agent._rate_limit_idle_entered_at is not None:
            wait_timeouts.append(timeout)
        raise TimeoutError

    with patch("maestro.agent.query", side_effect=probe_side_effect), \
         patch("asyncio.wait_for", side_effect=capture_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # Probe sleeps follow base * 2**n capped at cap: 10, 20, 40, 40, 40.
    # Each probe sleep is smaller than _STANDBY_MAX_SLEEP_SECONDS (300s)
    # so each probe contributes exactly one wait_for of its full sleep.
    assert wait_timeouts[:5] == [10, 20, 40, 40, 40]


@pytest.mark.asyncio
async def test_run_loop_rate_limit_idle_probe_sdk_errors_keep_probing(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Non-rate-limit ``SDKError`` during idle probing does not crash the probe loop.

    Only ``SDKError`` (the genuinely transient class) is suppressed by
    the probe; the dedicated branch logs the category and continues.
    Anything else propagates — see
    ``test_run_loop_rate_limit_idle_probe_propagates_generic_exception``.
    """
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    contemplate_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal contemplate_count
        contemplate_count += 1
        if contemplate_count <= _MAX_CONSECUTIVE_RATE_LIMITS + 1:
            raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    probe_calls = 0

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            raise SDKError("api hiccup", category=ErrorCategory.UNKNOWN)
        return MockQueryResult(response="OK")

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("maestro.agent.query", side_effect=probe_side_effect), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert probe_calls == 2
    # Idle exited cleanly, loop resumed contemplate once more.
    assert contemplate_count == _MAX_CONSECUTIVE_RATE_LIMITS + 2
    assert agent._rate_limit_idle_entered_at is None


@pytest.mark.asyncio
async def test_run_loop_rate_limit_idle_probe_propagates_generic_exception(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-``SDKError`` exception during idle probing crashes the loop.

    The probe used to swallow every ``Exception``; that hid real bugs
    (network, file I/O, programming error) under WARNING logs. The
    dedicated ``SDKError`` arm covers the legitimate transient class;
    anything else must propagate.
    """
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    contemplate_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal contemplate_count
        contemplate_count += 1
        raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    probe_calls = 0

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        nonlocal probe_calls
        probe_calls += 1
        raise RuntimeError("transient auth hiccup")

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with (
        patch("maestro.agent.query", side_effect=probe_side_effect),
        patch("asyncio.wait_for", side_effect=fake_wait_for),
        pytest.raises(RuntimeError, match="transient auth hiccup"),
    ):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert probe_calls == 1
    assert contemplate_count == _MAX_CONSECUTIVE_RATE_LIMITS + 1


@pytest.mark.asyncio
async def test_run_loop_rate_limit_idle_exit_resets_counter(make_agent) -> None:  # type: ignore[no-untyped-def]
    """After idle exits, a subsequent burst gets the full fast-retry window again."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Observe the counter at the moment of a later rate-limit to confirm
    # it was reset to 0 after probe success.
    counter_at_second_burst: list[int] = []
    contemplate_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal contemplate_count
        contemplate_count += 1
        # Burst 1: rate-limit for 6 calls (triggers idle at call 6).
        if contemplate_count <= _MAX_CONSECUTIVE_RATE_LIMITS + 1:
            raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)
        # Call 7 completes the iteration cleanly.
        if contemplate_count == _MAX_CONSECUTIVE_RATE_LIMITS + 2:
            return "ok"
        # Iteration 2: a single rate-limit in a fresh burst.
        if contemplate_count == _MAX_CONSECUTIVE_RATE_LIMITS + 3:
            counter_at_second_burst.append(agent._consecutive_rate_limits + 1)
            raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        return MockQueryResult(response="OK")

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("maestro.agent.query", side_effect=probe_side_effect), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=2, task_every=0, evolve_every=999,
        )

    # On the first rate-limit of the second burst the counter has just
    # been incremented to 1 — nowhere near the ceiling — proving it reset
    # between iterations.
    assert counter_at_second_burst == [1]


@pytest.mark.asyncio
async def test_run_loop_rate_limit_idle_persists_to_lifecycle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Entering idle writes ``rate_limit_idle_entered_at`` to lifecycle.json."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    lifecycle_snapshots: list[str | None] = []
    contemplate_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal contemplate_count
        contemplate_count += 1
        if contemplate_count <= _MAX_CONSECUTIVE_RATE_LIMITS + 1:
            raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)
        # Snapshot lifecycle.json right after idle exited and before
        # the next phase persists progress.
        try:
            data = json.loads(agent.config.lifecycle_path.read_text())
            lifecycle_snapshots.append(data.get("rate_limit_idle_entered_at"))
        except (FileNotFoundError, json.JSONDecodeError):
            lifecycle_snapshots.append("missing")
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    # Capture the lifecycle.json contents *during* the probe (before exit
    # clears the field).
    probe_captured: list[str | None] = []

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        try:
            data = json.loads(agent.config.lifecycle_path.read_text())
            probe_captured.append(data.get("rate_limit_idle_entered_at"))
        except (FileNotFoundError, json.JSONDecodeError):
            probe_captured.append(None)
        return MockQueryResult(response="OK")

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("maestro.agent.query", side_effect=probe_side_effect), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # While the probe ran, lifecycle.json carried the idle marker.
    assert len(probe_captured) == 1
    assert probe_captured[0] is not None
    # After exit, a subsequent phase cleared it to None.
    assert lifecycle_snapshots == [None]


@pytest.mark.asyncio
async def test_run_loop_rate_limit_idle_restart_resumes_in_probe(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A new agent started with ``_rate_limit_idle_entered_at`` set enters the probe loop immediately."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Simulate restart-into-idle: write lifecycle.json with the idle
    # flag set so ``_read_lifecycle_progress`` restores it on startup.
    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 0,
        "next_phase": None,
        "rate_limit_idle_entered_at": datetime.now(UTC).isoformat(),
    }))

    probe_calls = 0

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        nonlocal probe_calls
        probe_calls += 1
        return MockQueryResult(response="OK")

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("maestro.agent.query", side_effect=probe_side_effect), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # Probe ran on restart, then loop continued into the normal iteration.
    assert probe_calls == 1
    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]
    assert agent._rate_limit_idle_entered_at is None


@pytest.mark.asyncio
async def test_run_loop_rate_limit_idle_shutdown_interrupts_probe_sleep(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Setting the shutdown event during probe sleep exits cleanly without crash."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    contemplate_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal contemplate_count
        contemplate_count += 1
        if contemplate_count <= _MAX_CONSECUTIVE_RATE_LIMITS + 1:
            raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    async def fake_wait_for(
        coro: Any, *, timeout: float | None = None,
    ) -> None:
        # Set shutdown only once we are inside the probe sleep so the
        # cooldown sleeps before the ceiling don't cut the test short.
        if agent._rate_limit_idle_entered_at is not None:
            agent._ensure_shutdown_event().set()
        raise TimeoutError

    probe_query = AsyncMock()

    with patch("maestro.agent.query", probe_query), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=5, task_every=0, evolve_every=999,
        )

    # Shutdown fired during the probe sleep, before the probe query ran.
    probe_query.assert_not_called()
    # Idle state persists across the shutdown (so next startup resumes in
    # idle). Clean-iteration clearing never runs because the iteration
    # never finished.
    assert agent._rate_limit_idle_entered_at is not None


@pytest.mark.asyncio
async def test_run_loop_rate_limit_idle_writes_heartbeat_during_long_sleep(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Heartbeat fires mid-sleep when probe interval exceeds a chunk."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS, _STANDBY_MAX_SLEEP_SECONDS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Force probe interval > _STANDBY_MAX_SLEEP_SECONDS so chunks split.
    agent.config.sdk_rate_limit_idle_probe_interval = (
        _STANDBY_MAX_SLEEP_SECONDS * 2 + 50
    )
    agent.config.sdk_rate_limit_idle_max_probe_interval = (
        _STANDBY_MAX_SLEEP_SECONDS * 2 + 50
    )

    contemplate_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal contemplate_count
        contemplate_count += 1
        if contemplate_count <= _MAX_CONSECUTIVE_RATE_LIMITS + 1:
            raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    heartbeat_mock = MagicMock()
    agent._write_heartbeat = heartbeat_mock  # type: ignore[method-assign]

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        return MockQueryResult(response="OK")

    chunk_timeouts: list[float | None] = []

    async def fake_wait_for(
        coro: Any, *, timeout: float | None = None,
    ) -> None:
        chunk_timeouts.append(timeout)
        raise TimeoutError

    with patch("maestro.agent.query", side_effect=probe_side_effect), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # Sleep was split into 3 chunks (300 + 300 + 50), so _write_heartbeat
    # was called once per chunk (at least 3 times during the probe loop).
    # Other callers of _write_heartbeat may add more, so we assert >= 3.
    assert heartbeat_mock.call_count >= 3
    # Chunk timeouts include the split: 300, 300, 50 for the single probe
    # sleep.
    probe_chunks = [
        t for t in chunk_timeouts
        if t in (_STANDBY_MAX_SLEEP_SECONDS, 50)
    ]
    assert _STANDBY_MAX_SLEEP_SECONDS in probe_chunks
    assert 50 in probe_chunks


@pytest.mark.asyncio
async def test_run_loop_rate_limit_idle_emits_telegram_and_journal_on_enter_and_exit(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Entry and exit both notify and journal with the correct triggered_by."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    notify_mock = AsyncMock()
    agent.notifier.notify = notify_mock  # type: ignore[method-assign]

    journal_calls: list[dict[str, Any]] = []
    original_write = agent.journal.record_event

    def capture_write(**kwargs: Any) -> None:
        journal_calls.append(kwargs)
        original_write(**kwargs)

    agent.journal.record_event = capture_write  # type: ignore[method-assign]

    contemplate_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal contemplate_count
        contemplate_count += 1
        if contemplate_count <= _MAX_CONSECUTIVE_RATE_LIMITS + 1:
            raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        return MockQueryResult(response="OK")

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("maestro.agent.query", side_effect=probe_side_effect), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    triggers = [c.get("triggered_by") for c in journal_calls]
    assert "rate_limit_idle_enter" in triggers
    assert "rate_limit_idle_exit" in triggers
    enter = next(
        c for c in journal_calls
        if c.get("triggered_by") == "rate_limit_idle_enter"
    )
    exit_ = next(
        c for c in journal_calls
        if c.get("triggered_by") == "rate_limit_idle_exit"
    )
    assert enter["entry_type"] == "milestone"
    assert exit_["entry_type"] == "milestone"

    # Telegram notifier was called at both transitions.
    notify_contents = [str(c.args[0]) if c.args else "" for c in notify_mock.call_args_list]
    assert any("extended idle" in m for m in notify_contents)
    assert any("cleared" in m for m in notify_contents)


def test_rate_limit_idle_entered_at_property(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``rate_limit_idle_entered_at`` returns the underlying timestamp."""
    agent = make_agent()
    assert agent.rate_limit_idle_entered_at is None
    agent._rate_limit_idle_entered_at = "2026-04-22T12:00:00+00:00"
    assert agent.rate_limit_idle_entered_at == "2026-04-22T12:00:00+00:00"


@pytest.mark.asyncio
async def test_enter_rate_limit_idle_resilient_to_journal_failure(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A journal write failure during enter is caught locally and logged."""
    agent = make_agent()
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("disk full"),
    )
    agent._persist_progress = MagicMock()  # type: ignore[method-assign]

    await agent._enter_rate_limit_idle(iteration=1, phase="contemplate")

    assert agent._rate_limit_idle_entered_at is not None


@pytest.mark.asyncio
async def test_enter_rate_limit_idle_propagates_notify_exception(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A real ``Exception`` from the enter notification propagates.

    State mutation happens before the notify call, so the idle flag is
    still set when the notification raises. Best-effort notification
    suppresses only ``CancelledError``; real bugs surface for RCA.
    """
    agent = make_agent()
    agent.notifier.notify = AsyncMock(side_effect=RuntimeError("telegram down"))  # type: ignore[method-assign]
    agent._persist_progress = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="telegram down"):
        await agent._enter_rate_limit_idle(iteration=1, phase="contemplate")

    assert agent._rate_limit_idle_entered_at is not None


@pytest.mark.asyncio
async def test_exit_rate_limit_idle_resilient_to_journal_failure(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A journal write failure during exit is caught locally and logged."""
    agent = make_agent()
    agent._rate_limit_idle_entered_at = "2026-04-22T12:00:00+00:00"
    agent._consecutive_rate_limits = 6
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("disk full"),
    )

    await agent._exit_rate_limit_idle()

    assert agent._rate_limit_idle_entered_at is None
    assert agent._consecutive_rate_limits == 0


@pytest.mark.asyncio
async def test_exit_rate_limit_idle_propagates_notify_exception(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A real ``Exception`` from the exit notification propagates.

    State clearing happens before the notify call, so the idle flag is
    correctly cleared even though the notification raises.
    """
    agent = make_agent()
    agent._rate_limit_idle_entered_at = "2026-04-22T12:00:00+00:00"
    agent._consecutive_rate_limits = 6
    agent.notifier.notify = AsyncMock(side_effect=RuntimeError("telegram down"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="telegram down"):
        await agent._exit_rate_limit_idle()

    assert agent._rate_limit_idle_entered_at is None
    assert agent._consecutive_rate_limits == 0


@pytest.mark.asyncio
async def test_run_rate_limit_idle_probe_exits_immediately_on_shutdown(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Shutdown set before the probe loop's first sleep returns immediately."""
    agent = make_agent()
    agent._rate_limit_idle_entered_at = "2026-04-22T12:00:00+00:00"
    agent._ensure_shutdown_event().set()

    query_mock = AsyncMock()
    with patch("maestro.agent.query", query_mock):
        await agent._run_rate_limit_idle_probe()

    query_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_rate_limit_idle_restart_break_on_shutdown_after_probe(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Restart-into-idle path: shutdown set during probe breaks before exit."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Restart-into-idle: lifecycle.json has the idle flag set.
    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 0,
        "next_phase": None,
        "rate_limit_idle_entered_at": datetime.now(UTC).isoformat(),
    }))

    exit_called = False

    async def exit_spy() -> None:
        nonlocal exit_called
        exit_called = True

    async def probe_spy() -> None:
        agent._ensure_shutdown_event().set()

    agent._run_rate_limit_idle_probe = probe_spy  # type: ignore[method-assign]
    agent._exit_rate_limit_idle = exit_spy  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    # Probe set shutdown, so the loop breaks before _exit_rate_limit_idle runs
    # and contemplate never gets a chance to run.
    assert exit_called is False
    assert agent.contemplate.call_count == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_warning_hold_probe_propagates_generic_exception(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-SDKError exception during warning-hold probe crashes the loop.

    The probe's ``except SDKError`` arm handles the genuinely transient
    classes (rate limits, hard limits). Everything else is a real bug —
    network, file I/O, programming error — that must surface so it
    triggers root-cause analysis rather than be silently retried.
    """
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    hold_calls = [True, True, True, False, False]

    def fake_hold() -> bool:
        return hold_calls.pop(0) if hold_calls else False

    probe_call_count = 0

    async def probe_side_effect(*args: Any, **kwargs: Any) -> MockQueryResult:
        nonlocal probe_call_count
        probe_call_count += 1
        raise RuntimeError("network exploded")

    with (
        patch("maestro.agent.is_warning_hold_active", side_effect=fake_hold),
        patch("maestro.loop.iteration.is_warning_hold_active", side_effect=fake_hold),
        patch("maestro.agent.get_warning_hold_probe_interval", return_value=1),
        patch("maestro.agent.query", side_effect=probe_side_effect),
        patch("asyncio.wait_for", new_callable=AsyncMock),
        pytest.raises(RuntimeError, match="network exploded"),
    ):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert probe_call_count == 1
    assert agent.contemplate.call_count == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_rate_limit_counter_resets_on_clean_iteration(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A clean iteration after a rate-limit clears ``_consecutive_rate_limits``."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_count = 0

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError("rate", category=ErrorCategory.RATE_LIMIT)
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=2, task_every=0, evolve_every=999)

    # After the clean iteration completes, the counter is back to zero.
    assert agent._consecutive_rate_limits == 0


@pytest.mark.asyncio
async def test_clean_iteration_end_clears_ephemeral_state(make_agent) -> None:  # type: ignore[no-untyped-def]
    """On a clean iteration finish, ephemeral per-iteration fields are cleared."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection-text")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    # Seed stale ephemeral state to prove the clean-iteration block wipes it.
    agent._iter_session_a_id = "ses_stale_a"
    agent._iter_session_b_id = "ses_stale_b"
    agent._consecutive_rate_limits = 2
    # A stale per-iteration CI-failure-oracle surfaced-line fact (102) must not
    # survive into the fresh iteration's verdict; the start-of-iteration reset
    # drops it alongside the merge-truth / readiness memos.
    agent._ci_oracle_surfaced = {8: True}

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    assert agent._iter_message_context == ""
    assert agent._iter_reflection == ""
    assert agent._iter_session_a_id is None
    assert agent._iter_session_b_id is None
    assert agent._consecutive_rate_limits == 0
    assert agent._resume_phase is None
    assert agent._ci_oracle_surfaced == {}


@pytest.mark.asyncio
async def test_stale_session_clears_persisted_ids_and_retries(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A STALE_SESSION error clears persisted ids, no cooldown, and retries the phase."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent._iter_session_a_id = "ses_pre_existing"
    agent._iter_session_b_id = "ses_pre_existing_b"

    call_count = 0
    cleared_ids_observed: list[tuple[str | None, str | None]] = []

    async def contemplate_side_effect(**kwargs: Any) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SDKError(
                "session stale", category=ErrorCategory.STALE_SESSION,
            )
        cleared_ids_observed.append(
            (agent._iter_session_a_id, agent._iter_session_b_id),
        )
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    wait_timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        wait_timeouts.append(timeout)
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # Retry happened.
    assert call_count == 2
    # On the retry both ids were cleared (fresh sessions will be created).
    assert cleared_ids_observed == [(None, None)]
    # No 300s cooldown was slept; stale-session path does not cool down.
    assert 300 not in wait_timeouts
    # Rate-limit counter was not bumped (stale arm has its own counter).
    assert agent._consecutive_rate_limits == 0
    # Stale-session counter bumped to 1 during the retry, then reset at the
    # clean iteration end.
    assert agent._consecutive_stale_sessions == 0


@pytest.mark.asyncio
async def test_stale_session_ceiling_crashes_loop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Consecutive stale sessions above the ceiling re-raise to the crash path."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_STALE_SESSIONS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=SDKError(
            "session stale", category=ErrorCategory.STALE_SESSION,
        ),
    )

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for), \
         pytest.raises(SDKError):
        await agent.run_loop(interval=0, max_iters=99, task_every=0, evolve_every=999)

    # The ceiling is _MAX_CONSECUTIVE_STALE_SESSIONS — one past that re-raises.
    assert agent.contemplate.call_count == _MAX_CONSECUTIVE_STALE_SESSIONS + 1  # type: ignore[attr-defined]


