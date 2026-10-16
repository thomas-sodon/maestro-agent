"""Athrow-task snapshot/drain and the anyio cancel-scope probe (#1643); split out
of ``tests/test_sdk.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.sdk import (
    _probe_anyio_deadline_message_format,
    configure_sdk_transcripts,
    query,
)
from tests.test_sdk import (
    _result_msg,
)


@pytest.fixture(autouse=True)
def _reset_sdk_transcripts() -> None:
    """Reset the structured-transcript sidecar globals before every test.

    ``[logs] structured_transcripts_enabled`` now defaults on, so a test that
    builds an agent — which calls ``configure_sdk_transcripts`` from config —
    would otherwise leak the enabled flag into these session-logging tests,
    which assume the raw-``.log``-only default.
    """
    configure_sdk_transcripts(False)


# ---------------------------------------------------------------------------
# _snapshot_athrow_tasks (#1643 diagnostic probe)
# ---------------------------------------------------------------------------

class TestSnapshotAthrowTasks:
    """Pin the contract of the ``async_generator_athrow`` diagnostic probe."""

    @pytest.mark.asyncio
    async def test_silent_when_no_athrow_tasks_pending(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from maestro.sdk import _snapshot_athrow_tasks

        with caplog.at_level(logging.DEBUG, logger="maestro"):
            count = _snapshot_athrow_tasks("baseline")

        assert count == 0
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    @pytest.mark.asyncio
    async def test_logs_each_pending_athrow_task(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from maestro.sdk import _snapshot_athrow_tasks

        # Spawn an async generator whose GeneratorExit handler parks
        # forever, then schedule aclose() as a task without awaiting it.
        # That task lands inside ``async_generator_athrow`` waiting on the
        # never-firing event — exactly the residual-task shape #1643
        # describes.
        started = asyncio.Event()
        block = asyncio.Event()

        async def stuck_gen() -> Any:
            try:
                yield 1
            except GeneratorExit:
                started.set()
                await block.wait()
                raise

        g = stuck_gen()
        await g.__anext__()
        athrow_task = asyncio.create_task(g.aclose())
        await started.wait()

        try:
            with caplog.at_level(logging.WARNING, logger="maestro"):
                count = _snapshot_athrow_tasks("test-label")
            assert count >= 1
            assert "test-label" in caplog.text
            assert "async_generator_athrow" in caplog.text
        finally:
            block.set()
            athrow_task.cancel()
            with contextlib.suppress(
                asyncio.CancelledError, GeneratorExit, RuntimeError,
            ):
                await athrow_task

    def test_swallows_introspection_errors(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``asyncio.all_tasks`` raising must not propagate out of the probe."""
        from maestro.sdk import _snapshot_athrow_tasks

        with patch(
            "maestro.sdk.asyncio.all_tasks", side_effect=RuntimeError("no loop"),
        ), caplog.at_level(logging.DEBUG, logger="maestro"):
            count = _snapshot_athrow_tasks("oops")

        assert count == 0
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    def test_swallows_unexpected_introspection_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-``RuntimeError`` failures from ``all_tasks`` are still swallowed."""
        from maestro.sdk import _snapshot_athrow_tasks

        with patch(
            "maestro.sdk.asyncio.all_tasks", side_effect=ValueError("boom"),
        ), caplog.at_level(logging.DEBUG, logger="maestro"):
            count = _snapshot_athrow_tasks("oops")

        assert count == 0
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []
        debug_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert any("introspection failed" in m for m in debug_msgs)

    def test_skips_tasks_whose_introspection_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A task whose ``get_coro()`` raises is skipped, not propagated."""
        from maestro.sdk import _snapshot_athrow_tasks

        broken = MagicMock()
        broken.get_coro.side_effect = RuntimeError("inspect failed")

        with patch(
            "maestro.sdk.asyncio.all_tasks", return_value={broken},
        ), caplog.at_level(logging.WARNING, logger="maestro"):
            count = _snapshot_athrow_tasks("skip")

        assert count == 0
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == []

    def test_format_athrow_task_returns_none_on_introspection_failure(
        self,
    ) -> None:
        """``_format_athrow_task`` is called from log-line builders inside
        the snapshot probe and the drain wrapper. If ``get_name()`` or
        ``get_coro()`` raises after the scanner already accepted the task
        (TOCTOU window), the helper must yield ``None`` so the surrounding
        ``"; ".join`` skips it instead of propagating the failure out of
        a ``finally`` block."""
        from maestro.sdk import _format_athrow_task

        broken = MagicMock()
        broken.get_name.side_effect = RuntimeError("name broken")

        assert _format_athrow_task(broken) is None

    def test_collect_athrow_tasks_swallows_introspection_errors(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``_collect_athrow_tasks`` must absorb introspection failures
        from both ``asyncio.all_tasks`` and individual tasks. Pinning the
        contract here keeps both helpers (snapshot, drain) safe in
        ``finally`` blocks even after the refactor."""
        from maestro.sdk import _collect_athrow_tasks

        with patch(
            "maestro.sdk.asyncio.all_tasks", side_effect=RuntimeError("no loop"),
        ), caplog.at_level(logging.DEBUG, logger="maestro"):
            assert _collect_athrow_tasks() == []

        with patch(
            "maestro.sdk.asyncio.all_tasks", side_effect=ValueError("boom"),
        ), caplog.at_level(logging.DEBUG, logger="maestro"):
            assert _collect_athrow_tasks() == []

        broken = MagicMock()
        broken.get_coro.side_effect = RuntimeError("inspect failed")
        with patch(
            "maestro.sdk.asyncio.all_tasks", return_value={broken},
        ), caplog.at_level(logging.WARNING, logger="maestro"):
            assert _collect_athrow_tasks() == []

    def test_per_task_introspection_failure_logs_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A per-task introspection failure inside ``_collect_athrow_tasks``
        must emit a DEBUG record — honoring the docstring's DEBUG-swallow
        contract — while a healthy matching task is still returned. Before
        the fix the failing task's bare ``except Exception: continue`` dropped
        it completely silently, contradicting the documented contract."""
        from maestro.sdk import _collect_athrow_tasks

        class _AthrowCoro:
            def __init__(self) -> None:
                self.__qualname__ = "async_generator_athrow"

        healthy = MagicMock()
        healthy.get_coro.return_value = _AthrowCoro()
        broken = MagicMock()
        broken.get_coro.side_effect = RuntimeError("inspect failed")

        with patch(
            "maestro.sdk.asyncio.all_tasks", return_value={healthy, broken},
        ), caplog.at_level(logging.DEBUG, logger="maestro"):
            collected = _collect_athrow_tasks()

        assert collected == [healthy]
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG
            and "task introspection failed" in r.getMessage()
        ]
        assert len(debug_records) >= 1


class TestDrainAthrowTasks:
    """Pin the contract of the ``async_generator_athrow`` drain helper (#1648)."""

    @pytest.mark.asyncio
    async def test_returns_zero_pending_when_idle(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from maestro.sdk import _drain_athrow_tasks

        with caplog.at_level(logging.WARNING, logger="maestro"):
            done, pending = await _drain_athrow_tasks("idle")

        assert (done, pending) == (0, 0)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    @pytest.mark.asyncio
    async def test_drains_completing_athrow_task(self) -> None:
        """Spawn a stuck-then-completing athrow task and confirm the drain
        waits for it inside the caller's task. After the drain returns,
        no athrow tasks remain pending."""
        from maestro.sdk import _collect_athrow_tasks, _drain_athrow_tasks

        async def child() -> Any:
            try:
                yield 1
            except GeneratorExit:
                await asyncio.sleep(0)
                raise

        inner = child()
        await inner.__anext__()
        athrow_task = asyncio.create_task(inner.aclose())

        # Pre-drain: at least one athrow task is pending.
        assert any(
            t is athrow_task for t in _collect_athrow_tasks()
        ), "Setup invariant: athrow task must be pending before drain."

        done, pending = await _drain_athrow_tasks("test")

        assert pending == 0
        assert done >= 1
        assert _collect_athrow_tasks() == []

    @pytest.mark.asyncio
    async def test_actively_cancels_stuck_athrow_task(self) -> None:
        """An athrow task whose ``GeneratorExit`` handler awaits an
        ``asyncio.Event`` that is never set must be force-completed by
        the drain (#1704). Before the fix, ``asyncio.wait`` would just
        time out (returning ``pending=1``) and the orphan would bleed
        ``CancelledError`` into the next ``await`` in the caller's task.
        After the fix, the drain actively calls ``task.cancel()`` on
        every collected athrow task so the orphan's cancel scope
        unwinds in its own task context rather than ours."""
        from maestro.sdk import _collect_athrow_tasks, _drain_athrow_tasks

        started = asyncio.Event()
        block = asyncio.Event()  # intentionally never set

        async def stuck_child() -> Any:
            try:
                yield 1
            except GeneratorExit:
                started.set()
                await block.wait()
                raise

        inner = stuck_child()
        await inner.__anext__()
        athrow_task = asyncio.create_task(inner.aclose())
        await started.wait()

        assert any(
            t is athrow_task for t in _collect_athrow_tasks()
        ), "Setup invariant: athrow task must be pending before drain."

        done, pending = await _drain_athrow_tasks("test", timeout_seconds=0.5)

        assert pending == 0
        assert done >= 1
        assert athrow_task.done()
        # Active cancellation forced completion. Either ``cancelled()``
        # is True (the most common path) or the task's exception is a
        # ``CancelledError`` swallowed by ``aclose()`` finalization.
        assert athrow_task.cancelled() or isinstance(
            athrow_task.exception(), asyncio.CancelledError,
        )

    @pytest.mark.asyncio
    async def test_warns_when_drain_times_out(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A genuinely uncancellable athrow task — one whose
        ``GeneratorExit`` handler swallows ``CancelledError`` in a
        loop — must still surface as a WARNING after the drain
        timeout. That is the visibility signal for the shielded-orphan
        edge case the active-cancel fix cannot resolve (#1704)."""
        from maestro.sdk import _drain_athrow_tasks

        started = asyncio.Event()
        block = asyncio.Event()

        async def stuck_child() -> Any:
            try:
                yield 1
            except GeneratorExit:
                started.set()
                while True:
                    try:
                        await block.wait()
                        break
                    except asyncio.CancelledError:
                        continue
                raise

        inner = stuck_child()
        await inner.__anext__()
        athrow_task = asyncio.create_task(inner.aclose())
        await started.wait()

        try:
            with caplog.at_level(logging.WARNING, logger="maestro"):
                done, pending = await _drain_athrow_tasks(
                    "stuck-label", timeout_seconds=0.01,
                )
            assert done == 0
            assert pending >= 1
            assert "stuck-label" in caplog.text
            assert "async_generator_athrow" in caplog.text
        finally:
            block.set()
            with contextlib.suppress(
                asyncio.CancelledError, GeneratorExit, RuntimeError,
            ):
                await asyncio.wait_for(athrow_task, timeout=0.5)

    @pytest.mark.asyncio
    async def test_swallows_unexpected_wait_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Defence-in-depth: if ``asyncio.wait`` raises an ``Exception``,
        the drain must swallow it (helper runs in ``finally`` blocks
        where any escaping exception clobbers the in-flight one)."""
        from maestro.sdk import _drain_athrow_tasks

        fake_task = MagicMock(spec=asyncio.Task)

        # ``side_effect`` is a coroutine function, so the patched
        # ``asyncio.wait`` returns a coroutine that raises on await — the
        # raise comes from the awaited result, not the call itself.
        async def failing_wait(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("wait broken")

        with patch(
            "maestro.sdk._collect_athrow_tasks", return_value=[fake_task],
        ), patch(
            "maestro.sdk.asyncio.wait", side_effect=failing_wait,
        ), caplog.at_level(logging.DEBUG, logger="maestro"):
            done, pending = await _drain_athrow_tasks("oops")

        assert (done, pending) == (0, 1)
        debug_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert any("athrow drain oops: wait failed" in m for m in debug_msgs)


class TestCollectSdkChildTasks:
    """Pin the contract of ``_collect_sdk_child_tasks`` (#1719).

    Mirrors the ``TestSnapshotAthrowTasks`` shape: synthetic tasks
    parked on a never-firing ``asyncio.Event`` carry one of the three
    SDK child-task coroutine names (``stream_input`` /
    ``wait_for_result_and_end_input`` / ``_handle_control_request``)
    and must be discovered by the collector. Introspection failures
    must be swallowed so the helper is safe inside ``finally`` blocks.
    """

    @pytest.mark.asyncio
    async def test_finds_each_named_child_coroutine(self) -> None:
        from maestro.sdk import _collect_sdk_child_tasks, _sdk_child_owner_scope

        block = asyncio.Event()  # intentionally never set

        async def stream_input() -> None:
            await block.wait()

        async def wait_for_result_and_end_input() -> None:
            await block.wait()

        async def _handle_control_request() -> None:
            await block.wait()

        spawned: list[asyncio.Task[None]] = []
        try:
            with _sdk_child_owner_scope() as owner:
                for fn in (
                    stream_input,
                    wait_for_result_and_end_input,
                    _handle_control_request,
                ):
                    spawned.append(asyncio.create_task(fn()))
            await asyncio.sleep(0)  # let tasks reach the await

            collected = _collect_sdk_child_tasks(owner)
            collected_set = {id(t) for t in collected}
            for task in spawned:
                assert id(task) in collected_set
        finally:
            block.set()
            for t in spawned:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    @pytest.mark.asyncio
    async def test_silent_on_unrelated_tasks(self) -> None:
        from maestro.sdk import _collect_sdk_child_tasks, _sdk_child_owner_scope

        block = asyncio.Event()

        async def some_other_coro() -> None:
            await block.wait()

        with _sdk_child_owner_scope() as owner:
            unrelated = asyncio.create_task(some_other_coro())
        try:
            await asyncio.sleep(0)
            collected = _collect_sdk_child_tasks(owner)
            assert all(t is not unrelated for t in collected)
        finally:
            block.set()
            unrelated.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await unrelated

    @pytest.mark.asyncio
    async def test_silent_on_substring_collision(self) -> None:
        # A coroutine whose leaf name merely *contains* an SDK child token
        # (``custom_stream_input_wrapper`` contains ``stream_input``) must
        # not be collected — the old substring match would have caught it and
        # the drain helper would then cancel this unrelated task (#1722).
        from maestro.sdk import _collect_sdk_child_tasks, _sdk_child_owner_scope

        block = asyncio.Event()  # intentionally never set

        async def custom_stream_input_wrapper() -> None:
            await block.wait()

        with _sdk_child_owner_scope() as owner:
            collider = asyncio.create_task(custom_stream_input_wrapper())
        try:
            await asyncio.sleep(0)  # let the task reach the await
            collected = _collect_sdk_child_tasks(owner)
            assert all(t is not collider for t in collected)
        finally:
            block.set()
            collider.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await collider

    def test_leaf_from_repr_when_qualname_empty(self) -> None:
        # Fallback path: when ``__qualname__`` is unavailable the leaf name is
        # recovered from the coroutine repr, and the same exact-leaf rule
        # applies — a real child name is collected, a substring collision is
        # not (#1722).
        from maestro.sdk import _collect_sdk_child_tasks

        class _NoQualnameCoro:
            __qualname__ = ""

            def __init__(self, name: str) -> None:
                self._name = name

            def __repr__(self) -> str:
                return f"<coroutine object {self._name} at 0x1>"

        owner = object()
        child_task = MagicMock()
        child_task.get_coro.return_value = _NoQualnameCoro("stream_input")
        child_task.get_context.return_value.get.return_value = owner
        collider_task = MagicMock()
        collider_task.get_coro.return_value = _NoQualnameCoro(
            "custom_stream_input_wrapper"
        )
        collider_task.get_context.return_value.get.return_value = owner

        with patch(
            "maestro.sdk.asyncio.all_tasks",
            return_value={child_task, collider_task},
        ):
            collected = _collect_sdk_child_tasks(owner)

        assert child_task in collected
        assert collider_task not in collected

    def test_swallows_introspection_errors(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from maestro.sdk import _collect_sdk_child_tasks

        with patch(
            "maestro.sdk.asyncio.all_tasks", side_effect=RuntimeError("no loop"),
        ), caplog.at_level(logging.DEBUG, logger="maestro"):
            assert _collect_sdk_child_tasks(object()) == []

        with patch(
            "maestro.sdk.asyncio.all_tasks", side_effect=ValueError("boom"),
        ), caplog.at_level(logging.DEBUG, logger="maestro"):
            assert _collect_sdk_child_tasks(object()) == []

        broken = MagicMock()
        broken.get_coro.side_effect = RuntimeError("inspect failed")
        with patch(
            "maestro.sdk.asyncio.all_tasks", return_value={broken},
        ), caplog.at_level(logging.WARNING, logger="maestro"):
            assert _collect_sdk_child_tasks(object()) == []

    def test_per_task_introspection_failure_logs_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A per-task introspection failure inside
        ``_collect_sdk_child_tasks`` must emit a DEBUG record — honoring the
        docstring's DEBUG-swallow contract — while a healthy matching task is
        still returned. Before the fix the failing task's bare
        ``except Exception: continue`` dropped it completely silently,
        contradicting the documented contract."""
        from maestro.sdk import _collect_sdk_child_tasks

        class _ChildCoro:
            def __init__(self) -> None:
                self.__qualname__ = "stream_input"

        owner = object()
        healthy = MagicMock()
        healthy.get_coro.return_value = _ChildCoro()
        healthy.get_context.return_value.get.return_value = owner
        broken = MagicMock()
        broken.get_coro.side_effect = RuntimeError("inspect failed")

        with patch(
            "maestro.sdk.asyncio.all_tasks", return_value={healthy, broken},
        ), caplog.at_level(logging.DEBUG, logger="maestro"):
            collected = _collect_sdk_child_tasks(owner)

        assert collected == [healthy]
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG
            and "task introspection failed" in r.getMessage()
        ]
        assert len(debug_records) >= 1


class TestDrainSdkChildTasks:
    """Pin the contract of the ``_drain_sdk_child_tasks`` helper (#1719)."""

    @pytest.mark.asyncio
    async def test_returns_zero_pending_when_idle(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from maestro.sdk import _drain_sdk_child_tasks

        with caplog.at_level(logging.WARNING, logger="maestro"):
            done, pending = await _drain_sdk_child_tasks("idle", owner=object())

        assert (done, pending) == (0, 0)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    @pytest.mark.asyncio
    async def test_drains_completing_child_task(self) -> None:
        """A child task that returns immediately must be drained — done by
        the time the helper returns and absent from a follow-up scan."""
        from maestro.sdk import (
            _collect_sdk_child_tasks,
            _drain_sdk_child_tasks,
            _sdk_child_owner_scope,
        )

        async def stream_input() -> None:
            await asyncio.sleep(0)

        with _sdk_child_owner_scope() as owner:
            child_task = asyncio.create_task(stream_input())
        await asyncio.sleep(0)

        assert any(
            t is child_task for t in _collect_sdk_child_tasks(owner)
        ), "Setup invariant: child task must be pending before drain."

        done, pending = await _drain_sdk_child_tasks("test", owner=owner)

        assert pending == 0
        assert done >= 1
        assert _collect_sdk_child_tasks(owner) == []

    @pytest.mark.asyncio
    async def test_actively_cancels_stuck_child_task(self) -> None:
        """A child task parked forever on an ``asyncio.Event`` must be
        force-cancelled by the drain so its cancel scope unwinds in its
        own task context rather than leaking ``CancelledError`` into the
        next ``await`` (#1719). The same active-cancel shape #1704
        introduced for the athrow drain."""
        from maestro.sdk import (
            _collect_sdk_child_tasks,
            _drain_sdk_child_tasks,
            _sdk_child_owner_scope,
        )

        block = asyncio.Event()  # never set

        async def wait_for_result_and_end_input() -> None:
            await block.wait()

        with _sdk_child_owner_scope() as owner:
            child_task = asyncio.create_task(wait_for_result_and_end_input())
        await asyncio.sleep(0)

        assert any(
            t is child_task for t in _collect_sdk_child_tasks(owner)
        ), "Setup invariant: child task must be pending before drain."

        done, pending = await _drain_sdk_child_tasks(
            "test", owner=owner, timeout_seconds=0.5,
        )

        assert pending == 0
        assert done >= 1
        assert child_task.done()
        assert child_task.cancelled() or isinstance(
            child_task.exception(), asyncio.CancelledError,
        )

    @pytest.mark.asyncio
    async def test_warns_when_drain_times_out(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A genuinely uncancellable child task — one that swallows
        ``CancelledError`` in a loop — must surface as a WARNING after
        the drain timeout. Visibility for the shielded-orphan edge case
        the active-cancel cannot resolve."""
        from maestro.sdk import _drain_sdk_child_tasks, _sdk_child_owner_scope

        block = asyncio.Event()

        async def _handle_control_request() -> None:
            while True:
                try:
                    await block.wait()
                    break
                except asyncio.CancelledError:
                    continue

        with _sdk_child_owner_scope() as owner:
            child_task = asyncio.create_task(_handle_control_request())
        await asyncio.sleep(0)

        try:
            with caplog.at_level(logging.WARNING, logger="maestro"):
                done, pending = await _drain_sdk_child_tasks(
                    "stuck-child-label", owner=owner, timeout_seconds=0.01,
                )
            assert done == 0
            assert pending >= 1
            assert "stuck-child-label" in caplog.text
            assert "SDK child task" in caplog.text
        finally:
            block.set()
            with contextlib.suppress(
                asyncio.CancelledError, RuntimeError,
            ):
                await asyncio.wait_for(child_task, timeout=0.5)

    @pytest.mark.asyncio
    async def test_swallows_unexpected_wait_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Defence-in-depth: if ``asyncio.wait`` raises, the drain must
        swallow it. Helper runs in ``finally`` blocks where any escaping
        exception clobbers the in-flight one."""
        from maestro.sdk import _drain_sdk_child_tasks

        fake_task = MagicMock(spec=asyncio.Task)

        async def failing_wait(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("wait broken")

        with patch(
            "maestro.sdk._collect_sdk_child_tasks", return_value=[fake_task],
        ), patch(
            "maestro.sdk.asyncio.wait", side_effect=failing_wait,
        ), caplog.at_level(logging.DEBUG, logger="maestro"):
            done, pending = await _drain_sdk_child_tasks("oops", owner=object())

        assert (done, pending) == (0, 1)
        debug_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert any("sdk child drain oops: wait failed" in m for m in debug_msgs)

    def test_none_owner_collects_nothing(self) -> None:
        """An unidentifiable owner must match no task — the drain never
        falls back to a loop-wide sweep (#4158)."""
        from maestro.sdk import _collect_sdk_child_tasks

        unowned = MagicMock()
        unowned.get_coro.return_value.__qualname__ = "stream_input"
        unowned.get_context.return_value.get.return_value = None

        with patch("maestro.sdk.asyncio.all_tasks", return_value={unowned}):
            assert _collect_sdk_child_tasks(None) == []

    @pytest.mark.asyncio
    async def test_drain_leaves_sibling_owner_child_tasks_alive(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Owner A's drain cancels only A's children: a concurrent owner
        B's stdin-closer and an unowned same-named task stay alive (#4158)."""
        from maestro.sdk import _drain_sdk_child_tasks, _sdk_child_owner_scope

        block = asyncio.Event()  # never set

        async def wait_for_result_and_end_input() -> None:
            await block.wait()

        with _sdk_child_owner_scope() as owner_a:
            task_a = asyncio.create_task(wait_for_result_and_end_input())
        with _sdk_child_owner_scope():
            task_b = asyncio.create_task(wait_for_result_and_end_input())
        unowned = asyncio.create_task(wait_for_result_and_end_input())
        await asyncio.sleep(0)

        try:
            with caplog.at_level(logging.DEBUG, logger="maestro"):
                done, pending = await _drain_sdk_child_tasks(
                    "owner-a", owner=owner_a, timeout_seconds=0.5,
                )
            assert (done, pending) == (1, 0)
            assert task_a.cancelled()
            assert not task_b.done()
            assert not unowned.done()
            assert "skipped 2 foreign-owned" in caplog.text
        finally:
            block.set()
            await asyncio.gather(task_b, unowned)

class TestSdkChildOwnerScoping:
    """End-to-end ownership of SDK child tasks across concurrent callers (#4158)."""

    @pytest.mark.asyncio
    async def test_concurrent_query_keeps_sibling_stdin_closer(self) -> None:
        """Two concurrent ``query()`` calls: A finishing first must not cancel
        B's stdin-closer, so B still returns its result instead of failing."""
        # A's closer is released only once B's closer exists, so A's
        # post-aclose drain runs while B's stdin-closer is still parked.
        releases = {"a": asyncio.Event(), "b": asyncio.Event()}
        calls: list[str] = []

        async def gen(**kwargs: Any) -> Any:
            name = kwargs["prompt"]
            calls.append(name)

            async def wait_for_result_and_end_input() -> None:
                await releases[name].wait()

            closer = asyncio.create_task(wait_for_result_and_end_input())
            if name == "b":
                releases["a"].set()
            yield _result_msg(f"result-{name}")
            await closer

        async def run_a() -> str:
            result = await query(prompt="a")
            releases["b"].set()  # B's closer finishes only after A's drain
            return result.response

        async def run_b() -> str:
            return (await query(prompt="b")).response

        with patch("maestro.sdk._query", side_effect=gen):
            responses = await asyncio.gather(run_a(), run_b())

        assert responses == ["result-a", "result-b"]
        # No retry: a cancelled sibling closer would force B to re-run.
        assert sorted(calls) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_create_session_tags_client_and_connect_children(self) -> None:
        """``create_session`` stamps the client with the owner token that its
        connect-time children carry, and ``disconnect_session`` drains with it."""
        from maestro.sdk import (
            _SDK_CHILD_OWNER,
            _SESSION_CHILD_OWNER_ATTR,
            create_session,
            disconnect_session,
        )

        spawned: list[asyncio.Task[None]] = []
        block = asyncio.Event()

        async def stream_input() -> None:
            await block.wait()

        async def connect(_prompt: Any) -> None:
            spawned.append(asyncio.create_task(stream_input()))

        client = MagicMock()
        client.connect = connect
        client.disconnect = AsyncMock()

        with patch("maestro.sdk.ClaudeSDKClient", return_value=client):
            session = await create_session("hi")

        owner = getattr(session, _SESSION_CHILD_OWNER_ATTR)
        assert spawned[0].get_context().get(_SDK_CHILD_OWNER) is owner
        assert _SDK_CHILD_OWNER.get() is None  # scope reset after connect

        drain = AsyncMock(return_value=(0, 0))
        with patch("maestro.sdk._drain_sdk_child_tasks", drain):
            await disconnect_session(session)
        assert drain.await_args.kwargs["owner"] is owner

        block.set()
        await spawned[0]

    @pytest.mark.asyncio
    async def test_resume_fallback_client_carries_owner(self) -> None:
        """A failed resume falls back to a fresh client, which must carry the
        same owner token its connect-time children do, so
        ``disconnect_session`` still drains them."""
        from maestro.sdk import (
            _SDK_CHILD_OWNER,
            _SESSION_CHILD_OWNER_ATTR,
            create_session,
            disconnect_session,
        )

        spawned: list[asyncio.Task[None]] = []
        block = asyncio.Event()

        async def stream_input() -> None:
            await block.wait()

        async def connect(_prompt: Any) -> None:
            spawned.append(asyncio.create_task(stream_input()))

        resumed = MagicMock()
        resumed.connect = AsyncMock(side_effect=RuntimeError("stale resume"))
        resumed.disconnect = AsyncMock()
        fresh = MagicMock()
        fresh.connect = connect
        fresh.disconnect = AsyncMock()

        with patch("maestro.sdk.ClaudeSDKClient", side_effect=[resumed, fresh]):
            session = await create_session("hi", resume_session_id="old")

        assert session is fresh
        owner = getattr(session, _SESSION_CHILD_OWNER_ATTR)
        assert owner is not None
        assert spawned[0].get_context().get(_SDK_CHILD_OWNER) is owner

        drain = AsyncMock(return_value=(0, 0))
        with patch("maestro.sdk._drain_sdk_child_tasks", drain):
            await disconnect_session(session)
        assert drain.await_args.kwargs["owner"] is owner

        block.set()
        await spawned[0]


class TestDisconnectSession:
    """Pin the contract of ``disconnect_session`` (#1719).

    The helper wraps ``client.disconnect()`` plus the new SDK-child
    drain plus the existing athrow drain, in that order. Ordering is
    load-bearing: ``Query.close()`` cancels child tasks without
    awaiting them, and their cancel-scope unwind can spawn
    ``async_generator_athrow`` tasks for inner generators. Draining
    the child tasks first lets those athrow tasks materialize before
    the second drain scans the loop."""

    @pytest.mark.asyncio
    async def test_runs_child_drain_before_athrow_drain(self) -> None:
        from maestro.sdk import disconnect_session

        client = AsyncMock()
        order: list[str] = []

        async def child_drain(_label: str, **_kw: Any) -> tuple[int, int]:
            order.append("child")
            return (0, 0)

        async def athrow_drain(_label: str) -> tuple[int, int]:
            order.append("athrow")
            return (0, 0)

        async def disconnect() -> None:
            order.append("disconnect")

        client.disconnect = disconnect

        with patch(
            "maestro.sdk._drain_sdk_child_tasks", side_effect=child_drain,
        ), patch(
            "maestro.sdk._drain_athrow_tasks", side_effect=athrow_drain,
        ):
            await disconnect_session(client)

        assert order == ["disconnect", "child", "athrow"]

    @pytest.mark.asyncio
    async def test_swallows_disconnect_exception(self) -> None:
        """``client.disconnect()`` raising must not propagate — the
        helper is called from ``finally`` blocks at every existing
        call site."""
        from maestro.sdk import disconnect_session

        client = AsyncMock()
        client.disconnect = AsyncMock(side_effect=RuntimeError("disconnect failed"))

        with patch(
            "maestro.sdk._drain_sdk_child_tasks", AsyncMock(return_value=(0, 0)),
        ), patch(
            "maestro.sdk._drain_athrow_tasks", AsyncMock(return_value=(0, 0)),
        ):
            await disconnect_session(client)  # must not raise

    @pytest.mark.asyncio
    async def test_drains_run_even_when_disconnect_raises(self) -> None:
        """The drains are the *whole point* of the wrapper — they must
        still run when ``disconnect()`` itself raises. Otherwise an
        exception during disconnect would re-create exactly the orphan
        leak this helper exists to prevent."""
        from maestro.sdk import disconnect_session

        client = AsyncMock()
        client.disconnect = AsyncMock(side_effect=RuntimeError("disconnect failed"))
        child_drain = AsyncMock(return_value=(0, 0))
        athrow_drain = AsyncMock(return_value=(0, 0))

        with patch(
            "maestro.sdk._drain_sdk_child_tasks", child_drain,
        ), patch(
            "maestro.sdk._drain_athrow_tasks", athrow_drain,
        ):
            await disconnect_session(client)

        child_drain.assert_awaited_once()
        athrow_drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_absorbs_cancelled_error_from_child_drain(self) -> None:
        """A residual orphan firing ``CancelledError`` into the SDK-child
        drain itself must be absorbed inside ``disconnect_session``,
        otherwise the helper re-introduces the very leak class it was
        designed to eliminate. The athrow drain must still run after.
        """
        from maestro.sdk import disconnect_session

        client = AsyncMock()
        athrow_drain = AsyncMock(return_value=(0, 0))

        async def child_drain_raises(_label: str, **_kw: Any) -> tuple[int, int]:
            raise asyncio.CancelledError("residual from child drain")

        with patch(
            "maestro.sdk._drain_sdk_child_tasks", side_effect=child_drain_raises,
        ), patch(
            "maestro.sdk._drain_athrow_tasks", athrow_drain,
        ):
            await disconnect_session(client)

        athrow_drain.assert_awaited_once()
        # Caller's task must end with a clean cancellation counter so
        # the next ``await`` checkpoint does not crash.
        task = asyncio.current_task()
        assert task is not None
        assert task.cancelling() == 0

    @pytest.mark.asyncio
    async def test_absorbs_cancelled_error_from_athrow_drain(self) -> None:
        """Symmetric to the SDK-child case: a ``CancelledError`` from the
        athrow drain must also be absorbed."""
        from maestro.sdk import disconnect_session

        client = AsyncMock()
        child_drain = AsyncMock(return_value=(0, 0))

        async def athrow_drain_raises(_label: str) -> tuple[int, int]:
            raise asyncio.CancelledError("residual from athrow drain")

        with patch(
            "maestro.sdk._drain_sdk_child_tasks", child_drain,
        ), patch(
            "maestro.sdk._drain_athrow_tasks", side_effect=athrow_drain_raises,
        ):
            await disconnect_session(client)

        child_drain.assert_awaited_once()
        task = asyncio.current_task()
        assert task is not None
        assert task.cancelling() == 0

    def test_absorb_drain_cancel_no_current_task(self) -> None:
        """``_absorb_drain_cancel`` is called from ``except`` blocks that
        normally run inside a task. When invoked outside any running task
        (a defensive guard for cleanup paths in tests or sync callers),
        it must return cleanly without crashing."""
        from maestro.sdk import _absorb_drain_cancel

        with patch(
            "maestro.sdk.asyncio.current_task", return_value=None,
        ):
            _absorb_drain_cancel("no-task")  # must not raise

    @pytest.mark.asyncio
    async def test_absorbs_stacked_cancels_via_uncancel_loop(self) -> None:
        """When a residual ``CancelledError`` lands with multiple stacked
        ``cancel()`` calls (e.g. anyio cancel-scope storm), the helper
        must ``uncancel()`` until the counter reads zero — a single
        ``uncancel()`` would leave residual cancels to re-fire later."""
        from maestro.sdk import disconnect_session

        client = AsyncMock()
        athrow_drain = AsyncMock(return_value=(0, 0))

        async def child_drain_with_stacked_cancels(
            _label: str, **_kw: Any,
        ) -> tuple[int, int]:
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
            current.cancel()
            current.cancel()
            await asyncio.sleep(0)
            return (0, 0)  # unreachable — cancel fires first

        with patch(
            "maestro.sdk._drain_sdk_child_tasks",
            side_effect=child_drain_with_stacked_cancels,
        ), patch(
            "maestro.sdk._drain_athrow_tasks", athrow_drain,
        ):
            await disconnect_session(client)

        athrow_drain.assert_awaited_once()
        task = asyncio.current_task()
        assert task is not None
        assert task.cancelling() == 0

    @pytest.mark.asyncio
    async def test_preexisting_external_cancel_survives_drain(self) -> None:
        """A cancel already in flight when ``_disconnect_session_impl``
        begins — a real shutdown reaching ``disconnect_session`` from a
        ``finally`` — must survive the drains (#1721). Only the residual
        cancel that arises *during* a drain is absorbed; the pre-existing
        shutdown cancel is preserved so it re-fires at the caller's next
        checkpoint instead of being silently swallowed."""
        from maestro.sdk import _disconnect_session_impl

        client = AsyncMock()

        async def disconnect() -> None:
            return None

        client.disconnect = disconnect
        athrow_drain = AsyncMock(return_value=(0, 0))

        async def child_drain_adds_residual_then_raises(
            _label: str, **_kw: Any,
        ) -> tuple[int, int]:
            # A residual orphan cancel firing into the drain, stacked on
            # top of the external shutdown cancel already pending at entry.
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
            raise asyncio.CancelledError("residual from child drain")

        task = asyncio.current_task()
        assert task is not None
        # Simulate a shutdown cancel already delivered before cleanup ran.
        task.cancel()
        entry_baseline = task.cancelling()
        assert entry_baseline == 1

        with patch(
            "maestro.sdk._drain_sdk_child_tasks",
            side_effect=child_drain_adds_residual_then_raises,
        ), patch(
            "maestro.sdk._drain_athrow_tasks", athrow_drain,
        ):
            await _disconnect_session_impl(client)

        athrow_drain.assert_awaited_once()
        # The residual (stacked) cancel is absorbed, but the pre-existing
        # external cancel is preserved down to the entry baseline — not
        # zeroed as the pre-#1721 unconditional absorb did.
        assert task.cancelling() == entry_baseline
        # The preserved cancel must actually re-fire at the next await
        # checkpoint, proving the shutdown intent survived rather than
        # just the counter. Consume it here so the test task ends clean;
        # uncancelling to zero also clears the pending-cancel flag.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        task.uncancel()

    def test_absorb_drain_cancel_honors_floor(self) -> None:
        """``_absorb_drain_cancel`` stops uncancelling at the supplied
        ``floor`` rather than at zero, so a caller-supplied baseline of
        pending cancels is preserved (#1721). Driven directly with a
        mocked ``current_task``, mirroring
        ``test_absorb_drain_cancel_no_current_task``."""
        from maestro.sdk import _absorb_drain_cancel

        state = {"count": 3}
        fake_task = MagicMock()
        fake_task.cancelling.side_effect = lambda: state["count"]

        def _uncancel() -> int:
            state["count"] -= 1
            return state["count"]

        fake_task.uncancel.side_effect = _uncancel

        with patch(
            "maestro.sdk.asyncio.current_task", return_value=fake_task,
        ):
            _absorb_drain_cancel("floor-test", floor=1)

        # Drained 3 -> 1, stopping at the floor rather than zeroing.
        assert state["count"] == 1
        assert fake_task.uncancel.call_count == 2


class TestLatestContextWindow:
    """``latest_context_window`` delegates read-only to the active provider (062 Step 4)."""

    def test_returns_provider_reading_when_accessor_present(self) -> None:
        """A provider exposing the accessor surfaces its reading verbatim."""
        from maestro.diagnostics import ContextWindowSnapshot
        from maestro.sdk import latest_context_window

        reading = ContextWindowSnapshot(
            used_tokens=1_200, total_tokens=200_000, buckets=(("tools", 1_200),),
        )

        class _Provider:
            def latest_context_window(self) -> ContextWindowSnapshot | None:
                return reading

        with patch(
            "maestro.providers.registry.get_active_provider", return_value=_Provider(),
        ):
            assert latest_context_window() is reading

    def test_returns_none_when_provider_lacks_accessor(self) -> None:
        """The default anthropic-shaped provider has no accessor → ``None``."""
        from maestro.sdk import latest_context_window

        class _Provider:
            pass

        with patch(
            "maestro.providers.registry.get_active_provider", return_value=_Provider(),
        ):
            assert latest_context_window() is None


class TestLatestPlanHeadroom:
    """``latest_plan_headroom`` delegates read-only to the active provider (062 Step 5)."""

    def test_returns_provider_reading_when_accessor_present(self) -> None:
        """A provider exposing the accessor surfaces its reading verbatim."""
        from maestro.cost_tracking import PlanHeadroom
        from maestro.sdk import latest_plan_headroom

        reading = PlanHeadroom(percent_used=45.0, label="Weekly")

        class _Provider:
            def latest_plan_headroom(self) -> PlanHeadroom | None:
                return reading

        with patch(
            "maestro.providers.registry.get_active_provider", return_value=_Provider(),
        ):
            assert latest_plan_headroom() is reading

    def test_returns_none_when_provider_lacks_accessor(self) -> None:
        """The default anthropic-shaped provider has no accessor → ``None``."""
        from maestro.sdk import latest_plan_headroom

        class _Provider:
            pass

        with patch(
            "maestro.providers.registry.get_active_provider", return_value=_Provider(),
        ):
            assert latest_plan_headroom() is None


# ---------------------------------------------------------------------------
# anyio cancel-scope deadline message-format probe
# ---------------------------------------------------------------------------


class TestAnyioDeadlineMessageFormatProbe:
    @pytest.mark.asyncio
    async def test_anyio_deadline_probe_passes_against_current_anyio(self) -> None:
        """Regression canary: the live anyio still stamps ``deadline exceeded``.

        Fires on the next anyio bump that rewords the cancel-scope deadline
        message — failing this test is the loud signal that the heuristic in
        ``query()`` / ``query_on_session()`` is about to silently misclassify
        internal scope deadlines as external cancels.
        """
        await _probe_anyio_deadline_message_format()

    @pytest.mark.asyncio
    async def test_anyio_deadline_probe_raises_on_message_mismatch(self) -> None:
        """If anyio reworded the wording, the probe raises with diagnostics."""

        class _FakeCancelScopeSwallowsCancel:
            def __enter__(self) -> _FakeCancelScopeSwallowsCancel:
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                tb: Any,
            ) -> bool:
                return exc_type is not None and issubclass(
                    exc_type, asyncio.CancelledError,
                )

        async def _fake_sleep_forever_other_wording() -> None:
            raise asyncio.CancelledError(
                "Cancelled via cancel scope deadbeef; reason: timed out",
            )

        with (
            patch(
                "maestro.sdk.anyio.move_on_after",
                return_value=_FakeCancelScopeSwallowsCancel(),
            ),
            patch(
                "maestro.sdk.anyio.sleep_forever",
                new=_fake_sleep_forever_other_wording,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            await _probe_anyio_deadline_message_format()

        msg = str(exc_info.value)
        assert "deadline exceeded" in msg
        assert "anyio=" in msg
        # Reports the actual mismatched wording so the operator can see the
        # rewording at a glance instead of grepping anyio's source.
        assert "reason: timed out" in msg

    @pytest.mark.asyncio
    async def test_anyio_deadline_probe_raises_when_no_cancel_observed(
        self,
    ) -> None:
        """If anyio stopped cancelling on deadline at all, also fail loudly."""

        class _FakeNoCancelScope:
            def __enter__(self) -> _FakeNoCancelScope:
                return self

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                tb: Any,
            ) -> bool:
                return False

        async def _fake_sleep_forever_returns() -> None:
            return None

        with (
            patch(
                "maestro.sdk.anyio.move_on_after",
                return_value=_FakeNoCancelScope(),
            ),
            patch(
                "maestro.sdk.anyio.sleep_forever",
                new=_fake_sleep_forever_returns,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            await _probe_anyio_deadline_message_format()

        msg = str(exc_info.value)
        assert "failed to capture" in msg
        assert "anyio=" in msg
