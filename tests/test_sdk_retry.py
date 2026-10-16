"""query() retry logic, error classification and rate-limit detection; split out
of ``tests/test_sdk.py``.
"""

from __future__ import annotations

import asyncio
import errno
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from maestro import sdk as sdk_module
from maestro.sdk import (
    ErrorCategory,
    QueryOptions,
    SDKError,
    SessionLogger,
    _classify_error,
    _get_warning_throttle_state_for_tests,
    _is_transient,
    _update_warning_throttle_state,
    configure_sdk_transcripts,
    configure_sdk_warning_throttle,
    get_warning_throttle_multiplier,
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
# query() retry logic
# ---------------------------------------------------------------------------


class TestQueryRetry:
    @pytest.mark.asyncio
    async def test_retry_on_transient_error_succeeds(self) -> None:
        """First call fails with transient error, second succeeds."""
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Command failed with exit code 1")
            yield _result_msg("success")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await query(prompt="test")

        assert result.response == "success"
        assert call_count == 2
        mock_sleep.assert_awaited_once_with(1.0)

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises(self) -> None:
        """All attempts fail — SDKError propagates after retries."""
        async def gen(**kwargs: Any) -> Any:
            raise RuntimeError("Command failed with exit code 1")
            yield  # make it an async generator  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.retries_exhausted is True
        assert exc_info.value.retries_attempted == 3
        assert exc_info.value.category == ErrorCategory.CLI_CRASH
        assert "exit code 1" in str(exc_info.value)
        # 3 retries = 3 sleep calls with exponential backoff
        assert mock_sleep.await_count == 3
        mock_sleep.assert_any_await(1.0)
        mock_sleep.assert_any_await(2.0)
        mock_sleep.assert_any_await(4.0)

    @pytest.mark.asyncio
    async def test_retry_exhaustion_logs_giving_up_error(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Exhausting the retry budget emits one error-level giving-up marker.

        The marker fires only when retries are actually spent
        (``attempt == _MAX_RETRIES``); a non-transient error that raises on
        attempt 0 was never retried and must not claim "after 3 retries".
        """
        async def transient_gen(**kwargs: Any) -> Any:
            raise RuntimeError("Command failed with exit code 1")
            yield  # make it an async generator  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=transient_gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.ERROR, logger="maestro"), \
             pytest.raises(SDKError):
            await query(prompt="test")

        assert "SDK query failed after 3 retries" in caplog.text

        # A non-transient error raises on attempt 0 (never retried) — the
        # ``attempt == _MAX_RETRIES`` gate must keep the marker absent.
        caplog.clear()

        async def non_transient_gen(**kwargs: Any) -> Any:
            raise ValueError("bad input")
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=non_transient_gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.ERROR, logger="maestro"), \
             pytest.raises(SDKError):
            await query(prompt="test")

        assert "SDK query failed after 3 retries" not in caplog.text

    @pytest.mark.asyncio
    async def test_no_retry_on_non_transient_error(self) -> None:
        """Non-transient errors wrapped in SDKError immediately without retry."""
        async def gen(**kwargs: Any) -> Any:
            raise ValueError("bad input")
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.retries_exhausted is False
        assert exc_info.value.retries_attempted == 0
        assert exc_info.value.category == ErrorCategory.UNKNOWN
        assert isinstance(exc_info.value.__cause__, ValueError)
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retry_exhausted_preserves_exit_code(self) -> None:
        """SDKError raised after exhausted retries carries the ProcessError's exit_code."""
        from claude_agent_sdk._errors import ProcessError

        async def gen(**kwargs: Any) -> Any:
            raise ProcessError("subprocess died", exit_code=1)
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.retries_exhausted is True
        assert exc_info.value.retries_attempted == 3
        assert exc_info.value.category == ErrorCategory.CLI_CRASH
        assert exc_info.value.exit_code == 1

    @pytest.mark.asyncio
    async def test_non_transient_exit_code_skips_retries(self) -> None:
        """ProcessError with exit_code in the non-transient set raises immediately.

        OOM-killed subprocesses (exit 137) and the other permanent codes must
        not burn retry budget — the loop should crash loudly on the first
        attempt with the structured exit_code preserved.
        """
        from claude_agent_sdk._errors import ProcessError

        async def gen(**kwargs: Any) -> Any:
            raise ProcessError("oom killed", exit_code=137)
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.retries_exhausted is False
        assert exc_info.value.retries_attempted == 0
        # Not CLI_CRASH: that category is retried by the loop, which would
        # undo the skipped retries here one layer up (#3488).
        assert exc_info.value.category == ErrorCategory.UNKNOWN
        assert exc_info.value.exit_code == 137
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_retry_on_auth_error(self) -> None:
        """Auth-related RuntimeError not retried despite containing 'exit code'."""
        async def gen(**kwargs: Any) -> Any:
            raise RuntimeError("authentication failed exit code 1")
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.category == ErrorCategory.AUTH
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_retry_on_cli_not_found_error(self) -> None:
        """CLINotFoundError (missing Claude Code binary) must not burn retries."""
        from claude_agent_sdk._errors import CLINotFoundError

        async def gen(**kwargs: Any) -> Any:
            raise CLINotFoundError()
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.retries_exhausted is False
        assert exc_info.value.retries_attempted == 0
        assert exc_info.value.category == ErrorCategory.UNKNOWN
        assert isinstance(exc_info.value.__cause__, CLINotFoundError)
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_retry_on_message_parse_error(self) -> None:
        """MessageParseError (SDK/CLI protocol mismatch) must not retry."""
        from claude_agent_sdk._errors import MessageParseError

        async def gen(**kwargs: Any) -> Any:
            raise MessageParseError("unknown message type", {"type": "garbage"})
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.retries_attempted == 0
        assert exc_info.value.category == ErrorCategory.UNKNOWN
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connection_error_is_transient(self) -> None:
        """ConnectionError triggers retry."""
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("connection reset")
            yield _result_msg("recovered")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await query(prompt="test")

        assert result.response == "recovered"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_backoff_delays(self) -> None:
        """Verify exact exponential backoff sequence: 1.0, 2.0, 4.0."""
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise RuntimeError("Command failed with exit code 1")
            yield _result_msg("finally")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await query(prompt="test")

        assert result.response == "finally"
        assert mock_sleep.await_count == 3
        delays = [call.args[0] for call in mock_sleep.await_args_list]
        assert delays == [1.0, 2.0, 4.0]

    @pytest.mark.asyncio
    async def test_retry_session_logging(self, mem_fs: dict[str, str]) -> None:
        """Session log contains RETRY entries and error details on transient failure."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_retry")
        sdk_mod.configure_sdk_logging(sdk_dir)

        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Command failed with exit code 1")
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            await query(prompt="test")

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "RETRY 1/3" in content
        assert "exit code 1" in content
        assert "EXCEPTION (attempt 1)" in content
        assert "Query complete" in content

    @pytest.mark.asyncio
    async def test_internal_cancelled_error_is_retried(self) -> None:
        """An internal CancelledError (anyio scope deadline) is converted to SDKError.

        Reproduces the production crash where ``EvolutionEngine.reflect()`` ->
        ``query()`` had a fatal SDK error, the wrapper retried, and then an
        internal anyio cancel scope deadline expired during the retry.  The
        resulting ``CancelledError`` (a ``BaseException``, not ``Exception``)
        escaped the retry loop's ``except Exception:`` clause, propagated
        through ``evolve()`` -> ``run_loop()`` (whose phase handlers also use
        ``except Exception:``), and ultimately exited ``asyncio.run()`` with
        a non-zero status, prompting Docker to restart the container even
        though the graceful ``shutdown()`` had already completed.

        Crucially, anyio cancel scopes drive their own cancellation by
        calling ``task.cancel()`` internally, which bumps the current task's
        ``cancelling()`` counter to > 0 — and can **stack** multiple cancels
        before the CancelledError surfaces.  The 2026-04-08 fix added a
        single ``uncancel()`` call, but 2026-04-09 crashed with the same
        scope pattern and ``remaining=3`` after that single call, proving
        at least four cancels had been stacked.  The containment now drains
        the counter with a ``while current.cancelling() > 0: uncancel()``
        loop; this test exercises that drain by simulating four stacked
        cancels and asserting ``uncancel`` is called four times.
        """
        call_count = 0
        # Mutable stacked-cancel counter driven by ``gen`` (bumps it up) and
        # by ``fake_uncancel`` (decrements it).
        counter = [0]
        first_cancel_raised = [False]

        def fake_cancelling() -> int:
            return counter[0]

        def fake_uncancel() -> int:
            counter[0] = max(0, counter[0] - 1)
            return counter[0]

        fake_task = MagicMock()
        fake_task.cancelling.side_effect = fake_cancelling
        fake_task.uncancel.side_effect = fake_uncancel

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            # First call: normal CLI crash → triggers retry path
            if call_count == 1:
                raise RuntimeError("Command failed with exit code 1")
            # Second call: anyio scope deadline fires inside the SDK's
            # transport close path.  anyio's scope machinery calls
            # ``task.cancel()`` four times before the CancelledError
            # actually surfaces — so the current task's cancelling()
            # counter is at 4 at the moment the exception reaches the
            # containment block.  Subsequent retries also raise a
            # deadline cancel, but with no stacked cancels (counter
            # already drained to 0) so the drain loop is a no-op on
            # those iterations.
            if not first_cancel_raised[0]:
                counter[0] = 4
                first_cancel_raised[0] = True
            raise asyncio.CancelledError(
                "Cancelled via cancel scope 0xdead; reason: deadline exceeded",
            )
            yield  # pragma: no cover  - make this an async generator

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("maestro.sdk.asyncio.current_task", return_value=fake_task), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        # The cancel was internal (an anyio scope deadline, not an outside
        # task.cancel()), so it must NOT escape as CancelledError.  It is
        # wrapped in an SDKError so the calling phase handler's
        # ``except Exception:`` can catch it and the loop can continue.
        assert exc_info.value.category == ErrorCategory.TIMEOUT
        assert "deadline exceeded" in str(exc_info.value).lower() or \
               "cancelled" in str(exc_info.value).lower()
        # The real running task should still be clean — we converted the
        # internal cancel into a regular exception and never propagated it.
        real = asyncio.current_task()
        assert real is not None
        assert real.cancelling() == 0
        # Stacked-cancel drain: the first (and only) stacked cancel had
        # counter==4, so the drain loop must have called ``uncancel()``
        # exactly four times to fully clear the counter.  The pre-2026-04-09
        # implementation called it once and left ``remaining=3`` on the
        # task — any subsequent ``await`` in the same task would then
        # re-deliver one of the still-pending cancels and crash the loop.
        assert fake_task.uncancel.call_count == 4
        # And the stacked counter should be fully drained to 0.
        assert counter[0] == 0

    @pytest.mark.asyncio
    async def test_internal_deadline_with_zero_cancelling_also_wrapped(self) -> None:
        """A synthetic deadline-exceeded cancel with cancelling()==0 is still wrapped.

        Guards the simpler ``cancelling() == 0`` branch of the same check —
        this is the pre-regression path that earlier iterations of the fix
        exercised and we keep it covered so a future refactor cannot
        accidentally collapse the two branches.
        """
        async def gen(**kwargs: Any) -> Any:
            raise asyncio.CancelledError(
                "Cancelled via cancel scope 0xbeef; reason: deadline exceeded",
            )
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")
        assert exc_info.value.category == ErrorCategory.TIMEOUT
        assert asyncio.current_task() is not None
        current = asyncio.current_task()
        assert current is not None
        assert current.cancelling() == 0

    @pytest.mark.asyncio
    async def test_system_exit_propagates(self) -> None:
        """SystemExit (and other non-Exception BaseException) is never swallowed.

        The CancelledError handling in ``query()`` catches ``BaseException``,
        so we have to be careful not to accidentally swallow ``SystemExit``,
        ``KeyboardInterrupt``, or any other non-``Exception`` ``BaseException``
        subclass.  These must always propagate so the process can actually
        exit when something asks it to.
        """
        async def gen(**kwargs: Any) -> Any:
            raise SystemExit(2)
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SystemExit) as exc_info:
            await query(prompt="test")
        assert exc_info.value.code == 2

    @pytest.mark.asyncio
    async def test_external_cancelled_error_propagates(self) -> None:
        """An EXTERNAL cancellation (task.cancel()) still propagates as CancelledError.

        The fix above must NOT swallow real shutdown-driven cancellations:
        when the asyncio task running ``query()`` is cancelled by an outside
        party, that cancel must reach the caller so cooperative shutdown
        actually shuts things down.  We detect "external" via
        ``current_task.cancelling() > 0``.
        """
        started = asyncio.Event()
        blocker = asyncio.Event()  # never set — keeps the SDK gen awaiting

        async def gen(**kwargs: Any) -> Any:
            started.set()
            await blocker.wait()
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen):
            task = asyncio.create_task(query(prompt="test"))
            await started.wait()
            # External cancellation — bumps task.cancelling() to 1 and
            # delivers CancelledError at the next await point inside gen.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_stacked_internal_cancels_are_fully_drained(self) -> None:
        """Drain loop clears any stacked ``task.cancel()`` count, not just one.

        This is the direct regression test for the 2026-04-09 crash: a single
        ``current.uncancel()`` call only decrements the counter by one, so if
        anyio's scope machinery stacked N cancels before the CancelledError
        fired, N-1 of them would survive containment and re-deliver at the
        next await checkpoint.  The fix is a drain loop that calls
        ``uncancel()`` repeatedly until ``cancelling() == 0``.  This test
        pins that behaviour so a future refactor cannot collapse the drain
        loop back to a single call.
        """
        counter = [5]  # five cancels stacked by the time the exception surfaces

        def fake_cancelling() -> int:
            return counter[0]

        def fake_uncancel() -> int:
            counter[0] = max(0, counter[0] - 1)
            return counter[0]

        fake_task = MagicMock()
        fake_task.cancelling.side_effect = fake_cancelling
        fake_task.uncancel.side_effect = fake_uncancel

        async def gen(**kwargs: Any) -> Any:
            raise asyncio.CancelledError(
                "Cancelled via cancel scope 0xbad5; reason: deadline exceeded",
            )
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("maestro.sdk.asyncio.current_task", return_value=fake_task), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.category == ErrorCategory.TIMEOUT
        # The drain loop must fully clear the counter — 5 uncancels on the
        # first cancel, 0 on subsequent retries (counter already at 0).
        assert fake_task.uncancel.call_count == 5
        assert counter[0] == 0

    @pytest.mark.asyncio
    async def test_throttle_sleep_cancellation_is_contained(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A cancel fired during the warning-throttle sleep is contained and retried.

        Before 2026-04-09 the throttle sleep lived *above* the retry loop's
        ``try/except BaseException`` block, so an internal anyio cancel-scope
        deadline firing during ``asyncio.sleep(sleep_s)`` bypassed the
        containment entirely and propagated to the caller as an unhandled
        ``CancelledError`` — crashing the next phase.  The sleep now lives
        inside the inner try block (gated on ``attempt == 0`` so it still
        fires exactly once per ``query()`` call); this test verifies that a
        cancel raised from the throttle sleep is caught by the containment,
        wrapped as a ``TimeoutError``, and **retried** (the contained cancel
        is transient) — the retry succeeds and returns the result.
        """
        import maestro.sdk as sdk_mod

        # Force the throttle state to "active" so the sleep actually fires.
        monkeypatch.setattr(sdk_mod, "_warning_throttle_multiplier", 2)
        async def gen(**kwargs: Any) -> Any:
            yield _result_msg("ok")

        sleep_call_count = 0

        async def sleep_side(*args: Any, **kwargs: Any) -> None:
            nonlocal sleep_call_count
            sleep_call_count += 1
            # First sleep call is the throttle sleep — fire an anyio
            # scope-deadline cancel here.  Subsequent sleep calls
            # (retry backoff) succeed normally.
            if sleep_call_count == 1:
                raise asyncio.CancelledError(
                    "Cancelled via cancel scope 0xaaa; "
                    "reason: deadline exceeded",
                )

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", side_effect=sleep_side), \
             caplog.at_level(logging.WARNING, logger="maestro"):
            result = await query(prompt="test")

        # The contained cancel is transient and retried — the second
        # attempt succeeds.
        assert result.response == "ok"
        # The containment block must have logged the internal-cancel
        # containment warning — proves the cancel was caught by the
        # ``except BaseException`` branch and flowed through the
        # drain-and-wrap logic, not leaked past the retry loop.
        assert "SDK internal-cancel contained" in caplog.text
        # Real running task must remain clean.
        real = asyncio.current_task()
        assert real is not None
        assert real.cancelling() == 0

    @pytest.mark.asyncio
    async def test_throttle_sleep_fires_unconditionally_on_first_attempt(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The warning-throttle sleep fires unconditionally inside its outer guard.

        Regression pin for #1157: the inner ``if not _scope_contaminated:``
        wrapper around the throttle ``await asyncio.sleep(sleep_s)`` was
        structurally unreachable — ``_scope_contaminated`` is only set inside
        the ``except BaseException`` branch, which always increments
        ``attempt`` before re-entering the loop, while the throttle block is
        gated on ``attempt == 0``.  This test pins the post-fix invariant
        that whenever the outer guard (``attempt == 0`` and
        ``_warning_throttle_multiplier > 1`` and not
        ``bypass_warning_throttle``) holds, the throttle sleep fires exactly
        once with the expected duration.
        """
        import maestro.sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "_warning_throttle_multiplier", 2)
        async def gen(**kwargs: Any) -> Any:
            yield _result_msg("ok")

        sleep_durations: list[float] = []

        async def sleep_side(delay: float, *args: Any, **kwargs: Any) -> None:
            sleep_durations.append(delay)

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", side_effect=sleep_side):
            result = await query(prompt="test")

        assert result.response == "ok"
        # Exactly one sleep on a clean first-attempt call: the throttle
        # sleep.  No retry-backoff sleep (no retry happened), no
        # rate-limit sleep.  Duration is base * multiplier = 10 * 2.
        assert sleep_durations == [
            sdk_mod._sdk_warning_throttle_base_seconds * 2,
        ]

    @pytest.mark.asyncio
    async def test_session_throttle_sleep_fires_unconditionally_on_first_attempt(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``query_on_session()`` mirror of #1157's unconditional-throttle pin.

        The session path has the same outer guard
        (``attempt == 0 and _warning_throttle_multiplier > 1``) and the same
        previously-dead ``if not _scope_contaminated:`` inner wrapper.  Pin
        the post-fix invariant on the session path too so a future
        re-introduction of a conditional skip is caught.
        """
        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        monkeypatch.setattr(sdk_mod, "_warning_throttle_multiplier", 2)
        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg("ok")

        mock_client.receive_messages = receive

        sleep_durations: list[float] = []

        async def sleep_side(delay: float, *args: Any, **kwargs: Any) -> None:
            sleep_durations.append(delay)

        with patch("maestro.sdk.asyncio.sleep", side_effect=sleep_side):
            result = await query_on_session(mock_client, "test", caller="t")

        assert result.response == "ok"
        assert sleep_durations == [
            sdk_mod._sdk_warning_throttle_base_seconds * 2,
        ]

    @pytest.mark.asyncio
    async def test_session_throttle_sleep_skipped_when_bypass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``bypass_warning_throttle=True`` skips the session-path throttle sleep.

        The gate-convergence fix loop opts out of the soft per-call warning
        throttle so its must-finish grind is not paced by soft backpressure.
        Even with the same outer guard satisfied (``attempt == 0`` and
        ``_warning_throttle_multiplier > 1``) that the sibling
        ``..._fires_unconditionally...`` test pins as firing, the sleep must
        NOT fire once the bypass is set (proposal 110).
        """
        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        monkeypatch.setattr(sdk_mod, "_warning_throttle_multiplier", 2)
        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg("ok")

        mock_client.receive_messages = receive

        sleep_durations: list[float] = []

        async def sleep_side(delay: float, *args: Any, **kwargs: Any) -> None:
            sleep_durations.append(delay)

        with patch("maestro.sdk.asyncio.sleep", side_effect=sleep_side):
            result = await query_on_session(
                mock_client, "test", caller="t",
                bypass_warning_throttle=True,
            )

        assert result.response == "ok"
        # No throttle sleep on the bypassed call — contrast the sibling
        # ``..._fires_unconditionally...`` test, which sees ``[base * 2]``.
        assert sleep_durations == []

    @pytest.mark.asyncio
    async def test_session_bypass_still_updates_throttle_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bypassed session call still feeds ``_update_warning_throttle_state``.

        Skipping the pre-call sleep does not skip the post-call decay counter:
        ``_finalize_usage`` runs the state transition regardless of the bypass,
        so a bypassed round keeps the throttle easing normally (proposal 110).
        """
        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        monkeypatch.setattr(sdk_mod, "_warning_throttle_multiplier", 2)
        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg("ok")

        mock_client.receive_messages = receive

        update_spy = MagicMock()
        with patch("maestro.sdk.asyncio.sleep", new=AsyncMock()), \
             patch(
                 "maestro.sdk._update_warning_throttle_state", new=update_spy,
             ):
            await query_on_session(
                mock_client, "test", caller="t",
                bypass_warning_throttle=True,
            )

        # Clean call (no ``allowed_warning``) still drives the decay counter.
        update_spy.assert_called_once_with(False, None, None)

    @pytest.mark.asyncio
    async def test_retry_delay_sleep_cancellation_is_contained(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A cancel fired during the retry-delay sleep is contained and retried.

        The retry-delay ``asyncio.sleep(delay)`` must live inside the
        ``try/except BaseException`` containment block so that an internal
        anyio cancel-scope deadline firing during the sleep is caught,
        drained, and wrapped as a ``TimeoutError`` — not leaked as an
        unhandled ``CancelledError``.  This is the same structural fix
        applied to the warning-throttle sleep on 2026-04-09.
        """
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First attempt: transient failure that triggers a retry.
                raise RuntimeError("exit code 1")
            # Subsequent attempts succeed.
            yield _result_msg("ok")

        sleep_call_count = 0

        async def sleep_side(*args: Any, **kwargs: Any) -> None:
            nonlocal sleep_call_count
            sleep_call_count += 1
            # First sleep call is the retry-delay sleep (attempt > 0).
            # Fire an anyio scope-deadline cancel here.
            if sleep_call_count == 1:
                raise asyncio.CancelledError(
                    "Cancelled via cancel scope 0xbbb; "
                    "reason: deadline exceeded",
                )

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", side_effect=sleep_side), \
             caplog.at_level(logging.WARNING, logger="maestro"):
            result = await query(prompt="test")

        # The contained cancel is transient and retried — a later
        # attempt succeeds.
        assert result.response == "ok"
        # Proves the cancel flowed through the containment path.
        assert "SDK internal-cancel contained" in caplog.text
        # Real running task must remain clean.
        real = asyncio.current_task()
        assert real is not None
        assert real.cancelling() == 0

    @pytest.mark.asyncio
    async def test_entry_drain_cleans_dirty_counter(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Entry drain clears a pre-existing dirty cancellation counter.

        Defence-in-depth: if the task enters ``query()`` with a dirty
        ``cancelling()`` counter — e.g. an earlier caller caught a contained
        ``TimeoutError`` without itself calling ``uncancel()`` — the first
        await checkpoint inside ``query()`` would re-deliver a stale
        ``CancelledError``.  ``query()`` now drains the counter at entry
        before any await, logs a warning, and proceeds normally.
        """
        counter = [2]  # two pending cancels inherited from an earlier caller

        def fake_cancelling() -> int:
            return counter[0]

        def fake_uncancel() -> int:
            counter[0] = max(0, counter[0] - 1)
            return counter[0]

        fake_task = MagicMock()
        fake_task.cancelling.side_effect = fake_cancelling
        fake_task.uncancel.side_effect = fake_uncancel

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("maestro.sdk.asyncio.current_task", return_value=fake_task), \
             caplog.at_level(logging.WARNING, logger="maestro"):
            result = await query(prompt="test")

        assert result.response == "ok"
        # Entry drain drained both stacked cancels.
        assert fake_task.uncancel.call_count == 2
        assert counter[0] == 0
        # Entry drain logs a dedicated warning distinct from the in-flight
        # containment log so operators can tell the two sites apart.
        assert "SDK query() entered with dirty cancellation counter" \
            in caplog.text

    @pytest.mark.asyncio
    async def test_contained_internal_cancel_is_retried(self) -> None:
        """A contained internal-cancel TimeoutError is retried by query().

        Reproduces the 2026-04-10 crash where the drain loop successfully
        wrapped the CancelledError as TimeoutError, but _is_transient() did
        not recognise it as retryable, so the error escaped query() and
        crashed the dream phase.
        """
        call_count = 0
        counter = [0]

        def fake_cancelling() -> int:
            return counter[0]

        def fake_uncancel() -> int:
            counter[0] = max(0, counter[0] - 1)
            return counter[0]

        fake_task = MagicMock()
        fake_task.cancelling.side_effect = fake_cancelling
        fake_task.uncancel.side_effect = fake_uncancel

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            # First call: internal cancel → wrapped as retryable TimeoutError
            if call_count == 1:
                counter[0] = 1
                raise asyncio.CancelledError(
                    "Cancelled via cancel scope 0x7faf; reason: deadline exceeded",
                )
            # Second call: success
            yield _result_msg("recovered")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("maestro.sdk.asyncio.current_task", return_value=fake_task):
            result = await query(prompt="test")

        assert result.response == "recovered"
        assert call_count == 2  # first call failed, second succeeded

    @pytest.mark.asyncio
    async def test_scope_contaminated_skips_retry_sleep(self) -> None:
        """After an internal cancel is drained, subsequent retry sleeps are skipped.

        The ``_scope_contaminated`` flag is set when the drain loop fires.
        On the next retry iteration the backoff sleep is skipped to avoid
        giving a residual cancel another await checkpoint to fire on.
        """
        call_count = 0
        counter = [0]

        def fake_cancelling() -> int:
            return counter[0]

        def fake_uncancel() -> int:
            counter[0] = max(0, counter[0] - 1)
            return counter[0]

        fake_task = MagicMock()
        fake_task.cancelling.side_effect = fake_cancelling
        fake_task.uncancel.side_effect = fake_uncancel

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First: internal cancel (triggers drain, sets _scope_contaminated)
                counter[0] = 1
                raise asyncio.CancelledError(
                    "Cancelled via cancel scope 0xccc; reason: deadline exceeded",
                )
            # Subsequent: succeed
            yield _result_msg("ok")

        sleep_mock = AsyncMock()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", sleep_mock), \
             patch("maestro.sdk.asyncio.current_task", return_value=fake_task):
            result = await query(prompt="test")

        assert result.response == "ok"
        # The retry-delay sleep (attempt > 0) should have been SKIPPED
        # because _scope_contaminated was True after the first attempt's drain.
        sleep_mock.assert_not_called()


class TestSessionLoggerInitFailure:
    def test_log_failure_in_init_closes_file(self, mem_fs: dict[str, str]) -> None:
        """SessionLogger.__init__ closes file handle when initial log() raises (lines 90-92)."""
        call_count = 0
        captured_file: Any = None

        def failing_log(self: SessionLogger, message: str) -> None:
            nonlocal call_count, captured_file
            call_count += 1
            captured_file = self._file
            raise OSError("disk full")

        with patch.object(SessionLogger, "log", failing_log), \
             pytest.raises(OSError, match="disk full"):
            SessionLogger(Path("/fake/sdk_init_fail"), caller="test")

        # The file should have been closed in the except block. Observing the
        # captured handle (not just call_count) makes this a real regression
        # guard: dropping self._file.close() from the except-block leaks the
        # descriptor and flips this assertion, whereas call_count alone stays 1.
        assert call_count == 1
        assert captured_file is not None
        assert captured_file.closed is True


class TestProcessContentBlocksSessionLog:
    """Tests for _process_content_blocks with session_log (lines 256, 269, 288)."""

    def test_thinking_block_logged_to_session(self, mem_fs: dict[str, str]) -> None:
        """ThinkingBlock is logged to session_log (line 256)."""
        from maestro.sdk import _process_content_blocks

        sl = SessionLogger(Path("/fake/sdk_think"), caller="test")
        blocks = [ThinkingBlock(thinking="deep analysis here", signature="sig")]
        _process_content_blocks(blocks, [], {}, session_log=sl)
        sl.close()

        written = [k for k in mem_fs if k.startswith("/fake/sdk_think")]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Thinking: deep analysis here" in content

    def test_tool_use_block_logged_to_session(self, mem_fs: dict[str, str]) -> None:
        """ToolUseBlock is logged to session_log (line 269)."""
        from maestro.sdk import _process_content_blocks

        sl = SessionLogger(Path("/fake/sdk_tool"), caller="test")
        blocks = [ToolUseBlock(id="tu1", name="Bash", input={"command": "ls"})]
        _process_content_blocks(blocks, [], {}, session_log=sl)
        sl.close()

        written = [k for k in mem_fs if k.startswith("/fake/sdk_tool")]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Tool call: Bash" in content

    def test_tool_result_block_logged_to_session(self, mem_fs: dict[str, str]) -> None:
        """ToolResultBlock is logged to session_log (line 288)."""
        from maestro.sdk import _process_content_blocks

        sl = SessionLogger(Path("/fake/sdk_result"), caller="test")
        tool_call_by_id: dict[str, Any] = {}
        blocks = [ToolResultBlock(tool_use_id="tu1", content="file.txt")]
        _process_content_blocks(blocks, [], tool_call_by_id, session_log=sl)
        sl.close()

        written = [k for k in mem_fs if k.startswith("/fake/sdk_result")]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Tool result [tu1]:" in content


class TestQueryMessageTypesWithSessionLog:
    """Tests for message type handlers with session_log in query() (lines 365-367, 383, 394, 403)."""

    @pytest.mark.asyncio
    async def test_task_started_logged_to_session(self, mem_fs: dict[str, str]) -> None:
        """TaskStartedMessage is logged to session_log (lines 365-367)."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_task_started")
        sdk_mod.configure_sdk_logging(sdk_dir)

        started = TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="t1",
            description="running something",
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield started
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Task started: running something" in content

    @pytest.mark.asyncio
    async def test_task_progress_logged_to_session(self, mem_fs: dict[str, str]) -> None:
        """TaskProgressMessage is logged to session_log (line 383)."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_task_progress")
        sdk_mod.configure_sdk_logging(sdk_dir)

        progress = TaskProgressMessage(
            subtype="task_progress",
            data={},
            task_id="t1",
            description="working",
            usage={"total_tokens": 100, "tool_uses": 1, "duration_ms": 500},
            uuid="u1",
            session_id="s1",
            last_tool_name="Bash",
        )

        async def gen(**kwargs: Any) -> Any:
            yield progress
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Progress: 100 tokens" in content
        assert "1 tool uses" in content

    @pytest.mark.asyncio
    async def test_task_notification_logged_to_session(self, mem_fs: dict[str, str]) -> None:
        """TaskNotificationMessage is logged to session_log (line 394)."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_task_notif")
        sdk_mod.configure_sdk_logging(sdk_dir)

        notification = TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="t1",
            status="completed",
            output_file="/tmp/out",
            summary="all done",
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield notification
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Task completed: all done" in content

    @pytest.mark.asyncio
    async def test_rate_limit_logged_to_session(self, mem_fs: dict[str, str]) -> None:
        """RateLimitEvent is logged to session_log (line 403)."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_rate_limit")
        sdk_mod.configure_sdk_logging(sdk_dir)

        event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="allowed_warning"),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield event
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "RATE LIMIT: allowed_warning" in content

    @pytest.mark.asyncio
    async def test_rate_limit_detail_logged_to_session(self, mem_fs: dict[str, str]) -> None:
        """The session-log RATE LIMIT line carries the window detail (#4515)."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_rate_limit_detail")
        sdk_mod.configure_sdk_logging(sdk_dir)

        event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                rate_limit_type="seven_day",
                utilization=0.5,
                overage_resets_at=1760009999,
            ),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield event
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        expected = (
            "RATE LIMIT: allowed_warning type=seven_day utilization=0.5"
            " overage_resets_at=1760009999"
        )
        assert expected in content


class TestStderrCollection:
    """Tests for stderr line collection and dump on error."""

    @pytest.mark.asyncio
    async def test_stderr_lines_collected_and_logged_on_error(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When the SDK raises after CLI crash, collected stderr lines are logged."""
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            # Invoke the stderr callback before raising, simulating partial output.
            opts = kwargs.get("options")
            if opts and opts.stderr:
                opts.stderr("error: something went wrong")
                opts.stderr("detail: segfault in output handler")
            call_count += 1
            raise RuntimeError("Command failed with exit code 1")
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.ERROR), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert "exit code 1" in str(exc_info.value)
        # Stderr lines should appear in ERROR-level log.
        assert any("stderr captured (2 lines)" in r.message for r in caplog.records)
        assert any("segfault in output handler" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_stderr_attached_to_exception(self) -> None:
        """Collected stderr is attached to the SDKError's stderr attribute."""
        async def gen(**kwargs: Any) -> Any:
            opts = kwargs.get("options")
            if opts and opts.stderr:
                opts.stderr("fatal: bad config")
            raise RuntimeError("Command failed with exit code 1")
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            try:
                await query(prompt="test")
            except SDKError as exc:
                assert exc.stderr is not None
                assert "fatal: bad config" in exc.stderr
                assert "fatal: bad config" in exc.detail

    @pytest.mark.asyncio
    async def test_stderr_callback_overwrites_placeholder(self) -> None:
        """Callback-captured stderr always overwrites the SDK placeholder."""
        async def gen(**kwargs: Any) -> Any:
            opts = kwargs.get("options")
            if opts and opts.stderr:
                opts.stderr("new info")
            exc = RuntimeError("Command failed with exit code 1")
            # Simulate the SDK's useless placeholder (ProcessError.stderr).
            exc.stderr = "Check stderr output for details"  # type: ignore[attr-defined]
            raise exc
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            try:
                await query(prompt="test")
            except SDKError as sdk_exc:
                # Callback-captured content wins over the placeholder.
                assert sdk_exc.original is not None
                assert sdk_exc.original.stderr == "new info"  # type: ignore[union-attr]
                assert sdk_exc.stderr == "new info"

    @pytest.mark.asyncio
    async def test_stderr_cleared_between_retries(self) -> None:
        """Stderr lines are cleared between retry attempts."""
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            opts = kwargs.get("options")
            if opts and opts.stderr:
                opts.stderr(f"attempt-{call_count}")
            if call_count <= 1:
                raise RuntimeError("Command failed with exit code 1")
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await query(prompt="test")

        assert result.response == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_debug_to_stderr_set_in_query_extra_args(self) -> None:
        """query() forces ``--debug-to-stderr`` so the CLI emits debug output."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        opts = captured_kwargs["options"]
        assert opts.extra_args.get("debug-to-stderr") is None
        assert "debug-to-stderr" in opts.extra_args

    @pytest.mark.asyncio
    async def test_debug_to_stderr_does_not_clobber_caller_value(self) -> None:
        """``setdefault`` must not overwrite a pre-seeded ``extra_args`` value.

        No current caller seeds it, but the invariant matters if one ever does.
        """
        from maestro.sdk import _build_sdk_options as real_build

        captured_kwargs: dict[str, Any] = {}

        def fake_build(options: Any, system: Any) -> Any:
            sdk_opts, caller = real_build(options, system)
            sdk_opts["extra_args"] = {"debug-to-stderr": "custom"}
            return sdk_opts, caller

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._build_sdk_options", side_effect=fake_build), \
             patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        opts = captured_kwargs["options"]
        assert opts.extra_args["debug-to-stderr"] == "custom"

    @pytest.mark.asyncio
    async def test_stderr_callback_logs_at_info_level(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Per-line stderr forwarding is INFO, not WARNING. ``--debug-to-stderr``
        turns every successful call into a routine debug stream; WARNING would
        flood log-based alerting. The crash-time dump stays at ERROR."""
        async def gen(**kwargs: Any) -> Any:
            opts = kwargs.get("options")
            assert opts is not None and opts.stderr is not None
            opts.stderr("routine debug line")
            yield _result_msg("ok", total_cost_usd=0.0)

        with patch("maestro.sdk._query", side_effect=gen), \
             caplog.at_level(logging.DEBUG, logger="maestro"):
            await query(prompt="test")

        stderr_records = [
            r for r in caplog.records
            if "SDK CLI stderr:" in r.message
        ]
        assert stderr_records, "Expected a per-line stderr log record"
        for rec in stderr_records:
            assert rec.levelno == logging.INFO, (
                f"stderr per-line log must be INFO, got "
                f"{logging.getLevelName(rec.levelno)}: {rec.message}"
            )

    @pytest.mark.asyncio
    async def test_empty_stderr_lines_triggers_error_stateless(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A CLI crash with zero callback-captured stderr lines logs an ERROR."""
        async def gen(**kwargs: Any) -> Any:
            # Do NOT invoke the stderr callback — simulate a silent crash.
            raise RuntimeError("Command failed with exit code 1")
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.ERROR, logger="maestro"), \
             pytest.raises(SDKError):
            await query(
                prompt="test",
                options=QueryOptions(caller="test_caller"),
            )

        empty_records = [
            r for r in caplog.records
            if "empty captured stderr" in r.message
        ]
        assert empty_records, "Expected an empty-stderr ERROR log"
        # Check the first attempt's record contains the expected fields.
        first = empty_records[0]
        assert "attempt=1" in first.message
        assert "caller=test_caller" in first.message
        assert "exc_type=RuntimeError" in first.message
        # The session-path marker must NOT appear here.
        assert "(session" not in first.message

    @pytest.mark.asyncio
    async def test_non_empty_stderr_skips_empty_error(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When stderr lines are captured, the empty-state ERROR must not fire."""
        async def gen(**kwargs: Any) -> Any:
            opts = kwargs.get("options")
            if opts and opts.stderr:
                opts.stderr("got something")
            raise RuntimeError("Command failed with exit code 1")
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.ERROR, logger="maestro"), \
             pytest.raises(SDKError):
            await query(prompt="test")

        assert not any(
            "empty captured stderr" in r.message for r in caplog.records
        )


class TestPatchSdkStderrDrain:
    """Tests for the SDK monkey-patch that drains stderr before raising."""

    def test_patch_applied_on_import(self) -> None:
        """The patch replaces _read_messages_impl on SubprocessCLITransport."""
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )
        method = SubprocessCLITransport._read_messages_impl
        assert "patched" in method.__name__

    def test_patch_function_is_callable(self) -> None:
        """_patch_sdk_stderr_drain is importable and callable."""
        from maestro.sdk import _patch_sdk_stderr_drain
        assert callable(_patch_sdk_stderr_drain)

    @pytest.mark.asyncio
    async def test_patched_read_messages_yields_normally(self) -> None:
        """The patched _read_messages_impl yields messages when no error occurs."""
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        transport = object.__new__(SubprocessCLITransport)
        transport._stderr_task = None

        # Store original to restore, and create a simple original that yields.
        async def fake_original(self: Any) -> Any:
            yield {"type": "message", "data": "hello"}
            yield {"type": "message", "data": "world"}

        # Temporarily replace the captured _original with our fake.
        # The patched function calls _original from its closure, so we need
        # to set it on the class and re-patch.
        from maestro.sdk import _patch_sdk_stderr_drain

        saved = SubprocessCLITransport._read_messages_impl
        SubprocessCLITransport._read_messages_impl = fake_original  # type: ignore[assignment,method-assign]
        _patch_sdk_stderr_drain()
        patched_method = SubprocessCLITransport._read_messages_impl

        try:
            messages = []
            async for msg in patched_method(transport):
                messages.append(msg)
            assert len(messages) == 2
            assert messages[0]["data"] == "hello"
        finally:
            SubprocessCLITransport._read_messages_impl = saved  # type: ignore[assignment,method-assign]
            _patch_sdk_stderr_drain()  # Re-apply original patch

    @pytest.mark.asyncio
    async def test_patched_process_error_yields_before_reraise(self) -> None:
        """On ProcessError, the patch awaits asyncio.sleep() so the concurrent
        ``_handle_stderr`` task can drain pipe-buffered stderr via the
        callback before the exception unwinds."""
        from claude_agent_sdk._errors import ProcessError
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        transport = object.__new__(SubprocessCLITransport)
        # Sentinel: a non-None task group is enough to take the sleep branch.
        transport._stderr_task = MagicMock()

        async def fake_raising_original(self: Any) -> Any:
            raise ProcessError(
                "Command failed with exit code 1", exit_code=1, stderr="check",
            )
            yield  # pragma: no cover

        from maestro.sdk import _patch_sdk_stderr_drain

        saved = SubprocessCLITransport._read_messages_impl
        SubprocessCLITransport._read_messages_impl = fake_raising_original  # type: ignore[assignment,method-assign]
        _patch_sdk_stderr_drain()
        patched_method = SubprocessCLITransport._read_messages_impl

        try:
            with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
                 pytest.raises(ProcessError, match="exit code 1"):
                async for _ in patched_method(transport):
                    pass  # pragma: no cover
            mock_sleep.assert_awaited_once_with(0.05)
        finally:
            SubprocessCLITransport._read_messages_impl = saved  # type: ignore[assignment,method-assign]
            _patch_sdk_stderr_drain()

    @pytest.mark.asyncio
    async def test_patched_process_error_does_not_touch_task_group_aexit(self) -> None:
        """The patch must never await the stderr task to completion — it only
        yields via ``asyncio.sleep``. Awaiting the detached ``_stderr_task``
        (or, historically, ``__aexit__`` on the old task group) crossed tasks
        and produced the ``RuntimeError("Attempted to exit cancel scope in a
        different task")`` that masked rate-limit signals in stderr."""
        from claude_agent_sdk._errors import ProcessError
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        transport = object.__new__(SubprocessCLITransport)

        mock_tg = MagicMock()
        mock_tg.wait = AsyncMock(
            side_effect=RuntimeError(
                "Attempted to exit cancel scope in a different task than it "
                "was entered in",
            ),
        )
        transport._stderr_task = mock_tg

        async def fake_raising(self: Any) -> Any:
            raise ProcessError(
                "Command failed with exit code 1", exit_code=1, stderr="x",
            )
            yield  # pragma: no cover

        from maestro.sdk import _patch_sdk_stderr_drain

        saved = SubprocessCLITransport._read_messages_impl
        SubprocessCLITransport._read_messages_impl = fake_raising  # type: ignore[assignment,method-assign]
        _patch_sdk_stderr_drain()
        patched_method = SubprocessCLITransport._read_messages_impl

        try:
            with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
                 pytest.raises(ProcessError, match="exit code 1"):
                async for _ in patched_method(transport):
                    pass  # pragma: no cover
            # The stderr task must never be awaited to completion — only the
            # brief sleep yield runs, so its ``wait()`` is never called.
            mock_tg.wait.assert_not_called()
            # The task reference must remain intact for SDK.close() to find it
            # (close() guards with ``if self._stderr_task is not None`` and
            # cancels it wrapped in ``suppress(Exception)``).
            assert transport._stderr_task is mock_tg
        finally:
            SubprocessCLITransport._read_messages_impl = saved  # type: ignore[assignment,method-assign]
            _patch_sdk_stderr_drain()

    @pytest.mark.asyncio
    async def test_patched_process_error_no_task_group_skips_sleep(self) -> None:
        """When ``_stderr_task`` is None there is nothing to drain, so
        no yield happens and ProcessError propagates immediately."""
        from claude_agent_sdk._errors import ProcessError
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        transport = object.__new__(SubprocessCLITransport)
        transport._stderr_task = None

        async def fake_raising(self: Any) -> Any:
            raise ProcessError(
                "Command failed with exit code 1", exit_code=1, stderr="x",
            )
            yield  # pragma: no cover

        from maestro.sdk import _patch_sdk_stderr_drain

        saved = SubprocessCLITransport._read_messages_impl
        SubprocessCLITransport._read_messages_impl = fake_raising  # type: ignore[assignment,method-assign]
        _patch_sdk_stderr_drain()
        patched_method = SubprocessCLITransport._read_messages_impl

        try:
            with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
                 pytest.raises(ProcessError, match="exit code 1"):
                async for _ in patched_method(transport):
                    pass  # pragma: no cover
            mock_sleep.assert_not_called()
        finally:
            SubprocessCLITransport._read_messages_impl = saved  # type: ignore[assignment,method-assign]
            _patch_sdk_stderr_drain()

    @pytest.mark.asyncio
    async def test_patched_read_messages_reraises_other_exceptions(self) -> None:
        """Non-ProcessError exceptions propagate without stderr drain."""
        from claude_agent_sdk._internal.transport.subprocess_cli import (
            SubprocessCLITransport,
        )

        transport = object.__new__(SubprocessCLITransport)
        transport._stderr_task = MagicMock()

        async def fake_other_error(self: Any) -> Any:
            raise ValueError("something else")
            yield  # pragma: no cover

        from maestro.sdk import _patch_sdk_stderr_drain

        saved = SubprocessCLITransport._read_messages_impl
        SubprocessCLITransport._read_messages_impl = fake_other_error  # type: ignore[assignment,method-assign]
        _patch_sdk_stderr_drain()
        patched_method = SubprocessCLITransport._read_messages_impl

        try:
            with pytest.raises(ValueError, match="something else"):
                async for _ in patched_method(transport):
                    pass  # pragma: no cover
        finally:
            SubprocessCLITransport._read_messages_impl = saved  # type: ignore[assignment,method-assign]
            _patch_sdk_stderr_drain()


class TestRateLimitError:
    """Tests for rate limit handling — SDK now sleeps + retries internally."""

    @pytest.mark.asyncio
    async def test_rejected_sleeps_and_retries(self) -> None:
        """A 'rejected' rate limit event sleeps until reset and retries."""
        rejected_event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="rejected", resets_at=1743890400),
            uuid="u1",
            session_id="s1",
        )

        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield rejected_event
                raise RuntimeError("Command failed with exit code 1")
            yield _result_msg("retry success")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("time.time", return_value=1743890390.0):
            result = await query(prompt="test")

        assert "retry success" in result.response
        # Should have slept exactly once for rate limit (resets_at - now + 10).
        # No additional backoff sleep on the retry iteration.
        assert mock_sleep.await_count == 1

    @pytest.mark.asyncio
    async def test_rejected_exhausts_retries_raises_sdk_error(self) -> None:
        """Rate limit rejection after exhausting retries raises SDKError."""
        rejected_event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="rejected", resets_at=1743890400),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield rejected_event
            raise RuntimeError("Command failed with exit code 1")
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.time", return_value=1743890390.0), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.category == ErrorCategory.RATE_LIMIT
        assert exc_info.value.retries_exhausted

    @pytest.mark.asyncio
    async def test_rejected_exhausts_retries_decodes_bytes_stderr(self) -> None:
        """Rate limit exhaustion decodes bytes stderr on the exception."""
        rejected_event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="rejected", resets_at=1743890400),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield rejected_event
            exc = RuntimeError("Command failed with exit code 1")
            exc.stderr = b"rate limit detail bytes"  # type: ignore[attr-defined]
            raise exc
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.time", return_value=1743890390.0), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.stderr == "rate limit detail bytes"

    @pytest.mark.asyncio
    async def test_rejected_retry_logs_to_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Rate limit retry writes a message to the session log."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_rl_retry")
        sdk_mod.configure_sdk_logging(sdk_dir)

        rejected_event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="rejected", resets_at=1743890400,
            ),
            uuid="u1",
            session_id="s1",
        )

        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield rejected_event
                raise RuntimeError("Command failed with exit code 1")
            yield _result_msg("retry success")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.time", return_value=1743890390.0):
            result = await query(prompt="test")

        assert "retry success" in result.response
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Rate limited" in content
        assert "sleeping" in content

    @pytest.mark.asyncio
    async def test_rate_limit_sleep_skips_backoff_on_next_retry(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """After a rate-limit sleep, the next retry skips exponential backoff."""
        rejected_event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="rejected", resets_at=1743890400),
            uuid="u1",
            session_id="s1",
        )

        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield rejected_event
                raise RuntimeError("Command failed with exit code 1")
            yield _result_msg("ok after rate limit")

        with caplog.at_level(logging.WARNING, logger="maestro"), \
             patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("time.time", return_value=1743890390.0):
            result = await query(prompt="test")

        assert "ok after rate limit" in result.response
        # Exactly one sleep: the rate-limit sleep (20s = resets_at - now + 10).
        # No backoff sleep on retry iteration.
        assert mock_sleep.await_count == 1
        mock_sleep.assert_awaited_once_with(20.0)
        # Below the cap the log claims the sleep reaches the reset — and no
        # truncation warning fires.
        assert "sleeping 20s until reset" in caplog.text
        assert "capped" not in caplog.text

    @pytest.mark.asyncio
    async def test_rate_limit_sleep_capped_with_truncation_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A reset beyond the cap sleeps the cap and warns about truncation."""
        from maestro.sdk import _RATE_LIMIT_SLEEP_CAP_SECONDS

        now = 1743890390.0
        rejected_event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="rejected", resets_at=int(now) + 7200,
            ),
            uuid="u1",
            session_id="s1",
        )

        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield rejected_event
                raise RuntimeError("Command failed with exit code 1")
            yield _result_msg("retry success")

        with caplog.at_level(logging.WARNING, logger="maestro"), \
             patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("time.time", return_value=now):
            result = await query(prompt="test")

        assert "retry success" in result.response
        # The sleep is the cap, not the full resets_at - now + 10 = 7210s.
        mock_sleep.assert_awaited_once_with(_RATE_LIMIT_SLEEP_CAP_SECONDS)
        # The truncation is loud: the WARNING names the true time-to-reset
        # (buffer excluded, matching _log_rate_limit) and the capped sleep,
        # and never claims the sleep reaches the reset.
        assert "reset is 7200s away" in caplog.text
        assert "sleeping capped 1800s" in caplog.text
        assert "until reset" not in caplog.text

    @pytest.mark.asyncio
    async def test_rate_limit_flag_resets_after_one_skip(self) -> None:
        """The _slept_for_rate_limit flag resets so subsequent retries get backoff."""
        rejected_event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="rejected", resets_at=1743890400),
            uuid="u1",
            session_id="s1",
        )

        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Attempt 0: rejected rate limit
                yield rejected_event
                raise RuntimeError("Command failed with exit code 1")
            if call_count == 2:
                # Attempt 1: transient error (no rate limit)
                raise RuntimeError("Command failed with exit code 1")
            # Attempt 2: success
            yield _result_msg("finally ok")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             patch("time.time", return_value=1743890390.0):
            result = await query(prompt="test")

        assert "finally ok" in result.response
        # Sleep 1: rate-limit sleep (20s) after attempt 0.
        # Sleep 2: backoff sleep after attempt 1 (flag reset, so backoff fires).
        assert mock_sleep.await_count == 2
        calls = [c.args[0] for c in mock_sleep.await_args_list]
        assert calls[0] == 20.0  # rate-limit sleep
        assert calls[1] == 2.0   # backoff: 1.0 * 2^(2-1) = 2.0

    @pytest.mark.asyncio
    async def test_warning_triggers_state_machine_first_sighting(self) -> None:
        """First 'allowed_warning' observed jumps the multiplier 1 -> 2."""
        assert _get_warning_throttle_state_for_tests() == (1, 0, 0)

        warning_event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="allowed_warning"),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield warning_event
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        multiplier, clean, _ = _get_warning_throttle_state_for_tests()
        assert multiplier == 2
        assert clean == 0


class TestRateLimitStderrDetection:
    """Tests for rate-limit detection from stderr keywords when no
    RateLimitEvent is streamed (CLI crashes before emitting it)."""

    @pytest.mark.asyncio
    async def test_rate_limit_stderr_uses_long_backoff(self) -> None:
        """CLI exit-code-1 with 'rate limit' in stderr uses 60s base delay."""
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                exc = RuntimeError("Command failed with exit code 1")
                exc.stderr = "Error: rate limit exceeded"  # type: ignore[attr-defined]
                raise exc
            yield _result_msg("ok after rate limit")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await query(prompt="test")

        assert "ok after rate limit" in result.response
        assert mock_sleep.await_count == 1
        mock_sleep.assert_awaited_once_with(60.0)

    @pytest.mark.asyncio
    async def test_rate_limit_stderr_429_keyword(self) -> None:
        """CLI exit-code-1 with '429' in stderr triggers rate-limit path."""
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                exc = RuntimeError("Command failed with exit code 1")
                exc.stderr = "HTTP 429 Too Many Requests"  # type: ignore[attr-defined]
                raise exc
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await query(prompt="test")

        assert "ok" in result.response
        mock_sleep.assert_awaited_once_with(60.0)

    @pytest.mark.asyncio
    async def test_rate_limit_stderr_exhausts_retries(self) -> None:
        """All 6 attempts fail with rate-limit stderr — raises SDKError."""

        async def gen(**kwargs: Any) -> Any:
            exc = RuntimeError("Command failed with exit code 1")
            exc.stderr = "rate limit exceeded"  # type: ignore[attr-defined]
            raise exc
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.category == ErrorCategory.RATE_LIMIT
        assert exc_info.value.retries_exhausted
        assert exc_info.value.retries_attempted == 5

    @pytest.mark.asyncio
    async def test_rate_limit_stderr_exhausts_retries_bytes_stderr(self) -> None:
        """Bytes stderr is decoded when rate-limit stderr retries exhaust."""

        async def gen(**kwargs: Any) -> Any:
            exc = RuntimeError("Command failed with exit code 1")
            exc.stderr = b"rate limit exceeded"  # type: ignore[attr-defined]
            raise exc
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert exc_info.value.category == ErrorCategory.RATE_LIMIT
        assert exc_info.value.stderr == "rate limit exceeded"

    @pytest.mark.asyncio
    async def test_rate_limit_stderr_retry_logs_to_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Rate-limit stderr retry writes to the session log."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_stderr_rl")
        sdk_mod.configure_sdk_logging(sdk_dir)

        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                exc = RuntimeError("Command failed with exit code 1")
                exc.stderr = "rate limit exceeded"  # type: ignore[attr-defined]
                raise exc
            yield _result_msg("ok after stderr rate limit")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            result = await query(prompt="test")

        assert "ok after stderr rate limit" in result.response
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Rate limited (stderr)" in content
        assert "sleeping" in content

    @pytest.mark.asyncio
    async def test_rate_limit_stderr_backoff_sequence(self) -> None:
        """Verify the exact sleep sequence: 60, 120, 240, 300, 300."""

        async def gen(**kwargs: Any) -> Any:
            exc = RuntimeError("Command failed with exit code 1")
            exc.stderr = "rate limit"  # type: ignore[attr-defined]
            raise exc
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             pytest.raises(SDKError):
            await query(prompt="test")

        delays = [call.args[0] for call in mock_sleep.await_args_list]
        assert delays == [60.0, 120.0, 240.0, 300.0, 300.0]

    @pytest.mark.asyncio
    async def test_rate_limit_stderr_skips_next_generic_backoff(self) -> None:
        """After rate-limit stderr sleep, the generic backoff is skipped."""
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                exc = RuntimeError("Command failed with exit code 1")
                exc.stderr = "rate limit"  # type: ignore[attr-defined]
                raise exc
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await query(prompt="test")

        assert "ok" in result.response
        delays = [call.args[0] for call in mock_sleep.await_args_list]
        # Two rate-limit sleeps (60s, 120s), NO generic backoff (1s, 2s)
        assert delays == [60.0, 120.0]

    @pytest.mark.asyncio
    async def test_exit_code_without_rate_limit_uses_short_backoff(self) -> None:
        """CLI exit-code-1 without rate-limit keywords uses generic 1s/2s/4s."""
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise RuntimeError("Command failed with exit code 1")
            yield _result_msg("recovered")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await query(prompt="test")

        assert "recovered" in result.response
        mock_sleep.assert_awaited_once_with(1.0)

    @pytest.mark.asyncio
    async def test_rate_limit_in_message_not_stderr(self) -> None:
        """Error message containing 'rate limit' (no stderr) uses long path."""
        call_count = 0

        async def gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise RuntimeError("rate limit: exit code 1")
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await query(prompt="test")

        assert "ok" in result.response
        mock_sleep.assert_awaited_once_with(60.0)


class TestWarningThrottleStateMachine:
    """Unit tests for the per-call ``allowed_warning`` throttle state machine.

    These test the pure state transitions directly via
    ``_update_warning_throttle_state`` to avoid the overhead of driving a
    full mock query per iteration.
    """

    def setup_method(self) -> None:
        configure_sdk_warning_throttle(base_seconds=10, hysteresis=3)

    def test_first_sighting_jumps_to_two(self) -> None:
        """The first warning must move the multiplier 1 -> 2 (not stay at 1)."""
        _update_warning_throttle_state(saw_warning=True)
        assert _get_warning_throttle_state_for_tests() == (2, 0, 0)

    def test_successive_warnings_escalate(self) -> None:
        """Consecutive warnings double the multiplier: 1 -> 2 -> 4 -> 8 -> 16."""
        expected = [2, 4, 8, 16]
        for e in expected:
            _update_warning_throttle_state(saw_warning=True)
            multiplier, clean, _ = _get_warning_throttle_state_for_tests()
            assert multiplier == e
            assert clean == 0

    def test_multiplier_capped_at_max(self) -> None:
        """Multiplier never exceeds ``_WARNING_THROTTLE_MAX_MULTIPLIER`` (16)."""
        for _ in range(10):
            _update_warning_throttle_state(saw_warning=True)
        assert _get_warning_throttle_state_for_tests() == (16, 0, 7)

    def test_hysteresis_delays_decay(self) -> None:
        """Multiplier does not decay until ``hysteresis`` clean calls have passed."""
        # Escalate to 4.
        _update_warning_throttle_state(saw_warning=True)
        _update_warning_throttle_state(saw_warning=True)
        assert _get_warning_throttle_state_for_tests() == (4, 0, 0)

        # First two clean calls: counter increments but no decay.
        _update_warning_throttle_state(saw_warning=False)
        assert _get_warning_throttle_state_for_tests() == (4, 1, 0)
        _update_warning_throttle_state(saw_warning=False)
        assert _get_warning_throttle_state_for_tests() == (4, 2, 0)

        # Third clean call: decay fires, counter resets.
        _update_warning_throttle_state(saw_warning=False)
        assert _get_warning_throttle_state_for_tests() == (2, 0, 0)

    def test_clean_counter_resets_on_new_warning(self) -> None:
        """Accumulated clean calls are discarded when a warning re-fires."""
        _update_warning_throttle_state(saw_warning=True)  # 2
        _update_warning_throttle_state(saw_warning=False)  # clean=1
        _update_warning_throttle_state(saw_warning=False)  # clean=2
        assert _get_warning_throttle_state_for_tests() == (2, 2, 0)

        # New warning wipes the clean counter and doubles the multiplier.
        _update_warning_throttle_state(saw_warning=True)
        assert _get_warning_throttle_state_for_tests() == (4, 0, 0)

    def test_clean_call_at_multiplier_one_is_noop(self) -> None:
        """No state change when the multiplier is already at baseline."""
        _update_warning_throttle_state(saw_warning=False)
        assert _get_warning_throttle_state_for_tests() == (1, 0, 0)

    def test_full_decay_to_one(self) -> None:
        """Multiplier decays all the way back to 1 given enough clean calls."""
        _update_warning_throttle_state(saw_warning=True)  # 2
        # 3 clean calls -> decay to 1
        for _ in range(3):
            _update_warning_throttle_state(saw_warning=False)
        assert _get_warning_throttle_state_for_tests() == (1, 0, 0)
        # Further clean calls stay at 1 (no underflow)
        _update_warning_throttle_state(saw_warning=False)
        assert _get_warning_throttle_state_for_tests() == (1, 0, 0)

    def test_configure_hysteresis(self) -> None:
        """``configure_sdk_warning_throttle`` takes effect on the next update."""
        configure_sdk_warning_throttle(base_seconds=10, hysteresis=1)
        _update_warning_throttle_state(saw_warning=True)  # 2
        # hysteresis=1 decays on the first clean call
        _update_warning_throttle_state(saw_warning=False)
        assert _get_warning_throttle_state_for_tests() == (1, 0, 0)


class TestGetWarningThrottleMultiplier:
    """Tests for the public ``get_warning_throttle_multiplier`` getter."""

    def test_returns_one_after_reset(self) -> None:
        """Baseline multiplier is 1 (no throttle active)."""
        assert get_warning_throttle_multiplier() == 1

    def test_returns_two_after_one_warning(self) -> None:
        """First warning escalates the multiplier to 2."""
        _update_warning_throttle_state(saw_warning=True)
        assert get_warning_throttle_multiplier() == 2

    def test_returns_escalated_value_after_multiple_warnings(self) -> None:
        """Successive warnings double the multiplier each time."""
        for expected in (2, 4, 8, 16):
            _update_warning_throttle_state(saw_warning=True)
            assert get_warning_throttle_multiplier() == expected


class TestWarningThrottleQueryIntegration:
    """Tests for the query() integration of the per-call throttle."""

    def setup_method(self) -> None:
        configure_sdk_warning_throttle(base_seconds=10, hysteresis=3)

    @pytest.mark.asyncio
    async def test_query_does_not_sleep_when_multiplier_is_one(self) -> None:
        """query() skips the throttle sleep entirely at the baseline multiplier."""
        async def gen(**kwargs: Any) -> Any:
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await query(prompt="test")

        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_query_sleeps_base_times_multiplier(self) -> None:
        """query() sleeps base_seconds * multiplier before calling the SDK."""
        # Prime the state machine to multiplier=4.
        _update_warning_throttle_state(saw_warning=True)
        _update_warning_throttle_state(saw_warning=True)
        assert _get_warning_throttle_state_for_tests()[:2] == (4, 0)

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await query(prompt="test")

        # 10s base * 4x multiplier = 40s
        mock_sleep.assert_awaited_once_with(40)

    @pytest.mark.asyncio
    async def test_query_throttle_sleep_capped_at_max(self) -> None:
        """query() throttle sleep is capped at _WARNING_THROTTLE_MAX_SLEEP_SECONDS."""
        # Configure a huge base so base*multiplier > cap.
        configure_sdk_warning_throttle(base_seconds=500, hysteresis=3)
        # Prime to multiplier=16.
        for _ in range(4):
            _update_warning_throttle_state(saw_warning=True)
        assert _get_warning_throttle_state_for_tests()[:2] == (16, 0)

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await query(prompt="test")

        # 500 * 16 = 8000, capped at 1800.
        mock_sleep.assert_awaited_once_with(1800)

    @pytest.mark.asyncio
    async def test_query_updates_state_on_warning(self) -> None:
        """A query() that observes allowed_warning escalates the state machine."""
        warning_event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="allowed_warning"),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield warning_event
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock):
            await query(prompt="test")

        assert _get_warning_throttle_state_for_tests()[:2] == (2, 0)

    @pytest.mark.asyncio
    async def test_query_updates_state_on_clean_call(self) -> None:
        """A clean query() increments the hysteresis counter when multiplier > 1."""
        # Prime multiplier to 2.
        _update_warning_throttle_state(saw_warning=True)

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock):
            await query(prompt="test")

        # One clean call bumps the counter but does not yet decay.
        assert _get_warning_throttle_state_for_tests()[:2] == (2, 1)

    @pytest.mark.asyncio
    async def test_query_throttle_sleep_fires_once_per_query(self) -> None:
        """Throttle sleep is not re-applied across internal retries.

        Retries inside the query() happen via the internal retry loop; the
        throttle pre-sleep must fire once per outer call, not per retry,
        otherwise a transient SDK crash would compound throttle delays.
        """
        _update_warning_throttle_state(saw_warning=True)  # multiplier=2

        call_count = 0

        async def flaky_gen(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Command failed with exit code 1")
                yield  # pragma: no cover
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=flaky_gen), \
             patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await query(prompt="test")

        # Two sleeps total: the throttle pre-sleep (20s = 10 * 2) and the
        # retry backoff (_RETRY_BASE_DELAY * 1 = 1.0s).  Only ONE of them
        # is the throttle sleep — the other is the retry delay.
        sleep_args = [c.args[0] for c in mock_sleep.await_args_list]
        assert 20 in sleep_args  # throttle sleep (base=10 * multiplier=2)
        assert sleep_args.count(20) == 1  # throttle fires exactly once
        # The retry backoff is approximately _RETRY_BASE_DELAY=1.0
        assert any(
            isinstance(a, float) and a < 5.0 for a in sleep_args
        ), sleep_args

    @pytest.mark.asyncio
    async def test_query_bypasses_throttle_sleep_when_flag_set(self) -> None:
        """``bypass_warning_throttle=True`` skips the per-call throttle sleep.

        Pins the probe-bypass contract (#1203): a probe that opts in must
        not pay the ``_warning_throttle_multiplier`` wait it exists to
        detect. The state machine still observes the call (clean call
        ticks the hysteresis counter), but no ``asyncio.sleep`` fires
        before the SDK call.
        """
        # Prime multiplier to 4 — without the bypass this would force a
        # 40s pre-call sleep.
        _update_warning_throttle_state(saw_warning=True)
        _update_warning_throttle_state(saw_warning=True)
        assert _get_warning_throttle_state_for_tests()[:2] == (4, 0)

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await query(
                prompt="test",
                options=QueryOptions(
                    caller="warning_hold_probe",
                    bypass_warning_throttle=True,
                ),
            )

        mock_sleep.assert_not_awaited()
        # The state machine still observes the clean call — that is what
        # eventually clears the hold gate.
        assert _get_warning_throttle_state_for_tests()[:2] == (4, 1)

    @pytest.mark.asyncio
    async def test_query_default_does_not_bypass_throttle_sleep(self) -> None:
        """The default (no flag) still pays the throttle sleep.

        Pins the existing non-probe behaviour so the bypass flag only
        affects callers that explicitly opt in.
        """
        _update_warning_throttle_state(saw_warning=True)
        _update_warning_throttle_state(saw_warning=True)
        assert _get_warning_throttle_state_for_tests()[:2] == (4, 0)

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await query(
                prompt="test",
                options=QueryOptions(caller="non_probe"),
            )

        mock_sleep.assert_awaited_once_with(40)

    @pytest.mark.asyncio
    async def test_query_throttle_sleep_logs_to_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        """When a session log is active, the throttle pre-sleep is
        recorded in it so operators can correlate the sleep with the
        specific SDK session file."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_throttle_log")
        sdk_mod.configure_sdk_logging(sdk_dir)

        # Prime multiplier to 2 so the throttle pre-sleep path fires.
        _update_warning_throttle_state(saw_warning=True)

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock):
            await query(
                prompt="test",
                options=QueryOptions(caller="test_caller"),
            )

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Warning throttle: x2" in content
        assert "sleep=20s" in content
        assert "caller=test_caller" in content

    @pytest.mark.asyncio
    async def test_permission_mode_set(self) -> None:
        """The SDK query sets permission_mode='bypassPermissions'."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        opts = captured_kwargs.get("options")
        assert opts is not None
        assert opts.permission_mode == "bypassPermissions"

    def test_rate_limit_classified_correctly(self) -> None:
        """Rate limit errors are classified as RATE_LIMIT category."""
        exc = RuntimeError("429 too many requests")
        assert _classify_error(exc) == ErrorCategory.RATE_LIMIT


class TestSDKError:
    """Tests for the SDKError exception class."""

    def test_is_exception_subclass(self) -> None:
        """SDKError is catchable as Exception for backward compatibility."""
        err = SDKError("boom")
        assert isinstance(err, Exception)

    def test_message_preserved(self) -> None:
        err = SDKError("something failed")
        assert str(err) == "something failed"

    def test_detail_without_stderr(self) -> None:
        err = SDKError("cli crashed", category=ErrorCategory.CLI_CRASH)
        assert err.detail == "cli crashed"

    def test_detail_with_stderr(self) -> None:
        err = SDKError("cli crashed", stderr="segfault at 0x0")
        assert "cli crashed" in err.detail
        assert "stderr: segfault at 0x0" in err.detail

    def test_detail_with_retries_exhausted(self) -> None:
        err = SDKError(
            "failed", retries_attempted=3, retries_exhausted=True,
        )
        assert "(retries exhausted: 3 attempts)" in err.detail

    def test_detail_full(self) -> None:
        err = SDKError(
            "exit code 1",
            stderr="fatal error",
            retries_attempted=3,
            retries_exhausted=True,
        )
        detail = err.detail
        assert "exit code 1" in detail
        assert "stderr: fatal error" in detail
        assert "(retries exhausted: 3 attempts)" in detail

    def test_original_exception_preserved(self) -> None:
        orig = RuntimeError("original")
        err = SDKError("wrapped", original=orig)
        assert err.original is orig

    def test_defaults(self) -> None:
        err = SDKError("msg")
        assert err.original is None
        assert err.stderr is None
        assert err.category == ErrorCategory.UNKNOWN
        assert err.retries_attempted == 0
        assert err.retries_exhausted is False
        assert err.exit_code is None

    def test_exit_code_preserved(self) -> None:
        err = SDKError("cli crashed", exit_code=137)
        assert err.exit_code == 137

    def test_detail_includes_exit_code(self) -> None:
        err = SDKError("cli crashed", exit_code=1, stderr="segfault")
        detail = err.detail
        assert "exit_code: 1" in detail
        assert "stderr: segfault" in detail

    def test_detail_omits_exit_code_when_none(self) -> None:
        err = SDKError("cli crashed", stderr="segfault")
        assert "exit_code" not in err.detail


class TestClassifyError:
    """Tests for _classify_error."""

    @pytest.mark.parametrize("code", sorted(sdk_module._NON_TRANSIENT_EXIT_CODES))
    def test_non_transient_exit_codes_stay_out_of_cli_crash(self, code: int) -> None:
        """A permanent exit code must not be reported as a retryable CLI_CRASH."""
        from claude_agent_sdk._errors import ProcessError

        from maestro.agent import _TRANSIENT_SDK_CATEGORIES

        exc = ProcessError("cli failed", exit_code=code)
        category = _classify_error(exc)
        assert category is ErrorCategory.UNKNOWN
        assert category not in _TRANSIENT_SDK_CATEGORIES

    def test_transient_exit_code_still_cli_crash(self) -> None:
        """A generic subprocess failure keeps its retryable CLI_CRASH category."""
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError("cli failed", exit_code=1)
        assert _classify_error(exc) is ErrorCategory.CLI_CRASH

    @pytest.mark.parametrize("code", [*sorted(sdk_module._NON_TRANSIENT_EXIT_CODES), 1])
    def test_classification_agrees_with_transience(self, code: int) -> None:
        """Exit-code-only failures agree on retry eligibility and category (#3488).

        Scoped to exceptions carrying no content signal: the keyword arms of
        ``_classify_error`` run *before* the exit-code arm by design, so a
        message or stderr naming a transient fault deliberately outranks the
        code (pinned by ``test_content_signal_outranks_permanent_exit_code``).
        """
        from claude_agent_sdk._errors import ProcessError

        from maestro.agent import _TRANSIENT_SDK_CATEGORIES

        exc = ProcessError("cli failed", exit_code=code)
        transient = _is_transient(exc)
        categorized = _classify_error(exc) in _TRANSIENT_SDK_CATEGORIES
        assert transient == categorized

    def test_content_signal_outranks_permanent_exit_code(self) -> None:
        """A rate-limit stderr still classifies RATE_LIMIT despite exit code 137.

        The content arms precede the exit-code arm deliberately, so the
        agreement above is not a global invariant: ``_is_transient`` reads only
        ``str(exc)`` and returns False here, while the category stays transient.
        """
        from claude_agent_sdk._errors import ProcessError

        from maestro.agent import _TRANSIENT_SDK_CATEGORIES

        exc = ProcessError("cli failed", exit_code=137, stderr="rate limit exceeded")
        assert _classify_error(exc) is ErrorCategory.RATE_LIMIT
        assert _classify_error(exc) in _TRANSIENT_SDK_CATEGORIES
        assert _is_transient(exc) is False

    def test_environment_fault_matches_the_permanent_exit_codes(self) -> None:
        """``is_environment_fault`` is True for exactly the permanent exit codes."""
        from maestro.sdk import SDKError, is_environment_fault

        for code in sorted(sdk_module._NON_TRANSIENT_EXIT_CODES):
            assert is_environment_fault(SDKError("boom", exit_code=code)) is True
        assert is_environment_fault(SDKError("boom", exit_code=1)) is False
        assert is_environment_fault(SDKError("boom")) is False
        # A bare exception carrying the code is not an SDKError — the demote arm
        # only ever sees the wrapped form.
        assert is_environment_fault(RuntimeError("boom")) is False

    def test_auth_invalid_api_key(self) -> None:
        assert _classify_error(RuntimeError("Invalid API key")) == ErrorCategory.AUTH

    def test_auth_unauthorized(self) -> None:
        assert _classify_error(RuntimeError("unauthorized")) == ErrorCategory.AUTH

    def test_auth_authentication_failed(self) -> None:
        assert _classify_error(RuntimeError("authentication failed")) == ErrorCategory.AUTH

    def test_auth_permission_denied(self) -> None:
        assert _classify_error(RuntimeError("permission denied")) == ErrorCategory.AUTH

    class _StderrError(RuntimeError):
        def __init__(self, stderr: str | bytes) -> None:
            super().__init__("boom")
            self.stderr = stderr

    def _with_stderr(self, stderr: str | bytes) -> Exception:
        return self._StderrError(stderr)

    _DENY = "2026-09-29T12:00:00.000Z [DEBUG] Bash tool permission denied"

    def test_debug_deny_line_does_not_mask_rate_limit(self) -> None:
        exc = self._with_stderr(
            f"{self._DENY}\n"
            'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}'
        )
        assert _classify_error(exc) == ErrorCategory.RATE_LIMIT

    def test_debug_deny_line_does_not_mask_cli_crash(self) -> None:
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError("Command failed", exit_code=1, stderr=self._DENY)
        assert _classify_error(exc) == ErrorCategory.CLI_CRASH

    _EXEC_TIMEOUT = (
        "2026-04-19T00:36:02.030Z [DEBUG] Creating snapshot at: /x/snapshot-bash.sh\n"
        "2026-04-19T00:36:02.030Z [DEBUG] Execution timeout: 10000ms"
    )

    def test_debug_execution_timeout_line_does_not_mask_cli_crash(self) -> None:
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError(
            "Command failed with exit code 1", exit_code=1, stderr=self._EXEC_TIMEOUT,
        )
        assert _classify_error(exc) == ErrorCategory.CLI_CRASH

    def test_debug_network_keyword_line_does_not_mask_cli_crash(self) -> None:
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError(
            "Command failed with exit code 1", exit_code=1,
            stderr="2026-04-19T00:36:02.030Z [DEBUG] retrying after socket hang up",
        )
        assert _classify_error(exc) == ErrorCategory.CLI_CRASH

    def test_non_debug_timeout_stderr_still_timeout(self) -> None:
        for line in ("connect ETIMEDOUT 1.2.3.4:443", "request timed out"):
            exc = self._with_stderr(f"{self._EXEC_TIMEOUT}\n{line}")
            assert _classify_error(exc) == ErrorCategory.TIMEOUT

    def test_non_debug_timeout_bytes_stderr_still_timeout(self) -> None:
        exc = self._with_stderr(f"{self._EXEC_TIMEOUT}\nrequest timed out".encode())
        assert _classify_error(exc) == ErrorCategory.TIMEOUT

    def test_debug_timeout_bytes_stderr_is_not_timeout(self) -> None:
        exc = self._with_stderr(self._EXEC_TIMEOUT.encode())
        assert _classify_error(exc) == ErrorCategory.UNKNOWN

    def test_timeout_in_message_still_timeout(self) -> None:
        exc = self._StderrError(self._EXEC_TIMEOUT)
        exc.args = ("deadline exceeded",)
        assert _classify_error(exc) == ErrorCategory.TIMEOUT

    def test_debug_logged_rate_limit_still_rate_limit(self) -> None:
        exc = self._with_stderr(
            f"{self._EXEC_TIMEOUT}\n2026-04-19T00:36:03.000Z [DEBUG] API error 429\n",
        )
        assert _classify_error(exc) == ErrorCategory.RATE_LIMIT

    def test_debug_deny_line_alone_is_not_auth(self) -> None:
        exc = self._with_stderr("[DEBUG] Bash tool permission denied\n")
        assert _classify_error(exc) == ErrorCategory.UNKNOWN

    def test_non_debug_tool_deny_line_is_not_auth(self) -> None:
        exc = self._with_stderr("Bash tool permission denied")
        assert _classify_error(exc) == ErrorCategory.UNKNOWN

    def test_non_debug_auth_stderr_still_auth(self) -> None:
        for line in ("Invalid API key", "authentication failed", "401 Unauthorized"):
            exc = self._with_stderr(f"{self._DENY}\n{line}".encode())
            assert _classify_error(exc) == ErrorCategory.AUTH

    def test_debug_logged_auth_failure_still_auth(self) -> None:
        exc = self._with_stderr("2026-09-29T12:00:00.000Z [DEBUG] 401 Unauthorized")
        assert _classify_error(exc) == ErrorCategory.AUTH

    def test_rate_limit_keyword(self) -> None:
        assert _classify_error(RuntimeError("rate limit exceeded")) == ErrorCategory.RATE_LIMIT

    def test_rate_limit_429(self) -> None:
        assert _classify_error(RuntimeError("HTTP 429")) == ErrorCategory.RATE_LIMIT

    def test_429_embedded_in_digit_run_not_rate_limit(self) -> None:
        """429 inside a larger digit run (byte count) is not an HTTP status."""
        exc = RuntimeError("wrote 4290 bytes to artifact store")
        assert _classify_error(exc) == ErrorCategory.UNKNOWN

    def test_429_embedded_in_identifier_not_rate_limit(self) -> None:
        """429 inside an identifier is not an HTTP status."""
        exc = RuntimeError("cache key task-14293 not found")
        assert _classify_error(exc) == ErrorCategory.UNKNOWN

    def test_429_embedded_in_stderr_digit_run_not_rate_limit(self) -> None:
        """429 inside a port number in stderr is not a rate limit."""
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError(
            "subprocess died", exit_code=1, stderr="listening on 127.0.0.1:84291"
        )
        assert _classify_error(exc) == ErrorCategory.CLI_CRASH

    def test_rate_limit_too_many_requests(self) -> None:
        assert _classify_error(RuntimeError("too many requests")) == ErrorCategory.RATE_LIMIT

    def test_timeout(self) -> None:
        assert _classify_error(RuntimeError("request timeout")) == ErrorCategory.TIMEOUT

    def test_timed_out(self) -> None:
        assert _classify_error(RuntimeError("connection timed out")) == ErrorCategory.TIMEOUT

    def test_deadline_exceeded(self) -> None:
        assert _classify_error(RuntimeError("deadline exceeded")) == ErrorCategory.TIMEOUT

    def test_connection_error(self) -> None:
        assert _classify_error(ConnectionError("reset")) == ErrorCategory.NETWORK

    def test_connection_reset_error(self) -> None:
        """ConnectionResetError (subclass of ConnectionError) is NETWORK."""
        assert _classify_error(ConnectionResetError("reset")) == ErrorCategory.NETWORK

    def test_broken_pipe_error(self) -> None:
        """BrokenPipeError (subclass of ConnectionError) is NETWORK."""
        assert _classify_error(BrokenPipeError("pipe")) == ErrorCategory.NETWORK

    @pytest.mark.parametrize(
        "err_no", [errno.ECONNRESET, errno.EPIPE, errno.ECONNREFUSED],
    )
    def test_bare_oserror_with_network_errno(self, err_no: int) -> None:
        """A bare OSError carrying a network errno is NETWORK, matching
        _is_transient()'s view of the same exception (#405).

        errno is assigned post-construction: the two-arg OSError(errno, msg)
        constructor auto-maps to ConnectionError subclasses, which would
        dodge the gap under test.
        """
        exc = OSError("transport fault")
        exc.errno = err_no
        assert not isinstance(exc, ConnectionError)
        assert _classify_error(exc) == ErrorCategory.NETWORK
        assert _is_transient(exc) is True

    def test_connection_refused_keyword(self) -> None:
        """Network keywords in the message classify as NETWORK even
        without a typed connection exception (#405).
        """
        assert _classify_error(RuntimeError("connection refused")) == ErrorCategory.NETWORK

    @pytest.mark.parametrize(
        "message",
        [
            "Error: connect ECONNREFUSED 104.18.32.47:443",
            "Error: read ECONNRESET",
            "Error: write EPIPE",
            "Error: socket hang up",
            "Error: getaddrinfo ENOTFOUND api.anthropic.com",
            "Error: getaddrinfo EAI_AGAIN api.anthropic.com",
        ],
    )
    def test_node_style_errno_token(self, message: str) -> None:
        """Node-style transport-fault phrasing from CLI stderr is NETWORK.

        The CLI subprocess is Node.js — its transport faults never use the
        Python phrasing ("connection reset by peer"), only the raw errno
        token, "socket hang up", or a "getaddrinfo" DNS fault (#405).
        """
        assert _classify_error(RuntimeError(message)) == ErrorCategory.NETWORK

    def test_node_etimedout_is_timeout(self) -> None:
        """Node's "connect ETIMEDOUT <addr>" is TIMEOUT: the errno token has
        no space, so neither "timeout" nor "timed out" matched it (#405).
        """
        exc = RuntimeError("Error: connect ETIMEDOUT 104.18.32.47:443")
        assert _classify_error(exc) == ErrorCategory.TIMEOUT

    def test_wrapped_file_not_found_is_not_network(self) -> None:
        """A message embedding "FileNotFoundError" must not classify NETWORK:
        "enotfound" is matched only with its "getaddrinfo " prefix, or the
        substring inside "filenotfounderror" would false-positive (#405).
        """
        exc = RuntimeError("FileNotFoundError: [Errno 2] no such file: 'claude'")
        assert _classify_error(exc) == ErrorCategory.UNKNOWN

    def test_node_errno_token_wins_over_structured_exit_code(self) -> None:
        """Node errno tokens in stderr win over structured exit_code, so a
        transport-fault subprocess death is NETWORK, not CLI_CRASH — and is
        never promoted to STALE_SESSION on a resumed first turn (#405).
        """
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError(
            "subprocess died", exit_code=1, stderr="Error: read ECONNRESET",
        )
        assert _classify_error(exc) == ErrorCategory.NETWORK

    def test_cli_crash(self) -> None:
        assert _classify_error(RuntimeError("exit code 1")) == ErrorCategory.CLI_CRASH

    def test_unknown_fallback(self) -> None:
        assert _classify_error(ValueError("bad json")) == ErrorCategory.UNKNOWN

    def test_stderr_based_classification(self) -> None:
        """Classifies based on stderr content when message is generic."""
        exc = RuntimeError("Command failed with exit code 1")
        exc.stderr = "Error: rate limit exceeded"  # type: ignore[attr-defined]
        assert _classify_error(exc) == ErrorCategory.RATE_LIMIT

    def test_stderr_bytes(self) -> None:
        """Handles bytes stderr correctly."""
        exc = RuntimeError("failed")
        exc.stderr = b"unauthorized"  # type: ignore[attr-defined]
        assert _classify_error(exc) == ErrorCategory.AUTH

    def test_cli_crash_from_stderr(self) -> None:
        """CLI_CRASH detected when 'exit code' is only in stderr."""
        exc = RuntimeError("Command failed")
        exc.stderr = "process terminated with exit code 1"  # type: ignore[attr-defined]
        assert _classify_error(exc) == ErrorCategory.CLI_CRASH

    def test_cli_crash_from_structured_exit_code(self) -> None:
        """CLI_CRASH detected via ProcessError.exit_code without string match."""
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError("subprocess died", exit_code=1)
        assert _classify_error(exc) == ErrorCategory.CLI_CRASH

    def test_auth_wins_over_structured_exit_code(self) -> None:
        """Auth keywords in the message take priority over structured exit_code."""
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError("invalid api key", exit_code=1)
        assert _classify_error(exc) == ErrorCategory.AUTH

    def test_rate_limit_wins_over_structured_exit_code(self) -> None:
        """Rate-limit keywords in stderr win over structured exit_code classification."""
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError("subprocess died", exit_code=1, stderr="HTTP 429 too many requests")
        assert _classify_error(exc) == ErrorCategory.RATE_LIMIT

    def test_network_wins_over_structured_exit_code(self) -> None:
        """Network keywords in stderr win over structured exit_code classification."""
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError("subprocess died", exit_code=1, stderr="connection reset by peer")
        assert _classify_error(exc) == ErrorCategory.NETWORK

    def test_cli_not_found_error_is_unknown(self) -> None:
        """CLINotFoundError classifies as UNKNOWN (permanent config failure)."""
        from claude_agent_sdk._errors import CLINotFoundError

        exc = CLINotFoundError()
        assert _classify_error(exc) == ErrorCategory.UNKNOWN

    def test_cli_not_found_checked_before_cli_connection(self) -> None:
        """CLINotFoundError is a subclass of CLIConnectionError — must NOT
        be classified as NETWORK.
        """
        from claude_agent_sdk._errors import (
            CLIConnectionError,
            CLINotFoundError,
        )

        exc = CLINotFoundError()
        assert isinstance(exc, CLIConnectionError)
        assert _classify_error(exc) != ErrorCategory.NETWORK

    def test_cli_connection_error_is_network(self) -> None:
        """CLIConnectionError (not subclassed) classifies as NETWORK."""
        from claude_agent_sdk._errors import CLIConnectionError

        exc = CLIConnectionError("connection dropped")
        assert _classify_error(exc) == ErrorCategory.NETWORK

    def test_message_parse_error_is_unknown(self) -> None:
        """MessageParseError (SDK/CLI protocol mismatch) classifies as UNKNOWN."""
        from claude_agent_sdk._errors import MessageParseError

        exc = MessageParseError("unexpected message type", {"type": "garbage"})
        assert _classify_error(exc) == ErrorCategory.UNKNOWN

    def test_cli_json_decode_error_is_cli_crash(self) -> None:
        """CLIJSONDecodeError (corrupted CLI output) classifies as CLI_CRASH."""
        from claude_agent_sdk._errors import CLIJSONDecodeError

        orig = ValueError("Expecting value: line 1 column 1 (char 0)")
        exc = CLIJSONDecodeError("{broken", orig)
        assert _classify_error(exc) == ErrorCategory.CLI_CRASH

    def test_buffer_overflow_decode_error_is_unknown(self) -> None:
        """An oversized stream-json frame is not a crashed CLI (#3875)."""
        from claude_agent_sdk._errors import CLIJSONDecodeError

        exc = CLIJSONDecodeError(
            "JSON message exceeded maximum buffer size of 1048576 bytes",
            ValueError("Buffer size 1126618 exceeds limit 1048576"),
        )
        assert _classify_error(exc) == ErrorCategory.UNKNOWN

    def test_buffer_overflow_category_is_not_retried_by_the_loop(self) -> None:
        """UNKNOWN stays out of the loop's resume-in-place retry set (#3875).

        The carve-out is only worth anything if the category it lands in is
        not one ``run_loop`` replays: retrying replays the same oversized
        tool result and fails identically.
        """
        from maestro.agent import _TRANSIENT_SDK_CATEGORIES

        assert ErrorCategory.UNKNOWN not in _TRANSIENT_SDK_CATEGORIES
        assert ErrorCategory.CLI_CRASH in _TRANSIENT_SDK_CATEGORIES

    def test_buffer_overflow_marker_still_matches_the_sdk_message(self) -> None:
        """The marker is pinned to the installed SDK's wording (#3875).

        ``_BUFFER_OVERFLOW_MARKER`` is a substring of a message owned by
        ``claude_agent_sdk._internal.transport.subprocess_cli``. An upstream
        reword would silently send oversized frames back to CLI_CRASH and
        restore the retry storm, so assert the phrase is still in that source.
        """
        import inspect

        from claude_agent_sdk._internal.transport import subprocess_cli

        from maestro.sdk import _BUFFER_OVERFLOW_MARKER

        source = inspect.getsource(subprocess_cli)
        assert _BUFFER_OVERFLOW_MARKER in source, (
            "the SDK transport no longer raises "
            f"{_BUFFER_OVERFLOW_MARKER!r}; update the marker in maestro/sdk.py "
            "or the oversized-frame carve-out silently stops firing."
        )


# ---------------------------------------------------------------------------
# _is_rate_limit_ghost_completion
# ---------------------------------------------------------------------------

class TestIsRateLimitGhostCompletion:
    def test_phrase_and_zero_usage_is_ghost(self) -> None:
        from maestro.sdk import _is_rate_limit_ghost_completion

        assert _is_rate_limit_ghost_completion(
            "You've hit your limit · resets 6pm (UTC)",
            input_tokens=0,
            output_tokens=0,
            total_cost_usd=0.0,
        ) is True

    def test_alternate_phrase_hit_our_limit_is_ghost(self) -> None:
        from maestro.sdk import _is_rate_limit_ghost_completion

        assert _is_rate_limit_ghost_completion(
            "We've hit our limit for now — try again later.",
            input_tokens=0,
            output_tokens=0,
            total_cost_usd=0.0,
        ) is True

    def test_quota_keyword_is_ghost(self) -> None:
        """Unifying on the shared ``QUOTA_KEYWORDS`` broadens detection to the
        generic ``quota`` notice the log-tail observer already treats as a
        quota signal."""
        from maestro.sdk import _is_rate_limit_ghost_completion

        assert _is_rate_limit_ghost_completion(
            "Your monthly quota is exhausted.",
            input_tokens=0,
            output_tokens=0,
            total_cost_usd=0.0,
        ) is True

    def test_overloaded_keyword_is_ghost(self) -> None:
        """``overloaded`` (transient API-overload notice) is likewise a
        zero-usage ghost shape worth resuming in-place on."""
        from maestro.sdk import _is_rate_limit_ghost_completion

        assert _is_rate_limit_ghost_completion(
            "The service is overloaded right now.",
            input_tokens=0,
            output_tokens=0,
            total_cost_usd=0.0,
        ) is True

    def test_none_cost_is_ghost(self) -> None:
        from maestro.sdk import _is_rate_limit_ghost_completion

        assert _is_rate_limit_ghost_completion(
            "You've hit your limit",
            input_tokens=0,
            output_tokens=0,
            total_cost_usd=None,
        ) is True

    def test_no_phrase_is_not_ghost(self) -> None:
        from maestro.sdk import _is_rate_limit_ghost_completion

        assert _is_rate_limit_ghost_completion(
            '{"identity": "ok"}',
            input_tokens=0,
            output_tokens=0,
            total_cost_usd=0.0,
        ) is False

    def test_nonzero_input_tokens_is_not_ghost(self) -> None:
        from maestro.sdk import _is_rate_limit_ghost_completion

        assert _is_rate_limit_ghost_completion(
            "You've hit your limit",
            input_tokens=50,
            output_tokens=0,
            total_cost_usd=0.0,
        ) is False

    def test_nonzero_output_tokens_is_not_ghost(self) -> None:
        from maestro.sdk import _is_rate_limit_ghost_completion

        assert _is_rate_limit_ghost_completion(
            "You've hit your limit",
            input_tokens=0,
            output_tokens=20,
            total_cost_usd=0.0,
        ) is False

    def test_nonzero_cost_is_not_ghost(self) -> None:
        from maestro.sdk import _is_rate_limit_ghost_completion

        assert _is_rate_limit_ghost_completion(
            "You've hit your limit",
            input_tokens=0,
            output_tokens=0,
            total_cost_usd=0.01,
        ) is False


# ---------------------------------------------------------------------------
# _error_result_is_transient
# ---------------------------------------------------------------------------

class TestErrorResultIsTransient:
    def test_5xx_status_is_transient(self) -> None:
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=529, result="", errors=None,
        ) is True

    def test_5xx_lower_bound_is_transient(self) -> None:
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=500, result="", errors=None,
        ) is True

    def test_4xx_status_without_prose_is_not_transient(self) -> None:
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=400,
            result="API Error: 400 Bad Request.",
            errors=None,
        ) is False

    def test_429_status_without_quota_prose_is_transient(self) -> None:
        """A retries-exhausted ``429 Too Many Requests`` is a genuinely
        transient rate-limit, but its prose ("Too Many Requests" / "rate limit
        exceeded") is deliberately absent from ``QUOTA_KEYWORDS`` — so status
        alone must route it to the transient path, else it hard-crashes on
        UNKNOWN."""
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=429,
            result="API Error: 429 Too Many Requests.",
            errors=None,
        ) is True

    def test_408_status_is_transient(self) -> None:
        """``408 Request Timeout`` is a canonical retryable-4xx status."""
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=408, result="", errors=None,
        ) is True

    def test_425_status_is_transient(self) -> None:
        """``425 Too Early`` is a canonical retryable-4xx status."""
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=425, result="", errors=None,
        ) is True

    def test_non_retryable_4xx_status_stays_permanent(self) -> None:
        """A non-retryable 4xx just outside the retryable band (``404``) stays
        non-transient so genuinely permanent failures fail loud."""
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=404,
            result="API Error: 404 Not Found.",
            errors=None,
        ) is False

    def test_non_transient_status_with_quota_prose_stays_permanent(self) -> None:
        """A definitively non-transient status (``403``) whose prose happens to
        carry a ``QUOTA_KEYWORDS`` phrase ("quota") must still fail loud — a
        present status is authoritative, so the prose scan does not run and the
        permanent failure is not misrouted to RATE_LIMIT resume-in-place."""
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=403,
            result="API Error: 403 Forbidden — organization quota exceeded.",
            errors=None,
        ) is False

    def test_non_transient_status_with_overload_errors_stays_permanent(
        self,
    ) -> None:
        """The same guarantee holds when the incidental keyword ("overloaded")
        lives in the ``errors`` list rather than ``result``: a non-transient
        ``400`` stays permanent because status wins over prose."""
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=400,
            result="API Error: 400 Bad Request.",
            errors=["upstream reported the service was overloaded"],
        ) is False

    def test_no_status_overload_prose_is_transient(self) -> None:
        """Ledger r1 fallback: no HTTP status, but overload prose in
        ``result`` — treat as transient so resume-in-place handles it."""
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=None,
            result="API Error: Overloaded. Retry shortly.",
            errors=None,
        ) is True

    def test_no_status_overload_in_errors_list_is_transient(self) -> None:
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=None,
            result="Request failed.",
            errors=["upstream returned Overloaded after 5 retries"],
        ) is True

    def test_no_status_no_keyword_is_not_transient(self) -> None:
        from maestro.sdk import _error_result_is_transient

        assert _error_result_is_transient(
            api_error_status=None,
            result="Error: reached the maximum number of turns.",
            errors=None,
        ) is False


# ---------------------------------------------------------------------------
# Plain-text rate-limit payload detection (#1987)
# ---------------------------------------------------------------------------

# The degraded payload the SDK CLI returns as a *successful* assistant reply
# when the account quota is exhausted, instead of raising.
_RATE_LIMIT_PAYLOAD = "You've hit your limit · resets 6pm (UTC)"


class TestRateLimitPayload:
    @pytest.mark.asyncio
    async def test_early_exit_payload_raises_rate_limit(self) -> None:
        """A quota payload delivered on the early-exit path (``complete_on``
        fires, no ResultMessage) is classified RATE_LIMIT inside
        ``_consume_messages`` — upstream of ``_finalize_usage`` and its cost
        estimation, so a cache-token cost backfill can never defeat the
        zero-cost guard. Re-checking the buffer here recovers the early-exit
        shape the in-loop ResultMessage guard cannot see."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text=_RATE_LIMIT_PAYLOAD)],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("never reached")

        with pytest.raises(SDKError) as exc_info:
            await _consume_messages(
                gen(), "introspect", None,
                complete_on=lambda _msg, _buf: True,
            )
        assert exc_info.value.category == ErrorCategory.RATE_LIMIT

    @pytest.mark.asyncio
    async def test_early_exit_non_ghost_text_returns_normally(self) -> None:
        """A legitimate early-exit reply (no quota phrase) must pass the
        early-exit guard and flow through to ``_finalize_usage``."""
        from maestro.sdk import _consume_messages, _finalize_usage

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="here is the answer")],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("never reached")

        cr = await _consume_messages(
            gen(), "introspect", None,
            complete_on=lambda _msg, _buf: True,
        )
        assert cr.early_exit is True
        result = _finalize_usage(cr, None)
        assert result.response == "here is the answer"

    @pytest.mark.asyncio
    async def test_false_positive_nonzero_usage_returns_normally(
        self,
    ) -> None:
        """A genuine reply that *discusses* quota has non-zero usage, so the
        zero-usage guard keeps it from being misclassified — ``_consume_messages``
        returns it and ``_finalize_usage`` surfaces the response."""
        from maestro.sdk import _consume_messages, _finalize_usage

        async def gen() -> Any:
            yield _result_msg(
                "Let me explain how your quota works",
                usage={"input_tokens": 100, "output_tokens": 50},
            )

        cr = await _consume_messages(gen(), "contemplate", None)
        result = _finalize_usage(cr, None)
        assert result.response == "Let me explain how your quota works"

    @pytest.mark.asyncio
    async def test_query_propagates_rate_limit_payload_as_sdkerror(self) -> None:
        """End-to-end: ``query()`` surfaces the degraded payload as a
        categorized ``SDKError`` the agent's transient handler can resume on,
        rather than returning it as a successful response."""
        async def gen(**kwargs: Any) -> Any:
            yield _result_msg(_RATE_LIMIT_PAYLOAD)

        with (
            patch("maestro.sdk._query", side_effect=gen),
            pytest.raises(SDKError) as exc_info,
        ):
            await query(prompt="test")
        assert exc_info.value.category == ErrorCategory.RATE_LIMIT
