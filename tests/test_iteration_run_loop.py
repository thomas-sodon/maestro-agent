"""run_loop crash/shutdown paths, startup/shutdown notifications and Telegram
polling (``maestro/loop/iteration.py``); split out of ``tests/test_agent.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.agent import (
    MaestroAgent,
)
from maestro.sdk import ErrorCategory, SDKError
from tests.support.gap_fuel import stub_evolve_phase


@pytest.mark.asyncio
async def test_run_loop_crashes_on_contribution_check_failure_duplicate(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates contribution-check failures (duplicate coverage)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("git broken"),
    )
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="git broken"):
        await agent.run_loop(interval=0, max_iters=2, task_every=0, evolve_every=999)

    assert agent.contemplate.call_count == 0  # type: ignore[attr-defined]
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_shutdown_event_set_after_task_breaks_loop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() breaks when shutdown event is set after task execution (line 616)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    contemplate_count = [0]

    async def _contemplate_and_count(**kwargs: object) -> str:
        contemplate_count[0] += 1
        return "ok"

    agent.contemplate = _contemplate_and_count  # type: ignore[method-assign]

    from maestro.task_queue import QueuedTask

    # generate_task enqueues a task; after run(), we set the shutdown event
    async def _run_and_shutdown(task: str, **kwargs):  # type: ignore[no-untyped-def]
        agent._ensure_shutdown_event().set()
        return MagicMock()

    async def _fake_generate(session=None):  # type: ignore[no-untyped-def]
        agent.task_queue.save([QueuedTask(task_id="q", goal="Do a thing")])

    agent.generate_task = AsyncMock(side_effect=_fake_generate)  # type: ignore[method-assign]
    agent.run = _run_and_shutdown  # type: ignore[method-assign]

    # max_iters=5 but shutdown fires after first task -> should stop at iter 1
    await agent.run_loop(interval=0, max_iters=5, task_every=1, evolve_every=999)

    assert contemplate_count[0] == 1


@pytest.mark.asyncio
async def test_run_loop_crashes_on_evolve_exception(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates evolve() errors instead of swallowing them."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    evolve_call_count = [0]

    async def _flaky_evolve(*_a, **_) -> None:  # type: ignore[no-untyped-def]
        evolve_call_count[0] += 1
        raise RuntimeError("evolve exploded")

    stub_evolve_phase(agent, _flaky_evolve)

    # evolve_every=1 -> evolve fires on iteration 1 and crashes.
    with pytest.raises(RuntimeError, match="evolve exploded"):
        await agent.run_loop(interval=0, max_iters=3, task_every=0, evolve_every=1)

    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]
    assert evolve_call_count[0] == 1
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_crashes_loudly_on_internal_sdk_cancel_in_evolve(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() crashes loudly (not silently) when an internal SDK cancel leaks past evolve().

    Regression test for the production crash where ``EvolutionEngine.reflect()``
    -> ``query()`` had a CLI crash, the wrapper retried, then an internal anyio
    cancel scope deadline expired and a ``CancelledError`` (a ``BaseException``,
    not ``Exception``) escaped through ``evolve()``.

    The fix in ``maestro.sdk.query()`` converts internal cancels (asyncio task
    not externally cancelled) to ``TimeoutError``, so the resulting exception
    is a plain ``Exception`` rather than a ``BaseException`` and the outer
    ``finally`` in ``run_loop`` can detect it as a crash and call
    ``shutdown(crashed=True)``.  With phase-level error suppression removed,
    the loop no longer silently retries — instead it raises ``TimeoutError``
    to the caller and leaves ``lifecycle.json`` in a state that triggers
    crash triage on next startup.
    """
    import maestro.sdk as sdk_mod

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    evolve_call_count = [0]

    async def _evolve_with_internal_cancel(*_a, **_) -> None:  # type: ignore[no-untyped-def]
        evolve_call_count[0] += 1
        # Drive the real sdk.query() with a generator that leaks an internal
        # CancelledError on its only attempt, exactly mimicking the
        # production crash signature.
        async def gen(**kwargs: Any) -> Any:
            raise asyncio.CancelledError(
                "Cancelled via cancel scope 0xdead; reason: deadline exceeded",
            )
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await sdk_mod.query(prompt="reflect")

    stub_evolve_phase(agent, _evolve_with_internal_cancel)

    # The internal cancel is wrapped first in a TimeoutError by sdk.query()'s
    # cancel-containment logic, then in an SDKError by the retry exhaustion
    # path (retries_exhausted=True on the sole attempt).  With phase-level
    # suppression removed, the SDKError propagates out of run_loop, the outer
    # finally classifies it as a crash, and shutdown() is called with
    # crashed=True.
    from maestro.sdk import SDKError
    with pytest.raises(SDKError, match="SDK call cancelled by internal scope"):
        await agent.run_loop(interval=0, max_iters=3, task_every=0, evolve_every=1)

    assert evolve_call_count[0] == 1
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_shutdown_event_after_evolve_breaks_loop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() breaks when shutdown event is set after evolve() (line 628)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    async def _evolve_then_signal(*_a, **_) -> None:  # type: ignore[no-untyped-def]
        agent._ensure_shutdown_event().set()

    stub_evolve_phase(agent, _evolve_then_signal)

    # max_iters=5 but shutdown fires after first evolve -> should stop at iter 1
    await agent.run_loop(interval=0, max_iters=5, task_every=0, evolve_every=1)

    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_crashes_on_dream_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates dream-phase errors."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # dream_every=1 -> dream fires on iteration 1 and crashes.
    with patch(
        "maestro.agent.refresh_phase_cost_report",
        side_effect=RuntimeError("dream exploded"),
    ) as refresh, pytest.raises(RuntimeError, match="dream exploded"):
        await agent.run_loop(
            interval=0, max_iters=3, task_every=0, evolve_every=999, dream_every=1,
        )

    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]
    assert refresh.call_count == 1
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_invokes_archive_after_dream(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The dream-phase branch calls Memory.archive with the configured threshold."""
    agent = make_agent()
    agent.config.archive_threshold_days = 7

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.memory.archive = MagicMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999, dream_every=1,
    )

    agent.memory.archive.assert_called_once_with(
        7, retry_budget=3, open_pr_plan_ids=set(),
    )


@pytest.mark.asyncio
async def test_run_loop_swallows_archive_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A swallowed exception inside Memory.archive does not crash the iteration."""
    agent = make_agent()
    agent.config.archive_threshold_days = 7

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.memory.archive = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("disk full"),
    )

    # Iteration completes cleanly despite the archive failure.
    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999, dream_every=1,
    )
    assert agent.memory.archive.call_count == 1
    agent.shutdown.assert_called_once_with(crashed=False)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_rejects_negative_max_iters(make_agent):  # type: ignore[no-untyped-def]
    """run_loop should reject negative max_iters with ValueError."""
    agent = make_agent()
    with pytest.raises(ValueError, match="max_iters"):
        await agent.run_loop(max_iters=-1)


# --- run_loop() startup/shutdown notification tests ---


@pytest.mark.asyncio
async def test_run_loop_sends_startup_notification(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() should send a startup notification before entering the loop."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.notifier = AsyncMock()  # type: ignore[assignment]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    calls = agent.notifier.notify.call_args_list
    assert len(calls) >= 1
    assert "loop started" in calls[0].args[0].lower()


@pytest.mark.asyncio
async def test_run_loop_round_trips_startup_and_shutdown_notifications(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() emits exactly one startup then one shutdown notification.

    Regression for #187: the lifecycle round-trip was never asserted end to
    end on the *notification* surface. ``test_run_loop_sends_startup_notification``
    mocks ``shutdown`` out, so the real ``shutdown()`` — and its single
    ``"Maestro loop stopped."`` notification — never runs through ``run_loop``.

    Here ``shutdown`` is left real and is reached *only* through run_loop's
    outer ``finally``: ``contemplate`` neither calls ``shutdown()`` nor mocks
    it, and the loop exits on ``max_iters`` rather than a mid-iteration
    shutdown. The lone ``"Maestro loop stopped."`` notification can therefore
    only originate from the loop's exit path, so asserting exactly one stop
    fails if ``run_loop`` ever stops calling ``shutdown()`` on exit (zero
    stops). Per-iteration HTML status updates also call ``notifier.notify``,
    so the assertions filter by message content rather than the total call
    count. The duplicate-send guard (#189) is pinned separately, on the
    notification surface, by
    ``test_run_loop_shutdown_guard_collapses_duplicate_stop_notifications``.
    """
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    # shutdown() is deliberately left unmocked so the real stop notification
    # is exercised through run_loop's finally — the sole source of it here.
    agent.notifier = AsyncMock()  # type: ignore[assignment]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    messages = [call.args[0] for call in agent.notifier.notify.call_args_list]
    startup_indices = [
        i for i, msg in enumerate(messages) if "loop started" in msg.lower()
    ]
    stop_indices = [
        i for i, msg in enumerate(messages) if msg == "Maestro loop stopped."
    ]

    assert len(startup_indices) == 1
    assert len(stop_indices) == 1
    assert startup_indices[0] < stop_indices[0]


@pytest.mark.asyncio
async def test_run_loop_shutdown_guard_collapses_duplicate_stop_notifications(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The ``_shutdown_initiated`` guard collapses a double shutdown into one stop.

    Notification-surface companion to
    ``test_shutdown_guard_defeated_by_run_loop_inner_finally`` (issue #189),
    which pins the same invariant on the *journal* surface. ``contemplate``
    calls ``shutdown()`` directly mid-iteration (sending the first
    ``"Maestro loop stopped."``) and sets the shutdown event; run_loop's outer
    ``finally`` then calls ``shutdown()`` a second time. The real
    ``_shutdown_initiated`` guard must make that second call a no-op, so the
    notifier sees exactly one stop message. Asserting the count is ``== 1``
    fails if the guard ever regresses into a duplicate send (two stops).
    """
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    # shutdown() is left unmocked so the real idempotency guard runs.
    agent.notifier = AsyncMock()  # type: ignore[assignment]

    async def contemplate_then_shutdown(**kwargs: object) -> str:
        # Fire a direct shutdown() mid-iteration (issue #189 sequence), then
        # break the loop. The outer finally's shutdown() must be a guarded
        # no-op, so only one stop notification reaches the notifier.
        await agent.shutdown()
        agent._ensure_shutdown_event().set()
        return "reflection"

    agent.contemplate = contemplate_then_shutdown  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    messages = [call.args[0] for call in agent.notifier.notify.call_args_list]
    stop_indices = [
        i for i, msg in enumerate(messages) if msg == "Maestro loop stopped."
    ]

    assert len(stop_indices) == 1


@pytest.mark.asyncio
async def test_run_loop_propagates_startup_notification_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A real Exception from the startup notification crashes ``run_loop``.

    Notification helpers suppress only ``asyncio.CancelledError`` (the
    documented SDK-teardown leak); a generic ``Exception`` is a bug
    (network misconfig, broken Telegram client, …) that must propagate
    so it triggers root-cause analysis rather than a silent loop-up.
    """
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.notifier = AsyncMock()  # type: ignore[assignment]
    agent.notifier.notify = AsyncMock(side_effect=RuntimeError("Telegram down"))

    with pytest.raises(RuntimeError, match="Telegram down"):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    agent.contemplate.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_sends_notification(make_agent) -> None:  # type: ignore[no-untyped-def]
    """shutdown() should send a notification."""
    agent = make_agent()
    agent._running = True
    agent.notifier = AsyncMock()  # type: ignore[assignment]

    await agent.shutdown()

    agent.notifier.notify.assert_called_once_with("Maestro loop stopped.")
    assert not agent.is_running


@pytest.mark.asyncio
async def test_shutdown_tolerates_notification_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """shutdown() must complete even if notification fails."""
    agent = make_agent()
    agent._running = True
    agent.notifier = AsyncMock()  # type: ignore[assignment]
    agent.notifier.notify = AsyncMock(side_effect=Exception("Telegram down"))

    # Should complete without raising
    await agent.shutdown()
    assert not agent.is_running


class TestNotifyBestEffort:
    """Tests for MaestroAgent._notify_best_effort.

    The helper centralizes the catch discipline for best-effort
    Telegram notifications. It must suppress *only*
    ``asyncio.CancelledError`` (a ``BaseException`` that can leak from a
    residual SDK teardown task into a non-SDK ``await``). Real
    ``Exception`` subclasses (network failures, programming errors) are
    bugs that must propagate so the loop's iteration-level handler sees
    them, and ``KeyboardInterrupt`` / ``SystemExit`` continue to
    propagate as ``BaseException`` subclasses outside the catch.
    """

    @pytest.mark.asyncio
    async def test_propagates_exception(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]
        agent.notifier.notify = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await agent._notify_best_effort("hello")

    @pytest.mark.asyncio
    async def test_swallows_cancelled_error(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]
        agent.notifier.notify = AsyncMock(side_effect=asyncio.CancelledError())

        await agent._notify_best_effort("hello")

    @pytest.mark.asyncio
    async def test_propagates_keyboard_interrupt(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]
        agent.notifier.notify = AsyncMock(side_effect=KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            await agent._notify_best_effort("hello")

    @pytest.mark.asyncio
    async def test_propagates_system_exit(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]
        agent.notifier.notify = AsyncMock(side_effect=SystemExit(1))

        with pytest.raises(SystemExit):
            await agent._notify_best_effort("hello")

    @pytest.mark.asyncio
    async def test_passes_parse_mode(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]

        await agent._notify_best_effort("body", parse_mode="HTML")

        agent.notifier.notify.assert_called_once_with("body", parse_mode="HTML")

    @pytest.mark.asyncio
    async def test_default_parse_mode_is_none(self, make_agent) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]

        await agent._notify_best_effort("body")

        agent.notifier.notify.assert_called_once_with("body", parse_mode=None)

    @pytest.mark.asyncio
    async def test_logs_at_specified_level(self, make_agent, caplog) -> None:  # type: ignore[no-untyped-def]
        agent: MaestroAgent = make_agent()
        agent.notifier = AsyncMock()  # type: ignore[assignment]
        agent.notifier.notify = AsyncMock(side_effect=asyncio.CancelledError())

        with caplog.at_level(logging.DEBUG, logger="maestro"):
            await agent._notify_best_effort(
                "body", failure_log="custom log line", log_level=logging.DEBUG,
            )

        records = [r for r in caplog.records if r.message == "custom log line"]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG


@pytest.mark.asyncio
async def test_shutdown_graceful_writes_last_shutdown(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Graceful shutdown() (crashed=False) persists last_shutdown in lifecycle.json.

    This is the control case for the crashed=True test below: we must be
    certain that the default path *does* write last_shutdown so the startup
    path can compute downtime and the crash-detection check treats it as a
    clean exit.
    """
    agent = make_agent()
    agent._running = True
    agent._resume_iteration = 7
    agent._resume_phase = "dream"

    await agent.shutdown()  # default crashed=False

    data = json.loads(agent.config.lifecycle_path.read_text())
    assert "last_shutdown" in data
    assert data["iteration"] == 7
    assert data["next_phase"] == "dream"


@pytest.mark.asyncio
async def test_shutdown_crashed_skips_last_shutdown_write(make_agent) -> None:  # type: ignore[no-untyped-def]
    """shutdown(crashed=True) must NOT overwrite the mid-phase resume cursor.

    Reproduces the second half of the silent-restart production crash.
    A phase leaked an unhandled ``CancelledError`` past the
    ``except Exception:`` guards; ``run_loop()``'s ``finally`` still ran
    ``shutdown()``, which previously wrote ``last_shutdown`` unconditionally
    and masked the crash from ``_read_lifecycle_progress``.  The fix passes
    ``crashed=True`` when the stack is unwinding from an unhandled exception,
    and ``shutdown()`` leaves the ``_persist_progress``-written resume cursor
    untouched so the next startup correctly detects the crash and fires the
    triage agent.
    """
    agent = make_agent()
    # Simulate mid-phase state that _persist_progress would have written
    # just before the crashed phase.
    agent.config.lifecycle_path.write_text(
        json.dumps({"iteration": 10, "next_phase": "evolve"}),
    )
    agent._running = True

    await agent.shutdown(crashed=True)

    # The lifecycle file must still report the in-progress cursor with NO
    # last_shutdown key — this is exactly what _read_lifecycle_progress
    # interprets as crashed=True on the next startup.
    data = json.loads(agent.config.lifecycle_path.read_text())
    assert "last_shutdown" not in data
    assert data["iteration"] == 10
    assert data["next_phase"] == "evolve"

    # Verify the detection path agrees: _read_lifecycle_progress should
    # report crashed=True on this file.
    # Re-seed the file (the read clears it).
    agent.config.lifecycle_path.write_text(
        json.dumps({"iteration": 10, "next_phase": "evolve"}),
    )
    last_shutdown, iteration, next_phase, last_exit_code, crashed, *_ = agent._read_lifecycle_progress()
    assert last_shutdown is None
    assert iteration == 10
    assert next_phase == "evolve"
    assert last_exit_code is None
    assert crashed is True


def test_persist_crash_exit_code_writes_lifecycle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_persist_crash_exit_code merges last_exit_code into the lifecycle cursor."""
    agent = make_agent()
    agent._resume_iteration = 7
    agent._resume_phase = "contemplate"

    agent._persist_crash_exit_code(137)

    data = json.loads(agent.config.lifecycle_path.read_text())
    assert data["iteration"] == 7
    assert data["next_phase"] == "contemplate"
    assert data["last_exit_code"] == 137
    assert "last_shutdown" not in data


def test_persist_crash_exit_code_holds_file_lock(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The read-merge-write serializes on ``file_lock`` (persistence invariant).

    Pins the ordering — the atomic write must run while the advisory lock on
    ``lifecycle.json`` is held — so a refactor that drops the lock or moves the
    write outside it fails. Without the lock a concurrent lifecycle writer could
    interleave and clobber the merged crash context the next startup
    reconstitutes via ``_read_crash_sdk_context``.
    """
    agent = make_agent()
    agent._resume_iteration = 9
    agent._resume_phase = "evolve"
    sdk_error = SDKError(
        "boom",
        category=ErrorCategory.CLI_CRASH,
        exit_code=137,
        stderr="ProcessError\n",
        retries_attempted=1,
    )

    lock_held = False
    locked_paths: list[Any] = []

    @contextmanager
    def tracking_lock(path: Any) -> Iterator[None]:
        nonlocal lock_held
        locked_paths.append(path)
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    write_held: list[bool] = []

    def _record_write(*_args: object, **_kwargs: object) -> None:
        write_held.append(lock_held)

    with patch("maestro.agent.file_lock", tracking_lock), \
         patch("maestro.agent.atomic_write", side_effect=_record_write):
        agent._persist_crash_exit_code(137, sdk_error=sdk_error)

    assert locked_paths == [agent.config.lifecycle_path]
    assert write_held == [True]


def test_persist_crash_exit_code_noop_on_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """No exit code → no lifecycle write (preserves existing cursor)."""
    agent = make_agent()
    agent._resume_iteration = 3
    agent._resume_phase = "evolve"
    agent.config.lifecycle_path.write_text(
        json.dumps({"iteration": 3, "next_phase": "evolve"}),
    )

    agent._persist_crash_exit_code(None)

    data = json.loads(agent.config.lifecycle_path.read_text())
    assert "last_exit_code" not in data


def test_persist_crash_exit_code_swallows_oserror(make_agent, caplog) -> None:  # type: ignore[no-untyped-def]
    """Disk failures during crash-exit-code persistence are logged, not raised."""
    agent = make_agent()
    agent._resume_iteration = 5
    agent._resume_phase = "evolve"

    with patch("maestro.agent.atomic_write", side_effect=OSError("disk full")), \
         caplog.at_level("WARNING", logger="maestro"):
        agent._persist_crash_exit_code(1)

    assert any(
        "Failed to persist crash exit code" in r.message for r in caplog.records
    )


def test_read_lifecycle_progress_returns_exit_code(make_agent) -> None:  # type: ignore[no-untyped-def]
    """last_exit_code round-trips through lifecycle.json."""
    agent = make_agent()
    agent.config.lifecycle_path.write_text(
        json.dumps({"iteration": 4, "next_phase": "dream", "last_exit_code": 1}),
    )
    _, _, _, last_exit_code, crashed, *_ = agent._read_lifecycle_progress()
    assert last_exit_code == 1
    assert crashed is True


def test_persist_crash_exit_code_writes_sdk_context(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Passing ``sdk_error`` persists category, stderr head, and retry count."""
    agent = make_agent()
    agent._resume_iteration = 4
    agent._resume_phase = "contemplate"
    sdk_error = SDKError(
        "boom",
        category=ErrorCategory.CLI_CRASH,
        exit_code=1,
        stderr="ProcessError: 1\n",
        retries_attempted=2,
    )

    agent._persist_crash_exit_code(sdk_error.exit_code, sdk_error=sdk_error)

    data = json.loads(agent.config.lifecycle_path.read_text())
    assert data["last_exit_code"] == 1
    assert data["last_sdk_error"]["category"] == "cli_crash"
    assert data["last_sdk_error"]["exit_code"] == 1
    assert data["last_sdk_error"]["stderr_head"] == "ProcessError: 1"
    assert data["last_sdk_error"]["retries_attempted"] == 2
    assert data["last_sdk_error"]["message"] == "boom"


def test_persist_crash_exit_code_truncates_sdk_stderr(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Stderr head is truncated to ``_MAX_PERSISTED_SDK_STDERR_CHARS``."""
    agent = make_agent()
    agent._resume_iteration = 1
    agent._resume_phase = "dream"
    cap = agent._MAX_PERSISTED_SDK_STDERR_CHARS
    sdk_error = SDKError(
        "boom",
        category=ErrorCategory.CLI_CRASH,
        stderr="x" * (cap * 3),
    )

    agent._persist_crash_exit_code(None, sdk_error=sdk_error)

    data = json.loads(agent.config.lifecycle_path.read_text())
    assert len(data["last_sdk_error"]["stderr_head"]) == cap


# ── #4591: persisted stderr keeps the diagnostic tail, not the [DEBUG] head ──

_DEBUG_NOISE_LINE = "[DEBUG] Watching for changes in setting files ...\n"
_ERROR_LINE = "Error: connect ECONNREFUSED 127.0.0.1:443\n"


def test_persist_crash_exit_code_keeps_error_tail_past_debug_noise(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A debug-first stream keeps its trailing error line, not the noise."""
    agent = make_agent()
    agent._resume_iteration = 1
    agent._resume_phase = "dream"
    stderr = _DEBUG_NOISE_LINE * 40 + _ERROR_LINE
    assert len(stderr) > agent._MAX_PERSISTED_SDK_STDERR_CHARS
    sdk_error = SDKError(
        "boom", category=ErrorCategory.NETWORK, exit_code=1, stderr=stderr,
    )

    agent._persist_crash_exit_code(None, sdk_error=sdk_error)

    data = json.loads(agent.config.lifecycle_path.read_text())
    stderr_head = data["last_sdk_error"]["stderr_head"]
    assert "ECONNREFUSED" in stderr_head
    assert "[DEBUG]" not in stderr_head
    assert len(stderr_head) <= agent._MAX_PERSISTED_SDK_STDERR_CHARS


def test_persist_crash_exit_code_all_debug_stream_keeps_tail(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An all-debug stream (the exit-137 kill shape) persists its tail."""
    agent = make_agent()
    agent._resume_iteration = 1
    agent._resume_phase = "dream"
    cap = agent._MAX_PERSISTED_SDK_STDERR_CHARS
    lines = [f"[DEBUG] line {i} " for i in range(200)]
    stderr = "\n".join(lines)
    assert len(stderr) > cap * 3
    sdk_error = SDKError(
        "killed", category=ErrorCategory.CLI_CRASH, exit_code=137, stderr=stderr,
    )

    agent._persist_crash_exit_code(None, sdk_error=sdk_error)

    data = json.loads(agent.config.lifecycle_path.read_text())
    stderr_head = data["last_sdk_error"]["stderr_head"]
    assert stderr_head.endswith(lines[-1])
    assert "line 0 " not in stderr_head
    assert stderr_head.startswith("[DEBUG] line ")
    assert len(stderr_head) <= cap


def test_persist_crash_exit_code_debug_marker_case_insensitive(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``[debug]`` / ``[Debug]`` lines are dropped too, case preserved."""
    agent = make_agent()
    agent._resume_iteration = 1
    agent._resume_phase = "dream"
    stderr = (
        "[debug] lower noise line ...\n" * 15
        + "[Debug] title noise line ...\n" * 15
        + "[DEBUG] upper noise line ...\n" * 15
        + _ERROR_LINE
    )
    assert len(stderr) > agent._MAX_PERSISTED_SDK_STDERR_CHARS
    sdk_error = SDKError(
        "boom", category=ErrorCategory.NETWORK, exit_code=1, stderr=stderr,
    )

    agent._persist_crash_exit_code(None, sdk_error=sdk_error)

    data = json.loads(agent.config.lifecycle_path.read_text())
    stderr_head = data["last_sdk_error"]["stderr_head"]
    assert "Error: connect ECONNREFUSED 127.0.0.1:443" in stderr_head
    assert "noise line" not in stderr_head


def test_persist_then_read_crash_sdk_context_keeps_error_tail(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The SDKError rebuilt after restart still carries the error line."""
    agent = make_agent()
    agent._resume_iteration = 1
    agent._resume_phase = "dream"
    sdk_error = SDKError(
        "boom",
        category=ErrorCategory.NETWORK,
        exit_code=1,
        stderr=_DEBUG_NOISE_LINE * 40 + _ERROR_LINE,
    )

    agent._persist_crash_exit_code(None, sdk_error=sdk_error)
    rebuilt = agent._read_crash_sdk_context()

    assert rebuilt is not None
    assert rebuilt.stderr is not None
    assert "ECONNREFUSED" in rebuilt.stderr


def test_persist_crash_exit_code_without_sdk_error_omits_sdk_key(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Legacy signature (exit_code only) does not write ``last_sdk_error``."""
    agent = make_agent()
    agent._resume_iteration = 2
    agent._resume_phase = "evolve"

    agent._persist_crash_exit_code(1)

    data = json.loads(agent.config.lifecycle_path.read_text())
    assert "last_sdk_error" not in data
    assert data["last_exit_code"] == 1


def test_read_crash_sdk_context_reconstitutes_sdk_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The reader round-trips the persisted SDK context into an SDKError."""
    agent = make_agent()
    agent.config.lifecycle_path.write_text(
        json.dumps(
            {
                "iteration": 1,
                "next_phase": "evolve",
                "last_sdk_error": {
                    "message": "rate limited",
                    "category": "rate_limit",
                    "exit_code": None,
                    "stderr_head": "429 Too Many Requests",
                    "retries_attempted": 5,
                },
            }
        )
    )

    sdk_error = agent._read_crash_sdk_context()

    assert sdk_error is not None
    assert sdk_error.category == ErrorCategory.RATE_LIMIT
    assert sdk_error.exit_code is None
    assert sdk_error.stderr == "429 Too Many Requests"
    assert sdk_error.retries_attempted == 5
    assert str(sdk_error) == "rate limited"


def test_read_crash_sdk_context_absent_returns_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """No ``last_sdk_error`` key → the reader returns ``None``."""
    agent = make_agent()
    agent.config.lifecycle_path.write_text(
        json.dumps({"iteration": 4, "next_phase": "dream"}),
    )
    assert agent._read_crash_sdk_context() is None


def test_read_crash_sdk_context_unknown_category_falls_back(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An unrecognised category string degrades to ``UNKNOWN`` rather than crashing."""
    agent = make_agent()
    agent.config.lifecycle_path.write_text(
        json.dumps(
            {
                "iteration": 1,
                "next_phase": "dream",
                "last_sdk_error": {
                    "message": "weird",
                    "category": "not_a_category",
                    "exit_code": 42,
                    "stderr_head": None,
                    "retries_attempted": 0,
                },
            }
        )
    )

    sdk_error = agent._read_crash_sdk_context()

    assert sdk_error is not None
    assert sdk_error.category == ErrorCategory.UNKNOWN
    assert sdk_error.exit_code == 42


def test_read_crash_sdk_context_corrupt_file_returns_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Corrupt lifecycle file → reader returns ``None`` without raising."""
    agent = make_agent()
    agent.config.lifecycle_path.write_text("{not json")
    assert agent._read_crash_sdk_context() is None


def test_read_crash_sdk_context_non_dict_root_returns_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A JSON-valid but non-dict root (e.g. a list) degrades to ``None``."""
    agent = make_agent()
    agent.config.lifecycle_path.write_text(json.dumps([1, 2, 3]))
    assert agent._read_crash_sdk_context() is None


def test_read_crash_sdk_context_non_dict_value_returns_none(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A non-dict ``last_sdk_error`` (e.g. a list) is ignored."""
    agent = make_agent()
    agent.config.lifecycle_path.write_text(
        json.dumps({"iteration": 1, "next_phase": "dream", "last_sdk_error": []}),
    )
    assert agent._read_crash_sdk_context() is None


@pytest.mark.asyncio
async def test_shutdown_crashed_skips_graceful_journal_entry(make_agent) -> None:  # type: ignore[no-untyped-def]
    """shutdown(crashed=True) must NOT write a 'Shutting down gracefully' milestone.

    A crashed exit should not leave a journal entry claiming the shutdown
    was graceful — the startup handler writes a milestone when it reads the
    stale cursor and detects the crash, which is the truthful narrative.
    """
    agent = make_agent()
    agent._running = True
    agent.notifier = AsyncMock()  # type: ignore[assignment]

    await agent.shutdown(crashed=True)

    entries = agent.journal.read_recent_events(10)
    assert not any(
        e.triggered_by == "shutdown" and "gracefully" in e.content.lower()
        for e in entries
    )


@pytest.mark.asyncio
async def test_run_loop_unhandled_exception_defers_last_shutdown(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """An unhandled exception in a phase leaves no last_shutdown for triage.

    Reproduces the full production sequence: an unhandled ``BaseException``
    escapes a phase handler, ``run_loop()``'s outer ``finally`` runs
    ``shutdown()``, and the next startup reads ``lifecycle.json``.  Before
    the fix, ``shutdown()`` unconditionally wrote ``last_shutdown`` so
    ``_read_lifecycle_progress`` reported ``crashed=False`` and the triage
    agent never ran — the exact silent-restart Thomas had to diagnose.
    After the fix, ``run_loop()`` detects the in-flight exception via
    ``sys.exc_info()`` and passes ``crashed=True``, so the resume cursor
    stays intact and crash detection fires on the next boot.
    """
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent._write_heartbeat = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]

    async def _exploding_contemplate(**kwargs: object) -> str:
        # Leak a BaseException past the phase handler's except Exception:
        raise asyncio.CancelledError(
            "Cancelled via cancel scope 0xbaad; reason: deadline exceeded",
        )

    agent.contemplate = _exploding_contemplate  # type: ignore[method-assign]

    with patch.object(agent.evolution, "ensure_main_branch"), \
         pytest.raises(asyncio.CancelledError):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # The lifecycle file must NOT have last_shutdown — the finally block's
    # sys.exc_info() check should have classified this as crashed and
    # shutdown() should have left the resume cursor intact.
    data = json.loads(agent.config.lifecycle_path.read_text())
    assert "last_shutdown" not in data, (
        "Unhandled-exception unwind wrote last_shutdown — crash detection "
        "is masked and the triage agent will not fire on restart"
    )
    # Precondition for the crash-detection assertion below: _persist_progress
    # must have populated the resume cursor before the CancelledError escaped.
    # _read_lifecycle_progress only reports crashed=True when last_shutdown is
    # absent *and* the cursor is non-empty (iteration > 0 or next_phase set);
    # an empty cursor would make the crashed is True assertion vacuous. This
    # holds today because _persist_progress fires before any phase body runs —
    # at the iteration top and again before each phase's handler — so the
    # cursor is already populated by the time the patched contemplate raises,
    # regardless of contemplate's position in the phase order.
    cursor_populated = data.get("iteration", 0) > 0 or data.get("next_phase") is not None
    assert cursor_populated, (
        "Resume cursor was empty when the exception escaped — _persist_progress "
        "did not run before the crash, so the crashed is True assertion below "
        "would pass vacuously rather than proving crash detection fired"
    )
    # The next startup's _read_lifecycle_progress would therefore classify
    # this as a crash.
    last_shutdown, _, _, _, crashed, *_ = agent._read_lifecycle_progress()
    assert last_shutdown is None
    assert crashed is True


# ---------------------------------------------------------------------------
# run_loop Telegram polling tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_starts_telegram_polling(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() starts Telegram polling when notifier is TelegramNotifier."""
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock()
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

    mock_notifier.start_approval_polling.assert_called_once()
    mock_notifier.stop_approval_polling.assert_called_once()


@pytest.mark.asyncio
async def test_run_loop_polling_teardown_propagates_system_exit(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A SystemExit raised while stopping polling propagates, not swallowed (#398)."""
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock()
    mock_notifier.stop_approval_polling = AsyncMock(side_effect=SystemExit())
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

    # The explicit exit request raised during teardown must propagate out of
    # run_loop instead of being logged-and-swallowed. Against the old
    # ``except BaseException:`` it was swallowed and run_loop returned True.
    with pytest.raises(SystemExit):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    mock_notifier.stop_approval_polling.assert_awaited_once()
    # The exit request propagates, but shutdown() still runs first (in the
    # teardown ``finally``) so the PID file is unlinked and the graceful
    # lifecycle marker is written — otherwise the next startup would misread
    # this graceful exit as a crash and spuriously fire crash triage.
    agent.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_loop_polling_teardown_swallows_runtime_error(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A plain error while stopping polling is still swallowed; shutdown still runs (#398)."""
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock()
    mock_notifier.stop_approval_polling = AsyncMock(side_effect=RuntimeError("boom"))
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

    # The narrowed catch still absorbs ordinary Exception subclasses so a
    # teardown failure cannot itself block shutdown.
    result = await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    assert result is True
    mock_notifier.stop_approval_polling.assert_awaited_once()
    agent.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_loop_telegram_approve_callback_updates_status(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The Telegram _on_approve callback sets plan status to 'approved'."""
    from maestro.memory import EvolutionPlan
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()

    captured_callbacks: dict[str, object] = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured_callbacks["on_approve"] = on_approve
        captured_callbacks["on_reject"] = on_reject

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

    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="ptg01", title="TG Approve",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # Invoke the captured callback
    on_approve = captured_callbacks["on_approve"]
    result = await on_approve("ptg01")  # type: ignore[misc]
    assert result == "approved"

    plan = agent.memory.get_plan("ptg01")
    assert plan is not None
    assert plan.status == "approved"


@pytest.mark.asyncio
async def test_run_loop_telegram_reject_callback_updates_status(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The Telegram _on_reject callback sets plan status to 'rejected'."""
    from maestro.memory import EvolutionPlan
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()

    captured_callbacks: dict[str, object] = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured_callbacks["on_reject"] = on_reject

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

    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="ptg02", title="TG Reject",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_reject = captured_callbacks["on_reject"]
    result = await on_reject("ptg02")  # type: ignore[misc]
    assert result == "rejected"

    plan = agent.memory.get_plan("ptg02")
    assert plan is not None
    assert plan.status == "rejected"
    assert plan.rejection_reason == "Rejected via Telegram"


def _make_telegram_loop_agent(make_agent) -> tuple[object, dict[str, object]]:  # type: ignore[no-untyped-def]
    """Agent with a mocked TelegramNotifier whose approval callbacks are captured."""
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    captured_callbacks: dict[str, object] = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured_callbacks["on_approve"] = on_approve
        captured_callbacks["on_reject"] = on_reject

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

    agent._install_signal_handlers = MagicMock()
    agent._write_pid_file = MagicMock()
    agent.contributions.check_for_contributions = MagicMock()
    agent.contemplate = AsyncMock(return_value="reflection")
    agent._implement_approved_plans = AsyncMock()
    agent.evolve = AsyncMock()
    agent.shutdown = AsyncMock()
    return agent, captured_callbacks


@pytest.mark.asyncio
async def test_telegram_approve_refused_on_content_drift(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_on_approve refuses when the content hash drifted from the notified
    snapshot — the buttons approve what was displayed (proposal 021)."""
    from maestro.memory import EvolutionPlan

    agent, captured = _make_telegram_loop_agent(make_agent)

    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="ptgd1", title="TG Drift",
        category="capability", rationale="test", proposed_changes=["x"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    # Notified snapshot that does not match the plan's current content.
    agent.memory.update_plan_status(
        "ptgd1", "pending_review", notified_content_hash="0" * 64,
    )

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_approve = captured["on_approve"]
    with pytest.raises(ValueError, match="content drifted"):
        await on_approve("ptgd1")  # type: ignore[misc]

    plan = agent.memory.get_plan("ptgd1")
    assert plan is not None
    assert plan.status == "pending_review"


@pytest.mark.asyncio
async def test_telegram_reject_refused_on_content_drift(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_on_reject mirrors the drift refusal so drifted rejections never feed
    rejection learning."""
    from maestro.memory import EvolutionPlan

    agent, captured = _make_telegram_loop_agent(make_agent)

    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="ptgd2", title="TG Drift Reject",
        category="capability", rationale="test", proposed_changes=["x"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    agent.memory.update_plan_status(
        "ptgd2", "pending_review", notified_content_hash="0" * 64,
    )

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_reject = captured["on_reject"]
    with pytest.raises(ValueError, match="content drifted"):
        await on_reject("ptgd2")  # type: ignore[misc]

    plan = agent.memory.get_plan("ptgd2")
    assert plan is not None
    assert plan.status == "pending_review"
    assert plan.rejection_reason is None


@pytest.mark.asyncio
async def test_telegram_approve_proceeds_on_matching_hash(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_on_approve proceeds when the notified snapshot matches current content."""
    from maestro.memory import EvolutionPlan, compute_plan_content_hash

    agent, captured = _make_telegram_loop_agent(make_agent)

    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="ptgm1", title="TG Match",
        category="capability", rationale="test", proposed_changes=["x"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("ptgm1")
    assert saved is not None
    agent.memory.update_plan_status(
        "ptgm1", "pending_review",
        notified_content_hash=compute_plan_content_hash(saved),
    )

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_approve = captured["on_approve"]
    result = await on_approve("ptgm1")  # type: ignore[misc]
    assert result == "approved"

    plan = agent.memory.get_plan("ptgm1")
    assert plan is not None
    assert plan.status == "approved"


@pytest.mark.asyncio
async def test_telegram_reject_proceeds_on_matching_hash(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_on_reject proceeds when the notified snapshot matches current content."""
    from maestro.memory import EvolutionPlan, compute_plan_content_hash

    agent, captured = _make_telegram_loop_agent(make_agent)

    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="ptgm2", title="TG Match Reject",
        category="capability", rationale="test", proposed_changes=["x"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan("ptgm2")
    assert saved is not None
    agent.memory.update_plan_status(
        "ptgm2", "pending_review",
        notified_content_hash=compute_plan_content_hash(saved),
    )

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_reject = captured["on_reject"]
    result = await on_reject("ptgm2")  # type: ignore[misc]
    assert result == "rejected"

    plan = agent.memory.get_plan("ptgm2")
    assert plan is not None
    assert plan.status == "rejected"


@pytest.mark.asyncio
async def test_run_loop_telegram_message_callback(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The Telegram _on_message callback saves a message to message_store."""
    from maestro.messaging import MessageStore
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)

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
    await on_message("Hello from Thomas")  # type: ignore[misc]

    msgs = agent.message_store.get_recent(10)
    assert len(msgs) == 1
    assert msgs[0].sender == "thomas"
    assert msgs[0].content == "Hello from Thomas"
    assert msgs[0].message_type == "instruction"


@pytest.mark.asyncio
async def test_run_loop_tolerates_telegram_start_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() continues when start_approval_polling raises."""
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock(
        side_effect=RuntimeError("Bot init failed"),
    )
    mock_notifier.stop_approval_polling = AsyncMock()
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

    # Should not raise
    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    mock_notifier.start_approval_polling.assert_called_once()
    # stop should NOT be called because start failed
    mock_notifier.stop_approval_polling.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_process_rejected_plans_in_loop(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() calls _process_rejected_plans each iteration."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    plan = EvolutionPlan(
        plan_id="rejloop",
        title="Reject in Loop",
        category="capability",
        rationale="test",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status="rejected",
        rejection_reason="Test reason",
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    agent.evolution.learn_from_rejection.assert_called_once()
    reloaded = agent.memory.get_plan("rejloop")
    assert reloaded is not None
    assert reloaded.rejection_learned is True


# --- Telegram callback status-guard tests ---


@pytest.mark.asyncio
async def test_telegram_approve_bypasses_status_guard(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_on_approve must not flip status of a plan that is not in pending_review."""
    from maestro.memory import EvolutionPlan
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()

    captured_callbacks: dict = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured_callbacks["on_approve"] = on_approve

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

    plan = EvolutionPlan(
        plan_id="ptg_stale",
        title="Stale Plan",
        category="capability",
        rationale="test",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        created_at="2025-01-01T00:00:00Z",
        status="implemented",
    )
    agent.memory.save_evolution_plan(plan)

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_approve = captured_callbacks["on_approve"]

    with pytest.raises(ValueError, match="not in pending_review"):
        await on_approve("ptg_stale")

    retrieved = agent.memory.get_plan("ptg_stale")
    assert retrieved is not None
    assert retrieved.status == "implemented", (
        f"BUG: Plan status changed from 'implemented' to '{retrieved.status}' "
        "when stale Telegram approve was pressed."
    )


@pytest.mark.asyncio
async def test_telegram_approve_returns_ignored_for_nonexistent_plan(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_on_approve must raise ValueError (not approve) for a non-existent plan."""
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    captured_callbacks: dict = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured_callbacks["on_approve"] = on_approve

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

    on_approve = captured_callbacks["on_approve"]

    with pytest.raises(ValueError, match="not in pending_review"):
        await on_approve("completely_nonexistent_plan_id")


@pytest.mark.asyncio
async def test_on_approve_returns_ignored_when_update_plan_status_fails(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_on_approve must return 'ignored' (not 'approved') when update_plan_status returns False.

    This simulates a race condition where the plan exists at get_plan() time
    but disappears before update_plan_status() can commit the change.
    """
    from maestro.memory import EvolutionPlan
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    captured_callbacks: dict = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured_callbacks["on_approve"] = on_approve

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

    plan = EvolutionPlan(
        plan_id="race1",
        title="Race Plan",
        category="capability",
        rationale="test",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        created_at="2025-01-01T00:00:00Z",
        status="pending_review",
    )
    agent.memory.save_evolution_plan(plan)

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_approve = captured_callbacks["on_approve"]

    # Simulate race: update_plan_status returns False (plan vanished concurrently)
    agent.memory.update_plan_status = MagicMock(return_value=False)  # type: ignore[method-assign]

    result = await on_approve("race1")
    assert result == "ignored", (
        f"Expected 'ignored' when update_plan_status returns False, got {result!r}. "
        "The _on_approve callback silently ignores the failed update."
    )


@pytest.mark.asyncio
async def test_on_reject_returns_ignored_when_update_plan_status_fails(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_on_reject must return 'ignored' (not 'rejected') when update_plan_status returns False."""
    from maestro.memory import EvolutionPlan
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    captured_callbacks: dict = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured_callbacks["on_reject"] = on_reject

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

    plan = EvolutionPlan(
        plan_id="race2",
        title="Race Plan",
        category="capability",
        rationale="test",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        created_at="2025-01-01T00:00:00Z",
        status="pending_review",
    )
    agent.memory.save_evolution_plan(plan)

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    on_reject = captured_callbacks["on_reject"]

    # Simulate race: update_plan_status returns False (plan vanished concurrently)
    agent.memory.update_plan_status = MagicMock(return_value=False)  # type: ignore[method-assign]

    result = await on_reject("race2")
    assert result == "ignored", (
        f"Expected 'ignored' when update_plan_status returns False, got {result!r}. "
        "The _on_reject callback silently ignores the failed update."
    )


# ──────────────────────────────────────────────────────────────────────
# Coverage gap tests
# ──────────────────────────────────────────────────────────────────────


def test_build_identity_context_prefixes_objective(make_agent) -> None:  # type: ignore[no-untyped-def]
    """The identity context leads with the objective framing; no stage is read."""
    agent = make_agent()
    ctx = agent._build_identity_context()
    from maestro.prompts.defaults import OBJECTIVE_PROMPT
    assert ctx.startswith(OBJECTIVE_PROMPT)


@pytest.mark.asyncio
async def test_run_planner_exception_propagates(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When planner.decompose raises, run() lets the exception propagate."""
    agent = make_agent()

    with patch("maestro.planner.query", new_callable=AsyncMock,
               side_effect=RuntimeError("LLM timeout")), \
         pytest.raises(RuntimeError, match="LLM timeout"):
        await agent.run("Some goal")


@pytest.mark.asyncio
async def test_contemplate_journal_write_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """contemplate() continues even if journal.write_thought raises (lines 405-406)."""
    agent = make_agent()

    from maestro.consciousness import SelfModel as _SelfModel

    with patch.object(agent.consciousness, "introspect", new_callable=AsyncMock,
                      return_value="deep thought"), patch.object(agent.journal, "write_thought",
                      side_effect=RuntimeError("disk full")), patch.object(agent.consciousness, "update_self_model",
                      AsyncMock(return_value=_SelfModel())):
        result = await agent.contemplate()

    assert result == "deep thought"
