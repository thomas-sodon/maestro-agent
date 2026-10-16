"""Tests for MaestroAgent consecutive-failure counter persistence and the
failure-ceiling handoff — split out of tests/test_agent.py (#3875).
Fully mocked I/O."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import audit_checks, gate_environment
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
from maestro.loop import plan_holds
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner
from maestro.pr_facts import MergeTruth
from maestro.remediation_tracker import (
    TrackerState,
)
from maestro.sdk import ErrorCategory, SDKError
from maestro.vcs import DirtyWorkingTreeError
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


def _activation_tuples(
    mem_fs: dict[str, str], agent: MaestroAgent,
) -> list[tuple[str, str, str]]:
    """Read (gate, action, detail) tuples from gate_activations.jsonl."""
    raw = mem_fs.get(
        str(agent.config.memory_dir / "gate_activations.jsonl"), "",
    )
    return [
        (rec["gate"], rec["action"], rec["detail"])
        for rec in (json.loads(line) for line in raw.splitlines() if line.strip())
    ]


_FAIL_REVIEW = QualityGateResult(
    passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
    raw_output="Ruff: E501 line too long\nMypy: clean\nTests: clean",
)

# --- Consecutive-failure counter persistence across restarts ---


def test_persist_progress_writes_consecutive_failure_counters(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_persist_progress mirrors the four counters to lifecycle.json."""
    agent = make_agent()
    agent._consecutive_rate_limits = 3
    agent._consecutive_cli_crashes = 1
    agent._consecutive_stream_stalls = 0
    agent._consecutive_stale_sessions = 2

    agent._persist_progress(1, "contemplate")

    data = json.loads(agent.config.lifecycle_path.read_text())
    assert data["consecutive_rate_limits"] == 3
    assert data["consecutive_cli_crashes"] == 1
    assert data["consecutive_stream_stalls"] == 0
    assert data["consecutive_stale_sessions"] == 2


def test_write_lifecycle_shutdown_writes_consecutive_failure_counters(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_write_lifecycle_shutdown mirrors the four counters to lifecycle.json."""
    agent = make_agent()
    agent._consecutive_rate_limits = 2
    agent._consecutive_cli_crashes = 3
    agent._consecutive_stream_stalls = 1
    agent._consecutive_stale_sessions = 3

    agent._write_lifecycle_shutdown()

    data = json.loads(agent.config.lifecycle_path.read_text())
    assert data["consecutive_rate_limits"] == 2
    assert data["consecutive_cli_crashes"] == 3
    assert data["consecutive_stream_stalls"] == 1
    assert data["consecutive_stale_sessions"] == 3


def test_read_lifecycle_progress_restores_consecutive_failure_counters(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_read_lifecycle_progress returns the four counters at the tuple tail."""
    agent = make_agent()
    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 0,
        "next_phase": None,
        "consecutive_rate_limits": 3,
        "consecutive_cli_crashes": 1,
        "consecutive_stream_stalls": 0,
        "consecutive_stale_sessions": 2,
    }))

    result = agent._read_lifecycle_progress()

    # Four counters precede the trailing maintenance-milestone flag.
    assert result[-5:-1] == (3, 1, 0, 2)


def test_read_lifecycle_progress_legacy_file_without_counters(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A lifecycle.json missing the counter keys defaults to 0 with no warning."""
    agent = make_agent()
    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 0,
        "next_phase": None,
    }))

    result = agent._read_lifecycle_progress()

    assert result[-5:-1] == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_run_loop_restart_with_rate_limit_counter_at_ceiling_enters_idle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Counter at ceiling with no idle flag on disk triggers idle before first phase."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Seed lifecycle: counter at ceiling, idle flag NOT set. This is the
    # narrow race where the prior process crashed between the in-process
    # increment to the ceiling and the persisted idle-entry write.
    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 0,
        "next_phase": None,
        "consecutive_rate_limits": _MAX_CONSECUTIVE_RATE_LIMITS,
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

    # The startup safety net entered idle and the probe ran exactly once
    # before the normal iteration proceeded.
    assert probe_calls == 1
    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]
    # After probe success + clean iteration end, both the flag and the
    # counter are cleared.
    assert agent._rate_limit_idle_entered_at is None
    assert agent._consecutive_rate_limits == 0


@pytest.mark.asyncio
async def test_startup_honors_restored_idle_flag(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Restored ``rate_limit_idle_entered_at`` runs the idle probe before the startup probe."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Restored idle flag set, no crash.
    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 0,
        "next_phase": None,
        "rate_limit_idle_entered_at": "2026-04-22T12:00:00+00:00",
    }))

    call_log: list[str] = []

    async def fake_idle_probe() -> None:
        call_log.append("idle_probe")

    async def fake_exit() -> None:
        call_log.append("exit")
        agent._rate_limit_idle_entered_at = None

    agent._run_rate_limit_idle_probe = fake_idle_probe  # type: ignore[method-assign]
    agent._exit_rate_limit_idle = fake_exit  # type: ignore[method-assign]

    # The startup probe must NOT run when the idle path takes over.
    startup_probe_mock = AsyncMock()
    agent._probe_rate_limit = startup_probe_mock  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert call_log == ["idle_probe", "exit"]
    assert startup_probe_mock.call_count == 0
    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_startup_honors_restored_counter_threshold(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Restored counter at ceiling enters idle on startup, bypassing the probe."""
    from maestro.loop.iteration import _MAX_CONSECUTIVE_RATE_LIMITS

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 0,
        "next_phase": None,
        "consecutive_rate_limits": _MAX_CONSECUTIVE_RATE_LIMITS,
    }))

    call_log: list[str] = []

    async def fake_enter(iteration: int, phase: str) -> None:
        call_log.append(f"enter:{phase}")
        agent._rate_limit_idle_entered_at = "set"

    async def fake_idle_probe() -> None:
        call_log.append("idle_probe")

    async def fake_exit() -> None:
        call_log.append("exit")
        agent._rate_limit_idle_entered_at = None
        agent._consecutive_rate_limits = 0

    agent._enter_rate_limit_idle = fake_enter  # type: ignore[method-assign]
    agent._run_rate_limit_idle_probe = fake_idle_probe  # type: ignore[method-assign]
    agent._exit_rate_limit_idle = fake_exit  # type: ignore[method-assign]

    startup_probe_mock = AsyncMock()
    agent._probe_rate_limit = startup_probe_mock  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert call_log == ["enter:startup", "idle_probe", "exit"]
    assert startup_probe_mock.call_count == 0


@pytest.mark.asyncio
async def test_startup_skips_idle_path_when_smoke_mode(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Smoke mode bypasses the restored-idle handling so deploy validation stays deterministic."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 0,
        "next_phase": None,
        "rate_limit_idle_entered_at": "2026-04-22T12:00:00+00:00",
    }))

    idle_probe_mock = AsyncMock()
    agent._run_rate_limit_idle_probe = idle_probe_mock  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
            smoke=True,
        )

    assert idle_probe_mock.call_count == 0


@pytest.mark.asyncio
async def test_resume_iteration_synced_before_probe(make_agent) -> None:  # type: ignore[no-untyped-def]
    """self._resume_iteration is set from the lifecycle file before _probe_rate_limit() runs."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Write lifecycle file with non-zero iteration to distinguish from
    # the constructor default (0).
    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 42,
        "next_phase": None,
    }))

    captured_iteration: list[int] = []

    original_probe = agent._probe_rate_limit

    async def capturing_probe() -> None:
        captured_iteration.append(agent._resume_iteration)
        await original_probe()

    agent._probe_rate_limit = capturing_probe  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    assert captured_iteration == [42], (
        f"Expected _resume_iteration=42 at probe time, got {captured_iteration}"
    )


@pytest.mark.asyncio
async def test_startup_cooldown_on_recent_rate_limit_crash(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Crashed prior run with rate-limit category triggers configured cooldown."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # No last_shutdown → crashed=True. Plus rate-limit category in last_sdk_error.
    agent.config.lifecycle_path.write_text(json.dumps({
        "iteration": 2,
        "next_phase": "contemplate",
        "last_exit_code": 1,
        "last_sdk_error": {
            "message": "rate limited",
            "category": "rate_limit",
            "exit_code": 1,
            "stderr_head": None,
            "retries_attempted": 3,
        },
    }))
    agent.config.startup_rate_limit_cooldown_seconds = 60

    timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        timeouts.append(timeout)
        # Close any unawaited coroutine to avoid 'never awaited' warnings.
        if asyncio.iscoroutine(coro):
            coro.close()
        raise TimeoutError

    # Suppress crash reporting side-effects (it's tested separately).
    with patch("maestro.crash.handle_crash", AsyncMock()), \
         patch("maestro.crash.build_crash_event"), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # The cooldown timeout (60s) must appear among the wait_for calls.
    assert 60 in timeouts


@pytest.mark.asyncio
async def test_startup_cooldown_skipped_on_graceful_shutdown(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Same SDK error content but graceful shutdown means no cooldown."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 2,
        "next_phase": None,
        "last_sdk_error": {
            "message": "rate limited",
            "category": "rate_limit",
            "exit_code": 1,
            "stderr_head": None,
            "retries_attempted": 3,
        },
    }))
    agent.config.startup_rate_limit_cooldown_seconds = 60

    timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        timeouts.append(timeout)
        if asyncio.iscoroutine(coro):
            coro.close()
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # No 60-second cooldown wait should have fired.
    assert 60 not in timeouts


@pytest.mark.asyncio
async def test_startup_cooldown_disabled_when_zero(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``startup_rate_limit_cooldown_seconds = 0`` disables the cooldown gate."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.config.lifecycle_path.write_text(json.dumps({
        "iteration": 2,
        "next_phase": "contemplate",
        "last_sdk_error": {
            "message": "rate limited",
            "category": "rate_limit",
            "exit_code": 1,
            "stderr_head": None,
            "retries_attempted": 3,
        },
    }))
    agent.config.startup_rate_limit_cooldown_seconds = 0

    # Spy on the journal so we can prove the cooldown branch did not fire.
    journal_calls: list[dict[str, Any]] = []
    real_write = agent.journal.record_event

    def spy_write(**kwargs: Any) -> None:
        journal_calls.append(kwargs)
        real_write(**kwargs)

    agent.journal.record_event = MagicMock(side_effect=spy_write)  # type: ignore[method-assign]

    timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        timeouts.append(timeout)
        if asyncio.iscoroutine(coro):
            coro.close()
        raise TimeoutError

    with patch("maestro.crash.handle_crash", AsyncMock()), \
         patch("maestro.crash.build_crash_event"), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # The cooldown gate's first observable side effect is a milestone
    # journal write tagged ``triggered_by="startup_rate_limit_cooldown"``.
    # If the disable holds, that write must not have happened.
    cooldown_journal_calls = [
        c for c in journal_calls
        if c.get("triggered_by") == "startup_rate_limit_cooldown"
    ]
    assert cooldown_journal_calls == []


@pytest.mark.asyncio
async def test_startup_cooldown_journal_failure_swallowed(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Journal write failure during the cooldown gate is logged and swallowed."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    # Only the cooldown gate's milestone write fails — other journal
    # writers stay healthy.
    real_write = agent.journal.record_event

    def selective_write(**kwargs: Any) -> None:
        if kwargs.get("triggered_by") == "startup_rate_limit_cooldown":
            raise OSError("disk full")
        real_write(**kwargs)

    agent.journal.record_event = MagicMock(side_effect=selective_write)  # type: ignore[method-assign]

    agent.config.lifecycle_path.write_text(json.dumps({
        "iteration": 2,
        "next_phase": "contemplate",
        "last_sdk_error": {
            "message": "rate limited",
            "category": "rate_limit",
            "exit_code": 1,
            "stderr_head": None,
            "retries_attempted": 3,
        },
    }))
    agent.config.startup_rate_limit_cooldown_seconds = 60

    timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        timeouts.append(timeout)
        if asyncio.iscoroutine(coro):
            coro.close()
        raise TimeoutError

    with patch("maestro.crash.handle_crash", AsyncMock()), \
         patch("maestro.crash.build_crash_event"), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # The cooldown still ran even though the journal entry write failed.
    assert 60 in timeouts


@pytest.mark.asyncio
async def test_startup_cooldown_skipped_on_non_rate_limit_crash(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Crash with a non-rate-limit SDK category does not trigger the cooldown."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.config.lifecycle_path.write_text(json.dumps({
        "iteration": 2,
        "next_phase": "contemplate",
        "last_sdk_error": {
            "message": "cli crashed",
            "category": "cli_crash",
            "exit_code": 1,
            "stderr_head": None,
            "retries_attempted": 1,
        },
    }))
    agent.config.startup_rate_limit_cooldown_seconds = 60

    timeouts: list[float | None] = []

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        timeouts.append(timeout)
        if asyncio.iscoroutine(coro):
            coro.close()
        raise TimeoutError

    with patch("maestro.crash.handle_crash", AsyncMock()), \
         patch("maestro.crash.build_crash_event"), \
         patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert 60 not in timeouts


@pytest.mark.asyncio
async def test_run_loop_restart_clamps_cli_crash_and_stream_stall_counters(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Restart with CLI-crash / stream-stall / stale-session counters at ceiling clamps to ceiling-1."""
    from maestro.loop.iteration import (
        _MAX_CONSECUTIVE_CLI_CRASHES,
        _MAX_CONSECUTIVE_STALE_SESSIONS,
        _MAX_CONSECUTIVE_STREAM_STALLS,
    )

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 0,
        "next_phase": None,
        "consecutive_cli_crashes": _MAX_CONSECUTIVE_CLI_CRASHES,
        "consecutive_stream_stalls": _MAX_CONSECUTIVE_STREAM_STALLS,
        "consecutive_stale_sessions": _MAX_CONSECUTIVE_STALE_SESSIONS,
    }))

    captured: dict[str, int] = {}

    async def contemplate_side_effect(**kwargs: Any) -> str:
        # Capture the counters mid-iteration, before clean-iteration-end
        # clears them. The clamp ran in ``run_loop`` startup, so by the
        # time ``contemplate`` is called the values are already clamped.
        captured["cli"] = agent._consecutive_cli_crashes
        captured["stall"] = agent._consecutive_stream_stalls
        captured["stale"] = agent._consecutive_stale_sessions
        return "ok"

    agent.contemplate = AsyncMock(side_effect=contemplate_side_effect)  # type: ignore[method-assign]

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert captured["cli"] == _MAX_CONSECUTIVE_CLI_CRASHES - 1
    assert captured["stall"] == _MAX_CONSECUTIVE_STREAM_STALLS - 1
    assert captured["stale"] == _MAX_CONSECUTIVE_STALE_SESSIONS - 1


@pytest.mark.asyncio
async def test_run_loop_clean_iteration_persists_zero_counters(make_agent) -> None:  # type: ignore[no-untyped-def]
    """After a clean iteration, lifecycle.json reflects all four counters at 0."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # Seed non-zero counters so the restore path populates them before
    # the clean-iteration-end reset — proves the reset actually runs
    # rather than silently agreeing with a fresh-start default.
    agent.config.lifecycle_path.write_text(json.dumps({
        "last_shutdown": datetime.now(UTC).isoformat(),
        "iteration": 0,
        "next_phase": None,
        "consecutive_rate_limits": 3,
        "consecutive_cli_crashes": 1,
        "consecutive_stream_stalls": 2,
        "consecutive_stale_sessions": 2,
    }))

    async def fake_wait_for(coro: object, *, timeout: float | None = None) -> None:
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    data = json.loads(agent.config.lifecycle_path.read_text())
    assert data["consecutive_rate_limits"] == 0
    assert data["consecutive_cli_crashes"] == 0
    assert data["consecutive_stream_stalls"] == 0
    assert data["consecutive_stale_sessions"] == 0


# ---------------------------------------------------------------------------
# failed-vs-rejected distinction: quality-gate loop, retry eligibility,
# rejection-learning isolation, notification policy.
# ---------------------------------------------------------------------------


def _seed_failed_plan(
    agent: MaestroAgent,
    plan_id: str,
    title: str,
    *,
    failure_count: int = 1,
) -> None:
    """Seed an ``EvolutionPlan`` in ``failed`` status with the given counter."""
    from maestro.memory import EvolutionPlan

    plan = EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="failed",
        created_at="2025-01-01T00:00:00Z",
        failure_count=failure_count,
        last_failure_at="2025-01-01T00:00:00Z",
        last_failure_reason="prior failure",
        rejection_reason="prior failure",
    )
    agent.memory.save_evolution_plan(plan)


def _seed_approved_plan(agent: MaestroAgent, plan_id: str, title: str) -> None:
    from maestro.memory import EvolutionPlan

    plan = EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)


@pytest.mark.asyncio
async def test_failed_plan_not_processed_by_rejection_learning(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A 'failed' plan must not trigger rejection-learning — that pathway is
    reserved for plans a human actually rejected."""
    agent = make_agent()
    _seed_failed_plan(agent, "failed_plan", "Failed Plan")
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]

    await agent._process_rejected_plans()

    agent.evolution.learn_from_rejection.assert_not_called()


@pytest.mark.asyncio
async def test_failed_plan_does_not_block_cooldown(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A 'failed' plan must NOT activate submit_for_review's 7-day rejection
    cooldown. The cooldown is only for plans a human rejected."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    _seed_failed_plan(agent, "old_failed", "Duplicate Title")

    # Submit a new plan with the exact same title — must not be skipped.
    new_plan = EvolutionPlan(
        plan_id="new_plan",
        title="Duplicate Title",
        category="capability",
        rationale="fresh",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="pending_review",
        created_at="2025-06-01T00:00:00Z",
    )

    await agent.evolution.submit_for_review(new_plan)

    stored = agent.memory.get_plan("new_plan")
    assert stored is not None
    assert stored.status == "pending_review"


@pytest.mark.asyncio
async def test_implement_approved_plans_retries_failed(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_implement_approved_plans must retry a 'failed' plan below the cap."""
    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    _seed_failed_plan(agent, "retry_me", "Retry Me", failure_count=1)

    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]

    await agent._implement_approved_plans()

    agent.implement_plan.assert_awaited_once_with("retry_me")


@pytest.mark.asyncio
async def test_implement_approved_plans_skips_exhausted_failed(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_implement_approved_plans must NOT pick up a plan whose failure_count
    is already at max_implementation_attempts."""
    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    _seed_failed_plan(agent, "exhausted", "Exhausted", failure_count=3)

    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]

    await agent._implement_approved_plans()

    agent.implement_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_implement_approved_plans_prefers_approved_over_failed(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Approved plans run before failed retries so fresh work isn't stuck
    behind old retry cycles."""
    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    # Raise the cap above the seeded plan count so the test exercises sort
    # order rather than the slice.
    agent.config.max_implementations_per_iteration = 3
    _seed_failed_plan(agent, "fail_A", "Failed A", failure_count=1)
    _seed_failed_plan(agent, "fail_B", "Failed B", failure_count=2)
    _seed_approved_plan(agent, "approved_X", "Approved X")

    order: list[str] = []

    async def capture(plan_id: str) -> None:
        order.append(plan_id)

    agent.implement_plan = AsyncMock(side_effect=capture)  # type: ignore[method-assign]

    await agent._implement_approved_plans()

    # Approved first, then failed plans in ascending failure_count order.
    assert order == ["approved_X", "fail_A", "fail_B"]


@pytest.mark.asyncio
async def test_implement_approved_plans_continues_after_one_plan_fails(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A non-transient failure on one plan must not block subsequent plans.

    ``implement_plan`` has already demoted + notified on the non-transient
    path; re-raising past ``_implement_approved_plans`` would crash the loop
    and strand every remaining eligible plan. Log-and-continue isolates the
    failure to a single plan.
    """
    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    agent.config.max_implementations_per_iteration = 2
    _seed_approved_plan(agent, "plan_A", "Plan A")
    _seed_approved_plan(agent, "plan_B", "Plan B")

    attempted: list[str] = []

    async def flaky_implement(plan_id: str) -> None:
        attempted.append(plan_id)
        if plan_id == "plan_A":
            raise RuntimeError("non-transient boom")

    agent.implement_plan = AsyncMock(  # type: ignore[method-assign]
        side_effect=flaky_implement,
    )

    # Must not raise — _implement_approved_plans isolates the failure.
    await agent._implement_approved_plans()

    assert attempted == ["plan_A", "plan_B"]


@pytest.mark.asyncio
async def test_implement_approved_plans_swallows_non_transient_sdk_error(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A non-transient ``SDKError`` must be isolated to the failing plan.

    ``implement_plan`` has already demoted + notified before re-raising on
    the non-transient branch (e.g. ``ErrorCategory.UNKNOWN``); letting the
    error escape the for-loop would crash the phase and strand subsequent
    eligible plans.
    """
    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    agent.config.max_implementations_per_iteration = 2
    _seed_approved_plan(agent, "plan_A", "Plan A")
    _seed_approved_plan(agent, "plan_B", "Plan B")

    attempted: list[str] = []

    async def non_transient_on_first(plan_id: str) -> None:
        attempted.append(plan_id)
        if plan_id == "plan_A":
            raise SDKError("unknown", category=ErrorCategory.UNKNOWN)

    agent.implement_plan = AsyncMock(  # type: ignore[method-assign]
        side_effect=non_transient_on_first,
    )

    await agent._implement_approved_plans()

    assert attempted == ["plan_A", "plan_B"]


@pytest.mark.asyncio
async def test_implement_approved_plans_reraises_transient_sdk_error(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Transient SDK errors must propagate so run_loop retries the phase.

    Swallowing them here would consume the plan's approval slot without the
    iteration-level resume-in-place handler ever seeing the signal.
    """
    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    # Raise the cap so the assertion that only plan_A runs proves the raise
    # aborted the loop, not that the slice excluded plan_B.
    agent.config.max_implementations_per_iteration = 2
    _seed_approved_plan(agent, "plan_A", "Plan A")
    _seed_approved_plan(agent, "plan_B", "Plan B")

    attempted: list[str] = []

    async def transient_on_first(plan_id: str) -> None:
        attempted.append(plan_id)
        if plan_id == "plan_A":
            raise SDKError(
                "stall", category=ErrorCategory.STREAM_STALL,
            )

    agent.implement_plan = AsyncMock(  # type: ignore[method-assign]
        side_effect=transient_on_first,
    )

    with pytest.raises(SDKError) as ei:
        await agent._implement_approved_plans()

    assert ei.value.category is ErrorCategory.STREAM_STALL
    # Only plan_A attempted — the raise aborts the loop before plan_B.
    assert attempted == ["plan_A"]


@pytest.mark.asyncio
async def test_implement_approved_plans_default_cap_drains_one(make_agent) -> None:  # type: ignore[no-untyped-def]
    """With max_implementations_per_iteration=1, only the first eligible plan
    runs; the rest stay ``approved`` for the next iteration to drain."""
    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    agent.config.max_implementations_per_iteration = 1
    _seed_approved_plan(agent, "plan_A", "Plan A")
    _seed_approved_plan(agent, "plan_B", "Plan B")
    _seed_approved_plan(agent, "plan_C", "Plan C")

    attempted: list[str] = []

    async def capture(plan_id: str) -> None:
        attempted.append(plan_id)

    agent.implement_plan = AsyncMock(side_effect=capture)  # type: ignore[method-assign]

    await agent._implement_approved_plans()

    assert len(attempted) == 1
    # The two un-implemented plans must remain ``approved`` so a subsequent
    # iteration can pick them up.
    remaining = [p for p in agent.memory.get_all_plans() if p.plan_id != attempted[0]]
    assert all(p.status == "approved" for p in remaining)


@pytest.mark.asyncio
async def test_implement_approved_plans_cap_two_drains_two(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An operator-raised cap of 2 drains two plans in sort order."""
    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    agent.config.max_implementations_per_iteration = 2
    _seed_approved_plan(agent, "plan_A", "Plan A")
    _seed_approved_plan(agent, "plan_B", "Plan B")
    _seed_approved_plan(agent, "plan_C", "Plan C")

    attempted: list[str] = []

    async def capture(plan_id: str) -> None:
        attempted.append(plan_id)

    agent.implement_plan = AsyncMock(side_effect=capture)  # type: ignore[method-assign]

    await agent._implement_approved_plans()

    assert len(attempted) == 2


@pytest.mark.asyncio
async def test_implement_approved_plans_calls_ensure_main_branch_per_plan(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Defence-in-depth: ``ensure_main_branch`` runs before each
    ``implement_plan`` call so a future cap > 1 cannot inherit a sibling
    plan's branch as the base ref."""
    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    agent.config.max_implementations_per_iteration = 2
    _seed_approved_plan(agent, "plan_A", "Plan A")
    _seed_approved_plan(agent, "plan_B", "Plan B")

    calls: list[str] = []

    def record_ensure_main() -> None:
        calls.append("ensure_main_branch")

    async def record_implement(plan_id: str) -> None:
        calls.append(f"implement:{plan_id}")

    agent.evolution.ensure_main_branch = MagicMock(  # type: ignore[method-assign]
        side_effect=record_ensure_main,
    )
    agent.implement_plan = AsyncMock(  # type: ignore[method-assign]
        side_effect=record_implement,
    )

    await agent._implement_approved_plans()

    # ``ensure_main_branch`` must precede every ``implement_plan`` call.
    assert calls == [
        "ensure_main_branch", "implement:plan_A",
        "ensure_main_branch", "implement:plan_B",
    ]


# ---------------------------------------------------------------------------
# Gate convergence (proposal 110) — routed implement_plan loop + orchestrator.
# ---------------------------------------------------------------------------

_FAIL_TWO_REVIEW = QualityGateResult(
    passed=False, ruff_ok=False, mypy_ok=False, tests_ok=True,
    raw_output="Ruff: bad\nMypy: bad\nTests: clean",
)


@pytest.mark.asyncio
async def test_gate_convergence_reaches_green_creates_pr(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A failing-gate set that strictly shrinks each round reaches green and PR
    creation proceeds (no demote)."""
    agent = make_agent()
    agent.config.gate_convergence_ceiling = 20
    _seed_approved_plan(agent, "converge_green", "Converge Green")

    # {ruff,mypy} -> {ruff} -> clean: strict shrink each round.
    gate_returns = [_FAIL_TWO_REVIEW, _FAIL_REVIEW, _OK_REVIEW]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    create_pr_mock = AsyncMock(return_value="https://github.com/o/r/pull/1")
    auto_fix_mock = AsyncMock(return_value=True)

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", auto_fix_mock),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(
            agent.contributions, "get_current_head", return_value="sha",
        ),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
    ):
        await agent.implement_plan("converge_green")

    # initial gate + 2 re-checks after each productive fix round.
    assert gate_calls == 3
    assert auto_fix_mock.await_count == 2
    create_pr_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_convergence_stuck_demotes_no_pr(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A no-progress convergence sequence ends in a loud demote with no PR."""
    agent = make_agent()
    agent.config.gate_convergence_ceiling = 20
    _seed_approved_plan(agent, "converge_stuck", "Converge Stuck")

    # The same failing gate every check — the set never shrinks.
    gates_mock = MagicMock(return_value=_FAIL_REVIEW)
    auto_fix_mock = AsyncMock(return_value=True)
    create_pr_mock = AsyncMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(agent.evolution, "run_quality_gates", gates_mock),
        patch.object(agent.evolution, "auto_fix", auto_fix_mock),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
    ):
        await agent.implement_plan("converge_stuck")

    # initial gate + one re-check after the (unproductive) fix round.
    assert gates_mock.call_count == 2
    assert auto_fix_mock.await_count == 1
    create_pr_mock.assert_not_awaited()

    stored = agent.memory.get_plan("converge_stuck")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1


@pytest.mark.asyncio
async def test_self_review_exhausted_demotes_no_pr(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An exhausted-but-unapproved self-review terminates loudly — the plan is
    demoted to ``failed``, no gate loop runs, and no PR is created (proposal
    110: no unapproved head proceeds)."""
    agent = make_agent()
    agent.config.self_review_max_rounds = 2
    _seed_approved_plan(agent, "sr_exhaust", "SR Exhaust")

    # ``self_review`` signals no-LGTM (rounds exhausted without approval).
    self_review_mock = AsyncMock(return_value=False)
    converge_mock = AsyncMock()
    create_pr_mock = AsyncMock()
    cleanup_mock = MagicMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(agent.evolution, "self_review", self_review_mock),
        patch.object(agent.evolution, "converge_to_green", converge_mock),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent.evolution, "create_issue", MagicMock()),
    ):
        await agent.implement_plan("sr_exhaust")

    self_review_mock.assert_awaited_once()
    # The head never reached the gate loop or PR creation.
    converge_mock.assert_not_awaited()
    create_pr_mock.assert_not_awaited()
    cleanup_mock.assert_called_once_with("sr_exhaust")

    stored = agent.memory.get_plan("sr_exhaust")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1


@pytest.mark.asyncio
async def test_converge_to_green_initial_pass_zero_rounds(make_agent) -> None:  # type: ignore[no-untyped-def]
    """converge_to_green returns green with rounds_used=0 when gates already
    pass — auto_fix is never called."""
    agent = make_agent()
    _seed_approved_plan(agent, "cg_pass", "CG Pass")
    plan = agent.memory.get_plan("cg_pass")
    assert plan is not None

    auto_fix_mock = AsyncMock(return_value=True)
    with (
        patch.object(
            agent.evolution, "run_quality_gates",
            MagicMock(return_value=_OK_REVIEW),
        ),
        patch.object(agent.evolution, "auto_fix", auto_fix_mock),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "green"
    assert result.rounds_used == 0
    auto_fix_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_converge_to_green_no_change_is_stuck(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When auto_fix makes no change, convergence stops with stuck at the
    round it gave up (no progress possible)."""
    agent = make_agent()
    _seed_approved_plan(agent, "cg_nochange", "CG NoChange")
    plan = agent.memory.get_plan("cg_nochange")
    assert plan is not None

    with (
        patch.object(
            agent.evolution, "run_quality_gates",
            MagicMock(return_value=_FAIL_REVIEW),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "stuck"
    assert result.rounds_used == 0
    assert result.review is _FAIL_REVIEW


def _clean_observation():  # type: ignore[no-untyped-def]
    """A green ``GateRunObservation`` — every gate CLEAN (proposal 124)."""
    from maestro.gate_observation import GateObservation, GateRunObservation

    return GateRunObservation(
        gates=(
            GateObservation(tool="ruff", kind="CLEAN", failures=(), output=""),
            GateObservation(tool="mypy", kind="CLEAN", failures=(), output=""),
            GateObservation(tool="tests", kind="CLEAN", failures=(), output=""),
        ),
    )


def _red_observation(location: str):  # type: ignore[no-untyped-def]
    """A red ``GateRunObservation`` with one ruff failure at *location*."""
    from maestro.gate_observation import (
        GateFailure,
        GateObservation,
        GateRunObservation,
    )

    return GateRunObservation(
        gates=(
            GateObservation(
                tool="ruff", kind="FAILED",
                failures=(
                    GateFailure(
                        tool="ruff", kind="FAILED", location=location,
                        identifier=f"ruff:{location}:1:0:E501", message="",
                    ),
                ),
                output="",
            ),
        ),
    )


def _tests_observation(*nodeids: str):  # type: ignore[no-untyped-def]
    """A red ``GateRunObservation`` whose ``tests`` gate fails at each of *nodeids*
    (proposal 124) — the failure-identity granularity ``plan_next_round`` now reads.

    One ``GateFailure`` per nodeid, so ``signature()`` carries one ``tests:<nodeid>``
    identity apiece: shrinking the failing set from three nodeids to one is a strict
    subset within the single ``tests`` label, which the coarse three-label signal
    could never see."""
    from maestro.gate_observation import (
        GateFailure,
        GateObservation,
        GateRunObservation,
    )

    return GateRunObservation(
        gates=(
            GateObservation(
                tool="tests", kind="FAILED",
                failures=tuple(
                    GateFailure(
                        tool="tests", kind="FAILED",
                        location=nodeid.split("::", 1)[0],
                        identifier=nodeid, message="",
                    )
                    for nodeid in nodeids
                ),
                output="",
            ),
        ),
    )


def _env_red_observation(kind: str = "TIMEOUT"):  # type: ignore[no-untyped-def]
    """A red ``GateRunObservation`` whose ``tests`` gate is a non-code-fixable
    environment red (a ``TIMEOUT`` / ``COLLECTION_ERROR`` with no enumerated
    failures) — its ``signature()`` is the synthetic ``{"tests:<kind>"}`` identity
    (proposal 131). The local ``_tests_observation`` helper only builds
    ``FAILED``-kind runs, so this is built directly per the plan."""
    from maestro.gate_observation import GateObservation, GateRunObservation

    return GateRunObservation(
        gates=(GateObservation(tool="tests", kind=kind, failures=(), output=""),),
    )


@pytest.mark.asyncio
async def test_gate_convergence_reread_flips_stale_red_to_green_no_demote(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 regression: with gate observation on, an ``auto_fix`` that
    repaired the *environment* (a dependency install / cache purge — no tracked
    file changed, so it returns ``False``) no longer demotes a branch that is
    green at the terminal.

    Previously the not-fixed exit returned ``stuck`` carrying the *pre-fix* gate
    result, so ``implement_plan`` demoted a branch that had gone green minutes
    earlier. The terminal now re-reads the gate at the not-fixed verdict; a green
    re-read wins, so ``implement_plan`` creates a PR instead of demoting.
    """
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.gate_convergence_ceiling = 20
    _seed_approved_plan(agent, "cg_reread_pr", "CG Reread PR")

    stale_red = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="Ruff: stale red", observation=_red_observation("other.py"),
    )
    fresh_green = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
        raw_output="all clean", observation=_clean_observation(),
    )
    gate_returns = [stale_red, fresh_green]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    create_pr_mock = AsyncMock(return_value="https://github.com/o/r/pull/1")
    demote_mock = AsyncMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
        patch.object(agent.contributions, "get_current_head", return_value="sha"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
    ):
        await agent.implement_plan("cg_reread_pr")

    # initial red read + the terminal's re-read (green): exactly two reads.
    assert gate_calls == 2
    demote_mock.assert_not_awaited()
    create_pr_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_converge_terminal_reread_green_records_one_row(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """proposal 124: the flag-on terminal re-reads the gate on the not-fixed
    path, returns the *fresh* green review (not the stale pre-fix red), and
    appends exactly one ``gate_convergence_terminal`` activation row. A green
    re-read with no baseline classifies ``UNATTRIBUTABLE``."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "cg_terminal_green", "CG Terminal Green")
    plan = agent.memory.get_plan("cg_terminal_green")
    assert plan is not None

    stale_red = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="stale", observation=_red_observation("other.py"),
    )
    fresh_green = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
        raw_output="clean", observation=_clean_observation(),
    )
    gate_returns = [stale_red, fresh_green]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(
            plan, ceiling=20, base_hash="deadbeef1234",
        )

    assert result.outcome == "green"
    assert result.review is fresh_green  # the post-fix read, not the stale red
    assert result.attribution == "UNATTRIBUTABLE"
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent)
        if tup[0] == "gate_convergence_terminal"
    ]
    assert rows == [
        ("gate_convergence_terminal", "green",
         "[UNATTRIBUTABLE] rounds=0 base=deadbeef"),
    ]


@pytest.mark.asyncio
async def test_converge_terminal_reread_red_carries_attribution(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """proposal 124: a re-read that is still red carries the attribution verdict
    on the result and in the single terminal row. A failure located in the plan's
    own diff (``files_affected``) is ``ATTRIBUTED``."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    # ``_seed_approved_plan`` sets ``files_affected=["file.py"]``.
    _seed_approved_plan(agent, "cg_terminal_red", "CG Terminal Red")
    plan = agent.memory.get_plan("cg_terminal_red")
    assert plan is not None

    red = _red_observation("file.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )
    gate_returns = [pre_fix, reread]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(
            plan, ceiling=20, base_hash="abcdef01",
        )

    assert result.outcome == "stuck"
    assert result.review is reread  # the fresh re-read red, not the pre-fix one
    assert result.attribution == "ATTRIBUTED"
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent)
        if tup[0] == "gate_convergence_terminal"
    ]
    assert rows == [
        ("gate_convergence_terminal", "stuck",
         "[ATTRIBUTED] rounds=0 base=abcdef01"),
    ]


@pytest.mark.asyncio
async def test_converge_terminal_records_one_row_per_terminal_not_per_round(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """proposal 124: a multi-round shrink-to-green grind (flag on) records exactly
    one ``gate_convergence_terminal`` row — at the single terminal exit, not one
    per fix round."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "cg_terminal_multi", "CG Terminal Multi")
    plan = agent.memory.get_plan("cg_terminal_multi")
    assert plan is not None

    # Two failing test identities -> one (a strict identity subset) -> clean: two
    # productive rounds under the flag-on observation signal, one green terminal.
    # (Proposal 124 step 7: the flag-on progress predicate now reads the observation
    # signature, so the failing *identities* must shrink for the grind to continue.)
    two_red = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="two",
        observation=_tests_observation(
            "tests/test_x.py::test_a", "tests/test_x.py::test_b",
        ),
    )
    one_red = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="one",
        observation=_tests_observation("tests/test_x.py::test_a"),
    )
    green = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
        raw_output="clean", observation=_clean_observation(),
    )
    gate_returns = [two_red, one_red, green]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=True)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(
            plan, ceiling=20, base_hash="feedface",
        )

    assert result.outcome == "green"
    assert result.rounds_used == 2
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent)
        if tup[0] == "gate_convergence_terminal"
    ]
    assert rows == [
        ("gate_convergence_terminal", "green", "[UNATTRIBUTABLE] rounds=2 base=feedface"),
    ]


@pytest.mark.asyncio
async def test_converge_to_green_shrinks_to_green(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A strictly shrinking failing set converges to green over two rounds."""
    agent = make_agent()
    _seed_approved_plan(agent, "cg_shrink", "CG Shrink")
    plan = agent.memory.get_plan("cg_shrink")
    assert plan is not None

    gate_returns = [_FAIL_TWO_REVIEW, _FAIL_REVIEW, _OK_REVIEW]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=True)),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "green"
    assert result.rounds_used == 2


@pytest.mark.asyncio
async def test_gate_convergence_flag_on_shrinking_identity_set_continues(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 step 7: with gate observation on, a fix round that clears two
    of three failing tests — a strict *failure-identity* subset within the single
    ``tests`` label — reads as progress, so the loop continues past the round the
    coarse three-label signal would have stalled on and reaches green.

    Before this increment the {tests} label was unchanged across the 3→1 round, so
    ``plan_next_round`` returned ``"stuck"`` on round two; the identity-granularity
    signal now sees ``{tests:a}`` as a strict subset of ``{tests:a,b,c}`` and grinds
    the last failure to green over two productive rounds."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "cg_flag_on_shrink", "CG Flag On Shrink")
    plan = agent.memory.get_plan("cg_flag_on_shrink")
    assert plan is not None

    three = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="3 failing",
        observation=_tests_observation(
            "tests/test_x.py::test_a",
            "tests/test_x.py::test_b",
            "tests/test_x.py::test_c",
        ),
    )
    one = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="1 failing",
        observation=_tests_observation("tests/test_x.py::test_a"),
    )
    green = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
        raw_output="clean", observation=_clean_observation(),
    )
    gate_returns = [three, one, green]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=True)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(
            plan, ceiling=20, base_hash="cafebabe",
        )

    assert result.outcome == "green"
    assert result.rounds_used == 2


@pytest.mark.asyncio
async def test_gate_convergence_flag_off_same_label_red_still_stalls(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 step 7 regression: flag off, the loop still reads the coarse
    three-label signal, so a fix round that leaves the same gate (``tests``) red —
    however many individual tests it cleared — is an unchanged label set and stalls
    on round one, byte-identical to the pre-124 behaviour.

    The contrast to ``test_gate_convergence_flag_on_shrinking_identity_set_continues``:
    same 3→1 shape of progress, but with the observation absent the label set
    ``{tests}`` is unchanged, so ``plan_next_round`` returns ``"stuck"``."""
    agent = make_agent()
    # ``gate_observation_enabled`` defaults off — the observation is ``None``.
    _seed_approved_plan(agent, "cg_flag_off_stall", "CG Flag Off Stall")
    plan = agent.memory.get_plan("cg_flag_off_stall")
    assert plan is not None

    red = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="tests red",
    )
    gate_returns = [red, red]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=True)),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "stuck"
    assert result.rounds_used == 1
    assert result.attribution is None


def _two_read_gates(pre: QualityGateResult, reread: QualityGateResult):  # type: ignore[no-untyped-def]
    """A ``run_quality_gates`` side_effect returning *pre* then *reread*.

    proposal 124: the fix loop's initial gate read, then the terminal's
    not-fixed re-read — the two calls ``converge_to_green`` makes when
    ``auto_fix`` returns ``False`` under a flag-on terminal.
    """
    returns = [pre, reread]
    calls = {"n": 0}

    def _gates() -> QualityGateResult:
        result = returns[calls["n"]]
        calls["n"] += 1
        return result

    return _gates


@pytest.mark.asyncio
async def test_inherited_terminal_holds_plan_no_demote_no_pr(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 step 9: an ``INHERITED`` convergence terminal (flag on)
    holds the plan ``approved`` instead of demoting — the branch is cleaned up,
    no PR opens, ``failure_count`` is untouched, the ``origin/main`` head is
    stamped, the hold count increments to 1, and exactly one base-repair
    directive is written into the empty slot."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.max_implementation_attempts = 3
    _seed_approved_plan(agent, "hold_me", "Hold Me")  # files_affected=["file.py"]

    # A red at a file the diff never touched, whose identity the base baseline
    # already carries -> INHERITED.
    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )
    baseline = _red_observation("base_only.py")

    create_pr_mock = AsyncMock()
    demote_mock = AsyncMock()
    cleanup_mock = MagicMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=baseline),
        ),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("hold_me")

    demote_mock.assert_not_awaited()
    create_pr_mock.assert_not_awaited()
    cleanup_mock.assert_called_once_with("hold_me")

    stored = agent.memory.get_plan("hold_me")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.failure_count == 0
    assert stored.inherited_hold_main_sha == "0" * 40
    assert stored.inherited_hold_count == 1

    model = agent.consciousness.load_self_model()
    assert model.next_task_directive.startswith("Base repair:")
    assert "base_only.py" in model.next_task_directive


@pytest.mark.asyncio
async def test_inherited_terminal_holds_plan_flag_on_same_as_flag_off(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 131, step 8: the base-inherited hold is the oracle's HOLD_INHERITED
    verdict, honoured *regardless of the flag*. With ``attempt_disposition_enabled``
    on, an INHERITED terminal whose hold budget is unspent holds the plan
    ``approved`` exactly as flag-off — no demote, no PR, ``failure_count`` frozen,
    the ``origin/main`` head stamped, the hold count at 1, and one base-repair
    directive written into the empty slot."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.attempt_disposition_enabled = True
    agent.config.max_implementation_attempts = 3
    _seed_approved_plan(agent, "hold_on", "Hold On")

    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )
    baseline = _red_observation("base_only.py")

    create_pr_mock = AsyncMock()
    demote_mock = AsyncMock()
    cleanup_mock = MagicMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=baseline),
        ),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("hold_on")

    demote_mock.assert_not_awaited()
    create_pr_mock.assert_not_awaited()
    cleanup_mock.assert_called_once_with("hold_on")

    stored = agent.memory.get_plan("hold_on")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.failure_count == 0
    assert stored.inherited_hold_main_sha == "0" * 40
    assert stored.inherited_hold_count == 1

    model = agent.consciousness.load_self_model()
    assert model.next_task_directive.startswith("Base repair:")
    assert "base_only.py" in model.next_task_directive


@pytest.mark.asyncio
async def test_resume_verdict_preserves_branch_increments_resume_count(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 131: a strictly-progressing non-inherited stall with the flag on
    preserves the evolution branch — release, no cleanup/demote/PR — stamps the new
    failing baseline and branch head, increments ``resume_count`` while freezing
    ``failure_count``, and leaves the plan re-admittable (``failed``)."""
    from maestro.gate_convergence import ConvergenceResult

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.config.max_resume_attempts = 2
    agent._vcs.rev_parse.return_value = "resumehead" + "0" * 30
    _seed_failed_plan(agent, "resume_me", "Resume Me", failure_count=1)
    # Prior baseline the progress check measures against: three failing.
    agent.memory.update_plan_resume(
        "resume_me",
        resume_failing_identities=["tests:A", "tests:B", "tests:C"],
    )

    # Current stall: only one of the three still failing — a strict subset, so a
    # progressing stall the oracle resumes rather than abandons.
    current = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="stuck", observation=_tests_observation("A"),
    )
    convergence = ConvergenceResult(
        outcome="stuck", review=current, rounds_used=3, attribution="ATTRIBUTED",
    )
    demote_mock = AsyncMock()
    cleanup_mock = MagicMock()
    release_mock = MagicMock()
    create_pr_mock = AsyncMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "converge_to_green",
            AsyncMock(return_value=convergence),
        ),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent.evolution, "release_branch", release_mock),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("resume_me")

    demote_mock.assert_not_awaited()
    cleanup_mock.assert_not_called()
    release_mock.assert_called_once_with("resume_me")
    create_pr_mock.assert_not_awaited()

    stored = agent.memory.get_plan("resume_me")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1  # frozen — not a spent attempt
    assert stored.resume_count == 1  # incremented
    assert stored.resume_failing_identities == ["tests:A"]  # new baseline
    assert stored.resume_branch_head == "resumehead" + "0" * 30


@pytest.mark.asyncio
async def test_resume_verdict_restores_head_without_deleting_branch(make_agent) -> None:  # type: ignore[no-untyped-def]
    """#4370: the RESUME terminal releases HEAD back to main — so later phases
    of the iteration never read the half-fixed evolve tree — yet keeps the
    branch ref, and stamps the head read *before* the checkout."""
    from unittest.mock import call

    from maestro.gate_convergence import ConvergenceResult

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.config.max_resume_attempts = 2
    _seed_failed_plan(agent, "resume_me", "Resume Me", failure_count=1)
    agent.memory.update_plan_resume(
        "resume_me",
        resume_failing_identities=["tests:A", "tests:B", "tests:C"],
    )
    current = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="stuck", observation=_tests_observation("A"),
    )
    convergence = ConvergenceResult(
        outcome="stuck", review=current, rounds_used=3, attribution="ATTRIBUTED",
    )
    agent._vcs.reset_mock()
    agent._vcs.rev_parse.return_value = "resumehead" + "0" * 30

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "converge_to_green",
            AsyncMock(return_value=convergence),
        ),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "_vcs", agent._vcs),
        patch.object(agent, "_demote_plan_to_failed", AsyncMock()),
    ):
        await agent.implement_plan("resume_me")

    branch = "maestro/evolve/resume_me"
    rev_idx = agent._vcs.mock_calls.index(call.rev_parse(f"refs/heads/{branch}"))
    checkout_idx = agent._vcs.mock_calls.index(call.checkout("main"))
    assert rev_idx < checkout_idx
    agent._vcs.delete_branch.assert_not_called()

    stored = agent.memory.get_plan("resume_me")
    assert stored is not None
    assert stored.resume_branch_head == "resumehead" + "0" * 30


@pytest.mark.asyncio
async def test_resume_budget_spent_degrades_to_demote(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 131: a still-progressing stall whose resume budget is already
    spent degrades to today's demote — the branch is cleaned up and the plan is
    demoted — but the current failing baseline is still banked for the record."""
    from maestro.gate_convergence import ConvergenceResult

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.config.max_resume_attempts = 2
    _seed_failed_plan(agent, "spent", "Spent", failure_count=1)
    agent.memory.update_plan_resume(
        "spent",
        resume_count=2,  # budget already spent (== max_resume_attempts)
        resume_failing_identities=["tests:A", "tests:B", "tests:C"],
    )

    current = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="stuck", observation=_tests_observation("A"),
    )
    convergence = ConvergenceResult(
        outcome="stuck", review=current, rounds_used=3, attribution="ATTRIBUTED",
    )
    demote_mock = AsyncMock()
    cleanup_mock = MagicMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "converge_to_green",
            AsyncMock(return_value=convergence),
        ),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("spent")

    demote_mock.assert_awaited_once()
    cleanup_mock.assert_called_once_with("spent")

    stored = agent.memory.get_plan("spent")
    assert stored is not None
    assert stored.resume_count == 2  # not incremented past the budget
    assert stored.resume_failing_identities == ["tests:A"]  # baseline still banked


@pytest.mark.asyncio
async def test_stuck_terminal_flag_off_demotes_byte_identical(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 131: with the flag off, the non-inherited stuck terminal is
    byte-identical to today — the branch is cleaned up, the plan is demoted, and
    the resume carry-context fields are never touched."""
    from maestro.gate_convergence import ConvergenceResult

    agent = make_agent()
    agent.config.attempt_disposition_enabled = False
    _seed_failed_plan(agent, "flag_off", "Flag Off", failure_count=1)

    current = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="stuck", observation=_tests_observation("A"),
    )
    convergence = ConvergenceResult(
        outcome="stuck", review=current, rounds_used=3, attribution="ATTRIBUTED",
    )
    demote_mock = AsyncMock()
    cleanup_mock = MagicMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "converge_to_green",
            AsyncMock(return_value=convergence),
        ),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("flag_off")

    demote_mock.assert_awaited_once()
    cleanup_mock.assert_called_once_with("flag_off")

    stored = agent.memory.get_plan("flag_off")
    assert stored is not None
    assert stored.resume_count == 0  # untouched
    assert stored.resume_failing_identities == []  # untouched
    assert stored.resume_branch_head is None  # untouched


@pytest.mark.asyncio
async def test_ask_human_verdict_blocks_plan_asks_one_question(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 131 ASK_HUMAN arm: a recurring, non-code-fixable environment red
    (the same ``TIMEOUT`` identity as the prior baseline, ``UNATTRIBUTABLE``, flag
    on) asks Thomas exactly one question, moves the plan to the inert ``blocked``
    status keyed to the returned message id, freezes ``failure_count``, cleans up
    the branch, and never demotes or opens an issue."""
    from maestro.gate_convergence import ConvergenceResult

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.config.max_resume_attempts = 2
    _seed_failed_plan(agent, "ask_me", "Ask Me", failure_count=1)
    # Prior baseline: the same single environment identity, so the current red is a
    # recurrence (not a strict subset) — the RESUME progress guard cannot fire.
    agent.memory.update_plan_resume(
        "ask_me",
        resume_failing_identities=["tests:TIMEOUT"],
    )

    current = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="Tests: could not run", observation=_env_red_observation("TIMEOUT"),
    )
    convergence = ConvergenceResult(
        outcome="stuck", review=current, rounds_used=3,
        attribution="UNATTRIBUTABLE",
    )
    demote_mock = AsyncMock()
    cleanup_mock = MagicMock()
    create_pr_mock = AsyncMock()
    ask_mock = AsyncMock(return_value="deadbeef1234")

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "converge_to_green",
            AsyncMock(return_value=convergence),
        ),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
        patch.object(agent, "ask_thomas", ask_mock),
    ):
        await agent.implement_plan("ask_me")

    ask_mock.assert_awaited_once()
    demote_mock.assert_not_awaited()  # never demotes → never opens an issue
    create_pr_mock.assert_not_awaited()
    cleanup_mock.assert_called_once_with("ask_me")

    stored = agent.memory.get_plan("ask_me")
    assert stored is not None
    assert stored.status == "blocked"
    assert stored.blocked_question_id == "deadbeef1234"
    assert stored.failure_count == 1  # frozen — not a spent attempt
    assert stored.resume_count == 0  # untouched


@pytest.mark.asyncio
async def test_ask_human_journal_failure_still_blocks(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 131 ASK_HUMAN arm: the pause journal entry is best-effort — a
    ``record_event`` failure is swallowed and the plan is still moved to
    ``blocked`` with its question id recorded."""
    from maestro.gate_convergence import ConvergenceResult

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.config.max_resume_attempts = 2
    _seed_failed_plan(agent, "ask_journal", "Ask Journal", failure_count=1)
    agent.memory.update_plan_resume(
        "ask_journal",
        resume_failing_identities=["tests:TIMEOUT"],
    )

    current = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="Tests: could not run", observation=_env_red_observation("TIMEOUT"),
    )
    convergence = ConvergenceResult(
        outcome="stuck", review=current, rounds_used=3,
        attribution="UNATTRIBUTABLE",
    )
    ask_mock = AsyncMock(return_value="deadbeef1234")

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "converge_to_green",
            AsyncMock(return_value=convergence),
        ),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", AsyncMock()),
        patch.object(agent, "ask_thomas", ask_mock),
        patch.object(
            agent.journal, "record_event",
            side_effect=OSError("disk full"),
        ),
    ):
        await agent.implement_plan("ask_journal")

    ask_mock.assert_awaited_once()
    stored = agent.memory.get_plan("ask_journal")
    assert stored is not None
    assert stored.status == "blocked"
    assert stored.blocked_question_id == "deadbeef1234"


@pytest.mark.asyncio
async def test_ask_human_no_message_channel_falls_through_to_demote(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 131 ASK_HUMAN arm: with no reachable message channel
    (``ask_thomas`` returns ``""``), the plan must not be stranded ``blocked`` with
    no question to unblock it — it falls through to today's demote (branch cleaned
    up, plan demoted) and is never moved to ``blocked``."""
    from maestro.gate_convergence import ConvergenceResult

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.config.max_resume_attempts = 2
    _seed_failed_plan(agent, "no_channel", "No Channel", failure_count=1)
    agent.memory.update_plan_resume(
        "no_channel",
        resume_failing_identities=["tests:TIMEOUT"],
    )

    current = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="Tests: could not run", observation=_env_red_observation("TIMEOUT"),
    )
    convergence = ConvergenceResult(
        outcome="stuck", review=current, rounds_used=3,
        attribution="UNATTRIBUTABLE",
    )
    demote_mock = AsyncMock()
    cleanup_mock = MagicMock()
    ask_mock = AsyncMock(return_value="")

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "converge_to_green",
            AsyncMock(return_value=convergence),
        ),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
        patch.object(agent, "ask_thomas", ask_mock),
    ):
        await agent.implement_plan("no_channel")

    ask_mock.assert_awaited_once()
    demote_mock.assert_awaited_once()
    cleanup_mock.assert_called_once_with("no_channel")

    stored = agent.memory.get_plan("no_channel")
    assert stored is not None
    assert stored.status != "blocked"
    assert stored.blocked_question_id is None


@pytest.mark.asyncio
async def test_ask_human_flag_off_demotes_no_question(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 131 ASK_HUMAN arm: with the flag off, the same recurring
    environment red demotes exactly as today — no ``ask_thomas`` call, no
    ``blocked`` status, and the resume carry-context fields untouched."""
    from maestro.gate_convergence import ConvergenceResult

    agent = make_agent()
    agent.config.attempt_disposition_enabled = False
    _seed_failed_plan(agent, "off_env", "Off Env", failure_count=1)

    current = QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="Tests: could not run", observation=_env_red_observation("TIMEOUT"),
    )
    convergence = ConvergenceResult(
        outcome="stuck", review=current, rounds_used=3,
        attribution="UNATTRIBUTABLE",
    )
    demote_mock = AsyncMock()
    cleanup_mock = MagicMock()
    ask_mock = AsyncMock(return_value="deadbeef1234")

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "converge_to_green",
            AsyncMock(return_value=convergence),
        ),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
        patch.object(agent, "ask_thomas", ask_mock),
    ):
        await agent.implement_plan("off_env")

    ask_mock.assert_not_awaited()
    demote_mock.assert_awaited_once()
    cleanup_mock.assert_called_once_with("off_env")

    stored = agent.memory.get_plan("off_env")
    assert stored is not None
    assert stored.status != "blocked"
    assert stored.blocked_question_id is None
    assert stored.resume_count == 0  # untouched
    assert stored.resume_failing_identities == []  # untouched


@pytest.mark.asyncio
async def test_inherited_hold_does_not_overwrite_occupied_directive(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 step 9: the base-repair directive is first-writer-wins — an
    already-occupied ``next_task_directive`` is preserved, but the plan is still
    held and its count still increments."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "hold_occ", "Hold Occupied")
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "existing directive"
    agent.consciousness.save_self_model(model)

    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=_red_observation("base_only.py")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
    ):
        await agent.implement_plan("hold_occ")

    stored = agent.memory.get_plan("hold_occ")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.inherited_hold_count == 1

    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == "existing directive"


@pytest.mark.asyncio
async def test_inherited_hold_unmeasurable_origin_main_still_holds(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 step 9: an unmeasurable ``origin/main`` (``rev_parse`` ->
    ``None``) still holds and still increments — the stamp is ``None``, so the
    step-7 skip predicate simply will not fire on an unmeasurable base."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent._vcs.rev_parse.return_value = None
    _seed_approved_plan(agent, "hold_unmeasured", "Hold Unmeasured")

    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    demote_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=_red_observation("base_only.py")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("hold_unmeasured")

    demote_mock.assert_not_awaited()
    stored = agent.memory.get_plan("hold_unmeasured")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.inherited_hold_main_sha is None
    assert stored.inherited_hold_count == 1


@pytest.mark.asyncio
async def test_inherited_hold_survives_journal_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 step 9: the hold's journal + directive write is best-effort —
    a failing ``record_event`` is swallowed (logged loud), and the plan is still
    held, because the SHA stamp and count increment are persisted before the
    wrapper runs. The guard is narrow (``OSError``/``ValueError``) — only
    state-file failures are tolerated."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "hold_jf", "Hold Journal Fail")
    agent.consciousness.load_self_model()
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("journal down"),
    )

    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    demote_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=_red_observation("base_only.py")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        # Must not raise — the journal failure is swallowed.
        await agent.implement_plan("hold_jf")

    demote_mock.assert_not_awaited()
    stored = agent.memory.get_plan("hold_jf")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.inherited_hold_count == 1
    # The point of the split guards: the swallowed journal failure must not
    # skip the base-repair directive, the hold's release mechanism.
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive.startswith("Base repair:")
    assert "base_only.py" in model.next_task_directive


@pytest.mark.asyncio
async def test_inherited_hold_journal_unexpected_error_propagates(make_agent) -> None:  # type: ignore[no-untyped-def]
    """#3814: the hold's journal guard is narrow — a non-state-file failure from
    ``record_event`` crashes loudly instead of being swallowed, mirroring the
    environment-hold arm's contract."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "hold_ju", "Hold Journal Unexpected")
    agent.consciousness.load_self_model()
    agent.journal.record_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("journal bug"),
    )

    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=_red_observation("base_only.py")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        pytest.raises(RuntimeError, match="journal bug"),
    ):
        await agent.implement_plan("hold_ju")


@pytest.mark.asyncio
async def test_inherited_hold_directive_save_failure_is_named(  # type: ignore[no-untyped-def]
    make_agent, caplog,
) -> None:
    """#3814: the base-repair directive is the hold's designed release mechanism,
    so a failed ``save_self_model`` gets its own loud WARNING naming the directive
    and the stalled release — not the journal-shaped message the single broad
    handler used to emit. It cannot propagate: ``implement_plan``'s own outer
    handler demotes the plan before re-raising, which would destroy the hold just
    stamped, so the plan must stay ``approved`` and held."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "hold_ds", "Hold Directive Save")
    # Materialise the self-model first: a missing file makes ``load_self_model``
    # itself call ``save_self_model``, which would trip the mock below in the
    # load guard and never reach the directive write under test.
    agent.consciousness.load_self_model()
    agent.consciousness.save_self_model = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("self-model unwritable"),
    )

    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    demote_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=_red_observation("base_only.py")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
        caplog.at_level(logging.WARNING, logger="maestro"),
    ):
        await agent.implement_plan("hold_ds")

    demote_mock.assert_not_awaited()
    stored = agent.memory.get_plan("hold_ds")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.inherited_hold_count == 1
    directive_warnings = [
        record.getMessage()
        for record in caplog.records
        if "Failed to write the base-repair" in record.getMessage()
    ]
    assert len(directive_warnings) == 1
    assert "no release task queued" in directive_warnings[0]


@pytest.mark.asyncio
async def test_inherited_hold_self_model_load_failure_is_named(  # type: ignore[no-untyped-def]
    make_agent, caplog,
) -> None:
    """#3814: an unreadable self-model skips both the note and the directive, and
    says so — the hold still stands, but with no release task queued."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "hold_ld", "Hold Load Fail")
    agent.consciousness.load_self_model = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("self-model unreadable"),
    )

    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    demote_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=_red_observation("base_only.py")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
        caplog.at_level(logging.WARNING, logger="maestro"),
    ):
        await agent.implement_plan("hold_ld")

    demote_mock.assert_not_awaited()
    stored = agent.memory.get_plan("hold_ld")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.inherited_hold_count == 1
    load_warnings = [
        record.getMessage()
        for record in caplog.records
        if "Could not load self-model" in record.getMessage()
    ]
    assert len(load_warnings) == 1


@pytest.mark.asyncio
async def test_attributed_terminal_still_demotes(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 step 9: an ``ATTRIBUTED`` terminal — the red is in the plan's
    own diff — keeps today's demote path; the hold arm never runs."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "attr_demote", "Attr Demote")  # files_affected=["file.py"]

    # Red located in the diff -> ATTRIBUTED, which outranks INHERITED even with a
    # matching baseline.
    red = _red_observation("file.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    demote_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=_red_observation("file.py")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("attr_demote")

    demote_mock.assert_awaited_once()
    stored = agent.memory.get_plan("attr_demote")
    assert stored is not None
    assert stored.inherited_hold_count == 0


@pytest.mark.asyncio
async def test_unattributable_terminal_still_demotes(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 step 9: an ``UNATTRIBUTABLE`` terminal — no baseline captured —
    keeps today's demote path; the hold arm never runs."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    _seed_approved_plan(agent, "unattr_demote", "Unattr Demote")

    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    demote_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=None),  # no baseline -> UNATTRIBUTABLE
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("unattr_demote")

    demote_mock.assert_awaited_once()
    stored = agent.memory.get_plan("unattr_demote")
    assert stored is not None
    assert stored.inherited_hold_count == 0


@pytest.mark.asyncio
async def test_third_inherited_terminal_demotes_with_rendered_identities(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 step 9: with the hold budget spent (two prior holds at the
    default cap of 3), the next ``INHERITED`` terminal demotes — its
    ``last_failure_reason`` composed from the rendered inherited identities, not a
    truncated raw-output tail."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.max_implementation_attempts = 3
    _seed_approved_plan(agent, "hold_spent", "Hold Spent")
    # Two prior holds already banked -> the budget is spent (2 + 1 == 3).
    agent.memory.update_plan_inherited_hold(
        "hold_spent", inherited_hold_main_sha="oldbase", inherited_hold_count=2,
    )

    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    create_pr_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=_red_observation("base_only.py")),
        ),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
    ):
        await agent.implement_plan("hold_spent")

    create_pr_mock.assert_not_awaited()
    stored = agent.memory.get_plan("hold_spent")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1
    assert stored.last_failure_reason is not None
    assert "inherited base failures" in stored.last_failure_reason
    assert "base_only.py" in stored.last_failure_reason


@pytest.mark.asyncio
async def test_third_inherited_terminal_demotes_flag_on_same_as_flag_off(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 131, step 8: the inherited-budget-spent demote keeps its exact
    reason string *regardless of the flag*. With ``attempt_disposition_enabled`` on
    and the hold budget spent (two prior holds at the default cap of 3), the next
    INHERITED terminal still demotes with a ``last_failure_reason`` composed from the
    rendered inherited identities — the oracle returns ABANDON (the inherited hold
    banks no resume baseline, so the empty ``prior_failing`` cannot satisfy the
    RESUME progress guard), and the otherwise-bucket demote preserves the "held N
    time(s)" wording."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.attempt_disposition_enabled = True
    agent.config.max_implementation_attempts = 3
    _seed_approved_plan(agent, "hold_spent_on", "Hold Spent On")
    # Two prior holds already banked -> the budget is spent (2 + 1 == 3).
    agent.memory.update_plan_inherited_hold(
        "hold_spent_on", inherited_hold_main_sha="oldbase", inherited_hold_count=2,
    )

    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    create_pr_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=_red_observation("base_only.py")),
        ),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
    ):
        await agent.implement_plan("hold_spent_on")

    create_pr_mock.assert_not_awaited()
    stored = agent.memory.get_plan("hold_spent_on")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1
    assert stored.last_failure_reason is not None
    assert "inherited base failures" in stored.last_failure_reason
    assert "base_only.py" in stored.last_failure_reason


@pytest.mark.asyncio
async def test_stuck_unattributable_terminal_demotes_with_rendered_identities(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124: a stuck ``UNATTRIBUTABLE`` terminal (flag on) demotes the
    plan with a ``last_failure_reason`` composed from the rendered failure
    identities — no mid-word ``raw_output`` byte slice — and takes the
    non-inherited path (no 'inherited base failures' framing)."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.max_implementation_attempts = 3
    _seed_approved_plan(agent, "stuck_ua", "Stuck UA")  # files_affected=["file.py"]

    # A red whose failure sits in a file the diff never touched, with NO base
    # baseline -> attribute() is UNATTRIBUTABLE (not ATTRIBUTED, not INHERITED).
    red = _red_observation("base_only.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="MIDWORD_SLICE_MARKER", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="MIDWORD_SLICE_MARKER", observation=red,
    )

    create_pr_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline",
            MagicMock(return_value=None),
        ),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
    ):
        await agent.implement_plan("stuck_ua")

    create_pr_mock.assert_not_awaited()
    stored = agent.memory.get_plan("stuck_ua")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1
    assert stored.last_failure_reason is not None
    assert stored.last_failure_reason.startswith("Quality gate failed:")
    # The rendered identity names the failing file, not a raw-output slice.
    assert "base_only.py" in stored.last_failure_reason
    assert "MIDWORD_SLICE_MARKER" not in stored.last_failure_reason
    # Non-inherited path -> no inherited-base framing.
    assert "inherited base failures" not in stored.last_failure_reason


@pytest.mark.asyncio
async def test_implement_approved_plans_skips_and_readmits_inherited_hold(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 124 step 9: ``_implement_approved_plans`` declines a plan held
    against the current ``origin/main`` head, and re-admits it the moment the head
    differs (the base-repair task having landed)."""
    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    agent.config.max_implementations_per_iteration = 3
    _seed_approved_plan(agent, "held_plan", "Held Plan")
    agent.memory.update_plan_inherited_hold(
        "held_plan", inherited_hold_main_sha="0" * 40, inherited_hold_count=1,
    )
    # The mocked origin/main head equals the stamped hold SHA -> still held.
    agent._vcs.rev_parse.return_value = "0" * 40

    attempted: list[str] = []

    async def capture(plan_id: str) -> None:
        attempted.append(plan_id)

    agent.implement_plan = AsyncMock(  # type: ignore[method-assign]
        side_effect=capture,
    )

    await agent._implement_approved_plans()
    assert attempted == []  # skipped while the base has not moved

    # The base advances -> the hold releases and the plan re-admits.
    agent._vcs.rev_parse.return_value = "newbase" + "0" * 33
    await agent._implement_approved_plans()
    assert attempted == ["held_plan"]


@pytest.mark.asyncio
async def test_converge_to_green_ceiling_backstops(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The ceiling terminates convergence with stuck even when the failing set
    was still strictly shrinking that round."""
    agent = make_agent()
    _seed_approved_plan(agent, "cg_ceiling", "CG Ceiling")
    plan = agent.memory.get_plan("cg_ceiling")
    assert plan is not None

    # {ruff,mypy} -> {ruff}: a strict shrink, but ceiling=1 stops it anyway.
    gate_returns = [_FAIL_TWO_REVIEW, _FAIL_REVIEW]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=True)),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=1)

    assert result.outcome == "stuck"
    assert result.rounds_used == 1


@pytest.mark.asyncio
async def test_converge_to_green_forwards_long_context_when_seam_on(make_agent) -> None:  # type: ignore[no-untyped-def]
    """converge_to_green passes long_context=True to auto_fix when the
    long-context seam is active (sdk_model_long_context set, no session), so
    the fix step widens its context slices."""
    agent = make_agent()
    agent.config.sdk_model_long_context = "claude-opus-4-7[1m]"
    _seed_approved_plan(agent, "cg_lc_on", "CG LC On")
    plan = agent.memory.get_plan("cg_lc_on")
    assert plan is not None

    gate_returns = [_FAIL_REVIEW, _OK_REVIEW]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    auto_fix_mock = AsyncMock(return_value=True)
    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", auto_fix_mock),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "green"
    auto_fix_mock.assert_awaited_once()
    assert auto_fix_mock.await_args.kwargs["long_context"] is True


@pytest.mark.asyncio
async def test_converge_to_green_forwards_no_long_context_when_seam_off(make_agent) -> None:  # type: ignore[no-untyped-def]
    """With the long-context seam off (default sdk_model_long_context ""),
    converge_to_green passes long_context=False — the standard caps hold."""
    agent = make_agent()
    _seed_approved_plan(agent, "cg_lc_off", "CG LC Off")
    plan = agent.memory.get_plan("cg_lc_off")
    assert plan is not None

    gate_returns = [_FAIL_REVIEW, _OK_REVIEW]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    auto_fix_mock = AsyncMock(return_value=True)
    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", auto_fix_mock),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "green"
    auto_fix_mock.assert_awaited_once()
    assert auto_fix_mock.await_args.kwargs["long_context"] is False


@pytest.mark.asyncio
async def test_converge_to_green_forwards_bypass_warning_throttle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """converge_to_green passes bypass_warning_throttle=True to auto_fix on
    every round, so the must-finish grind skips the soft per-call warning
    throttle regardless of the long-context seam (proposal 110)."""
    agent = make_agent()
    _seed_approved_plan(agent, "cg_bypass", "CG Bypass")
    plan = agent.memory.get_plan("cg_bypass")
    assert plan is not None

    gate_returns = [_FAIL_REVIEW, _OK_REVIEW]
    gate_calls = 0

    def _gates() -> QualityGateResult:
        nonlocal gate_calls
        result = gate_returns[gate_calls]
        gate_calls += 1
        return result

    auto_fix_mock = AsyncMock(return_value=True)
    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", auto_fix_mock),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "green"
    auto_fix_mock.assert_awaited_once()
    assert auto_fix_mock.await_args.kwargs["bypass_warning_throttle"] is True


@pytest.mark.asyncio
async def test_gate_convergence_nontransient_exception_attributed(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-transient failure raised by converge_to_green is attributed to the
    auto-fix stage and re-raised loudly (fail-loud invariant).

    The demote must not crash referencing an unbound gate review: the
    convergence path never assigns ``gate_review``, so the outer handler
    tolerates its absence and demotes with no gate output.
    """
    agent = make_agent()
    _seed_approved_plan(agent, "converge_boom", "Converge Boom")

    cleanup_mock = MagicMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "converge_to_green",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await agent.implement_plan("converge_boom")

    stored = agent.memory.get_plan("converge_boom")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.last_failure_reason is not None
    assert stored.last_failure_reason.startswith(
        "Post-implementation auto-fix failed:",
    )
    cleanup_mock.assert_called_once_with("converge_boom")


@pytest.mark.asyncio
async def test_failed_plan_notification_includes_attempt_count(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Telegram message on every failure must include the attempt count."""
    agent = make_agent()
    agent.config.max_implementation_attempts = 5
    _seed_failed_plan(agent, "p_notify", "Notify Plan", failure_count=1)

    notify_mock = AsyncMock()
    with (
        patch.object(
            agent.evolution, "implement",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.notifier, "notify", notify_mock),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await agent.implement_plan("p_notify")

    notify_mock.assert_awaited_once()
    msg = notify_mock.await_args.args[0]
    # Prior failure_count was 1 — this is attempt 2 of 5.
    assert "attempt 2/5" in msg
    assert "Notify Plan" in msg


@pytest.mark.asyncio
async def test_github_issue_only_on_terminal_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-terminal failure must NOT open a GitHub issue; only the final
    attempt (failure_count == max_implementation_attempts) does."""
    agent = make_agent()
    agent.config.max_implementation_attempts = 2

    # First attempt: failure_count goes 0 -> 1, non-terminal, no issue.
    _seed_approved_plan(agent, "p_first", "Issue Test")

    create_issue_first = MagicMock()
    with (
        patch.object(
            agent.evolution, "implement",
            AsyncMock(side_effect=RuntimeError("boom1")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", create_issue_first),
        patch.object(agent.notifier, "notify", AsyncMock()),
        pytest.raises(RuntimeError, match="boom1"),
    ):
        await agent.implement_plan("p_first")

    create_issue_first.assert_not_called()

    # Second attempt: failure_count goes 1 -> 2, terminal, issue fires.
    create_issue_second = MagicMock()
    with (
        patch.object(
            agent.evolution, "implement",
            AsyncMock(side_effect=RuntimeError("boom2")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", create_issue_second),
        patch.object(agent.notifier, "notify", AsyncMock()),
        pytest.raises(RuntimeError, match="boom2"),
    ):
        await agent.implement_plan("p_first")

    create_issue_second.assert_called_once()
    stored = agent.memory.get_plan("p_first")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 2


# ---------------------------------------------------------------------------
# audit_self_model phase (mechanical self-audit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_audit_self_every_fires_phase(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``audit_self_every=1`` fires ``_audit_self_model`` every iteration."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=2,
        task_every=0, evolve_every=999, audit_self_every=1,
    )
    assert agent._audit_self_model.call_count == 2


@pytest.mark.asyncio
async def test_run_loop_audit_self_every_zero_disables(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=3,
        task_every=0, evolve_every=999, audit_self_every=0,
    )
    agent._audit_self_model.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_smoke_bypasses_audit_self_model(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``--smoke`` suppresses the audit phase regardless of cadence."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, smoke=True,
        task_every=0, evolve_every=999, audit_self_every=1,
    )
    agent._audit_self_model.assert_not_called()


@pytest.mark.asyncio
async def test_audit_phase_runs_between_evolve_and_dream(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Phase dispatch order: ``evolve`` → ``audit_self_model`` → ``dream``."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    call_order: list[str] = []

    async def evolve_spy(*a: Any, **kw: Any) -> None:
        call_order.append("evolve")

    async def audit_spy(*a: Any, **kw: Any) -> None:
        call_order.append("audit_self_model")

    async def dream_spy(*a: Any, **kw: Any) -> None:
        call_order.append("dream")

    stub_evolve_phase(agent, evolve_spy)
    agent._audit_self_model = audit_spy  # type: ignore[method-assign]
    agent._phase_runners["dream"] = dream_spy

    await agent.run_loop(
        interval=0, max_iters=1,
        task_every=0, evolve_every=1,
        dream_every=1, audit_self_every=1,
    )

    assert call_order == ["evolve", "audit_self_model", "dream"]


@pytest.mark.asyncio
async def test_audit_self_model_persists_findings_and_questions(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Non-empty findings write journal entries + ``[audit]`` questions + AuditResult."""
    from maestro.audit_checks import AuditFinding
    from maestro.consciousness import SelfModel

    agent = make_agent()
    findings = [
        AuditFinding(
            kind="contradicted_claim",
            statement="claim X vs rate 20%",
            cites=["t1", "t2"],
            rule="success_claim_vs_rate",
        ),
        AuditFinding(
            kind="undeclared_limitation",
            statement="Error Y recurred 4x",
            cites=["t3", "t4"],
            rule="repeated_error_without_limitation",
        ),
    ]
    # Seed self-model so save_self_model has a prior state to preserve.
    model = SelfModel()
    model.open_questions = ["What am I?"]
    agent.consciousness.save_self_model(model)

    with patch(
        "maestro.audit_checks.run_all_checks",
        return_value=findings,
    ):
        await agent._audit_self_model(iteration=7)

    # AuditResult persisted
    latest = agent.memory.get_most_recent_audit()
    assert latest is not None
    assert latest.iteration == 7
    assert len(latest.findings) == 2
    assert latest.rules_fired == sorted({
        "repeated_error_without_limitation",
        "success_claim_vs_rate",
    })

    # Two audit_observation journal entries — provenance is visible.
    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert len(audit_entries) == 2
    assert all(e.triggered_by == "audit_self_model" for e in audit_entries)
    assert "[contradicted_claim]" in audit_entries[0].content
    assert "claim X vs rate 20%" in audit_entries[0].content
    assert "cites: t1, t2" in audit_entries[0].content

    # Self-model gained two ``[audit]`` open_questions on top of the one
    # pre-seeded self-generated question.
    reloaded = agent.consciousness.load_self_model()
    audit_qs = [q for q in reloaded.open_questions if q.startswith("[audit")]
    assert len(audit_qs) == 2
    assert all("[audit] contradicted_claim:" in q or
               "[audit] undeclared_limitation:" in q for q in audit_qs)
    assert "What am I?" in reloaded.open_questions


@pytest.mark.asyncio
async def test_audit_self_model_refire_does_not_duplicate_question(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A finding re-firing across cycles holds one ``[audit]`` slot (#4113).

    Without upstream dedupe the positional sub-cap fills with verbatim copies
    and evicts the distinct audit entry and the ``[audit refuted]`` claim.
    """
    from maestro.audit_checks import AuditFinding
    from maestro.consciousness import SelfModel

    agent = make_agent()
    refuted = "[audit refuted] success_claim_vs_rate: the rate is fine"
    sibling = "[audit] unmerged_work: branch X is unmerged"
    model = SelfModel()
    model.open_questions = ["What am I?", refuted, sibling]
    agent.consciousness.save_self_model(model)
    finding = AuditFinding(
        kind="undeclared_limitation",
        statement="Error Y recurred 4x",
        cites=["t3"],
        rule="repeated_error_without_limitation",
    )
    expected = "[audit] undeclared_limitation: Error Y recurred 4x"

    with patch("maestro.audit_checks.run_all_checks", return_value=[finding]):
        for iteration in range(4):
            await agent._audit_self_model(iteration=iteration)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.open_questions.count(expected) == 1
    assert refuted in reloaded.open_questions
    assert sibling in reloaded.open_questions
    assert reloaded.open_questions[-1] == expected


@pytest.mark.asyncio
async def test_audit_self_model_same_cycle_duplicate_findings_single_question(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Two identical findings in one cycle yield a single ``[audit]`` question."""
    from maestro.audit_checks import AuditFinding
    from maestro.consciousness import SelfModel

    agent = make_agent()
    agent.consciousness.save_self_model(SelfModel())
    finding = AuditFinding(
        kind="undeclared_limitation",
        statement="Error Y recurred 4x",
        cites=["t3"],
        rule="repeated_error_without_limitation",
    )

    with patch(
        "maestro.audit_checks.run_all_checks", return_value=[finding, finding],
    ):
        await agent._audit_self_model(iteration=1)

    reloaded = agent.consciousness.load_self_model()
    expected = "[audit] undeclared_limitation: Error Y recurred 4x"
    assert reloaded.open_questions.count(expected) == 1


@pytest.mark.asyncio
async def test_audit_self_model_refire_long_kind_does_not_duplicate(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A question truncated on save still dedupes on the next cycle (#4113)."""
    from maestro.audit_checks import AuditFinding
    from maestro.consciousness import Consciousness, SelfModel

    agent = make_agent()
    agent.consciousness.save_self_model(SelfModel())
    finding = AuditFinding(
        kind="banked_owned_act_reperformance_extra",
        statement="x" * 200,
        cites=["t3"],
        rule="repeated_error_without_limitation",
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[finding]):
        for iteration in range(3):
            await agent._audit_self_model(iteration=iteration)

    reloaded = agent.consciousness.load_self_model()
    audit = [q for q in reloaded.open_questions if q.startswith("[audit] banked")]
    assert len(audit) == 1
    assert len(audit[0]) == Consciousness.MAX_ITEM_CHARS


@pytest.mark.asyncio
async def test_audit_bridge_stands_down_under_owned_wip(make_agent) -> None:  # type: ignore[no-untyped-def]
    """WIP-primacy stand-down: findings do not fill the slot the WIP owner claims.

    With ``wip_primacy_enabled`` on and ``_current_task_focus`` reporting a
    ``TendOwnedPR``, the audit→directive bridge leaves ``next_task_directive``
    empty so the head-of-chain ``wip_primacy`` gate can name the PR to finish —
    while the findings + ``[audit]`` questions are still persisted (the
    stand-down is surgical to the directive slot).
    """
    from maestro.audit_checks import AuditFinding
    from maestro.consciousness import SelfModel
    from maestro.wip_primacy import TendOwnedPR

    agent = make_agent()
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=TendOwnedPR(pr_number=99, owned_blocker="CONFLICTING"),
    )
    findings = [
        AuditFinding(
            kind="contradicted_claim",
            statement="claim X vs rate 20%",
            cites=["t1"],
            rule="success_claim_vs_rate",
        ),
    ]
    agent.consciousness.save_self_model(SelfModel())

    with patch(
        "maestro.audit_checks.run_all_checks",
        return_value=findings,
    ):
        await agent._audit_self_model(iteration=3)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive is None
    agent._current_task_focus.assert_called_once()
    # The stand-down only vacates the directive slot — findings still land.
    audit_qs = [q for q in reloaded.open_questions if q.startswith("[audit")]
    assert len(audit_qs) == 1
    latest = agent.memory.get_most_recent_audit()
    assert latest is not None
    assert len(latest.findings) == 1


@pytest.mark.asyncio
async def test_audit_bridge_fires_when_flag_on_but_discretionary(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag on but no owned WIP: the bridge fills the slot as usual."""
    from maestro.audit_checks import AuditFinding
    from maestro.consciousness import SelfModel
    from maestro.wip_primacy import Discretionary

    agent = make_agent()
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=Discretionary(),
    )
    findings = [
        AuditFinding(
            kind="contradicted_claim",
            statement="claim X vs rate 20%",
            cites=["t1"],
            rule="success_claim_vs_rate",
        ),
    ]
    agent.consciousness.save_self_model(SelfModel())

    with patch(
        "maestro.audit_checks.run_all_checks",
        return_value=findings,
    ):
        await agent._audit_self_model(iteration=4)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive is not None
    agent._current_task_focus.assert_called_once()


@pytest.mark.asyncio
async def test_undefined_coinage_directive_fills_empty_slot_when_flag_on(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Proposal 101: flag on, empty slot, no owned WIP → the top undefined
    coinage is routed into ``next_task_directive`` (and persisted despite a
    finding-free cycle, via the widened save guard)."""
    from maestro.consciousness import SelfModel
    from maestro.wip_primacy import Discretionary

    agent = make_agent()
    agent.config.audit_undefined_coinage_directive_enabled = True
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=Discretionary(),
    )
    agent.journal.write_thought("flux-capacitor " * 5, triggered_by="agent_tool")
    agent.consciousness.save_self_model(SelfModel())

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=5)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive is not None
    assert "flux-capacitor" in reloaded.next_task_directive


@pytest.mark.asyncio
async def test_undefined_coinage_directive_inert_when_flag_off(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off: the coinage bridge never runs, so the empty slot stays empty."""
    from maestro.consciousness import SelfModel
    from maestro.wip_primacy import Discretionary

    agent = make_agent()
    agent.config.audit_undefined_coinage_directive_enabled = False
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=Discretionary(),
    )
    agent.journal.write_thought("flux-capacitor " * 5, triggered_by="agent_tool")
    agent.consciousness.save_self_model(SelfModel())

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=6)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive is None


@pytest.mark.asyncio
async def test_undefined_coinage_directive_defers_to_occupied_slot(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An already-set directive is left untouched — the bridge fills only an
    empty slot, deferring to the prior writer."""
    from maestro.consciousness import SelfModel
    from maestro.wip_primacy import Discretionary

    agent = make_agent()
    agent.config.audit_undefined_coinage_directive_enabled = True
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=Discretionary(),
    )
    agent.journal.write_thought("flux-capacitor " * 5, triggered_by="agent_tool")
    agent.consciousness.save_self_model(
        SelfModel(next_task_directive="Finish PR #42"),
    )

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=7)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive == "Finish PR #42"


@pytest.mark.asyncio
async def test_undefined_coinage_directive_stands_down_for_owned_wip(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With owned WIP claiming the slot, the coinage bridge stands down and the
    directive slot stays empty for the ``wip_primacy`` gate."""
    from maestro.consciousness import SelfModel
    from maestro.wip_primacy import TendOwnedPR

    agent = make_agent()
    agent.config.audit_undefined_coinage_directive_enabled = True
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=TendOwnedPR(pr_number=99, owned_blocker="CONFLICTING"),
    )
    agent.journal.write_thought("flux-capacitor " * 5, triggered_by="agent_tool")
    agent.consciousness.save_self_model(SelfModel())

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=8)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive is None


@pytest.mark.asyncio
async def test_undefined_coinage_directive_self_extinguishes_when_defined(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """A coinage already defined in ``docs/lexicon.md`` produces no directive —
    the bridge self-extinguishes exactly as the display sensor does (this also
    exercises the lexicon-present branch)."""
    from maestro.consciousness import SelfModel
    from maestro.wip_primacy import Discretionary

    agent = make_agent()
    agent.config.audit_undefined_coinage_directive_enabled = True
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=Discretionary(),
    )
    agent.journal.write_thought("flux-capacitor " * 5, triggered_by="agent_tool")
    mem_fs[str(agent.config.workspace / "docs" / "lexicon.md")] = (
        "| term | definition | names | anchor | variants | minted |\n"
        "|---|---|---|---|---|---|\n"
        "| flux-capacitor | Makes time travel possible. | The 1.21GW handle. "
        "| `flux` |  | back to the future |\n"
    )
    agent.consciousness.save_self_model(SelfModel())

    with patch("maestro.audit_checks.run_all_checks", return_value=[]):
        await agent._audit_self_model(iteration=9)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive is None


@pytest.mark.asyncio
async def test_audit_self_model_threads_resolutions_to_run_all_checks(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The audit phase reads recent resolutions and forwards them to the rule
    set so ``check_stalled_question`` can fire on stalled targeting."""
    from maestro.consciousness import SelfModel
    from maestro.memory import QuestionResolution

    agent = make_agent()
    model = SelfModel(open_questions=["What am I capable of?"])
    agent.consciousness.save_self_model(model)
    for i in range(3):
        agent.memory.append_question_resolution(QuestionResolution(
            resolution_id=f"r{i}",
            iteration=i,
            timestamp="2026-04-26T00:00:00+00:00",
            question="What am I capable of?",
            task_id=f"t{i}",
            outcome="success",
            disposition="kept",
            finding="probe",
        ))

    captured: dict = {}

    def _fake_run_all_checks(
        self_model, tasks, success_rate,
        resolutions=None, settled_prs=frozenset(), *, stale_entry_corpus=None,
        cited_source_task_ids=None, branch_promotion_pressure_enabled=None,
        evidence_bound_claims_enabled=None, closed_refs=frozenset(),
    ):  # type: ignore[no-untyped-def]
        captured["resolutions"] = resolutions
        captured["stale_entry_corpus"] = stale_entry_corpus
        captured["cited_source_task_ids"] = cited_source_task_ids
        captured["branch_promotion_pressure_enabled"] = (
            branch_promotion_pressure_enabled
        )
        return []

    with patch(
        "maestro.audit_checks.run_all_checks",
        side_effect=_fake_run_all_checks,
    ):
        await agent._audit_self_model(iteration=8)

    assert captured["resolutions"] is not None
    assert len(captured["resolutions"]) == 3
    # The branch-promotion-pressure master switch is threaded default-on
    # (proposal 116, step 6).
    assert captured["branch_promotion_pressure_enabled"] is True


@pytest.mark.asyncio
async def test_audit_self_model_threads_cited_source_task_ids(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The audit phase fetches plan citations and forwards them to the rule
    set so ``check_kept_task_not_promoted`` can subtract already-promoted
    tasks from its evidence."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    agent.consciousness.save_self_model(SelfModel())
    fake_citations = {"task_a", "task_b"}
    captured: dict = {}

    def _fake_run_all_checks(
        self_model, tasks, success_rate,
        resolutions=None, settled_prs=frozenset(), *, stale_entry_corpus=None,
        cited_source_task_ids=None, branch_promotion_pressure_enabled=None,
        evidence_bound_claims_enabled=None, closed_refs=frozenset(),
    ):  # type: ignore[no-untyped-def]
        captured["cited_source_task_ids"] = cited_source_task_ids
        return []

    with patch.object(
        agent.memory, "get_cited_source_task_ids",
        return_value=fake_citations,
    ), patch(
        "maestro.audit_checks.run_all_checks",
        side_effect=_fake_run_all_checks,
    ):
        await agent._audit_self_model(iteration=11)

    assert captured["cited_source_task_ids"] == fake_citations


@pytest.mark.asyncio
async def test_audit_self_model_emits_stalled_question_finding(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """End-to-end: 3 'kept' resolutions + still-open question → audit entry."""
    from maestro.consciousness import SelfModel
    from maestro.memory import QuestionResolution

    agent = make_agent()
    model = SelfModel(open_questions=["What am I capable of?"])
    agent.consciousness.save_self_model(model)
    for i in range(3):
        agent.memory.append_question_resolution(QuestionResolution(
            resolution_id=f"r{i}",
            iteration=i,
            timestamp="2026-04-26T00:00:00+00:00",
            question="What am I capable of?",
            task_id=f"t{i}",
            outcome="success",
            disposition="kept",
            finding="probe",
        ))

    await agent._audit_self_model(iteration=9)

    # Audit_observation journal entry mentions the stalled-question kind.
    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert any("[stalled_question]" in e.content for e in audit_entries)
    # An ``[audit] stalled_question:`` entry is appended to open_questions.
    reloaded = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] stalled_question:")
        for q in reloaded.open_questions
    )


@pytest.mark.asyncio
async def test_audit_self_model_zero_findings_cycle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Empty findings still persist an AuditResult, with no journal / question writes."""
    agent = make_agent()

    with patch(
        "maestro.audit_checks.run_all_checks",
        return_value=[],
    ):
        await agent._audit_self_model(iteration=3)

    latest = agent.memory.get_most_recent_audit()
    assert latest is not None
    assert latest.iteration == 3
    assert latest.findings == []
    assert latest.rules_fired == []

    # No audit_observation journal entries.
    assert agent.journal.read_events_by_type("audit_observation") == []

    # No [audit] open_questions on the self-model.
    model = agent.consciousness.load_self_model()
    assert not any(q.startswith("[audit") for q in model.open_questions)


@pytest.mark.asyncio
async def test_audit_self_model_emits_owned_act_drought_finding(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Rule 18 dispatch: zero owned terminal acts + a prunable pending_review
    backlog routes the owned-act demand into an empty ``next_task_directive``
    via the audit→directive bridge."""
    from maestro.consciousness import SelfModel
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.consciousness.save_self_model(SelfModel())  # empty directive
    for i in range(2):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"pp{i}",
            title=f"Pending plan {i}",
            category="capability",
            rationale=f"rationale {i}",
            proposed_changes=["x"],
            files_affected=["maestro/x.py"],
            expected_impact="none",
            status="pending_review",
        ))

    await agent._audit_self_model(iteration=5)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert any("[owned_act_drought]" in e.content for e in audit_entries)
    reloaded = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] owned_act_drought:")
        for q in reloaded.open_questions
    )
    # The audit→directive bridge filled the empty directive with the demand.
    assert "mark_plan_obsolete" in reloaded.next_task_directive


@pytest.mark.asyncio
async def test_audit_self_model_owned_act_drought_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the rule-18 master switch off, the drought is never surfaced even
    with a prunable backlog and zero owned acts."""
    from maestro.consciousness import SelfModel
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.owned_act_drought_enabled = False
    agent.consciousness.save_self_model(SelfModel())
    for i in range(2):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=f"pp{i}",
            title=f"Pending plan {i}",
            category="capability",
            rationale=f"rationale {i}",
            proposed_changes=["x"],
            files_affected=["maestro/x.py"],
            expected_impact="none",
            status="pending_review",
        ))

    await agent._audit_self_model(iteration=6)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert not any("[owned_act_drought]" in e.content for e in audit_entries)
    reloaded = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] owned_act_drought:")
        for q in reloaded.open_questions
    )


def _seed_phantom_partial_prune(agent, plan_id: str = "a01c09e3b9bf") -> None:  # type: ignore[no-untyped-def]
    """Seed a landed owned-prune scenario for rule 19.

    A ``partial`` self-directed, ``pr_head_sha``-less task whose summary fired
    the ``mark_plan_obsolete`` lever on *plan_id*, plus that plan reading
    ``obsolete`` on disk — so the prune landed and the ``partial`` under-claimed
    it, exactly the phantom-partial-prune the rule flags.
    """
    from maestro.consciousness import SelfModel
    from maestro.memory import EvolutionPlan, TaskRecord

    agent.consciousness.save_self_model(SelfModel())  # empty directive
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id=plan_id,
        title="Stale pending plan",
        category="capability",
        rationale="superseded",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status="obsolete",
    ))
    agent.memory.record_task(TaskRecord(
        task_id="prune-task-1",
        goal="Prune stale pending_review plans",
        plan=[],
        outcome="partial",
        result_summary=(
            f"Fired mark_plan_obsolete on plan {plan_id}; the gross "
            f"pending_review count read 21->18 so I stamped it partial."
        ),
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-15T00:00:00+00:00",
    ))


@pytest.mark.asyncio
async def test_audit_self_model_emits_phantom_partial_prune_finding(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Rule 19 dispatch: with the flag on, a landed owned prune stamped
    ``partial`` surfaces a ``phantom_partial_prune`` finding into the journal
    and ``open_questions``, and the audit→directive bridge fills the empty
    directive."""
    agent = make_agent()
    agent.config.phantom_partial_prune_enabled = True
    _seed_phantom_partial_prune(agent)

    await agent._audit_self_model(iteration=7)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert any("[phantom_partial_prune]" in e.content for e in audit_entries)
    reloaded = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] phantom_partial_prune:")
        for q in reloaded.open_questions
    )
    # The plan_id under-claimed is named in the surfaced finding.
    assert any(
        "a01c09e3b9bf" in q
        for q in reloaded.open_questions
        if q.startswith("[audit] phantom_partial_prune:")
    )


@pytest.mark.asyncio
async def test_audit_self_model_phantom_partial_prune_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the rule-19 master switch off (the default), the same landed-prune
    scenario surfaces nothing — the flag is the single dispatch gate."""
    agent = make_agent()
    assert agent.config.phantom_partial_prune_enabled is False
    _seed_phantom_partial_prune(agent)

    await agent._audit_self_model(iteration=8)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert not any(
        "[phantom_partial_prune]" in e.content for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] phantom_partial_prune:")
        for q in reloaded.open_questions
    )


def _seed_banked_owned_act(agent, plan_id: str = "d5bcc3fc2a33") -> None:  # type: ignore[no-untyped-def]
    """Seed a banked obsolete-prune plus a directive that re-performs it.

    An ``EvolutionPlan`` reading ``obsolete`` on disk banks its 12-hex plan_id in
    the owned-act ledger; the self-model's ``next_task_directive`` names that same
    plan_id, so re-performing it only re-banks a duplicate — exactly the
    reperformance ``check_banked_owned_act_reperformance`` flags.
    """
    from maestro.consciousness import SelfModel
    from maestro.memory import EvolutionPlan

    agent.consciousness.save_self_model(SelfModel(
        next_task_directive=f"Prune the stale pending_review plan {plan_id}",
    ))
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id=plan_id,
        title="Stale pending plan",
        category="capability",
        rationale="superseded",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status="obsolete",
    ))


@pytest.mark.asyncio
async def test_audit_self_model_emits_banked_owned_act_finding(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Banked-owned-act dispatch: with the flag on, a ``next_task_directive``
    re-performing a banked obsolete-prune surfaces a
    ``banked_owned_act_reperformance`` finding into the journal and
    ``open_questions``, naming the banked plan_id."""
    agent = make_agent()
    agent.config.banked_owned_act_enabled = True
    _seed_banked_owned_act(agent)

    await agent._audit_self_model(iteration=7)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert any(
        "[banked_owned_act_reperformance]" in e.content for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] banked_owned_act_reperformance:")
        for q in reloaded.open_questions
    )
    assert any(
        "d5bcc3fc2a33" in q
        for q in reloaded.open_questions
        if q.startswith("[audit] banked_owned_act_reperformance:")
    )


@pytest.mark.asyncio
async def test_audit_self_model_banked_owned_act_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the banked-owned-act master switch off (the default), the same
    reperformance scenario surfaces nothing — the flag is the single dispatch
    gate."""
    agent = make_agent()
    assert agent.config.banked_owned_act_enabled is False
    _seed_banked_owned_act(agent)

    await agent._audit_self_model(iteration=8)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert not any(
        "[banked_owned_act_reperformance]" in e.content for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] banked_owned_act_reperformance:")
        for q in reloaded.open_questions
    )


def _seed_undercredited_landing(agent, sha: str = "ac48a769") -> None:  # type: ignore[no-untyped-def]
    """Seed a landed-SHA-under-non-success scenario for rule 20.

    A ``partial`` task whose ``result_summary`` cites an own-committed SHA (no
    on-branch handoff marker, so rule 14 leaves it alone); the git-ancestry
    oracle is patched by the caller to report the SHA landed, so the ``partial``
    under-claimed a real landing — exactly the under-credit rule 20 flags.
    """
    from maestro.consciousness import SelfModel
    from maestro.memory import TaskRecord

    agent.consciousness.save_self_model(SelfModel())  # empty directive
    agent.memory.record_task(TaskRecord(
        task_id="landing-task-1",
        goal="Land the blocker PR",
        plan=[],
        outcome="partial",
        result_summary=(
            f"Landed the 2-parent merge; committed {sha} — origin/main "
            f"is now a true ancestor."
        ),
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-15T00:00:00+00:00",
    ))


@pytest.mark.asyncio
async def test_audit_self_model_emits_undercredited_landing_finding(
    make_agent,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 20 dispatch: with the flag on, a ``partial`` task whose own SHA git
    shows landed surfaces an ``undercredited_landing`` finding into the journal
    and ``open_questions``."""
    agent = make_agent()
    agent.config.undercredited_landing_enabled = True
    _seed_undercredited_landing(agent)
    monkeypatch.setattr(
        "maestro.audit_checks_phantom.is_branch_merged_into_main",
        lambda sha: True,
    )

    await agent._audit_self_model(iteration=9)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert any(
        "[undercredited_landing]" in e.content for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] undercredited_landing:")
        for q in reloaded.open_questions
    )
    assert any(
        "ac48a769" in q
        for q in reloaded.open_questions
        if q.startswith("[audit] undercredited_landing:")
    )


@pytest.mark.asyncio
async def test_audit_self_model_undercredited_landing_disabled(
    make_agent,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the rule-20 master switch off (the default), the same landed-SHA
    scenario surfaces nothing — the flag is the single dispatch gate. The git
    oracle is patched to a no-network stub so the phase never shells out."""
    agent = make_agent()
    assert agent.config.undercredited_landing_enabled is False
    _seed_undercredited_landing(agent)
    monkeypatch.setattr(
        "maestro.audit_checks_phantom.is_branch_merged_into_main",
        lambda sha: True,
    )

    await agent._audit_self_model(iteration=10)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert not any(
        "[undercredited_landing]" in e.content for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] undercredited_landing:")
        for q in reloaded.open_questions
    )


def _seed_reconcile_near_miss(agent, *, behind: int = 7) -> None:  # type: ignore[no-untyped-def]
    """Seed a ≤N-behind git-unlanded own PR called 'awaiting' with no SHA.

    A ``partial`` remediation task citing own PR #3083 with no committed
    reconcile SHA; the open-evolution-PR snapshot maps #3083 to its head branch,
    the merge-truth chokepoint reports it unsettled, and ``commits_ahead_behind``
    reports it *behind* commits behind origin/main — exactly the reconcile-near
    miss rule 21 flags.
    """
    from unittest.mock import MagicMock

    from maestro.consciousness import SelfModel
    from maestro.memory import TaskRecord
    from maestro.vcs import PrMergeState

    agent.consciousness.save_self_model(SelfModel())  # empty directive
    agent.memory.record_task(TaskRecord(
        task_id="reconcile-task-1",
        goal="Remediate PR #3083",
        plan=[],
        outcome="partial",
        result_summary=(
            "Resolved threads on #3083 (and touched #9999, not an open own PR); "
            "branch a few commits behind main — awaiting review/merge."
        ),
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-15T00:00:00+00:00",
    ))
    agent.evolution._host.get_open_evolution_pr_states = MagicMock(
        return_value={
            "maestro/evolve/ep3083": PrMergeState(
                number=3083,
                head_ref="maestro/evolve/ep3083",
                title="cure",
                body="",
                state="OPEN",
                mergeable="MERGEABLE",
                merge_state_status="BEHIND",
                review_decision="",
                created_at="",
                updated_at="",
                url="https://example/pr/3083",
            ),
        },
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=3083,
            settled=False,
            state="OPEN",
            branch_landed=False,
            source="host_api",
            observed_at=0.0,
        ),
    )
    agent._vcs.commits_ahead_behind.return_value = (0, behind)


@pytest.mark.asyncio
async def test_audit_self_model_emits_reconcile_near_miss_finding(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Rule 21 dispatch: with the flag on, a ``partial`` remediation on a
    git-unlanded own PR measured ≤ max_behind behind origin/main with no
    reconcile SHA surfaces a ``reconcile_near_miss`` finding into the journal,
    ``open_questions``, and a reconcile-or-close ``next_task_directive``."""
    agent = make_agent()
    agent.config.reconcile_near_miss_enabled = True
    _seed_reconcile_near_miss(agent, behind=7)

    await agent._audit_self_model(iteration=11)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert any(
        "[reconcile_near_miss]" in e.content for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] reconcile_near_miss:")
        for q in reloaded.open_questions
    )
    assert reloaded.next_task_directive
    assert "#3083" in reloaded.next_task_directive


@pytest.mark.asyncio
async def test_audit_self_model_reconcile_near_miss_disabled(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """With the rule-21 master switch off (the default), the same near-miss
    scenario surfaces nothing — the flag is the single dispatch gate."""
    agent = make_agent()
    assert agent.config.reconcile_near_miss_enabled is False
    _seed_reconcile_near_miss(agent, behind=7)

    await agent._audit_self_model(iteration=12)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert not any(
        "[reconcile_near_miss]" in e.content for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] reconcile_near_miss:")
        for q in reloaded.open_questions
    )


def _seed_unwinnable_race(  # type: ignore[no-untyped-def]
    agent,
    pr: int = 3112,
    *,
    landed: bool = False,
    closed_unmerged: bool = False,
    laps: int = 4,
) -> None:
    """Seed *laps* remediation laps on PR #*pr* whose origin/main base moved each
    lap (a→b→c→d = 3 re-entries, ≥ the default threshold of 3).

    Attaches a real ``MessageStore`` so the actuator can persist its heads-up and
    mocks ``is_pr_settled`` to report the PR merged (*landed*), closed-unmerged
    (*closed_unmerged* — settled yet not landed), or still open (neither). A
    closed-unmerged PR must be skipped just like a merged one: it is settled, so
    it is not an unwinnable race. Proposal 099 step 8.
    """
    from maestro.consciousness import SelfModel
    from maestro.memory import TaskRecord
    from maestro.messaging import MessageStore

    assert not (landed and closed_unmerged), (
        "a PR cannot be both merged and closed-unmerged"
    )
    agent.consciousness.save_self_model(SelfModel())
    agent.message_store = MessageStore(agent.config.memory_dir)
    for i in range(laps):
        agent.memory.record_task(TaskRecord(
            task_id=f"race-lap-{i}",
            goal=f"Remediate PR #{pr}: resolve conflict",
            plan=[],
            outcome="partial",
            result_summary="reconciled against a moved base",
            errors=[],
            duration_secs=1.0,
            timestamp=f"2026-07-19T0{i}:00:00+00:00",
            base_head_sha=chr(ord("a") + i) * 40,
        ))
    if closed_unmerged:
        settled, state = True, "CLOSED"
    else:
        settled, state = landed, ("MERGED" if landed else "OPEN")
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=pr,
            settled=settled,
            state=state,
            branch_landed=landed,
            source="host_api",
            observed_at=0.0,
        ),
    )


@pytest.mark.asyncio
async def test_audit_self_model_delivers_unwinnable_race_at_threshold(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Proposal 099 step 8: a non-landing lineage that re-entered the
    base-advance race at/above the threshold earns exactly one durable
    ``unwinnable-race-<pr>`` heads-up — and NO stand-down, so the PR stays
    routable."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    _seed_unwinnable_race(agent, pr=3112, landed=False)

    await agent._audit_self_model(iteration=5)

    thread = agent.message_store.get_thread("unwinnable-race-3112")
    assert len(thread) == 1
    assert "#3112" in thread[0].content
    # The defining property: no stand-down was written, so the PR is not muted.
    assert has_standdown(agent.escalation_standdowns, 3112) is False


@pytest.mark.asyncio
async def test_audit_self_model_unwinnable_race_idempotent_across_laps(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Two audit laps deliver the heads-up exactly once (thread-keyed
    idempotency), and the PR stays free of any stand-down across both."""
    from maestro.escalation_bridge import has_standdown

    agent = make_agent()
    _seed_unwinnable_race(agent, pr=3112, landed=False)

    await agent._audit_self_model(iteration=5)
    await agent._audit_self_model(iteration=6)

    assert len(agent.message_store.get_thread("unwinnable-race-3112")) == 1
    assert has_standdown(agent.escalation_standdowns, 3112) is False


@pytest.mark.asyncio
async def test_audit_self_model_unwinnable_race_skips_landed_pr(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A landed PR at/above the threshold produces no heads-up (the
    ``is_pr_settled`` landed-filter)."""
    agent = make_agent()
    _seed_unwinnable_race(agent, pr=3112, landed=True)

    await agent._audit_self_model(iteration=5)

    assert agent.message_store.get_thread("unwinnable-race-3112") == []


@pytest.mark.asyncio
async def test_audit_self_model_unwinnable_race_below_threshold_silent(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A lineage that re-entered fewer times than the threshold stays silent
    (2 base moves < the default threshold of 3)."""
    agent = make_agent()
    # Three laps → bases a→b→c → 2 re-entries, one below the threshold.
    _seed_unwinnable_race(agent, pr=3112, landed=False, laps=3)

    await agent._audit_self_model(iteration=5)

    assert agent.message_store.get_thread("unwinnable-race-3112") == []


@pytest.mark.asyncio
async def test_audit_self_model_unwinnable_race_skips_closed_unmerged_pr(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A closed-but-unmerged PR (settled yet not ``branch_landed``) earns no
    heads-up: the settled-filter must skip it, not just merged PRs. Otherwise a
    PR Thomas already closed would be told to "merge it yourself" — and this
    deliverer records no stand-down whose auto-release would ever retract it."""
    agent = make_agent()
    _seed_unwinnable_race(agent, pr=3112, closed_unmerged=True)

    await agent._audit_self_model(iteration=5)

    assert agent.message_store.get_thread("unwinnable-race-3112") == []


@pytest.mark.asyncio
async def test_audit_self_model_unwinnable_race_window_outlasts_audit_tail(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The actuator measures re-entries over a dedicated window wider than the
    20-task audit tail, so a stuck PR whose laps interleave with other work still
    reaches the threshold. Seed 4 racing laps, then 20 filler non-remediation
    tasks: the racing laps fall outside a 20-task view but stay inside the wider
    window, so the heads-up still fires."""
    from maestro.memory import TaskRecord

    agent = make_agent()
    _seed_unwinnable_race(agent, pr=3113, landed=False)
    # Push the racing laps out of any 20-task tail: 20 newer, unrelated tasks.
    for i in range(20):
        agent.memory.record_task(TaskRecord(
            task_id=f"filler-{i}",
            goal=f"Evolve subsystem {i}",  # no "Remediate PR #N" → not a lap
            plan=[],
            outcome="success",
            result_summary="unrelated work",
            errors=[],
            duration_secs=1.0,
            timestamp=f"2026-07-20T{i:02d}:00:00+00:00",
        ))
    # Sanity: a 20-task tail would see none of PR #3113's laps.
    tail = agent.memory.get_recent_tasks(20)
    assert all("#3113" not in (t.goal or "") for t in tail)

    await agent._audit_self_model(iteration=5)

    assert len(agent.message_store.get_thread("unwinnable-race-3113")) == 1


def _seed_settled_phantom_escalation(agent, pr: int = 2579) -> None:  # type: ignore[no-untyped-def]
    """Seed a phantom-escalation partial on a settled ``Remediate PR #N`` lap.

    A ``partial`` remediation task on PR #*pr* whose summary escalates it with
    no outbound message (rule 12 fires) and no not-landed / SHA markers (rules
    10/14 never shell to git); ``is_pr_settled`` is mocked to report the PR
    settled (merged), so the reconciler records it and the ``settled_prs``
    pre-filter can darken the finding when the flag is on.
    """
    from maestro.consciousness import SelfModel
    from maestro.memory import TaskRecord

    agent.consciousness.save_self_model(SelfModel())  # empty directive
    agent.memory.record_task(TaskRecord(
        task_id="phantom-esc-task-1",
        goal=f"Remediate PR #{pr}: fix the gate",
        plan=[],
        outcome="partial",
        result_summary=f"Escalating PR #{pr} once — no reply from Thomas yet.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-15T00:00:00+00:00",
    ))
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=pr,
            settled=True,
            state="MERGED",
            branch_landed=True,
            source="host_api",
            observed_at=0.0,
        ),
    )


@pytest.mark.asyncio
async def test_audit_self_model_settlement_reconciler_suppresses_phantom(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Proposal 097 dispatch: with ``settlement_reconciler_enabled`` on, a
    settled PR present in the trailing history is reconciled and threaded into
    the phantom rules, so a full audit cycle yields zero phantom findings citing
    that PR — the finding genuinely self-extinguishes."""
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    _seed_settled_phantom_escalation(agent, pr=2579)

    await agent._audit_self_model(iteration=8)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert not any(
        "[phantom_escalation]" in e.content and "#2579" in e.content
        for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] phantom_escalation:") and "#2579" in q
        for q in reloaded.open_questions
    )
    # The reconciler was consulted for the PR (flag on).
    agent.is_pr_settled.assert_called_with(2579)


@pytest.mark.asyncio
async def test_audit_self_model_settlement_reconciler_off_keeps_phantom(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Baseline: with the reconciler flag set off (the committed default is now
    on, so the test disables it explicitly), the empty ``settled_prs`` is
    threaded everywhere, so the same settled-PR history still surfaces the
    phantom-escalation finding — byte-identical to pre-change — and
    ``is_pr_settled`` is never consulted (no ledger observation)."""
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = False
    _seed_settled_phantom_escalation(agent, pr=2579)

    await agent._audit_self_model(iteration=9)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert any(
        "[phantom_escalation]" in e.content and "#2579" in e.content
        for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] phantom_escalation:") and "#2579" in q
        for q in reloaded.open_questions
    )
    # Flag off ⇒ the reconciler never runs, so ``is_pr_settled`` is untouched.
    agent.is_pr_settled.assert_not_called()


def _seed_escalation_standdown(  # type: ignore[no-untyped-def]
    agent, mem_fs: dict[str, str], pr: int,
) -> None:
    """Seed a ``pr``-keyed escalation standdown in the agent's memory dir.

    Mirrors the ``escalation_handoffs.json`` record shape written elsewhere in
    this module so ``escalation_bridge.has_standdown`` / ``clear_standdown`` see
    a live standdown for *pr*.
    """
    ledger_path = str(agent.config.memory_dir / "escalation_handoffs.json")
    existing = json.loads(mem_fs[ledger_path]) if mem_fs.get(ledger_path) else {}
    existing[str(pr)] = {
        "head_sha": "h", "delivered_at": "2026-01-01T00:00:00+00:00",
        "serviced_comment_ids": [],
    }
    mem_fs[ledger_path] = json.dumps(existing)


@pytest.mark.asyncio
async def test_audit_self_model_settlement_record_reconciler_clears_standdown(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103 drive: with both the 097 and 103 flags on, a full audit cycle
    reconciles a settled PR's escalation standdown under the one on-settlement
    record reconciler — the standdown is gone and one ``escalation_standdown``
    provenance row lands in ``settlement_reconciliations.jsonl``."""
    from maestro import escalation_bridge

    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    agent.config.settlement_record_reconciler_enabled = True
    _seed_settled_phantom_escalation(agent, pr=2579)
    _seed_escalation_standdown(agent, mem_fs, 2579)
    # In production ``reconcile_settled_verdicts`` warms the per-iteration merge-
    # truth memo for every window-named PR by calling the real ``is_pr_settled``;
    # the drive reconciles only PRs already in that memo. Here ``is_pr_settled``
    # is stubbed (no git I/O), so seed the memo directly to model that warm state.
    agent._merge_truth_memo[2579] = MergeTruth(
        pr_number=2579, settled=True, state="MERGED", branch_landed=True,
        source="host_api", observed_at=0.0,
    )
    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 2579) is True

    await agent._audit_self_model(iteration=11)

    assert (
        escalation_bridge.has_standdown(agent.escalation_standdowns, 2579) is False
    )
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    rows = [
        json.loads(line)
        for line in mem_fs[recon_path].splitlines()
        if line.strip()
    ]
    assert any(
        row["reconciler"] == "escalation_standdown"
        and row["pr_number"] == 2579
        and row["cleared"] is True
        for row in rows
    )


@pytest.mark.asyncio
async def test_audit_self_model_settlement_record_reconciler_off_keeps_standdown(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Baseline: with the 103 flag off (its committed default), the same
    settled-PR audit cycle leaves the escalation standdown intact and writes no
    reconciliation ledger — the drive never runs."""
    from maestro import escalation_bridge

    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    agent.config.settlement_record_reconciler_enabled = False
    _seed_settled_phantom_escalation(agent, pr=2579)
    _seed_escalation_standdown(agent, mem_fs, 2579)

    await agent._audit_self_model(iteration=12)

    assert (
        escalation_bridge.has_standdown(agent.escalation_standdowns, 2579) is True
    )
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    assert recon_path not in mem_fs


@pytest.mark.asyncio
async def test_audit_self_model_settlement_record_reconciler_skips_unobserved_pr(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103 cost guard: a PR settled in the durable ledger but **not**
    observed this cycle (no ``Remediate PR #N`` record in the trailing window, so
    its merge-truth memo is cold) is skipped by the drive — its escalation
    standdown is left intact, no reconciliation row is written, and
    ``is_pr_settled`` is never consulted for it. This pins the fix that the drive
    adds zero merge-oracle observations and cannot grow the ledger unboundedly
    over ledger-only PRs the standing reconciler never re-touches."""
    from maestro import escalation_bridge

    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    agent.config.settlement_record_reconciler_enabled = True
    # A PR effectively settled in ``settled_verdicts.jsonl`` (so the standing
    # reconciler returns it in ``settled_prs``) but named by no trailing task, so
    # ``reconcile_settled_verdicts`` never observes it and its memo stays cold.
    mem_fs[str(agent.config.memory_dir / "settled_verdicts.jsonl")] = (
        '{"pr_number": 4242, "settled_at_iteration": 3, "kind": "merged"}\n'
    )
    _seed_escalation_standdown(agent, mem_fs, 4242)
    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 4242) is True

    await agent._audit_self_model(iteration=13)

    # Cold-memo PR skipped: standdown intact, no reconciliation ledger, and the
    # chokepoint was never asked — no fresh merge-truth observation was made.
    assert (
        escalation_bridge.has_standdown(agent.escalation_standdowns, 4242) is True
    )
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    assert recon_path not in mem_fs
    agent.is_pr_settled.assert_not_called()


def _save_settled_plan(  # type: ignore[no-untyped-def]
    agent, plan_id: str, *, pr_url: str | None,
) -> None:
    """Save a minimal ``implemented`` evolution plan carrying ``pr_url``.

    Mirrors the plan shape the ``check_merged_plans`` transition drive
    (proposal 103) resolves to a PR number via ``_pr_number_from_url``.
    """
    from maestro.memory import EvolutionPlan

    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id=plan_id,
        title=f"Plan {plan_id}",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="implemented",
        created_at="2026-01-01T00:00:00Z",
        pr_url=pr_url,
    ))


def _reconciliation_rows(  # type: ignore[no-untyped-def]
    agent, mem_fs: dict[str, str],
) -> list[dict[str, object]]:
    """Read the ``settlement_reconciliations.jsonl`` provenance rows, if any."""
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    if recon_path not in mem_fs:
        return []
    return [
        json.loads(line) for line in mem_fs[recon_path].splitlines() if line.strip()
    ]


@pytest.mark.asyncio
async def test_reconcile_settled_merge_check_drives_merged_pr(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103 transition drive: a just-merged plan's PR is reconciled the
    instant ``check_merged_plans`` reports it settled — its escalation standdown
    is cleared and one ``escalation_standdown`` provenance row lands, without
    waiting for a later audit cycle to warm-memo it."""
    from maestro import escalation_bridge
    from maestro.evolution import MergeCheckResult

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = True
    _save_settled_plan(agent, "merged-plan", pr_url="https://github.com/o/r/pull/5501")
    _seed_escalation_standdown(agent, mem_fs, 5501)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=5501, settled=True, state="MERGED", branch_landed=True,
            source="host_api", observed_at=0.0,
        ),
    )
    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5501) is True

    agent._reconcile_settled_merge_check(
        MergeCheckResult(merged=["merged-plan"]), iteration=20,
    )

    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5501) is False
    agent.is_pr_settled.assert_called_once_with(5501)
    rows = _reconciliation_rows(agent, mem_fs)
    assert any(
        row["reconciler"] == "escalation_standdown"
        and row["pr_number"] == 5501
        and row["cleared"] is True
        for row in rows
    )


@pytest.mark.asyncio
async def test_reconcile_settled_merge_check_drives_rejected_pr(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A just-closed (rejected) plan's PR is driven too — ``settled=True`` with
    ``branch_landed=False`` still clears the head-agnostic escalation standdown
    and writes its provenance row, exercising the ``rejected`` branch of the
    settled-plan gather."""
    from maestro import escalation_bridge
    from maestro.evolution import MergeCheckResult

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = True
    _save_settled_plan(agent, "closed-plan", pr_url="https://github.com/o/r/pull/5502")
    _seed_escalation_standdown(agent, mem_fs, 5502)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=5502, settled=True, state="CLOSED", branch_landed=False,
            source="host_api", observed_at=0.0,
        ),
    )
    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5502) is True

    agent._reconcile_settled_merge_check(
        MergeCheckResult(rejected=[("closed-plan", "closed without merge")]),
        iteration=21,
    )

    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5502) is False
    agent.is_pr_settled.assert_called_once_with(5502)
    rows = _reconciliation_rows(agent, mem_fs)
    assert any(
        row["reconciler"] == "escalation_standdown"
        and row["pr_number"] == 5502
        and row["cleared"] is True
        for row in rows
    )


@pytest.mark.asyncio
async def test_reconcile_settled_merge_check_flag_off_is_noop(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """With the default-off flag off, the transition drive is a pure no-op: the
    escalation standdown is left intact, the reconciliation ledger is never
    created, and the merge-truth chokepoint is never consulted."""
    from maestro import escalation_bridge
    from maestro.evolution import MergeCheckResult

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = False
    _save_settled_plan(agent, "merged-plan", pr_url="https://github.com/o/r/pull/5503")
    _seed_escalation_standdown(agent, mem_fs, 5503)
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    agent._reconcile_settled_merge_check(
        MergeCheckResult(merged=["merged-plan"]), iteration=22,
    )

    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5503) is True
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    assert recon_path not in mem_fs
    agent.is_pr_settled.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_settled_merge_check_skips_unresolvable_plans(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Plans that cannot be resolved to a PR are skipped without raising: a plan
    ID with no saved plan, a plan with no ``pr_url``, and a plan with an
    unparsable ``pr_url``. The one resolvable PR in the batch is still driven,
    and the chokepoint is consulted only for it."""
    from maestro import escalation_bridge
    from maestro.evolution import MergeCheckResult

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = True
    _save_settled_plan(agent, "no-url-plan", pr_url=None)
    _save_settled_plan(
        agent, "bad-url-plan", pr_url="https://github.com/o/r/pull/not-a-number",
    )
    _save_settled_plan(agent, "good-plan", pr_url="https://github.com/o/r/pull/5504")
    _seed_escalation_standdown(agent, mem_fs, 5504)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=5504, settled=True, state="MERGED", branch_landed=True,
            source="host_api", observed_at=0.0,
        ),
    )

    # ``ghost-plan`` was never saved (``get_plan`` -> None); the run must not raise.
    agent._reconcile_settled_merge_check(
        MergeCheckResult(
            merged=["ghost-plan", "no-url-plan", "bad-url-plan", "good-plan"],
        ),
        iteration=23,
    )

    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5504) is False
    agent.is_pr_settled.assert_called_once_with(5504)
    rows = _reconciliation_rows(agent, mem_fs)
    # The one resolvable PR is driven and produces rows (its standdown clear plus
    # the record-only settled-verdict entry); the three unresolvable plans produce
    # none, so 5504 is the sole PR the reconciliation ledger names.
    assert rows
    assert {row["pr_number"] for row in rows} == {5504}


@pytest.mark.asyncio
async def test_reconcile_settled_merge_check_dedupes_distinct_prs(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Two settled plans pointing at the same PR drive that PR exactly once —
    the chokepoint is consulted a single time for the distinct number."""
    from maestro.evolution import MergeCheckResult

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = True
    _save_settled_plan(agent, "dup-a", pr_url="https://github.com/o/r/pull/5506")
    _save_settled_plan(agent, "dup-b", pr_url="https://github.com/o/r/pull/5506")
    _seed_escalation_standdown(agent, mem_fs, 5506)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=5506, settled=True, state="MERGED", branch_landed=True,
            source="host_api", observed_at=0.0,
        ),
    )

    agent._reconcile_settled_merge_check(
        MergeCheckResult(merged=["dup-a", "dup-b"]), iteration=24,
    )

    agent.is_pr_settled.assert_called_once_with(5506)


@pytest.mark.asyncio
async def test_reconcile_settled_merge_check_unsettled_leaves_store(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """The reconciler guard holds through the transition drive: if the
    chokepoint reports the PR *not* settled (e.g. reopened), the standdown is
    left intact and no reconciliation row is written, even though the drive ran
    and consulted the chokepoint."""
    from maestro import escalation_bridge
    from maestro.evolution import MergeCheckResult

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = True
    _save_settled_plan(agent, "reopened-plan", pr_url="https://github.com/o/r/pull/5505")
    _seed_escalation_standdown(agent, mem_fs, 5505)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=5505, settled=False, state="OPEN", branch_landed=False,
            source="host_api", observed_at=0.0,
        ),
    )

    agent._reconcile_settled_merge_check(
        MergeCheckResult(merged=["reopened-plan"]), iteration=25,
    )

    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5505) is True
    agent.is_pr_settled.assert_called_once_with(5505)
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    assert recon_path not in mem_fs


@pytest.mark.asyncio
async def test_reconcile_settled_merge_check_upserts_settled_verdicts_ledger(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103 step 6: driving a merged plan-PR through the merge-transition
    drive upserts it into the window-independent ``settled_verdicts.jsonl``
    pre-filter, so a later ``reconcile_settled_verdicts`` returns it from the
    ledger even though this PR was never remediated and its ``Remediate PR #N``
    record never rode the reconciler window."""
    from maestro.audit_checks_settlement import (
        load_settled_prs,
        reconcile_settled_verdicts,
    )
    from maestro.evolution import MergeCheckResult
    from maestro.memory import TaskRecord

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = True
    _save_settled_plan(
        agent, "merged-plan", pr_url="https://github.com/o/r/pull/5601",
    )
    settled = MergeTruth(
        pr_number=5601, settled=True, state="MERGED", branch_landed=True,
        source="host_api", observed_at=0.0,
    )
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=settled,
    )

    agent._reconcile_settled_merge_check(
        MergeCheckResult(merged=["merged-plan"]), iteration=30,
    )

    # The settlement entered the durable ledger with the right kind and a null
    # verdict task id (this observation is not tied to a remediation verdict).
    ledger_path = str(agent.config.memory_dir / "settled_verdicts.jsonl")
    rows = [
        json.loads(line) for line in mem_fs[ledger_path].splitlines() if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["pr_number"] == 5601
    assert rows[0]["kind"] == "merged"
    assert rows[0]["verdict_task_id"] is None
    assert load_settled_prs(agent.settled_verdicts) == frozenset({5601})

    # Even a windowed not-landed remediation verdict for the PR does not change
    # the result: the reconciler surfaces 5601 as settled from the durable
    # ledger, not from any verdict riding the reconciler window.
    agent.memory.record_task(TaskRecord(
        task_id="phantom",
        goal="Remediate PR #5601: Fix the widget",
        plan=[],
        outcome="partial",
        result_summary="Resolved on branch — PR #5601 not yet landed.",
        errors=[],
        duration_secs=0.5,
        timestamp="2026-07-20T00:00:00+00:00",
    ))
    settled_prs = reconcile_settled_verdicts(
        agent.config.memory_dir, 20, 31, is_pr_settled=agent.is_pr_settled,
        backend=agent.settled_verdicts,
    )
    assert 5601 in settled_prs


@pytest.mark.asyncio
async def test_reconcile_settled_merge_check_flag_off_writes_no_settled_ledger(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103 step 6, flag off: the merge-transition drive writes no
    ``settled_verdicts.jsonl`` row and never consults the chokepoint, so the
    ledger upsert inherits the default-off gate exactly as the reconciler drive
    does."""
    from maestro.evolution import MergeCheckResult

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = False
    _save_settled_plan(
        agent, "merged-plan", pr_url="https://github.com/o/r/pull/5602",
    )
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    agent._reconcile_settled_merge_check(
        MergeCheckResult(merged=["merged-plan"]), iteration=32,
    )

    ledger_path = str(agent.config.memory_dir / "settled_verdicts.jsonl")
    assert ledger_path not in mem_fs
    agent.is_pr_settled.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_settled_tend_prs_drives_gone_pr(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103 tend drive: a PR that left the open evolution-PR set this lap
    (in ``settled_pr_numbers``) is reconciled — its escalation standdown is
    cleared and one ``escalation_standdown`` provenance row lands — without any
    plan→PR-URL resolution, since the candidates are PR numbers already."""
    from maestro import escalation_bridge
    from maestro.pr_facts import MergeTruth

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = True
    _seed_escalation_standdown(agent, mem_fs, 5701)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=5701, settled=True, state="MERGED", branch_landed=True,
            source="host_api", observed_at=0.0,
        ),
    )
    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5701) is True

    agent._reconcile_settled_tend_prs(
        TendPRsResult(settled_pr_numbers=[5701]), iteration=40,
    )

    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5701) is False
    agent.is_pr_settled.assert_called_once_with(5701)
    rows = _reconciliation_rows(agent, mem_fs)
    assert any(
        row["reconciler"] == "escalation_standdown"
        and row["pr_number"] == 5701
        and row["cleared"] is True
        for row in rows
    )


@pytest.mark.asyncio
async def test_reconcile_settled_tend_prs_flag_off_is_noop(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """With the default-off flag off, the tend drive is a pure no-op: the
    escalation standdown survives, the reconciliation ledger is never created,
    and the merge-truth chokepoint is never consulted."""
    from maestro import escalation_bridge

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = False
    _seed_escalation_standdown(agent, mem_fs, 5702)
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    agent._reconcile_settled_tend_prs(
        TendPRsResult(settled_pr_numbers=[5702]), iteration=41,
    )

    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5702) is True
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    assert recon_path not in mem_fs
    agent.is_pr_settled.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_settled_tend_prs_unsettled_leaves_store(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """The per-PR gate holds through the tend drive: if the chokepoint reports a
    disappeared PR *not* settled (a host hiccup, a renamed branch), the standdown
    is left intact and no reconciliation row is written, even though the drive ran
    and consulted the chokepoint."""
    from maestro import escalation_bridge
    from maestro.pr_facts import MergeTruth

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = True
    _seed_escalation_standdown(agent, mem_fs, 5703)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=5703, settled=False, state="OPEN", branch_landed=False,
            source="host_api", observed_at=0.0,
        ),
    )

    agent._reconcile_settled_tend_prs(
        TendPRsResult(settled_pr_numbers=[5703]), iteration=42,
    )

    assert escalation_bridge.has_standdown(agent.escalation_standdowns, 5703) is True
    agent.is_pr_settled.assert_called_once_with(5703)
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    assert recon_path not in mem_fs


@pytest.mark.asyncio
async def test_reconcile_settled_tend_prs_dedupes_repeated_numbers(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A ``settled_pr_numbers`` list carrying the same PR twice drives that PR
    exactly once — the chokepoint is consulted a single time for the distinct
    number."""
    from maestro.pr_facts import MergeTruth

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = True
    _seed_escalation_standdown(agent, mem_fs, 5704)
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=5704, settled=True, state="MERGED", branch_landed=True,
            source="host_api", observed_at=0.0,
        ),
    )

    agent._reconcile_settled_tend_prs(
        TendPRsResult(settled_pr_numbers=[5704, 5704]), iteration=43,
    )

    agent.is_pr_settled.assert_called_once_with(5704)


@pytest.mark.asyncio
async def test_reconcile_settled_tend_prs_empty_is_noop(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """An empty ``settled_pr_numbers`` (the common lap where no PR left the open
    set) drives nothing — the chokepoint is never consulted."""

    agent = make_agent()
    agent.config.settlement_record_reconciler_enabled = True
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    agent._reconcile_settled_tend_prs(TendPRsResult(), iteration=44)

    agent.is_pr_settled.assert_not_called()


@pytest.mark.asyncio
async def test_audit_self_model_record_reconciler_evicts_self_model_rows(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103 (store 2): with both the 097 and 103 flags on, a full audit
    cycle evicts a settled PR's sole-cited self-model traces — the
    ``[limitation]`` ``open_questions`` row, the ``limitations_known`` source
    behind it, and a sole-PR ``next_task_directive`` — and lands one
    ``self_model`` provenance row in ``settlement_reconciliations.jsonl``. The
    drive runs *after* the audit's own trailing ``save_self_model``, so its disk
    read-modify-write is authoritative and is not clobbered by that save. The
    seeded error-repetition history below guarantees the audit produces a finding
    so that trailing save actually *fires*: were the drive mis-ordered back before
    the save, that save would rewrite the stale in-memory model (still carrying the
    #3131 traces) over the reconciler's eviction and fail this test — the ordering
    is otherwise invisible on a finding-free cycle where the save is skipped."""
    from maestro.consciousness import SelfModel
    from maestro.memory import TaskRecord

    settled = MergeTruth(
        pr_number=3131, settled=True, state="MERGED", branch_landed=True,
        source="host_api", observed_at=0.0,
    )
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    agent.config.settlement_record_reconciler_enabled = True
    agent.consciousness.save_self_model(SelfModel(
        limitations_known=["Blocked forever on PR #3131"],
        open_questions=["[limitation] PR #3131 is stuck", "What am I?"],
        next_task_directive="Finish PR #3131",
    ))
    # Seed three tasks sharing one error fingerprint the seeded limitation does
    # not cover, so rule 3 (``repeated_error_without_limitation``) fires and the
    # audit's trailing ``save_self_model`` runs — the regression guard for the
    # drive's after-save ordering. The error text cites no ``#NNNN``, so the
    # surfaced ``[audit]`` question is not itself a sole-#3131 row the reconciler
    # would evict.
    for i in range(3):
        agent.memory.record_task(TaskRecord(
            task_id=f"err-task-{i}",
            goal="Investigate the flaky integration harness",
            plan=[],
            outcome="partial",
            result_summary="hit the same failure again",
            errors=["Network timeout after 30s"],
            duration_secs=1.0,
            timestamp="2026-07-20T00:00:00+00:00",
        ))
    # A settled PR in the durable 097 ledger so ``reconcile_settled_verdicts``
    # returns it in ``settled_prs``, plus a warm merge-truth memo so the record-
    # reconciler drive reaches it (a cold-memo ledger-only PR is skipped — see the
    # cost-guard test above).
    mem_fs[str(agent.config.memory_dir / "settled_verdicts.jsonl")] = (
        '{"pr_number": 3131, "settled_at_iteration": 2, "kind": "merged"}\n'
    )
    agent._merge_truth_memo[3131] = settled
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=settled,
    )

    await agent._audit_self_model(iteration=14)

    reloaded = agent.consciousness.load_self_model()
    # The sole-PR limitation source and its surfaced row are both gone; the
    # unrelated question survives.
    assert not any("#3131" in x for x in reloaded.limitations_known)
    assert not any(
        (q.startswith("[limitation") or q.startswith("[audit"))
        and "#3131" in q
        for q in reloaded.open_questions
    )
    assert "What am I?" in reloaded.open_questions
    # The sole-PR directive was cleared.
    assert reloaded.next_task_directive is None
    # One ``self_model`` provenance row landed.
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    rows = [
        json.loads(line)
        for line in mem_fs[recon_path].splitlines()
        if line.strip()
    ]
    assert any(
        row["reconciler"] == "self_model"
        and row["pr_number"] == 3131
        and row["cleared"] is True
        for row in rows
    )


@pytest.mark.asyncio
async def test_audit_self_model_record_reconciler_preserves_unsettled_multi(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103 guard: the conservative sole-PR rule never over-clears. A
    multi-PR ``limitations_known`` row that includes the settled PR, a
    ``[limitation]`` row citing a *different* (unsettled) PR, and a directive
    naming the settled PR *plus* another all survive the audit cycle, and no
    ``self_model`` provenance row is written because nothing changed."""
    from maestro.consciousness import SelfModel

    settled = MergeTruth(
        pr_number=3131, settled=True, state="MERGED", branch_landed=True,
        source="host_api", observed_at=0.0,
    )
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    agent.config.settlement_record_reconciler_enabled = True
    agent.consciousness.save_self_model(SelfModel(
        limitations_known=["Both #3131 and #4242 pending"],
        open_questions=["[limitation] other PR #4242", "What am I?"],
        next_task_directive="Land PR #3131 then hand off PR #4242",
    ))
    # Only #3131 is settled + memo-warm; #4242 is neither.
    mem_fs[str(agent.config.memory_dir / "settled_verdicts.jsonl")] = (
        '{"pr_number": 3131, "settled_at_iteration": 2, "kind": "merged"}\n'
    )
    agent._merge_truth_memo[3131] = settled
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=settled,
    )

    await agent._audit_self_model(iteration=15)

    reloaded = agent.consciousness.load_self_model()
    assert "Both #3131 and #4242 pending" in reloaded.limitations_known
    assert "[limitation] other PR #4242" in reloaded.open_questions
    assert reloaded.next_task_directive == "Land PR #3131 then hand off PR #4242"
    # Nothing changed, so the record-reconciler ledger is never created.
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    assert recon_path not in mem_fs


@pytest.mark.asyncio
async def test_audit_self_model_record_reconciler_evicts_remediation_tracker(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103 (store 3): with both the 097 and 103 flags on, a full audit
    cycle forgets a settled PR's per-PR remediation-tracker records — its no-op
    outcome list, items baseline, and skip counts — and lands one
    ``remediation_tracker`` provenance row in ``settlement_reconciliations.jsonl``.
    The reconciler mutates the agent's single long-lived
    ``remediation_tracker`` instance directly (it is bound to that instance in the
    agent-composed registry), so the eviction is observable on the same object the
    seed used. The escalation-standdown and self-model reconcilers have no seeded
    trace for this PR, so they are silent no-ops that leave the ledger at one row."""

    settled = MergeTruth(
        pr_number=3131, settled=True, state="MERGED", branch_landed=True,
        source="host_api", observed_at=0.0,
    )
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    agent.config.settlement_record_reconciler_enabled = True
    # Seed the PR across the tracker's axes so a full eviction is observable.
    agent.remediation_tracker.record_outcome(3131, noop=True, items_remaining=2)
    agent.remediation_tracker.record_outcome(3131, noop=True)
    agent.remediation_tracker.record_skip(3131)
    assert agent.remediation_tracker.should_reroute(3131) is False
    # A settled PR in the durable 097 ledger so ``reconcile_settled_verdicts``
    # returns it in ``settled_prs``, plus a warm merge-truth memo so the record-
    # reconciler drive reaches it (a cold-memo ledger-only PR is skipped).
    mem_fs[str(agent.config.memory_dir / "settled_verdicts.jsonl")] = (
        '{"pr_number": 3131, "settled_at_iteration": 2, "kind": "merged"}\n'
    )
    agent._merge_truth_memo[3131] = settled
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=settled,
    )

    await agent._audit_self_model(iteration=16)

    # The tracker forgot the PR on every axis.
    assert agent.remediation_tracker.should_reroute(3131) is True
    assert agent.remediation_tracker.last_items_remaining(3131) is None
    assert agent.remediation_tracker.skip_count(3131) == 0
    # One ``remediation_tracker`` provenance row landed.
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    rows = [
        json.loads(line)
        for line in mem_fs[recon_path].splitlines()
        if line.strip()
    ]
    assert any(
        row["reconciler"] == "remediation_tracker"
        and row["pr_number"] == 3131
        and row["cleared"] is True
        for row in rows
    )


@pytest.mark.asyncio
async def test_audit_self_model_record_reconciler_off_keeps_remediation_tracker(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Baseline: with the 103 flag off (its committed default), the same
    settled-PR audit cycle leaves the remediation-tracker records intact and
    writes no reconciliation ledger — the drive never runs."""

    settled = MergeTruth(
        pr_number=3131, settled=True, state="MERGED", branch_landed=True,
        source="host_api", observed_at=0.0,
    )
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    agent.config.settlement_record_reconciler_enabled = False
    agent.remediation_tracker.record_outcome(3131, noop=True)
    agent.remediation_tracker.record_outcome(3131, noop=True)
    assert agent.remediation_tracker.should_reroute(3131) is False
    mem_fs[str(agent.config.memory_dir / "settled_verdicts.jsonl")] = (
        '{"pr_number": 3131, "settled_at_iteration": 2, "kind": "merged"}\n'
    )
    agent._merge_truth_memo[3131] = settled
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=settled,
    )

    await agent._audit_self_model(iteration=17)

    # The records survive and no reconciliation ledger is written.
    assert agent.remediation_tracker.should_reroute(3131) is False
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    assert recon_path not in mem_fs


@pytest.mark.asyncio
async def test_audit_self_model_record_reconciler_evicts_pr_ledger(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 103 (store 5): with both the 097 and 103 flags on, a full audit
    cycle forgets a settled PR's per-PR remediation-ledger entry — its origin-head
    SHA, per-head attempt budget, and stuck latch — and lands one ``pr_ledger``
    provenance row in ``settlement_reconciliations.jsonl``. The reconciler is
    stateful (proposal 106): it holds the agent's own long-lived ``pr_ledger``
    and evicts through its flag-selected backend, so the clear is directly
    observable on that instance. The escalation-standdown, self-model,
    and stand-down-verdict reconcilers have no seeded trace for this PR, so they
    are silent no-ops that leave the reconciliation ledger at one row."""

    settled = MergeTruth(
        pr_number=3132, settled=True, state="MERGED", branch_landed=True,
        source="host_api", observed_at=0.0,
    )
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    agent.config.settlement_record_reconciler_enabled = True
    # Seed the PR's ledger entry so a full eviction is observable.
    agent.pr_ledger.observe_head(3132, "maestro/evolve/foo", "sha1")
    assert agent.pr_ledger.get(3132) is not None
    # A settled PR in the durable 097 ledger so ``reconcile_settled_verdicts``
    # returns it in ``settled_prs``, plus a warm merge-truth memo so the record-
    # reconciler drive reaches it (a cold-memo ledger-only PR is skipped).
    mem_fs[str(agent.config.memory_dir / "settled_verdicts.jsonl")] = (
        '{"pr_number": 3132, "settled_at_iteration": 2, "kind": "merged"}\n'
    )
    agent._merge_truth_memo[3132] = settled
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=settled,
    )

    await agent._audit_self_model(iteration=18)

    # The ledger forgot the settled PR.
    assert agent.pr_ledger.get(3132) is None
    # One ``pr_ledger`` provenance row landed.
    recon_path = str(
        agent.config.memory_dir / "settlement_reconciliations.jsonl"
    )
    rows = [
        json.loads(line)
        for line in mem_fs[recon_path].splitlines()
        if line.strip()
    ]
    assert any(
        row["reconciler"] == "pr_ledger"
        and row["pr_number"] == 3132
        and row["cleared"] is True
        for row in rows
    )


def _seed_scrolled_out_phantom_escalation(agent, pr: int = 2579) -> None:  # type: ignore[no-untyped-def]
    """Seed the scrolled-out shape: a settled PR in the ledger, not the window.

    The escalation-prose ``partial`` carries a **non-remediation** goal, so
    ``_REMEDIATION_GOAL_PATTERN`` never matches it and the reconciler's trailing
    window contains no ``Remediate PR #N`` record for *pr* — exactly the state
    reached once the remediation laps scroll off the tail while the claiming
    record (which keys rule 12 off the *summary*) survives. ``pr`` is present in
    the durable ``settled_verdicts.jsonl`` ledger from an earlier cycle, so only
    a ledger-backed pre-filter can darken the finding.
    """
    from maestro.consciousness import SelfModel
    from maestro.memory import TaskRecord

    agent.consciousness.save_self_model(SelfModel())  # empty directive
    agent.memory.record_task(TaskRecord(
        task_id="scrolled-esc-task-1",
        goal="Investigate the escalation delivery gate",
        plan=[],
        outcome="partial",
        result_summary=f"Escalating PR #{pr} once — no reply from Thomas yet.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-15T00:00:00+00:00",
    ))
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=MergeTruth(
            pr_number=pr,
            settled=True,
            state="MERGED",
            branch_landed=True,
            source="host_api",
            observed_at=0.0,
        ),
    )


@pytest.mark.asyncio
async def test_audit_self_model_ledger_prefilter_survives_window_scroll_out(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Proposal 097 durable dispatch: a PR recorded settled in
    ``settled_verdicts.jsonl`` but **absent** from the current task tail still
    darkens every phantom finding citing it across a full ``_audit_self_model``
    cycle.

    Before the durable set, the returned pre-filter was built only from PRs
    matched in this cycle's window, so the finding re-fired for as many cycles
    as the claiming record survived. ``is_pr_settled`` is never consulted here —
    proving the suppression came from the ledger, not a fresh observation.
    """
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = True
    _seed_scrolled_out_phantom_escalation(agent, pr=2579)
    mem_fs[str(agent.config.memory_dir / "settled_verdicts.jsonl")] = (
        '{"pr_number": 2579, "settled_at_iteration": 3, "kind": "merged"}\n'
    )

    await agent._audit_self_model(iteration=11)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert not any(
        "[phantom_escalation]" in e.content and "#2579" in e.content
        for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] phantom_escalation:") and "#2579" in q
        for q in reloaded.open_questions
    )
    # Ledger-backed, not window-bound: no ``Remediate PR #N`` record is in the
    # tail, so the chokepoint is never asked — the set came off disk.
    agent.is_pr_settled.assert_not_called()


@pytest.mark.asyncio
async def test_audit_self_model_ledger_prefilter_inert_when_flag_off(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag-off baseline for the scrolled-out shape: the injected ``settled_prs``
    is empty regardless of what the ledger holds, so the same history still
    surfaces the phantom-escalation finding and the ledger is never read."""
    agent = make_agent()
    agent.config.settlement_reconciler_enabled = False
    _seed_scrolled_out_phantom_escalation(agent, pr=2579)
    mem_fs[str(agent.config.memory_dir / "settled_verdicts.jsonl")] = (
        '{"pr_number": 2579, "settled_at_iteration": 3, "kind": "merged"}\n'
    )

    await agent._audit_self_model(iteration=12)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert any(
        "[phantom_escalation]" in e.content and "#2579" in e.content
        for e in audit_entries
    )
    reloaded = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] phantom_escalation:") and "#2579" in q
        for q in reloaded.open_questions
    )
    agent.is_pr_settled.assert_not_called()


@pytest.mark.asyncio
async def test_audit_phase_exception_propagates(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A rule bug / I/O error inside ``_audit_self_model`` propagates.

    ``audit_self_model`` is a deterministic, zero-SDK-cost phase: any
    exception is a real bug (rule misimplementation, file I/O failure)
    that must surface to the iteration-level handler in ``run_loop``,
    not be silently swallowed while ``dream`` continues over a corrupt
    audit history.
    """
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("rule blew up"),
    )
    dream_mock = AsyncMock()
    agent._phase_runners["dream"] = dream_mock
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="rule blew up"):
        await agent.run_loop(
            interval=0, max_iters=1,
            task_every=0, evolve_every=999,
            dream_every=1, audit_self_every=1,
        )

    agent._audit_self_model.assert_called_once()
    dream_mock.assert_not_called()


@pytest.mark.asyncio
async def test_contemplate_threads_audit_findings_into_update_self_model(
    make_agent,
) -> None:  # type: ignore[no-untyped-def]
    """Contemplation passes the latest audit findings to ``update_self_model``."""
    from maestro.audit_checks import AuditFinding
    from maestro.memory import AuditResult

    agent = make_agent()
    # Pre-seed an audit result so contemplate picks it up.
    agent.memory.append_audit_result(AuditResult(
        iteration=1,
        timestamp="2026-04-24T00:00:00Z",
        findings=[
            AuditFinding(
                kind="contradicted_claim",
                statement="claim X",
                cites=["tA"],
                rule="success_claim_vs_rate",
            ),
        ],
        rules_fired=["success_claim_vs_rate"],
    ))

    from maestro.consciousness import SelfModel as _SelfModel

    agent.consciousness.introspect = AsyncMock(return_value="some reflection")  # type: ignore[method-assign]
    update_mock = AsyncMock(return_value=_SelfModel())
    agent.consciousness.update_self_model = update_mock  # type: ignore[method-assign]

    await agent.contemplate()

    update_mock.assert_called_once()
    kwargs = update_mock.call_args.kwargs
    passed = kwargs.get("recent_audit_findings")
    assert passed is not None
    assert len(passed) == 1
    assert passed[0].kind == "contradicted_claim"
    assert passed[0].rule == "success_claim_vs_rate"


@pytest.mark.asyncio
async def test_contemplate_passes_none_when_no_audit_history(
    make_agent,
) -> None:  # type: ignore[no-untyped-def]
    """No audit history -> ``recent_audit_findings=None`` on the call."""
    from maestro.consciousness import SelfModel as _SelfModel

    agent = make_agent()
    agent.consciousness.introspect = AsyncMock(return_value="some reflection")  # type: ignore[method-assign]
    update_mock = AsyncMock(return_value=_SelfModel())
    agent.consciousness.update_self_model = update_mock  # type: ignore[method-assign]

    await agent.contemplate()

    kwargs = update_mock.call_args.kwargs
    assert kwargs.get("recent_audit_findings") is None


@pytest.mark.asyncio
async def test_contemplate_tolerates_audit_memory_read_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``get_most_recent_audit`` raising does not abort contemplation."""
    from maestro.consciousness import SelfModel as _SelfModel

    agent = make_agent()
    agent.consciousness.introspect = AsyncMock(return_value="reflection text")  # type: ignore[method-assign]
    update_mock = AsyncMock(return_value=_SelfModel())
    agent.consciousness.update_self_model = update_mock  # type: ignore[method-assign]

    with patch.object(
        agent.memory, "get_most_recent_audit",
        side_effect=RuntimeError("boom"),
    ):
        await agent.contemplate()

    update_mock.assert_called_once()
    # Failure path: ``recent_audit_findings`` stays None rather than crashing.
    assert update_mock.call_args.kwargs.get("recent_audit_findings") is None


@pytest.mark.asyncio
async def test_audit_self_model_propagates_journal_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Journal write failure propagates — uniform fail-loud with the other persists.

    The audit's three side-effects (``save_self_model``, the per-finding
    ``audit_observation`` journal writes, and ``append_audit_result``) all
    fail through the iteration-level handler. A journal I/O error must
    surface, not be swallowed — matching the method's docstring and the
    fail-loud invariant.
    """
    from maestro.audit_checks import AuditFinding

    agent = make_agent()
    findings = [
        AuditFinding(
            kind="contradicted_claim", statement="s",
            cites=["t1"], rule="success_claim_vs_rate",
        ),
    ]
    with (
        patch("maestro.audit_checks.run_all_checks", return_value=findings),
        patch.object(
            agent.journal, "record_event",
            side_effect=RuntimeError("journal down"),
        ),
        # The journal failure now propagates instead of being swallowed.
        pytest.raises(RuntimeError, match="journal down"),
    ):
        await agent._audit_self_model(iteration=4)

    # ``save_self_model`` runs before the journal loop, so the [audit]
    # open_question was persisted before the failure propagated.
    model = agent.consciousness.load_self_model()
    assert any(q.startswith("[audit]") for q in model.open_questions)

    # ``append_audit_result`` sits after the journal loop and is never
    # reached, so no AuditResult was recorded — the failure propagates
    # uniformly instead of being decorated with a partial record.
    assert agent.memory.get_most_recent_audit() is None


@pytest.mark.asyncio
async def test_audit_self_model_saves_before_writing_journal(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Self-model save MUST precede the audit_observation journal writes.

    Cross-file invariant: a crash between the two writes should leave
    persisted findings without a journal entry rather than the inverse
    (orphan journal entries citing findings the self-model never
    recorded). ``atomic_write`` only guarantees per-file atomicity, so
    ordering is the only lever we have.
    """
    from maestro.audit_checks import AuditFinding

    agent = make_agent()
    findings = [
        AuditFinding(
            kind="contradicted_claim", statement="s",
            cites=["t1"], rule="success_claim_vs_rate",
        ),
    ]

    call_order: list[str] = []
    original_save = agent.consciousness.save_self_model
    original_write = agent.journal.record_event

    def tracked_save(*args, **kwargs):  # type: ignore[no-untyped-def]
        call_order.append("save_self_model")
        return original_save(*args, **kwargs)

    def tracked_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        call_order.append("record_event")
        return original_write(*args, **kwargs)

    with (
        patch("maestro.audit_checks.run_all_checks", return_value=findings),
        patch.object(
            agent.consciousness, "save_self_model", side_effect=tracked_save,
        ),
        patch.object(
            agent.journal, "record_event", side_effect=tracked_write,
        ),
    ):
        await agent._audit_self_model(iteration=11)

    save_idx = call_order.index("save_self_model")
    write_idx = call_order.index("record_event")
    assert save_idx < write_idx, (
        f"save_self_model must precede record_event, got order: {call_order}"
    )


@pytest.mark.asyncio
async def test_audit_self_model_skips_stale_entry_corpus_when_flag_off(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """``audit_stale_entry_enabled=False`` (explicit override) → corpus is None.

    The flag is the single decision point for rule 6: when false, the
    agent must not pay the corpus-build cost and must pass
    ``stale_entry_corpus=None`` to ``run_all_checks`` so rule 6 stays
    silent.
    """
    from unittest.mock import MagicMock

    agent = make_agent()
    agent.config.audit_stale_entry_enabled = False

    spy = MagicMock(return_value=[])
    with patch("maestro.audit_checks.run_all_checks", side_effect=spy):
        await agent._audit_self_model(iteration=12)

    spy.assert_called_once()
    assert spy.call_args.kwargs.get("stale_entry_corpus") is None


@pytest.mark.asyncio
async def test_audit_self_model_builds_stale_entry_corpus_when_flag_on(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Flag on → corpus union of journal / tasks / audits / resolutions.

    Seeds one item per source so a single assertion proves all four
    feeds reach ``run_all_checks``.
    """
    from unittest.mock import MagicMock

    from maestro.audit_checks import AuditFinding
    from maestro.consciousness import SelfModel
    from maestro.memory import (
        AuditResult,
        PlanStep,
        QuestionResolution,
        TaskRecord,
    )

    agent = make_agent()
    agent.config.audit_stale_entry_enabled = True
    # Seed self-model with an entry whose tokens won't appear in the
    # corpus — but we don't care about rule 6 firing here, just that
    # the corpus is forwarded.
    agent.consciousness.save_self_model(SelfModel(
        capabilities_known=["alpha", "beta", "gamma"],
    ))
    agent.journal.write_thought(
        content="JOURNAL_TOKEN_journal_evidence",
        entry_type="reflection",
        triggered_by="agent_tool",
    )
    agent.memory.record_task(TaskRecord(
        task_id="t-stale-1",
        goal="TASK_TOKEN_task_evidence",
        plan=[PlanStep(step_id="s1", description="d", status="done")],
        outcome="success",
        result_summary="",
        errors=[],
        duration_secs=0.1,
        timestamp="2026-04-26T00:00:00+00:00",
        key_findings="FINDINGS_TOKEN_findings_evidence",
    ))
    agent.memory.append_audit_result(AuditResult(
        iteration=1,
        timestamp="2026-04-26T00:00:00+00:00",
        findings=[AuditFinding(
            kind="contradicted_claim",
            statement="AUDIT_TOKEN_audit_evidence",
            cites=[],
            rule="success_claim_vs_rate",
        )],
        rules_fired=["success_claim_vs_rate"],
    ))
    agent.memory.append_question_resolution(QuestionResolution(
        resolution_id="r1",
        iteration=1,
        timestamp="2026-04-26T00:00:00+00:00",
        question="Probe?",
        task_id="t1",
        outcome="success",
        disposition="kept",
        finding="RESOLUTION_TOKEN_resolution_evidence",
    ))

    spy = MagicMock(return_value=[])
    with patch("maestro.audit_checks.run_all_checks", side_effect=spy):
        await agent._audit_self_model(iteration=13)

    spy.assert_called_once()
    corpus = spy.call_args.kwargs.get("stale_entry_corpus")
    assert corpus is not None
    joined = "\n".join(corpus)
    assert "JOURNAL_TOKEN_journal_evidence" in joined
    assert "TASK_TOKEN_task_evidence" in joined
    assert "FINDINGS_TOKEN_findings_evidence" in joined
    assert "AUDIT_TOKEN_audit_evidence" in joined
    assert "RESOLUTION_TOKEN_resolution_evidence" in joined


@pytest.mark.asyncio
async def test_audit_self_model_writes_stale_entry_open_question_when_enabled(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """End-to-end: flag on + stale entry → ``[audit] stale_entry: …`` row."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    agent.config.audit_stale_entry_enabled = True
    # Three capabilities (cardinality floor satisfied), tokens chosen so
    # they won't accidentally overlap with the empty journal/tasks/audit
    # corpus the seeded agent has.
    agent.consciousness.save_self_model(SelfModel(
        capabilities_known=[
            "widget polishing",
            "lawnmower racing",
            "horseshoe sharpening",
        ],
    ))

    await agent._audit_self_model(iteration=14)

    audit_entries = agent.journal.read_events_by_type("audit_observation")
    assert any("[stale_entry]" in e.content for e in audit_entries)
    reloaded = agent.consciousness.load_self_model()
    assert any(
        q.startswith("[audit] stale_entry:")
        for q in reloaded.open_questions
    )


@pytest.mark.asyncio
async def test_audit_self_model_no_stale_entry_finding_when_disabled(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Flag off + same scenario → no ``stale_entry`` finding (other rules still run)."""
    from maestro.consciousness import SelfModel

    agent = make_agent()
    agent.config.audit_stale_entry_enabled = False
    agent.consciousness.save_self_model(SelfModel(
        capabilities_known=[
            "widget polishing",
            "lawnmower racing",
            "horseshoe sharpening",
        ],
    ))

    await agent._audit_self_model(iteration=15)

    reloaded = agent.consciousness.load_self_model()
    assert not any(
        q.startswith("[audit] stale_entry:")
        for q in reloaded.open_questions
    )


@pytest.mark.asyncio
async def test_audit_self_model_stale_entry_does_not_re_flag_same_entry(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Rule 6 fires once per stale entry per audit-history window.

    The audit's own outputs feed back into its corpus: the
    ``audit_observation`` journal entry written on cycle 1 and the
    matching ``AuditResult.findings[*].statement`` both contain the
    flagged entry's verbatim text. On the next cycle, that entry's
    tokens overlap with those corpus items via prefix-match, so the
    rule classifies the entry *live* and does not re-flag it — even
    though the LLM has not removed it. The per-rule cap of 1 means
    cycle 2 surfaces the next stale entry (advancing through the
    field), and once every stale entry is in audit history the rule
    falls silent until the audit window rolls past those rows. This
    is the documented design — the single nag per entry is enough
    attention; persistent re-firing would burn prompt budget without
    producing additional verdicts.
    """
    from maestro.consciousness import SelfModel

    agent = make_agent()
    agent.config.audit_stale_entry_enabled = True
    agent.consciousness.save_self_model(SelfModel(
        capabilities_known=[
            "widget polishing",
            "lawnmower racing",
            "horseshoe sharpening",
        ],
    ))

    # Cycle 1 — per-rule cap = 1, so exactly one stale_entry row.
    await agent._audit_self_model(iteration=20)
    after_first = [
        q for q in agent.consciousness.load_self_model().open_questions
        if q.startswith("[audit] stale_entry:")
    ]
    assert len(after_first) == 1
    flagged_first = after_first[0]
    assert "widget polishing" in flagged_first

    # Cycle 2 — LLM has not acted; "widget polishing" now appears in
    # the audit-observation journal entry and the AuditResult row from
    # cycle 1, so the feedback path classifies it live. The rule must
    # not re-flag it; the per-rule cap = 1 means at most one new row,
    # and that row must reference a *different* entry.
    await agent._audit_self_model(iteration=21)
    after_second = [
        q for q in agent.consciousness.load_self_model().open_questions
        if q.startswith("[audit] stale_entry:")
    ]
    # Exactly one new stale_entry row was added (or zero, if all
    # remaining entries also classify live somehow); none of them may
    # repeat the cycle-1 entry text.
    new_rows = [q for q in after_second if q not in after_first]
    assert len(new_rows) <= 1
    assert all("widget polishing" not in q for q in new_rows), (
        "rule 6 must not re-flag an entry already surfaced on a prior "
        "cycle — its own audit-history output classifies the entry "
        "live on the next pass"
    )




# ---------------------------------------------------------------------------
# Proposal 134 — the fix loop's impacted-scope rounds
# ---------------------------------------------------------------------------


def _tests_observation(*nodeids: str):  # type: ignore[no-untyped-def]
    """A red ``GateRunObservation`` whose tests gate failed at each *nodeid*."""
    from maestro.gate_observation import (
        GateFailure,
        GateObservation,
        GateRunObservation,
    )

    return GateRunObservation(
        gates=(
            GateObservation(tool="ruff", kind="CLEAN", failures=(), output=""),
            GateObservation(tool="mypy", kind="CLEAN", failures=(), output=""),
            GateObservation(
                tool="tests", kind="FAILED",
                failures=tuple(
                    GateFailure(
                        tool="tests", kind="FAILED",
                        location=nodeid.split("::", 1)[0],
                        identifier=nodeid, message="boom",
                    )
                    for nodeid in nodeids
                ),
                output="",
            ),
        ),
    )


def _timeout_observation():  # type: ignore[no-untyped-def]
    """A red run whose only identity is the synthetic ``tests:TIMEOUT``."""
    from maestro.gate_observation import GateObservation, GateRunObservation

    return GateRunObservation(
        gates=(
            GateObservation(tool="ruff", kind="CLEAN", failures=(), output=""),
            GateObservation(tool="mypy", kind="CLEAN", failures=(), output=""),
            GateObservation(tool="tests", kind="TIMEOUT", failures=(), output=""),
        ),
    )


def _red(observation) -> QualityGateResult:  # type: ignore[no-untyped-def]
    """Wrap *observation* as a failing gate result."""
    return QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output="red", observation=observation,
    )


def _green() -> QualityGateResult:
    """A passing gate result carrying an all-CLEAN observation."""
    return QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
        raw_output="clean", observation=_clean_observation(),
    )


def _scoped_agent(make_agent, impacted: tuple[str, ...]):  # type: ignore[no-untyped-def]
    """An agent with both proposal-134 reads on and a pinned impacted selection."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.gate_scope_impacted_enabled = True
    agent.evolution._impacted_scope = MagicMock(  # type: ignore[method-assign]
        return_value=(impacted, 400),
    )
    return agent


@pytest.mark.asyncio
async def test_converge_scope_round_one_compares_the_restricted_baseline(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Round 1 is judged against the pre-loop red *restricted to the narrowed suite*.

    The pre-loop read is full-scope and names a failure the selection cannot reach;
    comparing round 1's narrowed read against the unrestricted baseline would read
    the unreachable failure as newly cleared — progress the round did not make.
    """
    agent = _scoped_agent(make_agent, ("tests/test_alpha.py",))
    _seed_approved_plan(agent, "cg_scope_r1", "CG Scope R1")
    plan = agent.memory.get_plan("cg_scope_r1")
    assert plan is not None

    pre_loop = _red(_tests_observation(
        "tests/test_alpha.py::a", "tests/test_alpha.py::b", "tests/test_far.py::z",
    ))
    # The narrowed round clears one of the two in-scope failures: real progress
    # against the *restricted* baseline of two.
    round_one = _red(_tests_observation("tests/test_alpha.py::b"))
    # ...then the second narrowed round reads green, and the certification is red
    # on exactly the unreachable failure, which is a strict subset -> continue,
    # then auto_fix stops changing files and the loop ends honestly.
    round_two = _green()
    certification = _red(_tests_observation("tests/test_far.py::z"))
    # The certification is a strict subset -> the loop widens and continues; the
    # fixer then changes nothing, so the terminal takes its own full re-read.
    terminal_reread = _red(_tests_observation("tests/test_far.py::z"))
    calls: list[tuple[str, ...]] = []
    reads = [pre_loop, round_one, round_two, certification, terminal_reread]

    def _gates(*, scope: tuple[str, ...] = ()) -> QualityGateResult:
        calls.append(tuple(scope))
        return reads[len(calls) - 1]

    fixes = [True, True, False]

    async def _auto_fix(*args: Any, **kwargs: Any) -> bool:
        return fixes.pop(0)

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", side_effect=_auto_fix),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    # The pre-loop read and the certification are full; the two rounds are narrowed.
    assert calls == [
        (), ("tests/test_alpha.py",), ("tests/test_alpha.py",), (), (),
    ]
    # Round 1 was progress (2 -> 1 restricted), so the loop continued rather than
    # calling the unchanged full signature a stall.
    assert result.outcome == "stuck"  # the final not-fixed terminal
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[0] == "gate_scope"
    ]
    assert len(rows) == 1
    assert rows[0][1] == "narrowed"


@pytest.mark.asyncio
async def test_converge_scope_green_round_is_certified_full_scope(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A narrowed green issues exactly one full ``--cov`` read and returns green on it.

    The narrowed suite can only ever claim a subset is clean; only the full-scope,
    with-coverage certification may be a ``green`` terminal.
    """
    agent = _scoped_agent(make_agent, ("tests/test_alpha.py",))
    _seed_approved_plan(agent, "cg_scope_cert", "CG Scope Cert")
    plan = agent.memory.get_plan("cg_scope_cert")
    assert plan is not None

    reads = [
        _red(_tests_observation("tests/test_alpha.py::a")),  # pre-loop, full
        _green(),                                            # round 1, narrowed
        _green(),                                            # certification, full
    ]
    calls: list[tuple[str, ...]] = []

    def _gates(*, scope: tuple[str, ...] = ()) -> QualityGateResult:
        calls.append(tuple(scope))
        return reads[len(calls) - 1]

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=True)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "green"
    assert result.review is reads[2]  # the certification, not the narrowed read
    assert calls == [(), ("tests/test_alpha.py",), ()]
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[0] == "gate_scope"
    ]
    assert len(rows) == 1
    assert "certified=1" in rows[0][2]


@pytest.mark.asyncio
async def test_converge_scope_unchanged_red_certification_is_stuck(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A certification identical to the pre-loop full red ends ``stuck``.

    The anti-oscillation property: without it the loop could clear the subset,
    certify red, clear it again, and grind the ceiling away making no real
    progress.
    """
    agent = _scoped_agent(make_agent, ("tests/test_alpha.py",))
    _seed_approved_plan(agent, "cg_scope_stuck", "CG Scope Stuck")
    plan = agent.memory.get_plan("cg_scope_stuck")
    assert plan is not None

    full_red = _tests_observation("tests/test_alpha.py::a", "tests/test_far.py::z")
    reads = [_red(full_red), _green(), _red(full_red)]
    calls: list[tuple[str, ...]] = []

    def _gates(*, scope: tuple[str, ...] = ()) -> QualityGateResult:
        calls.append(tuple(scope))
        return reads[len(calls) - 1]

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=True)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "stuck"
    assert result.rounds_used == 2  # the round plus the certification it spent
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_converge_scope_subset_certification_widens_the_scope(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A strict-subset red certification continues and widens the selection.

    The surviving failure sits outside the diff's own modules, so the next
    narrowed rounds must include the module it names or the fixer would be graded
    against a suite that cannot see its own red.
    """
    agent = _scoped_agent(make_agent, ("tests/test_alpha.py",))
    _seed_approved_plan(agent, "cg_scope_widen", "CG Scope Widen")
    plan = agent.memory.get_plan("cg_scope_widen")
    assert plan is not None

    reads = [
        _red(_tests_observation("tests/test_alpha.py::a", "tests/test_far.py::z")),
        _green(),                                          # round 1, narrowed
        _red(_tests_observation("tests/test_far.py::z")),   # certification: subset
        _red(_tests_observation("tests/test_far.py::z")),   # round 2, widened
        _red(_tests_observation("tests/test_far.py::z")),   # terminal re-read, full
    ]
    calls: list[tuple[str, ...]] = []

    def _gates(*, scope: tuple[str, ...] = ()) -> QualityGateResult:
        calls.append(tuple(scope))
        return reads[len(calls) - 1]

    fixes = [True, True, False]

    async def _auto_fix(*args: Any, **kwargs: Any) -> bool:
        return fixes.pop(0)

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", side_effect=_auto_fix),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert calls[3] == ("tests/test_alpha.py", "tests/test_far.py")
    assert result.outcome == "stuck"
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[0] == "gate_scope"
    ]
    assert "widened=2" in rows[0][2]


@pytest.mark.asyncio
async def test_converge_scope_timeout_only_red_runs_full_rounds(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A pre-loop red of nothing but ``tests:TIMEOUT`` discards the narrowing.

    The restriction is empty, so there is no baseline a narrowed round could be
    compared against; the loop runs full-scope rounds exactly as it does today and
    the row records that the narrow was discarded.
    """
    agent = _scoped_agent(make_agent, ("tests/test_alpha.py",))
    _seed_approved_plan(agent, "cg_scope_timeout", "CG Scope Timeout")
    plan = agent.memory.get_plan("cg_scope_timeout")
    assert plan is not None

    calls: list[tuple[str, ...]] = []

    def _gates(*, scope: tuple[str, ...] = ()) -> QualityGateResult:
        calls.append(tuple(scope))
        return _red(_timeout_observation())

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "stuck"
    assert calls == [(), ()]  # the pre-loop read and the not-fixed terminal re-read
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[0] == "gate_scope"
    ]
    assert rows[0][1] == "full"
    assert "discarded" in rows[0][2]


@pytest.mark.asyncio
async def test_converge_scope_flag_off_is_round_for_round_identical(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Flag off, every gate call is the no-argument one and no ``gate_scope`` row lands.

    The rollback guarantee for the whole seam: the selection is never computed, the
    scope keyword is never passed, and the activation ledger is untouched.
    """
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    assert agent.config.gate_scope_impacted_enabled is False
    _seed_approved_plan(agent, "cg_scope_off", "CG Scope Off")
    plan = agent.memory.get_plan("cg_scope_off")
    assert plan is not None

    reads = [
        _red(_tests_observation("tests/test_alpha.py::a", "tests/test_alpha.py::b")),
        _red(_tests_observation("tests/test_alpha.py::b")),
        _green(),
    ]
    calls = 0

    def _gates() -> QualityGateResult:
        nonlocal calls
        calls += 1
        return reads[calls - 1]

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=True)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
        patch.object(
            agent.evolution, "_impacted_scope", MagicMock(return_value=((), 0)),
        ) as scope_mock,
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "green"
    assert result.rounds_used == 2
    assert calls == 3  # no certification read
    scope_mock.assert_called_once_with(plan)
    assert not [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[0] == "gate_scope"
    ]


@pytest.mark.asyncio
async def test_converge_scope_without_observation_discards_the_narrow(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Scope on but ``gate_observation_enabled`` off leaves the rounds full-scope.

    The coarse three-label signature has no per-failure locations, so there is
    nothing to project onto a narrowed suite — narrowing anyway would compare a
    subset's result against a whole-suite baseline. The row names that reason.
    """
    agent = _scoped_agent(make_agent, ("tests/test_alpha.py",))
    agent.config.gate_observation_enabled = False
    _seed_approved_plan(agent, "cg_scope_noobs", "CG Scope NoObs")
    plan = agent.memory.get_plan("cg_scope_noobs")
    assert plan is not None

    calls: list[tuple[str, ...]] = []

    def _gates(*, scope: tuple[str, ...] = ()) -> QualityGateResult:
        calls.append(tuple(scope))
        return _FAIL_REVIEW

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "stuck"
    assert calls == [()]  # flag-off terminal takes no re-read
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[0] == "gate_scope"
    ]
    assert rows[0] == ("gate_scope", "full", "discarded: no observation "
                      "(gate_observation_enabled off)")


@pytest.mark.asyncio
async def test_converge_scope_empty_selection_records_the_full_row(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A diff that selects no test module runs full-scope and says so.

    ``impacted_tests``' empty tuple is the documented "do not narrow" sentinel; the
    row still lands so the flag's fire rate distinguishes "narrowed" from "had
    nothing to narrow".
    """
    agent = _scoped_agent(make_agent, ())
    _seed_approved_plan(agent, "cg_scope_empty", "CG Scope Empty")
    plan = agent.memory.get_plan("cg_scope_empty")
    assert plan is not None

    with (
        patch.object(
            agent.evolution, "run_quality_gates",
            MagicMock(return_value=_red(_tests_observation("tests/test_a.py::x"))),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "stuck"
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[0] == "gate_scope"
    ]
    assert rows[0] == ("gate_scope", "full", "no impacted modules (400 in suite)")


@pytest.mark.asyncio
async def test_converge_scope_widening_that_restricts_to_nothing_goes_full(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A subset certification a narrowed probe could not re-observe ends the narrowing.

    The certification still times out — its only surviving identity is the
    synthetic ``tests:TIMEOUT``, which a narrowed ``--no-cov`` probe says nothing
    about. Widening buys no visible failure, so the loop re-baselines on the full
    signature and runs full-scope rounds rather than grinding against an empty
    narrowed baseline.
    """
    agent = _scoped_agent(make_agent, ("tests/test_alpha.py",))
    _seed_approved_plan(agent, "cg_scope_widefull", "CG Scope WideFull")
    plan = agent.memory.get_plan("cg_scope_widefull")
    assert plan is not None

    from maestro.gate_observation import GateObservation, GateRunObservation

    # A killed pre-loop suite that still salvaged one enumerated failure: the
    # synthetic identity *and* the real one (GateObservation.signature keeps both).
    salvaged = GateRunObservation(
        gates=(
            GateObservation(tool="ruff", kind="CLEAN", failures=(), output=""),
            GateObservation(tool="mypy", kind="CLEAN", failures=(), output=""),
            GateObservation(
                tool="tests", kind="TIMEOUT",
                failures=_tests_observation("tests/test_alpha.py::a").gates[2].failures,
                output="",
            ),
        ),
    )
    reads = [
        _red(salvaged),              # pre-loop, full: {tests:TIMEOUT, alpha::a}
        _green(),                    # round 1, narrowed: the in-scope red cleared
        _red(_timeout_observation()),  # certification: a strict subset
        _red(_timeout_observation()),  # terminal re-read, full
    ]
    calls: list[tuple[str, ...]] = []

    def _gates(*, scope: tuple[str, ...] = ()) -> QualityGateResult:
        calls.append(tuple(scope))
        return reads[len(calls) - 1]

    fixes = [True, False]

    async def _auto_fix(*args: Any, **kwargs: Any) -> bool:
        return fixes.pop(0)

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", side_effect=_auto_fix),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "stuck"
    assert calls[3] == ()  # the terminal re-read is full-scope again
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[0] == "gate_scope"
    ]
    assert "widened=full" in rows[0][2]


def test_widen_scope_with_no_observation_returns_the_selection(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Widening a review that carries no observation changes nothing.

    There are no enumerated failure locations to widen with, so the selection is
    returned as-is rather than being silently emptied.
    """
    agent = make_agent()
    assert agent.evolution._widen_scope(
        _FAIL_REVIEW, ("tests/test_alpha.py",), 400,
    ) == ("tests/test_alpha.py",)


def test_widen_scope_falls_back_to_full_past_the_fraction(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A widening that would name most of the suite degrades to "do not narrow".

    Regression: the widening was unbounded, so a broad red could splice most of
    the suite into the pytest argv — slower than running the suite, which defeats
    the narrowing outright.
    """
    agent = make_agent()
    review = _red(_tests_observation(
        *(f"tests/test_m{i}.py::t" for i in range(6)),
    ))
    # Six modules against a ten-module suite is past the half-suite fraction.
    assert agent.evolution._widen_scope(review, ("tests/test_a.py",), 10) == ()
    # Three against ten is not.
    trimmed = _red(_tests_observation(
        *(f"tests/test_m{i}.py::t" for i in range(3)),
    ))
    assert agent.evolution._widen_scope(trimmed, (), 10) == (
        "tests/test_m0.py", "tests/test_m1.py", "tests/test_m2.py",
    )


def test_widen_scope_with_an_unknown_suite_size_returns_full(make_agent) -> None:  # type: ignore[no-untyped-def]
    """No denominator means no way to bound the widening, so it does not narrow."""
    agent = make_agent()
    assert agent.evolution._widen_scope(
        _FAIL_REVIEW, ("tests/test_alpha.py",), 0,
    ) == ()


@pytest.mark.asyncio
async def test_converge_scope_stuck_terminal_is_certified_full_scope(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A narrowed stall takes one fresh full-scope read before the terminal attributes.

    Regression: the stuck terminal was handed the narrowed ``--no-cov`` review, so
    ``attribute()`` classified a subset read — fewer enumerated identities makes
    ``INHERITED`` strictly easier to reach, and a diff-caused red in an unselected
    module could be held as the base's.
    """
    agent = _scoped_agent(make_agent, ("tests/test_alpha.py",))
    _seed_approved_plan(agent, "cg_scope_stuck", "CG Scope Stuck")
    plan = agent.memory.get_plan("cg_scope_stuck")
    assert plan is not None

    stalled = _tests_observation("tests/test_alpha.py::a")
    # The narrowed round re-reads the same in-scope failure -> no progress.
    certification = _red(_tests_observation(
        "tests/test_alpha.py::a", "tests/test_far.py::z",
    ))
    reads = [_red(stalled), _red(stalled), certification]
    calls: list[tuple[str, ...]] = []

    def _gates(*, scope: tuple[str, ...] = ()) -> QualityGateResult:
        calls.append(tuple(scope))
        return reads[len(calls) - 1]

    async def _auto_fix(*args: Any, **kwargs: Any) -> bool:
        return True

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", side_effect=_auto_fix),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert calls == [(), ("tests/test_alpha.py",), ()]
    assert result.outcome == "stuck"
    # The terminal carries the *full* certification, not the narrowed round.
    assert result.review is certification
    rows = [
        tup for tup in _activation_tuples(mem_fs, agent) if tup[0] == "gate_scope"
    ]
    assert "stuck-certified" in rows[0][2]


@pytest.mark.asyncio
async def test_converge_scope_narrowed_timeout_is_not_read_as_progress(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A narrowed round killed at the deadline keeps its ``tests:TIMEOUT`` identity.

    Regression: restricting the narrowed round's *own* result dropped the synthetic
    non-answer identity, so a probe that timed out with no in-scope survivors
    restricted to the empty set — read by the loop as maximal progress from a gate
    that never finished.
    """
    agent = _scoped_agent(make_agent, ("tests/test_alpha.py",))
    _seed_approved_plan(agent, "cg_scope_to", "CG Scope TO")
    plan = agent.memory.get_plan("cg_scope_to")
    assert plan is not None

    pre_loop = _red(_tests_observation("tests/test_alpha.py::a"))
    narrowed_timeout = _red(_timeout_observation())
    certification = _red(_tests_observation("tests/test_alpha.py::a"))
    reads = [pre_loop, narrowed_timeout, certification]
    calls: list[tuple[str, ...]] = []

    def _gates(*, scope: tuple[str, ...] = ()) -> QualityGateResult:
        calls.append(tuple(scope))
        return reads[len(calls) - 1]

    async def _auto_fix(*args: Any, **kwargs: Any) -> bool:
        return True

    with (
        patch.object(agent.evolution, "run_quality_gates", side_effect=_gates),
        patch.object(agent.evolution, "auto_fix", side_effect=_auto_fix),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    # The timed-out narrowed round is neither green nor progress: it stalls, and
    # the stall is certified full-scope before the terminal.
    assert calls == [(), ("tests/test_alpha.py",), ()]
    assert result.outcome == "stuck"


def test_impacted_scope_reads_the_tests_tree_once(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag on, the selection and the denominator come from one read of the tree.

    Both gate path atoms come from ``Config.work_target()``, and the same source
    mapping feeds ``select_impacted_tests`` and ``count_test_modules`` so a walk of
    the tests tree is not paid twice per convergence.
    """
    agent = make_agent()
    agent.config.gate_scope_impacted_enabled = True
    _seed_approved_plan(agent, "cg_scope_sel", "CG Scope Sel")
    plan = agent.memory.get_plan("cg_scope_sel")
    assert plan is not None
    plan.files_affected = ["maestro/alpha.py"]

    sources = {
        "tests/test_alpha.py": "from maestro.alpha import thing\n",
        "tests/test_beta.py": "x = 1\n",
        "tests/conftest.py": "",
    }
    with patch(
        "maestro.gate_scope.read_test_sources", MagicMock(return_value=sources),
    ) as read_mock:
        selected, total = agent.evolution._impacted_scope(plan)

    assert selected == ("tests/test_alpha.py",)
    assert total == 2
    read_mock.assert_called_once_with(agent.config.workspace, "tests/")


def test_impacted_scope_flag_off_reads_nothing(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag off, the seam costs one flag read and never touches the tests tree."""
    agent = make_agent()
    assert agent.config.gate_scope_impacted_enabled is False
    _seed_approved_plan(agent, "cg_scope_off_sel", "CG Scope Off Sel")
    plan = agent.memory.get_plan("cg_scope_off_sel")
    assert plan is not None

    with patch("maestro.gate_scope.read_test_sources") as read_mock:
        assert agent.evolution._impacted_scope(plan) == ((), 0)
    read_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Pre-spend gate-runnability consult (proposal 133)
# ---------------------------------------------------------------------------


def _unprovisioned(fingerprint: str = "fp1") -> Any:
    from maestro.gate_environment import GateRunnabilityVerdict

    return GateRunnabilityVerdict(
        kind="UNPROVISIONED", fingerprint=fingerprint,
        detail="maestro_sdk does not resolve",
    )


def _uncollectable(fingerprint: str) -> Any:
    from maestro.gate_environment import GateRunnabilityVerdict

    return GateRunnabilityVerdict(
        kind="UNCOLLECTABLE", fingerprint=fingerprint,
        identities=frozenset({"tests:COLLECTION_ERROR"}),
        detail="collection COLLECTION_ERROR",
    )


def _sound() -> Any:
    from maestro.gate_environment import GateRunnabilityVerdict

    return GateRunnabilityVerdict(
        kind="SOUND", fingerprint="sound", detail="fit",
    )


@pytest.mark.asyncio
async def test_environment_hold_spends_no_attempt_and_stamps_every_plan(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 133: a non-SOUND verdict opens no implementation session; the
    plans it would have selected keep their status and failure_count and carry
    the verdict's fingerprint."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    agent.config.max_implementations_per_iteration = 3
    _seed_approved_plan(agent, "env_a", "Env A")
    _seed_approved_plan(agent, "env_b", "Env B")
    agent._vcs.rev_parse.return_value = "head0"

    attempted: list[str] = []
    agent.implement_plan = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda plan_id: attempted.append(plan_id),
    )

    with (
        patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned()),
        patch.object(agent, "ask_thomas", AsyncMock(return_value="")),
    ):
        await agent._implement_approved_plans(iteration=7)

    assert attempted == []
    for plan_id in ("env_a", "env_b"):
        plan = agent.memory.get_plan(plan_id)
        assert plan is not None
        assert plan.status == "approved"
        assert plan.failure_count == 0
        assert plan.environment_hold_fingerprint == "fp1"


@pytest.mark.asyncio
async def test_environment_hold_writes_one_activation_row(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """The hold leaves exactly one ``gate_environment_hold`` activation row
    naming the kind and its detail."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_row", "Env Row")
    agent._vcs.rev_parse.return_value = "head0"
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned()),
        patch.object(agent, "ask_thomas", AsyncMock(return_value="")),
    ):
        await agent._implement_approved_plans(iteration=1)

    assert [
        tup for tup in _activation_tuples(mem_fs, agent)
        if tup[0] == "gate_environment_hold"
    ] == [
        ("gate_environment_hold", "UNPROVISIONED", "maestro_sdk does not resolve"),
    ]


@pytest.mark.asyncio
async def test_environment_hold_asks_thomas_exactly_once_per_fact(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Two consecutive holds on the same (kind, identities) fact ask once, even
    though the UNCOLLECTABLE fingerprint moves with ``origin/main``."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_once", "Env Once")
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    ask = AsyncMock(return_value="")

    with patch.object(agent, "ask_thomas", ask):
        agent._vcs.rev_parse.return_value = "head0"
        with patch(
            "maestro.loop.plan_holds.gate_environment_verdict", return_value=_uncollectable("fp-old"),
        ):
            await agent._implement_approved_plans(iteration=1)
        agent._vcs.rev_parse.return_value = "head1"
        with patch(
            "maestro.loop.plan_holds.gate_environment_verdict", return_value=_uncollectable("fp-new"),
        ):
            await agent._implement_approved_plans(iteration=2)

    assert ask.await_count == 1
    # The moving fingerprint still re-stamped the held plan.
    plan = agent.memory.get_plan("env_once")
    assert plan is not None
    assert plan.environment_hold_fingerprint == "fp-new"


@pytest.mark.asyncio
async def test_environment_hold_asks_again_for_a_genuinely_new_fact(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A different (kind, identities) pair is a different fact and earns an ask."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_two", "Env Two")
    agent._vcs.rev_parse.return_value = "head0"
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    ask = AsyncMock(return_value="")

    with patch.object(agent, "ask_thomas", ask):
        with patch(
            "maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned(),
        ):
            await agent._implement_approved_plans(iteration=1)
        with patch(
            "maestro.loop.plan_holds.gate_environment_verdict", return_value=_uncollectable("fp-x"),
        ):
            await agent._implement_approved_plans(iteration=2)

    assert ask.await_count == 2


@pytest.mark.asyncio
async def test_sound_verdict_releases_every_held_plan(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The hold self-extinguishes: the next SOUND verdict implements the first
    plan and clears the stamp it carried."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_rel", "Env Rel")
    agent._vcs.rev_parse.return_value = "head0"

    attempted: list[str] = []
    agent.implement_plan = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda plan_id: attempted.append(plan_id),
    )

    with (
        patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned()),
        patch.object(agent, "ask_thomas", AsyncMock(return_value="")),
    ):
        await agent._implement_approved_plans(iteration=1)
    assert attempted == []

    with patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_sound()):
        await agent._implement_approved_plans(iteration=2)

    assert attempted == ["env_rel"]
    plan = agent.memory.get_plan("env_rel")
    assert plan is not None
    assert plan.environment_hold_fingerprint is None


@pytest.mark.asyncio
async def test_sound_verdict_clears_the_notice_so_a_recurrence_asks_again(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A repair followed by the same break is a fresh fact, not a muted one."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_recur", "Env Recur")
    agent._vcs.rev_parse.return_value = "head0"
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    ask = AsyncMock(return_value="")

    with patch.object(agent, "ask_thomas", ask):
        with patch(
            "maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned(),
        ):
            await agent._implement_approved_plans(iteration=1)
        with patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_sound()):
            await agent._implement_approved_plans(iteration=2)
        with patch(
            "maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned(),
        ):
            await agent._implement_approved_plans(iteration=3)

    assert ask.await_count == 2


@pytest.mark.asyncio
async def test_environment_hold_writes_a_bounded_repair_directive(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An empty ``next_task_directive`` is filled with the bounded repair ask."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_dir", "Env Dir")
    agent._vcs.rev_parse.return_value = "head0"
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned()),
        patch.object(agent, "ask_thomas", AsyncMock(return_value="")),
    ):
        await agent._implement_approved_plans(iteration=1)

    directive = agent.consciousness.load_self_model().next_task_directive
    assert directive is not None
    assert directive.startswith("Environment repair: UNPROVISIONED")
    assert len(directive) <= Consciousness.MAX_DIRECTIVE_CHARS


@pytest.mark.asyncio
async def test_environment_hold_does_not_overwrite_occupied_directive(make_agent) -> None:  # type: ignore[no-untyped-def]
    """First-writer-wins: an occupied directive slot is preserved, and the plan
    is still held."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_occ", "Env Occ")
    agent._vcs.rev_parse.return_value = "head0"
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "existing directive"
    agent.consciousness.save_self_model(model)
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned()),
        patch.object(agent, "ask_thomas", AsyncMock(return_value="")),
    ):
        await agent._implement_approved_plans(iteration=1)

    assert (
        agent.consciousness.load_self_model().next_task_directive
        == "existing directive"
    )
    plan = agent.memory.get_plan("env_occ")
    assert plan is not None
    assert plan.environment_hold_fingerprint == "fp1"


@pytest.mark.asyncio
async def test_environment_hold_survives_a_raising_journal(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A journal (or ledger) that cannot be written must not cost the hold —
    the stamp is what protects the attempt budget."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_j", "Env J")
    agent._vcs.rev_parse.return_value = "head0"
    attempted: list[str] = []
    agent.implement_plan = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda plan_id: attempted.append(plan_id),
    )

    with (
        patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned()),
        patch.object(agent, "ask_thomas", AsyncMock(return_value="")),
        patch.object(
            agent.journal, "record_event", side_effect=RuntimeError("journal down"),
        ),
        patch.object(
            agent.gate_activations, "append", side_effect=RuntimeError("ledger down"),
        ),
    ):
        await agent._implement_approved_plans(iteration=1)

    assert attempted == []
    plan = agent.memory.get_plan("env_j")
    assert plan is not None
    assert plan.environment_hold_fingerprint == "fp1"


@pytest.mark.asyncio
async def test_gate_environment_verdict_is_none_while_the_flag_is_off(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag off: no probe is issued at all and the helper answers ``None``."""
    agent = make_agent()
    assert agent.config.gate_environment_oracle_enabled is False

    with patch(
        "maestro.loop.plan_holds.gate_environment.refresh_verdict",
    ) as refresh:
        assert plan_holds.gate_environment_verdict(agent, 3) is None

    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_gate_environment_verdict_assembles_the_probe_evidence(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag on: the helper hands the oracle the collector, the telemetry rows,
    the planned width, a derived deadline and the origin/main head, keyed by the
    iteration so the probe runs at most once per iteration.

    With ``[loop] gate_budget_lease_owned_enabled`` on, the derived deadline is
    the budget ``_run_gate_command`` actually enforces, so the oracle judges the
    suite against it and is handed ``selfcheck_max`` as the escalation cap
    (proposal 134).
    """
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    agent.config.gate_budget_lease_owned_enabled = True
    agent.config.selfcheck_max_timeout = 1350
    agent._vcs.rev_parse.return_value = "abc123"

    with (
        patch("maestro.loop.plan_holds.gate_capacity.read_gate_runs", return_value=["row"]),
        patch("maestro.loop.plan_holds.gate_capacity.measure_host_capacity"),
        patch(
            "maestro.loop.plan_holds.gate_capacity.uncontended_width",
            return_value=4,
        ),
        patch("maestro.loop.plan_holds.gate_capacity.derive_deadline", return_value=900),
        patch(
            "maestro.loop.plan_holds.gate_environment.refresh_verdict",
            return_value=_sound(),
        ) as refresh,
    ):
        verdict = plan_holds.gate_environment_verdict(agent, 11)

    assert verdict.kind == "SOUND"
    kwargs = refresh.call_args.kwargs
    assert kwargs["iteration_key"] == "11"
    assert kwargs["collector"] == agent.evolution.collect_suite
    assert kwargs["readings"] == ["row"]
    assert kwargs["workers"] == 4
    assert kwargs["deadline_seconds"] == 900
    assert kwargs["cap_seconds"] == 1350
    assert kwargs["head"] == "abc123"
    assert refresh.call_args.args[0] == agent.config.gate_environment_path


@pytest.mark.asyncio
@pytest.mark.parametrize("cgroup_reserve", [True, False])
async def test_gate_environment_verdict_plans_width_under_the_cgroup_reserve_flag(  # type: ignore[no-untyped-def]
    make_agent, cgroup_reserve: bool,
) -> None:
    """The oracle's width honours ``gate_lease_cgroup_reserve_enabled`` exactly
    as the admission seam does, so it judges rows real runs actually record."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    agent.config.gate_lease_cgroup_reserve_enabled = cgroup_reserve
    agent._vcs.rev_parse.return_value = "abc123"

    with (
        patch("maestro.loop.plan_holds.gate_capacity.read_gate_runs", return_value=[]),
        patch("maestro.loop.plan_holds.gate_capacity.measure_host_capacity"),
        patch(
            "maestro.loop.plan_holds.gate_capacity.uncontended_width",
            return_value=4,
        ) as width,
        patch("maestro.loop.plan_holds.gate_capacity.derive_deadline", return_value=900),
        patch(
            "maestro.loop.plan_holds.gate_environment.refresh_verdict",
            return_value=_sound(),
        ),
    ):
        plan_holds.gate_environment_verdict(agent, 13)

    assert width.call_args.kwargs == {"cgroup_reserve": cgroup_reserve}


@pytest.mark.asyncio
async def test_gate_environment_verdict_judges_the_enforced_budget_flag_off(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Budget-lease flag off: the oracle is judged at the ceiling that kills runs.

    ``_run_gate_command`` spends the lease-derived budget only under
    ``[loop] gate_budget_lease_owned_enabled``; off, every gate run is killed at
    ``selfcheck``. So the flag-off assembly must hand the oracle ``selfcheck``
    and *no* cap — there is no escalation to wait out — and must not spend the
    derivation at all (proposal 134).
    """
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    agent.config.gate_budget_lease_owned_enabled = False
    agent.config.selfcheck_timeout = 900
    agent._vcs.rev_parse.return_value = "abc123"

    with (
        patch("maestro.loop.plan_holds.gate_capacity.read_gate_runs", return_value=["row"]),
        patch("maestro.loop.plan_holds.gate_capacity.measure_host_capacity"),
        patch(
            "maestro.loop.plan_holds.gate_capacity.uncontended_width",
            return_value=4,
        ),
        patch(
            "maestro.loop.plan_holds.gate_capacity.derive_deadline",
        ) as derive,
        patch(
            "maestro.loop.plan_holds.gate_environment.refresh_verdict",
            return_value=_sound(),
        ) as refresh,
    ):
        plan_holds.gate_environment_verdict(agent, 12)

    derive.assert_not_called()
    kwargs = refresh.call_args.kwargs
    assert kwargs["deadline_seconds"] == 900
    assert kwargs["cap_seconds"] == 0


@pytest.mark.asyncio
async def test_gate_environment_verdict_tolerates_an_unresolvable_head(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An unmeasurable ``origin/main`` becomes the empty head, not a crash."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    agent._vcs.rev_parse.return_value = None

    with (
        patch("maestro.loop.plan_holds.gate_capacity.measure_host_capacity"),
        patch(
            "maestro.loop.plan_holds.gate_capacity.uncontended_width",
            return_value=1,
        ),
        patch("maestro.loop.plan_holds.gate_capacity.derive_deadline", return_value=600),
        patch(
            "maestro.loop.plan_holds.gate_environment.refresh_verdict",
            return_value=_sound(),
        ) as refresh,
    ):
        plan_holds.gate_environment_verdict(agent, 1)

    assert refresh.call_args.kwargs["head"] == ""


@pytest.mark.asyncio
async def test_no_eligible_plans_pays_for_no_runnability_probe(make_agent) -> None:  # type: ignore[no-untyped-def]
    """133: the probe is a whole ``pytest --collect-only``, so an iteration with
    nothing to implement must not pay for it — and no hold may be recorded over
    zero plans."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    agent._vcs.rev_parse.return_value = "head0"
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    ask = AsyncMock(return_value="")

    with (
        patch("maestro.loop.plan_holds.gate_environment.refresh_verdict") as refresh,
        patch.object(agent, "ask_thomas", ask),
    ):
        await agent._implement_approved_plans(iteration=4)

    refresh.assert_not_called()
    ask.assert_not_awaited()
    assert agent.consciousness.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_gate_environment_verdict_declines_an_empty_plan_set(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An empty eligible set answers ``None`` without probing; omitting the set
    entirely (the direct-evidence callers) still probes."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    agent._vcs.rev_parse.return_value = "head0"

    with patch(
        "maestro.loop.plan_holds.gate_environment.refresh_verdict",
        return_value=_sound(),
    ) as refresh:
        assert plan_holds.gate_environment_verdict(agent, 1, []) is None
        refresh.assert_not_called()
        assert plan_holds.gate_environment_verdict(agent, 1) is not None
        assert refresh.call_count == 1


@pytest.mark.asyncio
async def test_gate_environment_verdict_declines_an_unkeyed_iteration(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``iteration=None`` would key the cache on the literal string ``"None"``,
    which matches itself forever and would freeze a stale non-SOUND verdict — so
    the helper issues no probe at all."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True

    with patch(
        "maestro.loop.plan_holds.gate_environment.refresh_verdict",
    ) as refresh:
        assert plan_holds.gate_environment_verdict(agent, None) is None

    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_sound_verdict_releases_a_plan_the_cap_filtered_out(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The hold covered every eligible plan, so a SOUND verdict must clear the
    stamp on all of them — including one this iteration's cap did not select."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    agent.config.max_implementations_per_iteration = 1
    _seed_approved_plan(agent, "env_cap_a", "Env Cap A")
    _seed_approved_plan(agent, "env_cap_b", "Env Cap B")
    agent._vcs.rev_parse.return_value = "head0"
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned()),
        patch.object(agent, "ask_thomas", AsyncMock(return_value="")),
    ):
        await agent._implement_approved_plans(iteration=1)
    for plan_id in ("env_cap_a", "env_cap_b"):
        held = agent.memory.get_plan(plan_id)
        assert held is not None
        assert held.environment_hold_fingerprint == "fp1"

    with patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_sound()):
        await agent._implement_approved_plans(iteration=2)

    for plan_id in ("env_cap_a", "env_cap_b"):
        released = agent.memory.get_plan(plan_id)
        assert released is not None
        assert released.environment_hold_fingerprint is None


@pytest.mark.asyncio
async def test_environment_hold_question_says_eligible_not_approved(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``eligible`` also carries retryable ``failed`` plans, so the question must
    not misreport them as approved ones."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_word", "Env Word")
    agent._vcs.rev_parse.return_value = "head0"
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    ask = AsyncMock(return_value="")

    with (
        patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned()),
        patch.object(agent, "ask_thomas", ask),
    ):
        await agent._implement_approved_plans(iteration=1)

    question = ask.await_args.args[0]
    assert "1 eligible plan(s)" in question
    assert "approved plan(s)" not in question


@pytest.mark.asyncio
async def test_clear_environment_notice_skips_an_already_empty_write(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The steady state is an empty notice, so the SOUND path must not take the
    lock and rewrite the sidecar to persist a value it already holds."""
    agent = make_agent()

    with (
        patch("maestro.loop.plan_holds.gate_environment.read_notice", return_value=""),
        patch("maestro.loop.plan_holds.gate_environment.record_notice") as record,
    ):
        plan_holds.clear_environment_notice(agent)
    record.assert_not_called()

    with (
        patch(
            "maestro.loop.plan_holds.gate_environment.read_notice",
            return_value="UNPROVISIONED",
        ),
        patch("maestro.loop.plan_holds.gate_environment.record_notice") as record,
    ):
        plan_holds.clear_environment_notice(agent)
    record.assert_called_once_with(agent.config.gate_environment_notice_path, "")


@pytest.mark.asyncio
async def test_hold_stamps_every_plan_in_one_batched_write(make_agent) -> None:  # type: ignore[no-untyped-def]
    """N held plans cost one plan-file round-trip, not N."""
    agent = make_agent()
    agent.config.gate_environment_oracle_enabled = True
    for plan_id in ("env_b1", "env_b2", "env_b3"):
        _seed_approved_plan(agent, plan_id, plan_id)
    agent._vcs.rev_parse.return_value = "head0"
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("maestro.loop.plan_holds.gate_environment_verdict", return_value=_unprovisioned()),
        patch.object(agent, "ask_thomas", AsyncMock(return_value="")),
        patch.object(
            agent.memory, "update_plans_environment_hold",
            wraps=agent.memory.update_plans_environment_hold,
        ) as batch,
    ):
        await agent._implement_approved_plans(iteration=1)

    assert batch.call_count == 1
    assert sorted(batch.call_args.args[0]) == ["env_b1", "env_b2", "env_b3"]


@pytest.mark.asyncio
async def test_release_environment_holds_is_a_noop_without_stamps(make_agent) -> None:  # type: ignore[no-untyped-def]
    """No plan carries a stamp in the steady state, so the release takes no write."""
    agent = make_agent()
    _seed_approved_plan(agent, "env_clean", "Env Clean")
    plan = agent.memory.get_plan("env_clean")
    assert plan is not None

    with patch.object(agent.memory, "update_plans_environment_hold") as batch:
        plan_holds.release_environment_holds(agent, [plan])

    batch.assert_not_called()


@pytest.mark.asyncio
async def test_flag_off_selection_sheds_a_legacy_stamp(make_agent) -> None:  # type: ignore[no-untyped-def]
    """With the oracle off no verdict is read, so the set-wide release never
    runs — but a stamp left by an earlier flag-on run must still not outlive its
    hold, and the per-plan release in the selection loop sheds it."""
    agent = make_agent()
    agent.config.pre_mint_generation_gate_enabled = False
    agent.config.wip_primacy_enabled = False
    assert agent.config.gate_environment_oracle_enabled is False
    agent._advance_slice_rollouts = AsyncMock()  # type: ignore[method-assign]
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    agent.notifier.resolve_plan = AsyncMock()  # type: ignore[method-assign]
    agent._vcs.rev_parse.return_value = "head0"
    _seed_approved_plan(agent, "env_legacy", "Env Legacy")
    agent.memory.update_plan_environment_hold(
        "env_legacy", environment_hold_fingerprint="stale",
    )

    await agent._implement_approved_plans(iteration=1)

    agent.implement_plan.assert_awaited_once_with("env_legacy")
    shed = agent.memory.get_plan("env_legacy")
    assert shed is not None
    assert shed.environment_hold_fingerprint is None


@pytest.mark.asyncio
async def test_release_environment_hold_state_does_both_releases(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The SOUND path's one call clears the notice and sheds every stamp."""
    agent = make_agent()
    _seed_approved_plan(agent, "env_state", "Env State")
    agent.memory.update_plan_environment_hold(
        "env_state", environment_hold_fingerprint="fp1",
    )
    plan = agent.memory.get_plan("env_state")
    assert plan is not None

    with patch(
        "maestro.loop.plan_holds.gate_environment.read_notice",
        return_value="UNPROVISIONED",
    ), patch(
        "maestro.loop.plan_holds.gate_environment.record_notice",
    ) as record:
        plan_holds.release_environment_hold_state(agent, [plan])

    record.assert_called_once_with(agent.config.gate_environment_notice_path, "")
    released = agent.memory.get_plan("env_state")
    assert released is not None
    assert released.environment_hold_fingerprint is None



@pytest.mark.asyncio
async def test_implement_approved_plans_environment_oracle_flag_off_is_byte_identical(make_agent) -> None:  # type: ignore[no-untyped-def]
    """133: with the flag off no runnability probe is issued at all — the approved
    plan is implemented, no directive is written, and no verdict or notice row is
    persisted, byte-identical to today.

    Lives here rather than beside its ``pre_mint_gate`` sibling in
    ``tests/test_agent.py`` because that file carries a shrink-only size pin
    (``tests/test_source_file_size.py``).
    """
    agent = make_agent()
    agent.config.pre_mint_generation_gate_enabled = False
    agent.config.wip_primacy_enabled = False
    assert agent.config.gate_environment_oracle_enabled is False
    agent._advance_slice_rollouts = AsyncMock()  # type: ignore[method-assign]
    agent.implement_plan = AsyncMock()  # type: ignore[method-assign]
    agent.notifier.resolve_plan = AsyncMock()  # type: ignore[method-assign]
    agent._vcs.rev_parse.return_value = "head0"
    _seed_approved_plan(agent, "flag_off", "Flag Off")

    with (
        patch("maestro.loop.plan_holds.gate_environment.refresh_verdict") as refresh,
        patch("maestro.loop.plan_holds.gate_environment.record_notice") as record,
    ):
        await agent._implement_approved_plans(iteration=3)

    refresh.assert_not_called()
    record.assert_not_called()
    agent.implement_plan.assert_awaited_once_with("flag_off")
    assert agent.consciousness.load_self_model().next_task_directive is None

@pytest.mark.asyncio
async def test_dirty_working_tree_refusal_does_not_charge_the_plan(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A refused mutating phase must not spend the plan's attempt budget.

    ``EvolutionEngine.implement()`` refuses before cutting a branch when the
    host tree is not a clean ``main`` (#3711). Nothing about the plan was
    tested, so demoting it would burn a human approval on an operator's
    in-flight edit: the exception propagates and the plan stays ``approved``.
    """
    agent = make_agent()
    _seed_approved_plan(agent, "dirty_plan", "Dirty Plan")
    agent.evolution.implement = AsyncMock(  # type: ignore[method-assign]
        side_effect=DirtyWorkingTreeError("main has uncommitted changes"),
    )
    agent.evolution.cleanup_branch = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(DirtyWorkingTreeError):
        await agent.implement_plan("dirty_plan")

    refreshed = agent.memory.get_plan("dirty_plan")
    assert refreshed is not None
    assert refreshed.status == "approved"
    assert refreshed.failure_count == 0
    # The refusal fires before a branch is cut, and ``cleanup_branch``
    # discards the working tree — so the exception is handed to it and it
    # returns untouched rather than destroying the operator's edit (#3711).
    agent.evolution.cleanup_branch.assert_called_once()
    assert agent.evolution.cleanup_branch.call_args.args[0] == "dirty_plan"
    assert isinstance(
        agent.evolution.cleanup_branch.call_args.args[1], DirtyWorkingTreeError
    )


@pytest.mark.asyncio
async def test_dirty_working_tree_refusal_never_discards_the_tree(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The refusal must leave the operator's uncommitted work on disk.

    Exercises the *real* ``cleanup_branch`` body through a mock VCS: the
    dirty-tree arm must not reach ``discard_changes`` (``git checkout -- .``
    plus ``git clean -fd``), which would wipe the in-flight edit (#3711).
    """
    agent = make_agent()
    _seed_approved_plan(agent, "dirty_plan2", "Dirty Plan 2")
    agent.evolution.implement = AsyncMock(  # type: ignore[method-assign]
        side_effect=DirtyWorkingTreeError("main has uncommitted changes"),
    )
    vcs = MagicMock()
    agent.evolution._vcs = vcs

    with pytest.raises(DirtyWorkingTreeError):
        await agent.implement_plan("dirty_plan2")

    vcs.discard_changes.assert_not_called()
    vcs.delete_branch.assert_not_called()


# ── The mid-attempt environment hold (proposal 133, step 4) ────────────────────


@pytest.mark.asyncio
async def test_environment_terminal_holds_plan_no_demote_no_pr(make_agent) -> None:  # type: ignore[no-untyped-def]
    """proposal 133: an ``ENVIRONMENT`` convergence terminal (flag on) holds the
    plan ``approved`` — the branch is cleaned up, no PR opens, no issue is filed,
    ``failure_count`` stays 0, ``inherited_hold_count`` is untouched, the
    verdict's fingerprint is stamped, and the repair directive fills the empty
    slot."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_hold", "Env Hold")  # files_affected=["file.py"]

    # The red sits in the *changed* file, so only the ENVIRONMENT arm — which
    # outranks ATTRIBUTED — can hold it.
    red = _red_observation("file.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )
    verdict = gate_environment.GateRunnabilityVerdict(
        kind="UNCOLLECTABLE",
        fingerprint="env-fp",
        identities=red.signature(),
        detail="collection COLLECTION_ERROR",
    )

    create_pr_mock = AsyncMock()
    demote_mock = AsyncMock()
    cleanup_mock = MagicMock()
    issue_mock = MagicMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
        patch.object(
            gate_environment, "read_verdict", MagicMock(return_value=verdict),
        ),
        patch.object(agent.evolution, "create_pr", create_pr_mock),
        patch.object(agent.evolution, "create_issue", issue_mock),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("env_hold")

    demote_mock.assert_not_awaited()
    create_pr_mock.assert_not_awaited()
    issue_mock.assert_not_called()
    cleanup_mock.assert_called_once_with("env_hold")

    stored = agent.memory.get_plan("env_hold")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.failure_count == 0
    assert stored.inherited_hold_count == 0
    assert stored.inherited_hold_main_sha is None
    assert stored.environment_hold_fingerprint == "env-fp"

    model = agent.consciousness.load_self_model()
    assert model.next_task_directive.startswith("Environment repair:")


@pytest.mark.asyncio
async def test_environment_terminal_flag_off_demotes_as_before(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag-off the same red is the diff's: no verdict is read, no ``ENVIRONMENT``
    attribution can be produced, and the plan demotes exactly as it does today."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    assert agent.config.gate_environment_oracle_enabled is False
    _seed_approved_plan(agent, "env_off", "Env Off")

    red = _red_observation("file.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )

    demote_mock = AsyncMock()
    read_mock = MagicMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
        patch.object(gate_environment, "read_verdict", read_mock),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("env_off")

    read_mock.assert_not_called()
    demote_mock.assert_awaited_once()

    stored = agent.memory.get_plan("env_off")
    assert stored is not None
    assert stored.environment_hold_fingerprint is None


@pytest.mark.asyncio
async def test_environment_terminal_leaves_an_occupied_directive_slot(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The repair directive is a bridge into an *empty* slot, never an overwrite."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_hold_occ", "Env Hold Occupied")
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Already here"
    agent.consciousness.save_self_model(model)

    red = _red_observation("file.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )
    verdict = gate_environment.GateRunnabilityVerdict(
        kind="UNFIT", fingerprint="fp2", identities=red.signature(), detail="unfit",
    )

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
        patch.object(
            gate_environment, "read_verdict", MagicMock(return_value=verdict),
        ),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
    ):
        await agent.implement_plan("env_hold_occ")

    assert agent.consciousness.load_self_model().next_task_directive == "Already here"


@pytest.mark.asyncio
async def test_environment_terminal_survives_a_failing_journal(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A journal that cannot be written must not cost the hold — the stamp and the
    preserved status are what actually protect the attempt budget."""
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_hold_jf", "Env Hold Journal Fail")

    red = _red_observation("file.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )
    verdict = gate_environment.GateRunnabilityVerdict(
        kind="UNPROVISIONED", fingerprint="fp3", identities=red.signature(),
        detail="missing sibling",
    )

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
        patch.object(
            gate_environment, "read_verdict", MagicMock(return_value=verdict),
        ),
        patch.object(
            agent.journal, "record_event", MagicMock(side_effect=OSError("disk")),
        ),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
    ):
        await agent.implement_plan("env_hold_jf")

    stored = agent.memory.get_plan("env_hold_jf")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.environment_hold_fingerprint == "fp3"


@pytest.mark.asyncio
async def test_environment_terminal_writes_a_gate_environment_hold_row(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The terminal hold is visible to ``maestro audit-flags``.

    ``flag_lifecycle`` promises a ``gate_environment_hold`` activation row per
    hold, and ``audit-flags`` joins each opt-in flag to its recent activation
    counts to produce a promote-or-delete horizon. A hold arm that fires silently
    is a flag that reads as unexercised no matter how often it actually fires.
    """
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_hold_row", "Env Hold Row")

    red = _red_observation("file.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )
    verdict = gate_environment.GateRunnabilityVerdict(
        kind="UNFIT", fingerprint="fp-row", identities=red.signature(),
        detail="p50 over budget",
    )

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
        patch.object(
            gate_environment, "read_verdict", MagicMock(return_value=verdict),
        ),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
    ):
        await agent.implement_plan("env_hold_row")

    rows = [
        row for row in agent.gate_activations.read_tail(50)
        if row.gate == "gate_environment_hold"
    ]
    assert len(rows) == 1
    assert rows[0].action == "UNFIT"
    assert "env_hold_row" in rows[0].detail


@pytest.mark.asyncio
async def test_environment_terminal_survives_a_raising_activation_ledger(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A ledger that cannot be written must not cost the hold.

    Best-effort in the same shape as the selector's pre-spend hold: the stamp is
    what protects the attempt budget, and it is already persisted before the row.
    """
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_hold_ledger", "Env Hold Ledger")

    red = _red_observation("file.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )
    verdict = gate_environment.GateRunnabilityVerdict(
        kind="UNFIT", fingerprint="fp-led", identities=red.signature(), detail="d",
    )
    demote_mock = AsyncMock()

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
        patch.object(
            gate_environment, "read_verdict", MagicMock(return_value=verdict),
        ),
        patch.object(
            audit_checks, "log_gate_activation",
            MagicMock(side_effect=OSError("ledger")),
        ),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
    ):
        await agent.implement_plan("env_hold_ledger")

    demote_mock.assert_not_awaited()
    stored = agent.memory.get_plan("env_hold_ledger")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.environment_hold_fingerprint == "fp-led"


@pytest.mark.asyncio
async def test_environment_terminal_reads_the_verdict_exactly_once(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The hold consumes the verdict the attribution rested on, never a re-read.

    A second independent read of the cached sidecar could answer ``None`` if the
    row moved in between — and ``None`` is how ``update_plan_environment_hold``
    spells *release*. Threading it on ``ConvergenceResult`` makes the pairing
    structural: one read per terminal, and the verdict is non-``None`` exactly
    when the attribution is ``ENVIRONMENT``.
    """
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_hold_once", "Env Hold Once")

    red = _red_observation("file.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )
    verdict = gate_environment.GateRunnabilityVerdict(
        kind="UNCOLLECTABLE", fingerprint="fp-once", identities=red.signature(),
        detail="d",
    )
    read_mock = MagicMock(return_value=verdict)

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_observe_baseline", MagicMock(return_value=None),
        ),
        patch.object(gate_environment, "read_verdict", read_mock),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
    ):
        await agent.implement_plan("env_hold_once")

    # Two reads, both inside the convergence owner: the round's — which feeds the
    # attribution into the auto-fix prompt — and the terminal's. The hold adds no
    # third; it takes the verdict the terminal carried. Before the fix this was
    # three, and the extra one was the only read whose ``None`` could silently
    # clear the stamp.
    assert read_mock.call_count == 2
    stored = agent.memory.get_plan("env_hold_once")
    assert stored is not None
    assert stored.environment_hold_fingerprint == "fp-once"


@pytest.mark.asyncio
async def test_environment_terminal_without_a_verdict_preserves_a_prior_stamp(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A verdict-less ``ENVIRONMENT`` terminal holds the plan without *clearing* an
    existing stamp.

    Unreachable by construction now that ``ConvergenceResult`` carries the verdict
    the attribution was derived from — so the pair is seeded directly. The
    regression it guards is real and was shipped: stamping
    ``environment_hold_fingerprint=None`` is how ``update_plan_environment_hold``
    spells *release*, so the arm that exists to create a hold would have silently
    dissolved the one already on the plan.
    """
    agent = make_agent()
    agent.config.gate_observation_enabled = True
    agent.config.gate_environment_oracle_enabled = True
    _seed_approved_plan(agent, "env_hold_gone", "Env Hold Gone")
    agent.memory.update_plan_environment_hold(
        "env_hold_gone", environment_hold_fingerprint="prior-fp",
    )

    red = _red_observation("file.py")
    pre_fix = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="pre", observation=red,
    )
    reread = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="reread", observation=red,
    )
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(
            agent.evolution, "run_quality_gates",
            side_effect=_two_read_gates(pre_fix, reread),
        ),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(
            agent.evolution, "_attribute_review",
            MagicMock(return_value=("ENVIRONMENT", None)),
        ),
        patch.object(gate_environment, "read_verdict", MagicMock(return_value=None)),
        patch.object(agent.evolution, "create_pr", AsyncMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
    ):
        await agent.implement_plan("env_hold_gone")

    stored = agent.memory.get_plan("env_hold_gone")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.failure_count == 0
    assert stored.environment_hold_fingerprint == "prior-fp"
    assert not agent.consciousness.load_self_model().next_task_directive


# ── plan_holds.stamp_terminal_environment_hold, directly (proposal 133) ─────────


def test_stamp_terminal_environment_hold_skips_a_none_verdict(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A verdict-less call leaves the plan untouched rather than clearing it.

    The direction is the point: ``update_plan_environment_hold(None)`` *releases*
    a hold, so the helper must skip the write, not pass the ``None`` through.
    """
    agent = make_agent()
    _seed_approved_plan(agent, "nv", "No Verdict")
    agent.memory.update_plan_environment_hold(
        "nv", environment_hold_fingerprint="prior",
    )

    with patch.object(agent.memory, "update_plan_environment_hold") as update:
        plan_holds.stamp_terminal_environment_hold(agent, "nv", None)

    update.assert_not_called()
    stored = agent.memory.get_plan("nv")
    assert stored is not None
    assert stored.environment_hold_fingerprint == "prior"


def test_stamp_terminal_environment_hold_stamps_then_records(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The stamp is persisted *before* the best-effort ledger row."""
    agent = make_agent()
    _seed_approved_plan(agent, "sv", "Some Verdict")
    verdict = gate_environment.GateRunnabilityVerdict(
        kind="UNPROVISIONED", fingerprint="fp-sv", detail="maestro_sdk missing",
    )
    calls: list[str] = []

    with (
        patch.object(
            agent.memory, "update_plan_environment_hold",
            side_effect=lambda *a, **k: calls.append("stamp"),
        ),
        patch.object(
            audit_checks, "log_gate_activation",
            side_effect=lambda *a, **k: calls.append("row"),
        ),
    ):
        plan_holds.stamp_terminal_environment_hold(agent, "sv", verdict)

    assert calls == ["stamp", "row"]


def _pytest_red_review(count: int):  # type: ignore[no-untyped-def]
    """A red review whose tests gate enumerated *count* failures (capped)."""
    from maestro.evolution import QualityGateResult
    from maestro.gate_observation import (
        GateObservation,
        GateRunObservation,
        parse_gate_output,
    )

    rows = "".join(
        f"FAILED tests/test_m.py::t{n:03d} - AssertionError\n"
        for n in range(100 - count, 100)
    )
    observation = GateRunObservation(gates=(
        GateObservation(tool="ruff", kind="CLEAN", failures=(), output=""),
        GateObservation(tool="mypy", kind="CLEAN", failures=(), output=""),
        parse_gate_output("tests", rows, "", 1),
    ))
    return QualityGateResult(
        passed=False, ruff_ok=True, mypy_ok=True, tests_ok=False,
        raw_output=rows, observation=observation,
    )


@pytest.mark.asyncio
async def test_converge_to_green_truncated_shrink_earns_another_round(make_agent) -> None:  # type: ignore[no-untyped-def]
    """#4309: 60 → 55 → 55 capped failures. The window churns (not a subset) but
    the threaded counts fall, so a second auto_fix round runs; the unchanged
    third read is stuck."""
    agent = make_agent()
    _seed_approved_plan(agent, "cg_trunc", "CG Trunc")
    plan = agent.memory.get_plan("cg_trunc")
    assert plan is not None

    reviews = [_pytest_red_review(60), _pytest_red_review(55), _pytest_red_review(55)]
    auto_fix_mock = AsyncMock(return_value=True)
    with (
        patch.object(
            agent.evolution, "run_quality_gates", MagicMock(side_effect=reviews),
        ),
        patch.object(agent.evolution, "auto_fix", auto_fix_mock),
    ):
        result = await agent.evolution.converge_to_green(plan, ceiling=20)

    assert result.outcome == "stuck"
    assert auto_fix_mock.await_count == 2
