"""Tests for agent lifecycle: shutdown, signals, PID management, and CI workflows."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.cli import _read_pid, cmd_kill, cmd_serve, cmd_shutdown
from maestro.config import Config
from maestro.evolution import MergeCheckResult
from maestro.memory import EvolutionPlan
from tests.conftest import MockQueryResult, capture_stdout
from tests.support.gap_fuel import stub_evolve_phase

_ITER = "maestro.loop.iteration"

FAKE_DIR = Path("/fake/lifecycle")


def _make_config() -> Config:
    return Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)


# ──────────────────────────────────────────────────────────────────────
# Agent shutdown tests
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_writes_journal_entry(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._running = True
    agent._current_task = "something"

    await agent.shutdown()

    assert agent.is_running is False
    assert agent.current_task is None
    assert agent._signal_handlers_installed is False

    entries = agent.journal.read_recent_events()
    assert len(entries) == 1
    assert entries[0].entry_type == "milestone"
    assert entries[0].triggered_by == "shutdown"
    assert "Shutting down gracefully" in entries[0].content


@pytest.mark.asyncio
async def test_shutdown_journal_failure_is_resilient(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._running = True

    with patch.object(agent.journal, "record_event", side_effect=OSError("disk full")):
        await agent.shutdown()

    # Should not raise
    assert agent.is_running is False


@pytest.mark.asyncio
async def test_shutdown_removes_pid_file(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._running = True
    pid_path = str(agent.config.pid_path)
    mem_fs[pid_path] = "12345"

    await agent.shutdown()

    assert pid_path not in mem_fs


# ──────────────────────────────────────────────────────────────────────
# Lifecycle awareness tests (shutdown timestamp + startup detection)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_writes_lifecycle_file(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._running = True
    agent._resume_iteration = 4
    agent._resume_phase = "evolve"

    await agent.shutdown()

    lifecycle_path = str(agent.config.lifecycle_path)
    assert lifecycle_path in mem_fs
    data = json.loads(mem_fs[lifecycle_path])
    assert "last_shutdown" in data
    # Verify it's a valid ISO timestamp
    datetime.fromisoformat(data["last_shutdown"])
    # Resume cursor is persisted alongside the timestamp.
    assert data["iteration"] == 4
    assert data["next_phase"] == "evolve"


@pytest.mark.asyncio
async def test_shutdown_lifecycle_write_failure_is_resilient(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._running = True

    with patch.object(agent, "_write_lifecycle_shutdown", side_effect=OSError("disk full")):
        await agent.shutdown()

    assert agent.is_running is False


@pytest.mark.asyncio
async def test_shutdown_notify_hang_bounded(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """shutdown() must not hang when notifier.notify() stalls.

    In production the Telegram notify call has been observed to hang until
    anyio's internal cancel-scope deadline (tens of seconds) on flaky
    connections. Shutdown must bound the notify call itself so it cannot
    exhaust Docker's ``stop_grace_period``, which would let SIGKILL arrive
    before the finally/shutdown path finishes — triggering a false-positive
    crash on the next startup.
    """
    agent = make_agent()
    agent._running = True
    agent._resume_iteration = 2
    agent._resume_phase = "contemplate"

    # Notify never resolves — simulates a stuck HTTP connection.
    notify_started = asyncio.Event()

    async def _hang(_message: str) -> None:
        notify_started.set()
        await asyncio.Event().wait()  # never set

    agent.notifier.notify = AsyncMock(side_effect=_hang)  # type: ignore[method-assign]

    # Shrink the timeout so the test does not pay the real 5-second bound.
    with patch("maestro.agent._SHUTDOWN_NOTIFY_TIMEOUT", 0.01):
        await agent.shutdown()

    # notify() was invoked (proving we actually tried to send), but
    # shutdown() returned because the bounded wait_for fired.
    assert notify_started.is_set()

    # Shutdown completed even though notify never returned. The lifecycle
    # file was written before the notify call so the next startup will
    # classify this as a clean shutdown, not a crash.
    assert agent.is_running is False
    lifecycle_path = str(agent.config.lifecycle_path)
    assert lifecycle_path in mem_fs
    data = json.loads(mem_fs[lifecycle_path])
    assert "last_shutdown" in data
    assert data["iteration"] == 2
    assert data["next_phase"] == "contemplate"


# ──────────────────────────────────────────────────────────────────────
# Signal handler: eager lifecycle write
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signal_handler_eagerly_persists_last_shutdown(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """The signal handler must write ``last_shutdown`` synchronously.

    When SIGTERM arrives mid-phase (e.g. during a long-running SDK call),
    the running coroutine is not cancelled, so the loop does not reach
    ``shutdown()`` before Docker's ``stop_grace_period`` expires. Without
    an eager write the lifecycle file carries ``iteration > 0`` without
    ``last_shutdown``, and the next startup reports a false-positive
    crash. This test is the regression guard.
    """
    agent = make_agent()
    agent._resume_iteration = 1
    agent._resume_phase = "contemplate"
    event = agent._ensure_shutdown_event()
    assert not event.is_set()

    agent._on_shutdown_signal()

    # The event is set so the loop body can bail at the next phase.
    assert event.is_set()
    # And the lifecycle file now carries a clean-shutdown marker.
    lifecycle_path = str(agent.config.lifecycle_path)
    assert lifecycle_path in mem_fs
    data = json.loads(mem_fs[lifecycle_path])
    assert "last_shutdown" in data
    datetime.fromisoformat(data["last_shutdown"])
    assert data["iteration"] == 1
    assert data["next_phase"] == "contemplate"

    # Confirm the next startup would not classify this as a crash, even
    # though the loop body never reached shutdown().
    _last, _it, _ph, _ec, crashed, *_ = agent._read_lifecycle_progress()
    assert crashed is False


@pytest.mark.asyncio
async def test_signal_handler_lifecycle_write_failure_swallowed(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """The signal handler must never raise back into the event loop.

    Disk errors during the eager lifecycle write are logged and swallowed
    so the signal handler is safe to install as an ``add_signal_handler``
    callback.
    """
    agent = make_agent()
    with patch.object(
        agent, "_write_lifecycle_shutdown", side_effect=OSError("disk full"),
    ):
        agent._on_shutdown_signal()  # must not raise

    assert agent._ensure_shutdown_event().is_set()


@pytest.mark.asyncio
async def test_install_signal_handlers_registers_on_shutdown_signal(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """_install_signal_handlers wires ``_on_shutdown_signal`` (not ``event.set``)
    so that a real SIGTERM runs the eager lifecycle write."""
    agent = make_agent()
    loop = asyncio.get_running_loop()
    registered: dict[int, object] = {}

    def _capture(sig: int, callback: object) -> None:
        registered[sig] = callback

    with patch.object(loop, "add_signal_handler", side_effect=_capture):
        agent._install_signal_handlers()

    assert signal.SIGTERM in registered
    assert signal.SIGINT in registered
    assert registered[signal.SIGTERM] == agent._on_shutdown_signal
    assert registered[signal.SIGINT] == agent._on_shutdown_signal


@pytest.mark.asyncio
async def test_startup_detects_downtime_and_writes_journal(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)
    # Simulate a shutdown that happened 1h 30m 15s ago
    shutdown_ts = "2026-03-31T10:00:00+00:00"
    mem_fs[lifecycle_path] = json.dumps({"last_shutdown": shutdown_ts})

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with patch(f"{_ITER}.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 3, 31, 11, 30, 15, tzinfo=UTC)
        mock_dt.fromisoformat = datetime.fromisoformat
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    entries = agent.journal.read_recent_events(10)
    startup_entries = [e for e in entries if e.triggered_by == "startup"]
    assert len(startup_entries) == 1
    assert "1h 30m 15s" in startup_entries[0].content
    assert shutdown_ts in startup_entries[0].content
    assert startup_entries[0].entry_type == "milestone"
    # The original shutdown record is consumed; the loop re-persists progress
    # for the iteration it just completed (iteration=1, next_phase=None).
    assert lifecycle_path in mem_fs
    data = json.loads(mem_fs[lifecycle_path])
    assert data["iteration"] == 1
    assert data["next_phase"] is None
    assert "last_shutdown" not in data


@pytest.mark.asyncio
async def test_startup_no_lifecycle_file_skips_journal(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    agent = make_agent()
    # No lifecycle.json pre-populated

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    entries = agent.journal.read_recent_events(10)
    assert not any(e.triggered_by == "startup" for e in entries)


@pytest.mark.asyncio
async def test_startup_corrupted_lifecycle_file_is_resilient(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)
    mem_fs[lifecycle_path] = "not valid json!!!"

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    entries = agent.journal.read_recent_events(10)
    assert not any(e.triggered_by == "startup" for e in entries)
    # The corrupt bytes are consumed by replacement, not deletion:
    # _read_lifecycle_progress leaves the malformed file on disk, then
    # _persist_progress overwrites it atomically with the just-completed
    # iteration's cursor. Assert the disposition invariant the sibling downtime
    # test relies on so the corrupt "not valid json!!!" content is proven
    # replaced by valid state (iteration=1, next_phase=None, no last_shutdown)
    # rather than left behind.
    assert lifecycle_path in mem_fs
    data = json.loads(mem_fs[lifecycle_path])
    assert data["iteration"] == 1
    assert data["next_phase"] is None
    assert "last_shutdown" not in data


@pytest.mark.asyncio
async def test_merge_detection_runs_only_first_iteration(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=3, task_every=0, evolve_every=999)

    # check_merged_plans should be called exactly once (first iteration only)
    assert agent.evolution.check_merged_plans.call_count == 1


@pytest.mark.asyncio
async def test_run_loop_crashes_on_contemplate_failure(make_agent):  # type: ignore[no-untyped-def]
    """run_loop() propagates contemplate() errors (phase-level suppression removed)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(side_effect=RuntimeError("SDK failure"))  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="SDK failure"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


def _make_telegram_notifier():  # type: ignore[no-untyped-def]
    """Build a ``TelegramNotifier`` with a mocked ``Bot`` (no network)."""
    from maestro.notifications.telegram import TelegramNotifier

    with patch("maestro.notifications.telegram.Bot", return_value=MagicMock()):
        return TelegramNotifier(bot_token="fake-token", chat_id="123")


def test_raise_pending_poll_failure_paths(make_agent):  # type: ignore[no-untyped-def]
    """The probe crashes on a stored poll failure and no-ops otherwise."""
    agent = make_agent()

    # Default ``NullNotifier`` is not a ``TelegramNotifier`` — nothing to probe.
    agent._raise_pending_poll_failure()

    notifier = _make_telegram_notifier()
    agent.notifier = notifier

    # Healthy channel: no stored failure, so no crash.
    assert notifier.poll_failure is None
    agent._raise_pending_poll_failure()

    # Dead channel: the stored failure is re-raised and consumed so it fires once.
    notifier.poll_failure = OSError("channel down")
    with pytest.raises(OSError, match="channel down"):
        agent._raise_pending_poll_failure()
    assert notifier.poll_failure is None


@pytest.mark.asyncio
async def test_run_loop_crashes_on_dead_poll_task(make_agent):  # type: ignore[no-untyped-def]
    """A dead poll task surfaced at the probe crashes the loop into ``crashed=True``.

    Wires the probe into ``run_loop``'s crash boundary: a re-raised poll failure
    must propagate past the per-phase guards so ``shutdown(crashed=True)`` runs
    and next-startup crash detection fires (instead of the channel dying silent).
    """
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    # Safety net: never reached, because the probe fires before contemplate.
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Simulate a poll-task death surfacing at the top of the iteration.
    agent._raise_pending_poll_failure = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("poll task died"),
    )

    with pytest.raises(OSError, match="poll task died"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_propagates_corrupt_self_model(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """A corrupt ``self_model.json`` propagates out of ``run_loop`` to crash handling.

    Cross-cutting fail-loud guard: site 1's removal of the
    ``except (json.JSONDecodeError, ...)`` swallow in
    ``Consciousness.load_self_model`` means corruption raises through
    every caller. ``run_loop`` reaches one such caller during phase
    work, the iteration handler classifies the failure, and shutdown
    is invoked with ``crashed=True`` so ``last_shutdown`` is **not**
    written — preserving the crash-evidence chain (the next start
    will run ``handle_crash``).
    """
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )

    # Corruption surfaces through any consciousness call the loop makes.
    agent.consciousness.load_self_model = MagicMock(  # type: ignore[method-assign]
        side_effect=json.JSONDecodeError("Expecting value", "", 0),
    )

    with pytest.raises(json.JSONDecodeError):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    lifecycle_path = str(agent.config.lifecycle_path)
    if lifecycle_path in mem_fs:
        data = json.loads(mem_fs[lifecycle_path])
        # ``last_shutdown`` must NOT be present after a crashed run —
        # its absence is the signal that ``handle_crash`` runs next
        # boot.
        assert "last_shutdown" not in data


@pytest.mark.asyncio
async def test_run_loop_returns_true_on_success(make_agent):  # type: ignore[no-untyped-def]
    """run_loop() should return True when all iterations succeed."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    result = await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    assert result is True


# ──────────────────────────────────────────────────────────────────────
# Phase-level progress persistence and resume
# ──────────────────────────────────────────────────────────────────────


def _stub_loop_externals(agent) -> None:  # type: ignore[no-untyped-def]
    """Mock the agent collaborators that run_loop touches."""
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
    agent._process_messages = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    stub_evolve_phase(agent, AsyncMock())
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_iteration_persists_completion_cursor(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """A clean iteration leaves ``next_phase=None`` on disk so the next
    restart begins a fresh iteration N+1 instead of replaying iteration N."""
    agent = make_agent()
    _stub_loop_externals(agent)

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    lifecycle_path = str(agent.config.lifecycle_path)
    assert lifecycle_path in mem_fs
    data = json.loads(mem_fs[lifecycle_path])
    assert data["iteration"] == 1
    assert data["next_phase"] is None


@pytest.mark.asyncio
async def test_shutdown_after_phase_persists_resume_cursor(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """A shutdown signal raised mid-iteration lets the current phase finish,
    persists ``(iteration, next_phase)``, and stops before the next phase."""
    agent = make_agent()
    _stub_loop_externals(agent)

    # Trip the shutdown event when contemplate() finishes. The current phase
    # ("contemplate") completes, persistence runs with next_phase set to
    # "respond_to_messages", and the loop then exits.
    async def _contemplate(context: str = "", session=None, iteration: int = 0, diag_snapshot=None) -> str:  # type: ignore[no-untyped-def]
        agent._ensure_shutdown_event().set()
        return "reflection"

    agent.contemplate = AsyncMock(side_effect=_contemplate)  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=5, task_every=0, evolve_every=999)

    # Phases that should run:
    agent.evolution.ensure_main_branch.assert_called()
    agent.contributions.check_for_contributions.assert_called()
    agent._process_messages.assert_awaited()
    agent.contemplate.assert_awaited()
    # Phases that should NOT run (shutdown hit before them):
    agent._respond_to_messages.assert_not_awaited()
    agent._implement_approved_plans.assert_not_awaited()
    agent._process_rejected_plans.assert_not_awaited()
    agent.evolution.check_merged_plans.assert_not_called()

    lifecycle_path = str(agent.config.lifecycle_path)
    assert lifecycle_path in mem_fs
    data = json.loads(mem_fs[lifecycle_path])
    assert data["iteration"] == 1
    assert data["next_phase"] == "respond_to_messages"


@pytest.mark.asyncio
async def test_startup_resumes_from_saved_phase(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """A pre-populated lifecycle file with ``next_phase`` causes the loop
    to skip earlier phases of that iteration and resume mid-iteration."""
    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)
    mem_fs[lifecycle_path] = json.dumps({
        "last_shutdown": "2026-04-05T10:00:00+00:00",
        "iteration": 7,
        "next_phase": "evolve",
    })

    _stub_loop_externals(agent)

    await agent.run_loop(
        interval=0, max_iters=1, task_every=1, evolve_every=1, dream_every=999,
    )

    # Phases earlier than "evolve" must NOT run on the resume iteration.
    agent.evolution.ensure_main_branch.assert_not_called()
    agent.contributions.check_for_contributions.assert_not_called()
    agent._process_messages.assert_not_awaited()
    agent.contemplate.assert_not_awaited()
    agent._respond_to_messages.assert_not_awaited()
    agent._implement_approved_plans.assert_not_awaited()
    agent.evolution.check_merged_plans.assert_not_called()
    agent._process_rejected_plans.assert_not_awaited()
    agent.generate_task.assert_not_awaited()
    # "evolve" and onwards do run.
    agent._evolve_with_fuel.assert_awaited()

    # After the partial iteration finishes, the cursor advances to
    # iteration=7, next_phase=None — i.e. iteration 7 is now complete.
    data = json.loads(mem_fs[lifecycle_path])
    assert data["iteration"] == 7
    assert data["next_phase"] is None


@pytest.mark.asyncio
async def test_resume_then_full_next_iteration(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """After resuming a partial iteration, the next iteration runs every
    phase from the start and the iteration counter advances normally."""
    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)
    mem_fs[lifecycle_path] = json.dumps({
        "last_shutdown": "2026-04-05T10:00:00+00:00",
        "iteration": 4,
        "next_phase": "evolve",
    })

    _stub_loop_externals(agent)

    await agent.run_loop(
        interval=0, max_iters=2, task_every=0, evolve_every=999,
    )

    # ensure_main_branch should run exactly once: skipped on the resume
    # (iteration 4) and run on the fresh iteration 5.
    assert agent.evolution.ensure_main_branch.call_count == 1
    assert agent.contemplate.await_count == 1

    data = json.loads(mem_fs[lifecycle_path])
    assert data["iteration"] == 5
    assert data["next_phase"] is None


@pytest.mark.asyncio
async def test_merge_detection_skipped_when_resuming_past_it(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """If we resume at a phase past ``check_merged_plans``, that phase is
    not re-run on the resume iteration — it already ran before shutdown."""
    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)
    mem_fs[lifecycle_path] = json.dumps({
        "last_shutdown": "2026-04-05T10:00:00+00:00",
        "iteration": 3,
        "next_phase": "dream",
    })

    _stub_loop_externals(agent)
    dream = AsyncMock()
    agent._phase_runners["dream"] = dream

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999, dream_every=1,
    )

    agent.evolution.check_merged_plans.assert_not_called()
    dream.assert_awaited()


@pytest.mark.asyncio
async def test_merge_detection_runs_on_first_iteration_after_resume(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
):
    """Resuming at a phase before ``check_merged_plans`` still triggers
    merge detection on that resumed iteration (it's the first after
    restart)."""
    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)
    mem_fs[lifecycle_path] = json.dumps({
        "last_shutdown": "2026-04-05T10:00:00+00:00",
        "iteration": 3,
        "next_phase": "ensure_main_branch",
    })

    _stub_loop_externals(agent)

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    # ``check_merged_plans`` runs once for the first iteration after restart,
    # regardless of the absolute iteration number.
    assert agent.evolution.check_merged_plans.call_count == 1


@pytest.mark.asyncio
async def test_unknown_resume_phase_restarts_iteration(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """A lifecycle file naming a phase we no longer know about falls back
    to running the iteration from the beginning rather than crashing."""
    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)
    mem_fs[lifecycle_path] = json.dumps({
        "last_shutdown": "2026-04-05T10:00:00+00:00",
        "iteration": 2,
        "next_phase": "phase_that_no_longer_exists",
    })

    _stub_loop_externals(agent)

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    agent.evolution.ensure_main_branch.assert_called()
    agent.contemplate.assert_awaited()

    data = json.loads(mem_fs[lifecycle_path])
    # We re-ran iteration 2 from scratch, so the cursor still says 2 / done.
    assert data["iteration"] == 2
    assert data["next_phase"] is None


@pytest.mark.asyncio
async def test_persist_progress_failure_is_swallowed(make_agent):  # type: ignore[no-untyped-def]
    """Failing to write the lifecycle progress file must not break the loop
    — best-effort persistence is recoverable on the next phase boundary."""
    agent = make_agent()
    _stub_loop_externals(agent)

    with patch("maestro.agent.atomic_write", side_effect=OSError("disk full")):
        result = await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert result is True


@pytest.mark.asyncio
async def test_shutdown_event_stops_loop(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()

    call_count = 0

    async def counting_contemplate(**kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        # Set shutdown after first contemplation
        agent._ensure_shutdown_event().set()
        return "reflected"

    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    with patch.object(agent, "contemplate", side_effect=counting_contemplate):
        await agent.run_loop(interval=1, max_iters=0, evolve_every=100)

    assert call_count == 1
    assert agent.is_running is False

    # Shutdown journal entry should exist
    entries = agent.journal.read_recent_events()
    assert any(e.triggered_by == "shutdown" for e in entries)


@pytest.mark.asyncio
async def test_run_loop_installs_signal_handlers(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()

    installed_signals: list[signal.Signals] = []

    def tracking_install() -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            installed_signals.append(sig)
        agent._signal_handlers_installed = True
        # Set shutdown after install so loop exits (clear() runs before this)
        agent._ensure_shutdown_event().set()

    with patch.object(agent, "_install_signal_handlers", side_effect=tracking_install):
        await agent.run_loop(interval=1, max_iters=0, evolve_every=100)

    assert signal.SIGINT in installed_signals
    assert signal.SIGTERM in installed_signals


@pytest.mark.asyncio
async def test_shutdown_event_exits_sleep_early(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()

    async def contemplate_then_signal(**kwargs: object) -> str:
        # Schedule shutdown after a short delay
        asyncio.get_event_loop().call_later(0.05, agent._ensure_shutdown_event().set)
        return "reflected"

    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    with patch.object(agent, "contemplate", side_effect=contemplate_then_signal):
        start = time.monotonic()
        await agent.run_loop(interval=60, max_iters=0, evolve_every=100)
        elapsed = time.monotonic() - start

    # Should exit in well under 60 seconds
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_run_completes_remaining_steps_after_shutdown_event(make_agent):  # type: ignore[no-untyped-def]
    """084 (retiring 057): a shutdown event set mid-task no longer breaks the
    step-group loop — every remaining step still runs and the task records a
    normal outcome. ``run_loop`` stops only at the next phase boundary."""
    agent = make_agent()

    plan_response = json.dumps([
        {"description": "Step 1"},
        {"description": "Step 2"},
        {"description": "Step 3"},
    ])

    call_count = 0

    async def exec_then_signal(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            agent._ensure_shutdown_event().set()
        return MockQueryResult(response="done")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=exec_then_signal), \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")

        record = await agent.run("Multi-step goal")

    # All 3 steps execute despite the shutdown event set during step 1 — the
    # task runs to completion rather than breaking mid-task.
    assert call_count == 3
    assert record.outcome == "success"


@pytest.mark.asyncio
async def test_run_respects_pre_set_shutdown_event(make_agent):  # type: ignore[no-untyped-def]
    """Regression: run() must not clear a pre-set shutdown event.

    084 (retiring 057): a pre-set event no longer skips the task — every step
    runs to completion — but run() must leave the event set so ``run_loop``'s
    phase-boundary check still sees it and stops after ``run_task``."""
    agent = make_agent()

    plan_response = json.dumps([
        {"description": "Step 1"},
        {"description": "Step 2"},
    ])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="done")
        mock_distill.return_value = MockQueryResult(response="findings")

        # Set shutdown BEFORE calling run() — simulates a signal arriving
        # between run_loop()'s shutdown check and the run() call.
        agent._ensure_shutdown_event().set()

        record = await agent.run("Runs to completion")

    # The task runs to completion despite the pre-set event.
    assert mock_exec.called
    assert record.outcome == "success"
    # run() must not clear the event — run_loop relies on it after the phase.
    assert agent._ensure_shutdown_event().is_set()


@pytest.mark.asyncio
async def test_pid_file_written_and_cleaned(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    agent = make_agent()
    pid_path = str(agent.config.pid_path)

    pid_written = False

    original_write = agent._write_pid_file

    def tracking_write() -> None:
        nonlocal pid_written
        original_write()
        pid_written = True
        # Set shutdown so loop exits after PID file is written
        agent._ensure_shutdown_event().set()

    with patch.object(agent, "_write_pid_file", side_effect=tracking_write):
        await agent.run_loop(interval=1, max_iters=0, evolve_every=100)

    assert pid_written
    # PID file should be cleaned up after shutdown
    assert pid_path not in mem_fs


# ──────────────────────────────────────────────────────────────────────
# CLI shutdown/kill tests
# ──────────────────────────────────────────────────────────────────────


def test_read_pid_no_file(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    assert _read_pid(config.pid_path) is None


def test_read_pid_valid(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    pid_path = str(config.pid_path)
    mem_fs[pid_path] = "42"
    assert _read_pid(config.pid_path) == 42


def test_read_pid_invalid_content(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    pid_path = str(config.pid_path)
    mem_fs[pid_path] = "not-a-number"
    assert _read_pid(config.pid_path) is None


def test_cmd_shutdown_no_process(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    args = argparse.Namespace(dashboard=False, all_processes=False)
    output = capture_stdout(cmd_shutdown, args, config)
    assert "No running Maestro loop process found" in output


def test_cmd_shutdown_sends_sigterm(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    pid_path = str(config.memory_dir / "maestro.pid")
    mem_fs[pid_path] = "12345"

    args = argparse.Namespace(dashboard=False, all_processes=False)
    with patch("os.kill") as mock_kill:
        output = capture_stdout(cmd_shutdown, args, config)

    mock_kill.assert_called_once_with(12345, signal.SIGTERM)
    assert "graceful shutdown signal" in output
    assert "12345" in output


def test_cmd_shutdown_stale_pid(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    pid_path = str(config.memory_dir / "maestro.pid")
    mem_fs[pid_path] = "99999"

    args = argparse.Namespace(dashboard=False, all_processes=False)
    with patch("os.kill", side_effect=ProcessLookupError):
        output = capture_stdout(cmd_shutdown, args, config)

    assert "stale PID file removed" in output


def test_cmd_kill_no_process(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    args = argparse.Namespace(dashboard=False, all_processes=False)
    output = capture_stdout(cmd_kill, args, config)
    assert "No running Maestro loop process found" in output


def test_cmd_kill_sends_sigkill(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    pid_path = str(config.memory_dir / "maestro.pid")
    mem_fs[pid_path] = "12345"

    args = argparse.Namespace(dashboard=False, all_processes=False)
    with patch("os.kill") as mock_kill:
        output = capture_stdout(cmd_kill, args, config)

    mock_kill.assert_called_once_with(12345, signal.SIGKILL)
    assert "Killed" in output
    assert "12345" in output
    # PID file should be removed
    assert pid_path not in mem_fs


def test_cmd_kill_stale_pid(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    pid_path = str(config.memory_dir / "maestro.pid")
    mem_fs[pid_path] = "99999"

    args = argparse.Namespace(dashboard=False, all_processes=False)
    with patch("os.kill", side_effect=ProcessLookupError):
        output = capture_stdout(cmd_kill, args, config)

    assert "stale PID file removed" in output
    # PID file should be removed
    assert pid_path not in mem_fs


# ──────────────────────────────────────────────────────────────────────
# Dashboard PID and --dashboard/--all flag tests
# ──────────────────────────────────────────────────────────────────────


def test_dashboard_pid_path_is_separate(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    assert config.dashboard_pid_path == FAKE_DIR / "maestro-dashboard.pid"
    assert config.dashboard_pid_path != config.pid_path


def test_cmd_shutdown_dashboard(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    pid_path = str(config.dashboard_pid_path)
    mem_fs[pid_path] = "54321"

    args = argparse.Namespace(dashboard=True, all_processes=False)
    with patch("os.kill") as mock_kill:
        output = capture_stdout(cmd_shutdown, args, config)

    mock_kill.assert_called_once_with(54321, signal.SIGTERM)
    assert "dashboard" in output
    assert "54321" in output


def test_cmd_shutdown_all(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    mem_fs[str(config.pid_path)] = "111"
    mem_fs[str(config.dashboard_pid_path)] = "222"

    args = argparse.Namespace(dashboard=False, all_processes=True)
    with patch("os.kill") as mock_kill:
        output = capture_stdout(cmd_shutdown, args, config)

    assert mock_kill.call_count == 2
    mock_kill.assert_any_call(111, signal.SIGTERM)
    mock_kill.assert_any_call(222, signal.SIGTERM)
    assert "loop" in output
    assert "dashboard" in output


def test_cmd_shutdown_all_no_processes(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    args = argparse.Namespace(dashboard=False, all_processes=True)
    output = capture_stdout(cmd_shutdown, args, config)
    assert "No running Maestro loop process found" in output
    assert "No running Maestro dashboard process found" in output


def test_cmd_kill_dashboard(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    pid_path = str(config.dashboard_pid_path)
    mem_fs[pid_path] = "54321"

    args = argparse.Namespace(dashboard=True, all_processes=False)
    with patch("os.kill") as mock_kill:
        output = capture_stdout(cmd_kill, args, config)

    mock_kill.assert_called_once_with(54321, signal.SIGKILL)
    assert "dashboard" in output
    assert pid_path not in mem_fs


def test_cmd_kill_all(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    mem_fs[str(config.pid_path)] = "111"
    mem_fs[str(config.dashboard_pid_path)] = "222"

    args = argparse.Namespace(dashboard=False, all_processes=True)
    with patch("os.kill") as mock_kill:
        capture_stdout(cmd_kill, args, config)

    assert mock_kill.call_count == 2
    mock_kill.assert_any_call(111, signal.SIGKILL)
    mock_kill.assert_any_call(222, signal.SIGKILL)
    # Both PID files cleaned up
    assert str(config.pid_path) not in mem_fs
    assert str(config.dashboard_pid_path) not in mem_fs


def test_cmd_serve_writes_and_cleans_pid(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    pid_path = str(config.dashboard_pid_path)
    pid_seen = False

    def check_pid_exists(*args: object, **kwargs: object) -> None:
        nonlocal pid_seen
        pid_seen = pid_path in mem_fs

    args = argparse.Namespace(port=None)
    with patch("maestro.cli._build_agent"), \
         patch("maestro.dashboard.app.create_app"), \
         patch("uvicorn.run", side_effect=check_pid_exists):
        cmd_serve(args, config)

    assert pid_seen, "PID file should exist while uvicorn is running"
    # After uvicorn.run returns, PID file should be cleaned up
    assert pid_path not in mem_fs


def test_cmd_serve_cleans_pid_on_exception(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    pid_path = str(config.dashboard_pid_path)

    args = argparse.Namespace(port=None)
    with patch("maestro.cli._build_agent"), \
         patch("maestro.dashboard.app.create_app"), \
         patch("uvicorn.run", side_effect=RuntimeError("crash")), \
         pytest.raises(RuntimeError):
        cmd_serve(args, config)

    assert pid_path not in mem_fs


def _make_plan(plan_id: str, title: str = "Some title") -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale="Test rationale",
        proposed_changes=["change one"],
        files_affected=["maestro/memory.py"],
        expected_impact="faster",
    )


# ──────────────────────────────────────────────────────────────────────
# implement_plan — CI verification and error handling
# ──────────────────────────────────────────────────────────────────────


def _sub_side_effect_factory(
    *, porcelain_output: str = " M maestro/agent.py\n", pr_url: str = "https://github.com/o/r/pull/42\n",
    clean_after_commit: bool = True,
) -> object:
    """Return a subprocess side_effect that handles common git/gh commands.

    The tree reads dirty until a ``git commit`` lands, then clean, so
    ``create_pr``'s pre-push dirty-tree refusal (#4067) sees the committed tree.
    """
    committed = [False]

    def sub_side_effect(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["git", "commit"]:
            committed[0] = clean_after_commit
        if cmd[:2] == ["git", "status"] and "--porcelain" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="" if committed[0] else porcelain_output, stderr="",
            )
        if cmd[0] == "gh":
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=pr_url, stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="main\n", stderr="")
    return sub_side_effect


def _make_approved_plan(plan_id: str, title: str) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale=f"Testing {title}",
        proposed_changes=["Change X"],
        files_affected=["maestro/agent.py"],
        expected_impact="Improvement",
        priority="capability",
        status="approved",
        created_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_resume_cursor_synced_before_first_persist(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """If shutdown fires between _read_lifecycle_progress and the first
    _persist_progress call, the resume cursor should be preserved, not
    reverted to (0, None)."""
    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)

    # --- Mid-iteration resume: iteration=5, next_phase='dream' ---
    mem_fs[lifecycle_path] = json.dumps({
        "last_shutdown": "2026-04-05T10:00:00+00:00",
        "iteration": 5,
        "next_phase": "dream",
    })

    _last_shutdown, resume_iter, resume_phase, _exit_code, _crashed, *_ = (
        agent._read_lifecycle_progress()
    )
    assert resume_iter == 5
    assert resume_phase == "dream"

    from maestro.agent import _LOOP_PHASES

    # Simulate the resume cursor logic from run_loop
    if resume_phase is not None and resume_iter > 0:
        start_phase_index = _LOOP_PHASES.index(resume_phase)
        iteration = resume_iter
    else:
        iteration = resume_iter
        start_phase_index = 0

    # Simulate the fix: sync instance vars (same code now in run_loop)
    agent._resume_iteration = iteration
    agent._resume_phase = _LOOP_PHASES[start_phase_index] if start_phase_index > 0 else None

    # After the fix, instance vars must reflect the loaded cursor
    assert agent._resume_iteration == 5
    assert agent._resume_phase == "dream"

    # --- Clean iteration boundary: iteration=3, next_phase=None ---
    agent._resume_iteration = 0
    agent._resume_phase = None
    mem_fs[lifecycle_path] = json.dumps({
        "last_shutdown": "2026-04-05T10:00:00+00:00",
        "iteration": 3,
        "next_phase": None,
    })

    _, resume_iter2, resume_phase2, *_ = agent._read_lifecycle_progress()
    assert resume_iter2 == 3
    assert resume_phase2 is None

    iteration2 = resume_iter2
    start_phase_index2 = 0
    agent._resume_iteration = iteration2
    agent._resume_phase = _LOOP_PHASES[start_phase_index2] if start_phase_index2 > 0 else None

    assert agent._resume_iteration == 3
    assert agent._resume_phase is None


# ──────────────────────────────────────────────────────────────────────
# Resume-in-place: ephemeral state persistence on lifecycle.json
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_progress_writes_ephemeral_state(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """_persist_progress serializes message_context, reflection, iteration
    usage, and session ids into lifecycle.json so a mid-iteration crash
    can restore the same context on restart."""
    from maestro.sdk import IterationUsage

    agent = make_agent()
    agent._iter_message_context = "hi from thomas"
    agent._iter_reply_context = "reply from thomas"
    agent._iter_reflection = "reflecting on progress"
    agent._iteration_usage = IterationUsage(
        iteration=3,
        started_at="2026-04-16T10:00:00+00:00",
        calls=2, input_tokens=500, output_tokens=250,
    )
    agent._iter_session_a_id = "ses_a_1"
    agent._iter_session_b_id = "ses_b_1"

    agent._persist_progress(iteration=3, next_phase="contemplate")

    data = json.loads(mem_fs[str(agent.config.lifecycle_path)])
    assert data["iteration"] == 3
    assert data["next_phase"] == "contemplate"
    assert data["message_context"] == "hi from thomas"
    assert data["reply_context"] == "reply from thomas"
    assert data["reflection"] == "reflecting on progress"
    assert data["iteration_usage"]["iteration"] == 3
    assert data["iteration_usage"]["calls"] == 2
    assert data["session_a_id"] == "ses_a_1"
    assert data["session_b_id"] == "ses_b_1"


@pytest.mark.asyncio
async def test_read_lifecycle_progress_restores_ephemeral_state(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """_read_lifecycle_progress round-trips the ephemeral state back."""
    from maestro.sdk import IterationUsage

    agent = make_agent()
    agent._iter_message_context = "restored msg"
    agent._iter_reply_context = "restored reply"
    agent._iter_reflection = "restored reflection"
    agent._iteration_usage = IterationUsage(
        iteration=7, started_at="2026-04-16T10:00:00+00:00", calls=1,
    )
    agent._iter_session_a_id = "ses_restored_a"
    agent._iter_session_b_id = "ses_restored_b"
    agent._persist_progress(iteration=7, next_phase="evolve")

    (
        _last, it, ph, _exit_code, crashed,
        msg_ctx, refl, usage, sid_a, sid_b, *_,
    ) = agent._read_lifecycle_progress()
    assert it == 7
    assert ph == "evolve"
    assert crashed is True  # no last_shutdown written
    assert msg_ctx == "restored msg"
    assert refl == "restored reflection"
    assert usage is not None
    assert usage.iteration == 7
    assert usage.calls == 1
    assert sid_a == "ses_restored_a"
    assert sid_b == "ses_restored_b"
    assert agent._lifecycle.read_reply_context() == "restored reply"


@pytest.mark.asyncio
async def test_read_lifecycle_progress_legacy_file_reply_context_falls_back(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A lifecycle.json written before ``reply_context`` existed restores the
    reply context from ``message_context`` (today's behaviour), so an
    in-flight upgrade never drops a real reply."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "iteration": 3,
        "next_phase": "respond_to_messages",
        "message_context": "legacy msg",
    })

    assert agent._lifecycle.read_reply_context() == "legacy msg"


@pytest.mark.parametrize("raw", [None, "{not json", "[1, 2]"])
def test_read_reply_context_missing_corrupt_or_non_dict_is_empty(  # type: ignore[no-untyped-def]
    make_agent, mem_fs, raw,
) -> None:
    agent = make_agent()
    if raw is not None:
        mem_fs[str(agent.config.lifecycle_path)] = raw

    assert agent._lifecycle.read_reply_context() == ""


def test_read_reply_context_non_str_legacy_is_empty(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({"message_context": 5})

    assert agent._lifecycle.read_reply_context() == ""


@pytest.mark.asyncio
async def test_read_lifecycle_progress_preserves_file_on_disk(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """The read does not delete the file. If this startup itself crashes
    mid-phase before the first ``_persist_progress`` phase-boundary write,
    the prior cursor must still be on disk so the next startup can resume
    with the correct iteration and trigger crash detection."""
    agent = make_agent()
    payload = json.dumps({
        "iteration": 20,
        "next_phase": "evolve",
        "message_context": "prior msg",
        "reflection": "prior refl",
        "iteration_usage": None,
        "session_a_id": None,
        "session_b_id": None,
    })
    lifecycle_path = str(agent.config.lifecycle_path)
    mem_fs[lifecycle_path] = payload

    _last, it, ph, _exit_code, crashed, *_ = agent._read_lifecycle_progress()

    assert it == 20
    assert ph == "evolve"
    assert crashed is True  # no last_shutdown
    assert lifecycle_path in mem_fs
    assert mem_fs[lifecycle_path] == payload


@pytest.mark.asyncio
async def test_iteration_counter_survives_mid_phase_crash(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """When a phase crashes before any phase-boundary ``_persist_progress``,
    the on-disk cursor from the early-persist call (top of iteration) must
    still carry the bumped iteration, and a fresh read must report
    ``crashed=True`` at that same iteration."""
    agent = make_agent()
    _stub_loop_externals(agent)
    agent.evolution.ensure_main_branch = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("phase 0 boom"),
    )

    with pytest.raises(RuntimeError, match="phase 0 boom"):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    lifecycle_path = str(agent.config.lifecycle_path)
    assert lifecycle_path in mem_fs
    data = json.loads(mem_fs[lifecycle_path])
    assert data["iteration"] == 1
    assert data["next_phase"] == "ensure_main_branch"
    assert "last_shutdown" not in data

    fresh_agent = make_agent()
    _last, it, ph, _exit_code, crashed, *_ = (
        fresh_agent._read_lifecycle_progress()
    )
    assert it == 1
    assert ph == "ensure_main_branch"
    assert crashed is True


@pytest.mark.asyncio
async def test_early_persist_after_iteration_bump(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Fresh iterations persist the bumped cursor before the first phase
    runs, so a crash inside that phase cannot reset the counter."""
    from maestro.agent import _LOOP_PHASES

    agent = make_agent()
    _stub_loop_externals(agent)
    calls: list[tuple[int, str | None]] = []
    original = agent._persist_progress

    def recording(iteration: int, next_phase: str | None) -> None:
        calls.append((iteration, next_phase))
        original(iteration, next_phase)

    agent._persist_progress = recording  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    assert calls, "expected at least one _persist_progress call"
    assert calls[0] == (1, _LOOP_PHASES[0])


@pytest.mark.asyncio
async def test_read_lifecycle_progress_drops_mismatched_iteration_usage(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """When iteration_usage.iteration != cursor iteration, drop the usage."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "iteration": 10,
        "next_phase": "contemplate",
        "iteration_usage": {
            "iteration": 9,  # stale — does not match cursor
            "started_at": "2026-04-16T10:00:00+00:00",
            "calls": 1,
        },
    })

    _last, it, _ph, _exit_code, _crashed, _m, _r, usage, _sa, _sb, *_ = (
        agent._read_lifecycle_progress()
    )
    assert it == 10
    assert usage is None


@pytest.mark.asyncio
async def test_read_lifecycle_progress_old_usage_missing_task_id(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Old lifecycle.json with iteration_usage that predates the task_id field.

    ``from_dict`` must tolerate the missing key and default to ``None``
    so a crash-restart against a pre-task_id lifecycle file does not raise.
    """
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "iteration": 8,
        "next_phase": "generate_task",
        "iteration_usage": {
            "iteration": 8,
            "started_at": "2026-04-20T00:00:00Z",
            "calls": 5,
            "input_tokens": 1000,
            "output_tokens": 400,
            "cache_read_tokens": 200,
            "cache_creation_tokens": 50,
            "total_cost_usd": 0.3,
            "per_caller": {"contemplate": 3, "execute_step": 2},
            "total_turns": 6,
            "per_caller_turns": {"contemplate": 3, "execute_step": 3},
            # No "task_id" key — simulates old schema.
        },
    })

    _last, it, _ph, _exit_code, _crashed, _m, _r, usage, *_ = (
        agent._read_lifecycle_progress()
    )
    assert it == 8
    assert usage is not None
    assert usage.iteration == 8
    assert usage.calls == 5
    assert usage.task_id is None


@pytest.mark.asyncio
async def test_lifecycle_round_trip_preserves_task_id(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """task_id survives a persist → read lifecycle round-trip."""
    from maestro.sdk import IterationUsage

    agent = make_agent()
    agent._iteration_usage = IterationUsage(
        iteration=9, started_at="2026-05-12T00:00:00Z",
        calls=1, task_id="abcdef012345",
    )
    agent._persist_progress(iteration=9, next_phase="evolve")

    _last, _it, _ph, _exit_code, _crashed, _m, _r, usage, *_ = (
        agent._read_lifecycle_progress()
    )
    assert usage is not None
    assert usage.task_id == "abcdef012345"


@pytest.mark.asyncio
async def test_read_lifecycle_progress_ignores_non_string_session_ids(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Corrupt session_id values (non-string) are ignored."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "iteration": 2,
        "next_phase": "contemplate",
        "session_a_id": 12345,  # not a string
        "session_b_id": None,
    })

    _last, _it, _ph, _exit_code, _crashed, _m, _r, _u, sid_a, sid_b, *_ = (
        agent._read_lifecycle_progress()
    )
    assert sid_a is None
    assert sid_b is None


@pytest.mark.asyncio
async def test_write_lifecycle_shutdown_includes_ephemeral_state(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Shutdown mirrors the same ephemeral state fields for graceful stops."""
    from maestro.sdk import IterationUsage

    agent = make_agent()
    agent._resume_iteration = 4
    agent._resume_phase = "respond_to_messages"
    agent._iter_message_context = "shutdown ctx"
    agent._iter_reply_context = "shutdown reply"
    agent._iter_reflection = "shutdown refl"
    agent._iteration_usage = IterationUsage(
        iteration=4, started_at="2026-04-16T10:00:00+00:00",
    )
    agent._iter_session_a_id = "ses_shutdown_a"
    agent._iter_session_b_id = "ses_shutdown_b"

    agent._write_lifecycle_shutdown()

    data = json.loads(mem_fs[str(agent.config.lifecycle_path)])
    assert data["iteration"] == 4
    assert data["next_phase"] == "respond_to_messages"
    assert data["message_context"] == "shutdown ctx"
    assert data["reply_context"] == "shutdown reply"
    assert data["reflection"] == "shutdown refl"
    assert data["iteration_usage"]["iteration"] == 4
    assert data["session_a_id"] == "ses_shutdown_a"
    assert data["session_b_id"] == "ses_shutdown_b"
    assert "last_shutdown" in data


@pytest.mark.asyncio
async def test_persist_progress_truncates_oversized_ephemeral_state(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Oversized context/reflection are capped to the configured limits."""
    from maestro.agent import MaestroAgent

    agent = make_agent()
    agent._iter_message_context = "x" * 10000
    agent._iter_reply_context = "z" * 10000
    agent._iter_reflection = "y" * 10000

    agent._persist_progress(iteration=1, next_phase="contemplate")

    data = json.loads(mem_fs[str(agent.config.lifecycle_path)])
    assert len(data["message_context"]) == (
        MaestroAgent._MAX_PERSISTED_MESSAGE_CONTEXT_CHARS
    )
    assert len(data["reply_context"]) == (
        MaestroAgent._MAX_PERSISTED_MESSAGE_CONTEXT_CHARS
    )
    assert len(data["reflection"]) == (
        MaestroAgent._MAX_PERSISTED_REFLECTION_CHARS
    )


def test_persisted_message_context_cap_covers_producer_bound() -> None:
    """Resume restores all a failed pass saw: persisted cap >= producer bound."""
    from maestro.agent import MaestroAgent
    from maestro.messaging import MAX_MESSAGE_CONTEXT_CHARS

    assert MaestroAgent._MAX_PERSISTED_MESSAGE_CONTEXT_CHARS >= (
        MAX_MESSAGE_CONTEXT_CHARS
    )


@pytest.mark.asyncio
async def test_persist_progress_writes_current_phase(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """``current_phase`` in lifecycle.json mirrors ``self._current_phase``.

    This is the cross-container signal: the dashboard container reads the
    phase from disk, so the loop must write it here.
    """
    agent = make_agent()

    agent._current_phase = "evolve"
    agent._persist_progress(iteration=2, next_phase="evolve")
    data = json.loads(mem_fs[str(agent.config.lifecycle_path)])
    assert data["current_phase"] == "evolve"

    # Budget-throttle override must round-trip identically.
    agent._current_phase = "budget_throttle"
    agent._persist_progress(iteration=2, next_phase="contemplate")
    data = json.loads(mem_fs[str(agent.config.lifecycle_path)])
    assert data["current_phase"] == "budget_throttle"

    # None means idle between iterations.
    agent._current_phase = None
    agent._persist_progress(iteration=2, next_phase=None)
    data = json.loads(mem_fs[str(agent.config.lifecycle_path)])
    assert data["current_phase"] is None


@pytest.mark.asyncio
async def test_write_lifecycle_shutdown_writes_current_phase_none(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Shutdown means no phase is running — lifecycle.json must say so."""
    agent = make_agent()
    # Even if the in-memory attribute somehow carried a stale value,
    # shutdown must publish ``current_phase=None`` to disk.
    agent._current_phase = "evolve"
    agent._write_lifecycle_shutdown()
    data = json.loads(mem_fs[str(agent.config.lifecycle_path)])
    assert data["current_phase"] is None


@pytest.mark.asyncio
async def test_run_loop_persists_live_phase_before_body(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """During each phase's body, lifecycle.json must name the executing phase.

    Without this guarantee the dashboard's visualizer would always show the
    phase that *previously* completed, not the one currently running. We
    patch ``contemplate`` to snapshot the on-disk payload *while* the phase
    is executing — the only moment the dashboard would read during that
    phase.
    """
    from maestro.agent import MaestroAgent

    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)
    snapshots: list[dict] = []

    async def snap_during_contemplate(*_args, **_kwargs) -> str:
        snapshots.append(json.loads(mem_fs[lifecycle_path]))
        return "reflection"

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(side_effect=snap_during_contemplate)  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    assert snapshots, "contemplate was never invoked"
    assert snapshots[-1]["current_phase"] == "contemplate"
    assert isinstance(agent, MaestroAgent)  # sanity: right fixture


@pytest.mark.asyncio
async def test_run_loop_clears_current_phase_on_disk_between_iterations(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """After a clean iteration, on-disk ``current_phase`` is None (idle)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    data = json.loads(mem_fs[str(agent.config.lifecycle_path)])
    assert data["current_phase"] is None


@pytest.mark.asyncio
async def test_run_loop_writes_budget_throttle_override_during_sleep(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """During throttled inter-iteration sleep, on-disk ``current_phase`` is ``budget_throttle``.

    Phases themselves never report ``budget_throttle`` (they run at their
    normal names); only the stretched sleep between iterations does. The
    throttle multiplier is mocked high so the sleep block takes the override
    path, and ``_persist_progress`` is wrapped to capture which phase was
    live when each lifecycle.json write landed.

    The write is display-only: it must not reopen the finished iteration's
    resume cursor (#4148). Setting ``next_phase`` to the last phase would make
    a hard kill during the sleep resume iteration N at ``dream`` and skip
    ``check_merged_plans``.
    """
    agent = make_agent()
    writes: list[tuple[str | None, str | None]] = []
    real_persist = agent._persist_progress

    # Trigger shutdown the first time the sleep-block override fires. That
    # keeps iteration 1's body running to completion (no phase-level
    # ``stop_for_shutdown``), then the chunked sleep observes the set event
    # on its first ``asyncio.wait_for`` and exits promptly — the outer
    # ``while`` loop breaks on its next shutdown check.
    def capture_persist(iteration: int, next_phase: str | None) -> None:
        writes.append((agent._current_phase, next_phase))
        if agent._current_phase == "budget_throttle":
            agent._ensure_shutdown_event().set()
        real_persist(iteration, next_phase)

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    # Drive the sleep block into the throttle override path.
    agent._compute_budget_throttle_multiplier = MagicMock(  # type: ignore[method-assign]
        return_value=(1.0, 5.0, None),
    )
    agent._maybe_notify_throttle_transition = AsyncMock()  # type: ignore[method-assign]
    agent._persist_progress = capture_persist  # type: ignore[method-assign]

    await agent.run_loop(
        interval=1, max_iters=0, task_every=0, evolve_every=1,
    )

    assert ("budget_throttle", None) in writes
    assert agent._resume_phase is None
    # ``shutdown`` is mocked, so lifecycle.json holds the state a hard kill
    # during the throttled sleep would leave behind.
    data = json.loads(mem_fs[str(agent.config.lifecycle_path)])
    assert data["next_phase"] is None
    assert data["current_phase"] == "budget_throttle"


@pytest.mark.asyncio
async def test_get_or_create_session_disabled_returns_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When session reuse is disabled, helper returns None without creating."""
    agent = make_agent()
    agent.config.enable_session_reuse = False  # explicit pin: prod default True; see make_agent fixture

    result = await agent._get_or_create_session(
        slot="a", existing=None, options=MagicMock(), system="sys",
    )

    assert result is None


@pytest.mark.asyncio
async def test_get_or_create_session_returns_existing_client(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Helper returns the existing client when one is already provided."""
    agent = make_agent()
    agent.config.enable_session_reuse = True
    existing = MagicMock()

    result = await agent._get_or_create_session(
        slot="a", existing=existing, options=MagicMock(), system="sys",
    )

    assert result is existing


@pytest.mark.asyncio
async def test_get_or_create_session_clears_saved_id_on_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """If ``create_session`` raises even on fresh path, the saved id is cleared."""
    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent._iter_session_a_id = "ses_stale"

    from maestro.sdk import ErrorCategory, SDKError

    async def failing_create(**kwargs: object) -> object:
        raise SDKError("boom", category=ErrorCategory.CLI_CRASH)

    with patch("maestro.agent.create_session", side_effect=failing_create), pytest.raises(SDKError):
        await agent._get_or_create_session(
            slot="a", existing=None, options=MagicMock(), system="sys",
        )

    assert agent._iter_session_a_id is None


def test_slot_session_id_unknown_slot_raises(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Unknown slot name raises ValueError."""
    agent = make_agent()
    with pytest.raises(ValueError, match="unknown session slot"):
        agent._slot_session_id("c")


def test_set_slot_session_id_unknown_slot_raises(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Unknown slot name raises ValueError on set."""
    agent = make_agent()
    with pytest.raises(ValueError, match="unknown session slot"):
        agent._set_slot_session_id("c", "ses_x")


def test_capture_session_id_stores_string(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A real string session_id on the client is mirrored into the slot."""
    agent = make_agent()
    client = MagicMock()
    client._maestro_last_session_id = "ses_real_abc"

    agent._capture_session_id("b", client)

    assert agent._iter_session_b_id == "ses_real_abc"


def test_read_lifecycle_progress_iteration_usage_from_dict_raises(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """If ``IterationUsage.from_dict`` raises, usage is dropped and cursor kept."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "iteration": 7,
        "next_phase": "contemplate",
        "iteration_usage": {"iteration": 7},
    })

    with patch(
        "maestro.agent.IterationUsage.from_dict",
        side_effect=RuntimeError("corrupt"),
    ):
        _last, it, ph, _exit_code, _crashed, _m, _r, usage, _sa, _sb, *_ = (
            agent._read_lifecycle_progress()
        )

    assert it == 7
    assert ph == "contemplate"
    assert usage is None


def test_read_lifecycle_progress_non_string_session_b_warned(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Non-string session_b_id is dropped with a warning."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "iteration": 3,
        "next_phase": "evolve",
        "session_b_id": 4242,
    })

    _last, _it, _ph, _exit_code, _crashed, _m, _r, _u, _sa, sb, *_ = (
        agent._read_lifecycle_progress()
    )

    assert sb is None


def test_read_lifecycle_progress_non_dict_iteration_usage_warned(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Non-dict iteration_usage is logged and dropped; cursor is preserved."""
    agent = make_agent()
    mem_fs[str(agent.config.lifecycle_path)] = json.dumps({
        "iteration": 3,
        "next_phase": "evolve",
        "iteration_usage": "not-a-dict",
    })

    _last, it, ph, _exit_code, _crashed, _m, _r, usage, _sa, _sb, *_ = (
        agent._read_lifecycle_progress()
    )

    assert it == 3
    assert ph == "evolve"
    assert usage is None


# ──────────────────────────────────────────────────────────────────────
# Regression: #1719 — residual CancelledError in inter-iteration sleep
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_loop_survives_residual_cancelled_error_in_sleep(
    make_agent, caplog,  # type: ignore[no-untyped-def]
) -> None:
    """Regression for #1719: a residual CancelledError from a prior SDK
    session's anyio teardown orphan fires into the inter-iteration sleep.
    Before the fix, this propagated through ``run_loop`` and crashed the
    loop. After the fix, ``_safe_shutdown_wait`` absorbs the orphan and
    the loop continues to the next iteration.

    Strategy: patch ``asyncio.wait_for`` so the first call *inside*
    ``_safe_shutdown_wait`` raises ``CancelledError`` (simulating the
    leaked orphan). The helper's containment logic catches it (shutdown
    event is NOT set), logs a warning, and returns ``False`` — the loop
    survives and runs a second iteration.
    """
    agent = make_agent()
    _stub_loop_externals(agent)

    iterations_completed: list[int] = []
    inject_count = 0
    _real_wait_for = asyncio.wait_for

    async def _wait_for_with_injection(
        coro: object, *, timeout: float | None = None,
    ) -> object:
        """Raise CancelledError on the first call to simulate the
        orphan; delegate to the real ``asyncio.wait_for`` afterwards.
        """
        nonlocal inject_count
        inject_count += 1
        if inject_count == 1:
            # Ensure we don't leak the coroutine
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[union-attr]
            raise asyncio.CancelledError
        return await _real_wait_for(coro, timeout=timeout)  # type: ignore[arg-type]

    # Track completed iterations via contemplate side-effect
    async def tracking_contemplate(*a: object, **kw: object) -> str:
        iterations_completed.append(len(iterations_completed) + 1)
        return "reflection"

    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=tracking_contemplate,
    )

    with patch("asyncio.wait_for", side_effect=_wait_for_with_injection), \
         caplog.at_level("WARNING", logger="maestro.agent"):
        result = await agent.run_loop(
            interval=1, max_iters=2, task_every=0, evolve_every=999,
        )

    assert result is True
    # Both iterations should complete — the CancelledError in the first
    # inter-iteration sleep was absorbed, not propagated.
    assert len(iterations_completed) == 2
    assert any(
        "Residual CancelledError" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_tend_pr_count_preserved_on_transient_tend_error(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A transient tend failure must not zero ``_open_evolution_pr_count``.

    Regression: ``tend_evolution_prs`` has four early-return paths that
    return ``TendPRsResult(tended=0, errors=[...])`` without ever
    enumerating upstream PRs (fetch failure, local-main tamper, list-PR
    failure).  Previously the call site overwrote the at-cap count with
    that transient 0, which then opened the ``max_open_evolution_prs``
    gate on the very next ``_implement_approved_plans`` and silently
    bypassed integrate-before-create.
    """
    from maestro.evolution_tend import TendPRsResult

    agent = make_agent()
    agent.config.max_open_evolution_prs = 5
    # Pre-set the count above the cap, as if a previous iteration had
    # enumerated 5 open evolution PRs upstream.
    agent._open_evolution_pr_count = 5

    # Simulate a transient ``git fetch origin`` failure — tend returns
    # tended=0 with a single fetch-error entry.
    transient_result = TendPRsResult()
    transient_result.errors.append("fetch origin failed: connection reset")

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.tend_evolution_prs = AsyncMock(  # type: ignore[method-assign]
        return_value=transient_result,
    )
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    # The transient error must NOT have reset the at-cap count to 0;
    # the gate is preserved so the next implementation phase still
    # honours integrate-before-create.
    assert agent._open_evolution_pr_count == 5


@pytest.mark.asyncio
async def test_tend_pr_count_refreshed_on_clean_tend(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A clean tend pass (no errors) does refresh the open-PR count.

    Complements the transient-error test by proving the gate is not
    permanently sticky: an enumeration that succeeded with a smaller
    upstream PR count opens the gate as expected.
    """
    from maestro.evolution_tend import TendPRsResult

    agent = make_agent()
    agent.config.max_open_evolution_prs = 5
    agent._open_evolution_pr_count = 5  # stale at-cap value

    clean_result = TendPRsResult()
    clean_result.tended = 2  # upstream now has only 2 open PRs
    clean_result.enumerated = True  # listing succeeded

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.tend_evolution_prs = AsyncMock(  # type: ignore[method-assign]
        return_value=clean_result,
    )
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    assert agent._open_evolution_pr_count == 2


@pytest.mark.asyncio
async def test_tend_phase_drives_settlement_reconcile_for_gone_prs(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Proposal 103: the tend phase routes ``settled_pr_numbers`` (PRs that left
    the open set this lap) into the on-settlement record reconciler via
    ``_reconcile_settled_tend_prs``, which delegates each to the single flag-gated
    ``_reconcile_settled_pr_records`` helper. Spy that helper to prove the wiring
    fires from the tend-phase handler."""
    from maestro.evolution_tend import TendPRsResult

    agent = make_agent()

    tend_result = TendPRsResult()
    tend_result.enumerated = True
    tend_result.settled_pr_numbers = [8801]

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.tend_evolution_prs = AsyncMock(  # type: ignore[method-assign]
        return_value=tend_result,
    )
    # Empty merge check so the *only* reconcile helper call comes from the tend
    # face — not the merge/close-transition drive.
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    agent._reconcile_settled_pr_records = MagicMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    agent._reconcile_settled_pr_records.assert_called_once()
    assert agent._reconcile_settled_pr_records.call_args.args[0] == 8801


@pytest.mark.asyncio
async def test_tend_pr_count_refreshed_despite_per_branch_error(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A per-branch tend error must not discard a valid enumeration.

    Regression: ``result.tended`` increments for every open evolution PR
    *before* the per-branch work, and reminder / merge / push failures
    append to ``errors`` *after* counting — so a pass can return an
    authoritative count alongside a non-empty ``errors``.  Previously the
    call site skipped the refresh whenever ``errors`` was non-empty; a
    recurring per-branch failure (e.g. one persistently un-pushable
    branch) then pinned the count at a stale value indefinitely — a
    post-restart 0 silently disabled the ``max_open_evolution_prs``
    integrate-before-create gate.
    """
    from maestro.evolution_tend import TendPRsResult

    agent = make_agent()
    agent.config.max_open_evolution_prs = 5
    agent._open_evolution_pr_count = 0  # post-restart default

    partial_result = TendPRsResult()
    partial_result.enumerated = True  # listing succeeded: 6 open PRs
    partial_result.tended = 6
    partial_result.errors.append("reminder failed for plan ep1: telegram 502")

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.ensure_main_branch = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.tend_evolution_prs = AsyncMock(  # type: ignore[method-assign]
        return_value=partial_result,
    )
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    # The valid count must be adopted so the integrate-before-create gate
    # engages on the next implementation phase.
    assert agent._open_evolution_pr_count == 6

# NOTE: There is intentionally no separate `_GATE_DIFF_WIRED` constant. The
# wired-vs-unwired distinction is driven entirely by the `subprocess.run`
# mock (`_grep_has_match` vs `_grep_no_match`) because
# `_check_uncalled_symbol_gate` uses `git grep` to discover call sites
# rather than inspecting the diff body. Reusing `_GATE_DIFF_UNWIRED` keeps
# the source of the distinction unambiguous. Because the gate now batches one
# `git grep` for all symbols and attributes rows per symbol by substring,
# `_grep_has_match` returns one call-site row per symbol in `_GATE_DIFF_UNWIRED`.


def _grep_no_match(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    """Simulate git grep finding nothing (exit code 1)."""
    return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")


@pytest.mark.asyncio
async def test_process_messages_skips_transaction_when_every_candidate_dedups(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """An all-no-op drain must not re-write the self-model (#3716).

    The transaction re-runs cap enforcement and the archive append, so
    entering it for a drain the tail-dedup fully absorbs is pure churn.
    """
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["Same content"]
    agent.consciousness.save_self_model(model)
    agent.message_store.save_message(Message(
        message_id="m1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="Same content", message_type="instruction",
    ))

    with patch.object(agent.consciousness, "mutate_self_model") as mutate:
        await agent._process_messages()

    mutate.assert_not_called()
    assert agent.consciousness.load_self_model().pending_user_requests == [
        "Same content",
    ]


@pytest.mark.asyncio
async def test_process_messages_skips_transaction_when_every_candidate_already_recorded(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """A drain whose every candidate id is already recorded skips the write (#3716).

    ``m1`` was enqueued (and its id recorded) by an earlier drain that crashed
    before ``mark_read_many``; the head has since been popped, so the queue no
    longer holds it. The re-drain must recognise the recorded id and not enter
    the ``mutate_self_model`` transaction at all (#4602 / #4577).
    """
    from maestro.messaging import Message, MessageStore

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    model_path = agent.consciousness._model_path
    agent.consciousness.save_self_model(agent.consciousness.load_self_model())
    raw = json.loads(model_path.read_text(encoding="utf-8"))
    raw["pending_user_requests"] = []
    raw["enqueued_instruction_ids"] = ["m1"]
    model_path.write_text(json.dumps(raw), encoding="utf-8")
    agent.message_store.save_message(Message(
        message_id="m1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="A", message_type="instruction",
    ))

    with patch.object(agent.consciousness, "mutate_self_model") as mutate:
        await agent._process_messages()

    mutate.assert_not_called()
    assert agent.consciousness.load_self_model().pending_user_requests == []
