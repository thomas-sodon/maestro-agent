"""SDK options, create_session / query_on_session, resume and session-id capture;
split out of ``tests/test_sdk.py``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    TextBlock,
)

from maestro.sdk import (
    ErrorCategory,
    QueryOptions,
    SDKError,
    _get_warning_throttle_state_for_tests,
    _update_warning_throttle_state,
    configure_sdk_concurrency,
    configure_sdk_model,
    configure_sdk_transcripts,
    configure_sdk_warning_throttle,
    get_warning_hold_probe_interval,
    get_warning_hold_reset_wait,
    get_warning_hold_resets_at,
    get_warning_throttle_multiplier,
    is_warning_hold_active,
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
# _build_sdk_options
# ---------------------------------------------------------------------------

class TestBuildSdkOptions:
    def test_basic_system_prompt(self) -> None:
        from maestro.sdk import _build_sdk_options

        opts, caller = _build_sdk_options(None, "You are helpful")
        assert opts["system_prompt"] == "You are helpful"
        assert caller is None
        assert opts["permission_mode"] == "bypassPermissions"

    def test_caller_extracted(self) -> None:
        from maestro.sdk import _build_sdk_options

        _opts, caller = _build_sdk_options(
            QueryOptions(caller="contemplate"), None,
        )
        assert caller == "contemplate"

    def test_configured_default_model_applied_when_options_none(self) -> None:
        """The configured module default applies even when ``options`` is
        ``None`` — the "no options" path must resolve the model exactly like
        the "options without a model" path (#537). With no configured default,
        ``model`` stays absent so the SDK keeps picking its own default."""
        from maestro.sdk import _build_sdk_options

        try:
            configure_sdk_model("claude-opus-4-6")
            opts, _ = _build_sdk_options(None, None)
            assert opts["model"] == "claude-opus-4-6"
        finally:
            configure_sdk_model(None)

        opts_unset, _ = _build_sdk_options(None, None)
        assert "model" not in opts_unset

    def test_disallowed_tools_default(self) -> None:
        from maestro.sdk import _build_sdk_options

        opts, _ = _build_sdk_options(QueryOptions(), None)
        assert opts["disallowed_tools"] == ["Agent", "Monitor"]

    def test_disallowed_tools_override(self) -> None:
        from maestro.sdk import _build_sdk_options

        opts, _ = _build_sdk_options(
            QueryOptions(disallowed_tools=["Bash"]), None,
        )
        assert opts["disallowed_tools"] == ["Bash", "Monitor"]

    def test_betas_propagates_to_sdk_opts(self) -> None:
        """``QueryOptions.betas`` flows into the dict for
        ``ClaudeAgentOptions``.  The 1M-context call sites pass
        ``["context-1m-2025-08-07"]`` so the SDK enables the beta header."""
        from maestro.sdk import _build_sdk_options

        opts, _ = _build_sdk_options(
            QueryOptions(betas=["context-1m-2025-08-07"]), None,
        )
        assert opts["betas"] == ["context-1m-2025-08-07"]

    def test_betas_omitted_when_none_or_empty(self) -> None:
        """No ``betas`` key when the field is ``None`` or empty — the SDK
        falls back to the empty default."""
        from maestro.sdk import _build_sdk_options

        opts_none, _ = _build_sdk_options(QueryOptions(), None)
        assert "betas" not in opts_none

        opts_empty, _ = _build_sdk_options(QueryOptions(betas=[]), None)
        assert "betas" not in opts_empty

    def test_betas_rejects_unknown_beta(self) -> None:
        """An unrecognised beta raises at the boundary instead of attaching
        verbatim. Guarding here converts the silent drift — a typo'd beta
        disabling the 1M window while still billed ``[1m]`` — into a loud,
        early failure at the site that supplies the beta."""
        from maestro.sdk import LONG_CONTEXT_BETA, _build_sdk_options

        with pytest.raises(ValueError, match="context-1m-typo"):
            _build_sdk_options(QueryOptions(betas=["context-1m-typo"]), None)

        # The known beta still propagates and a mix still rejects on the typo.
        opts, _ = _build_sdk_options(
            QueryOptions(betas=[LONG_CONTEXT_BETA]), None,
        )
        assert opts["betas"] == [LONG_CONTEXT_BETA]

        with pytest.raises(ValueError, match="context-1m-typo"):
            _build_sdk_options(
                QueryOptions(betas=[LONG_CONTEXT_BETA, "context-1m-typo"]), None,
            )

    def test_hooks_omitted_by_default(self) -> None:
        """No ``hooks`` key when the field is ``None`` (the default)."""
        from maestro.sdk import _build_sdk_options

        opts, _ = _build_sdk_options(QueryOptions(), None)
        assert "hooks" not in opts

    def test_hooks_forwarded(self) -> None:
        """``QueryOptions.hooks`` flows into the dict for
        ``ClaudeAgentOptions``."""
        from maestro.sdk import _build_sdk_options

        hooks = {"PreToolUse": []}
        opts, _ = _build_sdk_options(QueryOptions(hooks=hooks), None)
        assert opts["hooks"] is hooks

    def test_hooks_keep_agent_denied(self) -> None:
        """Supplying ``hooks`` is additive — the ``disallowed_tools=
        ["Agent"]`` default must survive (the guard never relaxes it)."""
        from maestro.sdk import _build_sdk_options

        hooks = {"PreToolUse": []}
        opts, _ = _build_sdk_options(QueryOptions(hooks=hooks), None)
        assert opts["disallowed_tools"] == ["Agent", "Monitor"]
        assert opts["hooks"] is hooks


# ---------------------------------------------------------------------------
# create_session / query_on_session
# ---------------------------------------------------------------------------

class TestCreateSession:
    @pytest.mark.asyncio
    async def test_creates_and_connects_client(self) -> None:
        from maestro.sdk import create_session

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        with patch("maestro.sdk.ClaudeSDKClient", return_value=mock_client):
            client = await create_session("hello", system="You are helpful")

        assert client is mock_client
        mock_client.connect.assert_awaited_once_with("hello")

    @pytest.mark.asyncio
    async def test_drains_residual_athrow_before_connect(self) -> None:
        """A residual ``async_generator_athrow`` task pending when
        ``create_session`` is entered must be drained inside this task
        (mirroring ``sdk.query``'s post-aclose drain at sdk.py:2174) so
        it cannot fire ``CancelledError`` into the surrounding scope.
        Recipe matches ``TestDrainAthrowTasks::test_drains_completing_athrow_task``
        — see #1648/#1656."""
        from maestro.sdk import _collect_athrow_tasks, create_session

        async def child() -> Any:
            try:
                yield 1
            except GeneratorExit:
                await asyncio.sleep(0)
                raise

        inner = child()
        await inner.__anext__()
        athrow_task = asyncio.create_task(inner.aclose())

        # Pre-call invariant: the athrow task is pending.
        assert any(
            t is athrow_task for t in _collect_athrow_tasks()
        ), (
            "Setup invariant: residual athrow task must be pending before "
            "create_session is entered."
        )

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        with patch("maestro.sdk.ClaudeSDKClient", return_value=mock_client):
            await create_session("hello", system="You are helpful")

        # Post-call invariant: the drain inside create_session completed
        # the residual task before client.connect ran. Without the fix
        # this assertion fails because the athrow task leaks past
        # create_session into the surrounding scope.
        assert _collect_athrow_tasks() == []

    @pytest.mark.asyncio
    async def test_connects_without_initial_prompt(self) -> None:
        from maestro.sdk import create_session

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        with patch("maestro.sdk.ClaudeSDKClient", return_value=mock_client):
            client = await create_session(system="You are helpful")

        assert client is mock_client
        mock_client.connect.assert_awaited_once_with(None)

    @pytest.mark.asyncio
    async def test_stderr_callback_installed(self) -> None:
        from maestro.sdk import create_session

        captured_opts: list[Any] = []
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        def capture_client(options: Any = None, **kwargs: Any) -> Any:
            captured_opts.append(options)
            return mock_client

        with patch("maestro.sdk.ClaudeSDKClient", side_effect=capture_client):
            await create_session("hello")

        assert len(captured_opts) == 1
        assert captured_opts[0].stderr is not None
        assert callable(captured_opts[0].stderr)

    @pytest.mark.asyncio
    async def test_stderr_callback_captures_lines(self) -> None:
        from maestro.sdk import create_session

        captured_opts: list[Any] = []
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        def capture_client(options: Any = None, **kwargs: Any) -> Any:
            captured_opts.append(options)
            return mock_client

        with patch("maestro.sdk.ClaudeSDKClient", side_effect=capture_client):
            await create_session("hello")

        stderr_fn = captured_opts[0].stderr
        stderr_fn("test error line")
        stderr_fn("another error")
        # Callback logs without raising
        assert callable(stderr_fn)

    @pytest.mark.asyncio
    async def test_session_stderr_accumulator_is_bounded(self) -> None:
        """The per-session stderr buffer is a fixed-size ring, not unbounded.

        A persistent session forces ``--debug-to-stderr`` and only clears the
        buffer on the error path, so without a cap it would grow on every
        successful turn. Drive the installed callback well past the cap and
        confirm the live buffer plateaus rather than climbing.
        """
        from maestro.sdk import _SESSION_STDERR_MAX_LINES, create_session

        captured_opts: list[Any] = []
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        def capture_client(options: Any = None, **kwargs: Any) -> Any:
            captured_opts.append(options)
            return mock_client

        with patch("maestro.sdk.ClaudeSDKClient", side_effect=capture_client):
            await create_session("hello")

        stderr_fn = captured_opts[0].stderr
        for i in range(_SESSION_STDERR_MAX_LINES + 50):
            stderr_fn(f"line {i}")

        # The buffer exposed to the per-turn consumer is bounded at the cap.
        assert len(mock_client._maestro_stderr_lines) == _SESSION_STDERR_MAX_LINES
        # It retains the most-recent lines (FIFO eviction of the oldest).
        assert mock_client._maestro_stderr_lines[-1] == (
            f"line {_SESSION_STDERR_MAX_LINES + 49}"
        )

    @pytest.mark.asyncio
    async def test_passes_options_to_client(self) -> None:
        from maestro.sdk import create_session

        captured_opts: list[Any] = []
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        def capture_client(options: Any = None, **kwargs: Any) -> Any:
            captured_opts.append(options)
            return mock_client

        with patch("maestro.sdk.ClaudeSDKClient", side_effect=capture_client):
            await create_session(
                "hello",
                system="sys",
                options=QueryOptions(cwd="/work", caller="test"),
            )

        assert len(captured_opts) == 1
        assert captured_opts[0].system_prompt == "sys"
        assert captured_opts[0].cwd == "/work"

    @pytest.mark.asyncio
    async def test_debug_to_stderr_set_in_session_extra_args(self) -> None:
        """create_session() forces ``--debug-to-stderr`` on the session client."""
        from maestro.sdk import create_session

        captured_opts: list[Any] = []
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        def capture_client(options: Any = None, **kwargs: Any) -> Any:
            captured_opts.append(options)
            return mock_client

        with patch("maestro.sdk.ClaudeSDKClient", side_effect=capture_client):
            await create_session("hello")

        assert len(captured_opts) == 1
        assert captured_opts[0].extra_args.get("debug-to-stderr") is None
        assert "debug-to-stderr" in captured_opts[0].extra_args

    @pytest.mark.asyncio
    async def test_output_format_stamped_on_session_sidecar(self) -> None:
        """create_session() stamps ``_maestro_output_format`` from the options.

        The session freezes its options at construction, so
        ``query_on_session`` reads the baked ``output_format`` back off this
        sidecar to thread ``structured_output_requested`` (#2533, ledger r1).
        """
        from maestro.sdk import create_session

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        schema = {"type": "json_schema", "schema": {"type": "object"}}

        with patch(
            "maestro.sdk.ClaudeSDKClient", side_effect=lambda **_: mock_client,
        ):
            await create_session(
                "hello", options=QueryOptions(output_format=schema),
            )

        assert mock_client._maestro_output_format == schema

    @pytest.mark.asyncio
    async def test_no_output_format_stamps_none_on_session_sidecar(self) -> None:
        """A session with no ``output_format`` stamps ``None`` on the sidecar.

        Complements the #2533 fix: the ``query_on_session`` read must find
        ``None`` (flag stays ``False``) rather than an auto-vivified truthy stub.
        """
        from maestro.sdk import create_session

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        with patch(
            "maestro.sdk.ClaudeSDKClient", side_effect=lambda **_: mock_client,
        ):
            await create_session("hello", options=QueryOptions(caller="t"))

        assert mock_client._maestro_output_format is None

    @pytest.mark.asyncio
    async def test_session_stderr_callback_logs_at_info_level(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Session-path per-line stderr forwarding is INFO, not WARNING."""
        from maestro.sdk import create_session

        captured_opts: list[Any] = []
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        def capture_client(options: Any = None, **kwargs: Any) -> Any:
            captured_opts.append(options)
            return mock_client

        with patch("maestro.sdk.ClaudeSDKClient", side_effect=capture_client):
            await create_session("hello")

        stderr_fn = captured_opts[0].stderr
        with caplog.at_level(logging.DEBUG, logger="maestro"):
            stderr_fn("routine session debug line")

        stderr_records = [
            r for r in caplog.records
            if "SDK CLI stderr (session):" in r.message
        ]
        assert stderr_records, "Expected a per-line session stderr log record"
        for rec in stderr_records:
            assert rec.levelno == logging.INFO, (
                f"session stderr per-line log must be INFO, got "
                f"{logging.getLevelName(rec.levelno)}: {rec.message}"
            )


class TestQueryOnSession:
    @pytest.mark.asyncio
    async def test_sends_query_and_returns_result(self) -> None:
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg("session reply")

        mock_client.receive_messages = receive

        result = await query_on_session(mock_client, "follow up", caller="test")
        assert result.response == "session reply"
        mock_client.query.assert_awaited_once_with("follow up")

    @pytest.mark.asyncio
    async def test_baked_output_format_threads_structured_requested(self) -> None:
        """A session baked with ``output_format`` threads the request flag so a
        validated-``null`` turn stays productive (#2533).

        Regression for ledger finding r1: ``_query_on_session_impl`` previously
        left ``structured_output_requested`` at its ``False`` default, so a
        session turn that produced a validated ``null`` (empty text, no tools,
        ``ResultMessage.structured_output`` at ``None``) was misclassified as an
        unproductive ``STREAM_STALL`` and a produced structured value was
        dropped. The flag is read from the session ``__dict__`` (like the
        provider tag), so a plain object — not a ``MagicMock`` — models the
        session ``_create_session_impl`` returns.
        """
        from maestro.sdk import query_on_session

        class _FakeSession:
            def __init__(self) -> None:
                # Stamped as a real instance attribute, exactly as
                # ``_create_session_impl`` stamps it.
                self._maestro_output_format = {
                    "type": "json_schema", "schema": {"type": "object"},
                }
                self.query = AsyncMock()

            async def receive_messages(self) -> Any:
                yield _result_msg("", structured_output=None)

        result = await query_on_session(_FakeSession(), "p", caller="planner")
        assert result.structured_output is None
        assert result.parse_json() is None

    @pytest.mark.asyncio
    async def test_no_baked_output_format_leaves_flag_unset(self) -> None:
        """A session with no baked ``output_format`` leaves the field unset (#2533).

        The complement of the r1 regression: an ordinary session turn must not
        acquire a phantom ``None`` the shared sentinel would misread as a
        validated ``null`` — the ``__dict__`` read yields nothing, so the flag
        stays ``False`` and the packing leaves ``_STRUCTURED_UNSET``.
        """
        from maestro.sdk import _STRUCTURED_UNSET, query_on_session

        class _FakeSession:
            def __init__(self) -> None:
                self.query = AsyncMock()

            async def receive_messages(self) -> Any:
                yield _result_msg("plain reply", structured_output=None)

        result = await query_on_session(_FakeSession(), "p", caller="planner")
        assert result.response == "plain reply"
        assert result.structured_output is _STRUCTURED_UNSET

    @pytest.mark.asyncio
    async def test_returns_on_result_without_stop_async_iteration(self) -> None:
        """Regression: on a persistent ``ClaudeSDKClient`` the real
        ``client.receive_messages()`` iterator never terminates between turns
        (the CLI subprocess stays alive with stdout open). Exit must come from
        the ``ResultMessage`` branch breaking out of ``_consume_messages``, not
        from the iterator running out."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="all done")],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("all done")
            # Pre-fix: _consume_messages would block here forever because
            # nothing breaks the while-True loop after ResultMessage and the
            # stream-idle safety net is disabled below.
            await asyncio.sleep(3600)

        mock_client.receive_messages = receive

        result = await asyncio.wait_for(
            query_on_session(
                mock_client, "follow up", caller="test",
                stream_idle_timeout_seconds=0,
            ),
            timeout=2.0,
        )
        assert result.response == "all done"

    @pytest.mark.asyncio
    async def test_acquires_semaphore(self) -> None:
        from maestro.sdk import query_on_session

        configure_sdk_concurrency(2)

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg("ok")

        mock_client.receive_messages = receive

        result = await query_on_session(mock_client, "test")
        assert result.response == "ok"

    @pytest.mark.asyncio
    async def test_applies_warning_throttle(self) -> None:
        from maestro.sdk import query_on_session

        _update_warning_throttle_state(True)  # escalate multiplier

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg("ok")

        mock_client.receive_messages = receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await query_on_session(mock_client, "test", caller="test")

        assert result.response == "ok"
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_usage_tracking(self) -> None:
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg("ok", total_cost_usd=0.05, num_turns=3)

        mock_client.receive_messages = receive

        result = await query_on_session(mock_client, "test", caller="tracker")
        assert result.usage.num_turns == 3
        assert result.usage.total_cost_usd == 0.05
        assert result.usage.caller == "tracker"

    @pytest.mark.asyncio
    async def test_session_log_created_when_log_dir_set(
        self, mem_fs: dict[str, str],
    ) -> None:
        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        sdk_dir = Path("/fake/sdk_session_test")
        sdk_mod.configure_sdk_logging(sdk_dir)

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg("logged reply")

        mock_client.receive_messages = receive

        result = await query_on_session(mock_client, "session prompt", caller="t")
        assert result.response == "logged reply"

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "session prompt" in content

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self) -> None:
        from maestro.sdk import query_on_session

        call_count = 0
        mock_client = MagicMock()

        async def query_side_effect(prompt: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Command failed with exit code 1")

        mock_client.query = AsyncMock(side_effect=query_side_effect)

        async def receive() -> Any:
            yield _result_msg("recovered")

        mock_client.receive_messages = receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await query_on_session(mock_client, "test", caller="test")

        assert result.response == "recovered"
        assert call_count == 2
        mock_sleep.assert_any_await(1.0)

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises_sdk_error(self) -> None:
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(
            side_effect=RuntimeError("Command failed with exit code 1"),
        )

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "test")

        assert exc_info.value.retries_exhausted is True
        assert exc_info.value.retries_attempted == 3

    @pytest.mark.asyncio
    async def test_session_retry_exhaustion_logs_giving_up_error(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Exhausting the session retry budget emits the giving-up marker.

        The session wording ("SDK session query failed after N retries")
        mirrors the per-attempt "SDK session query retry" warnings so
        operators can tell the two raise sites apart.
        """
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(
            side_effect=RuntimeError("Command failed with exit code 1"),
        )

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.ERROR, logger="maestro"), \
             pytest.raises(SDKError):
            await query_on_session(mock_client, "test")

        assert "SDK session query failed after 3 retries" in caplog.text

    @pytest.mark.asyncio
    async def test_no_retry_on_non_transient_error(self) -> None:
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(side_effect=ValueError("bad input"))

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "test")

        assert exc_info.value.retries_exhausted is False
        assert exc_info.value.retries_attempted == 0
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rate_limit_retry_with_rejected_event(self) -> None:
        import time as _time_mod

        from maestro.sdk import query_on_session

        call_count = 0
        mock_client = MagicMock()

        async def combined_query(prompt: str) -> None:
            nonlocal call_count
            call_count += 1

        mock_client.query = AsyncMock(side_effect=combined_query)

        async def reject_receive() -> Any:
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="rejected",
                    resets_at=int(_time_mod.time()) + 5,
                ),
                uuid="u1",
                session_id="s1",
            )
            raise RuntimeError("rate limited")

        mock_client.receive_messages = reject_receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
             pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "test", caller="test")

        # The prompt was accepted: raise for resume-in-place, never re-send (#4159).
        assert exc_info.value.category == ErrorCategory.RATE_LIMIT
        assert call_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_scope_containment(self) -> None:
        from maestro.sdk import query_on_session

        call_count = 0
        cancel_counter = [0]
        mock_client = MagicMock()

        real_current_task = asyncio.current_task()

        def fake_current_task() -> Any:
            if cancel_counter[0] > 0:
                fake = MagicMock()
                fake.cancelling.side_effect = lambda: cancel_counter[0]
                def fake_uncancel() -> int:
                    cancel_counter[0] = max(0, cancel_counter[0] - 1)
                    return cancel_counter[0]
                fake.uncancel.side_effect = fake_uncancel
                return fake
            return real_current_task

        async def query_side_effect(prompt: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                cancel_counter[0] = 2
                raise asyncio.CancelledError(
                    "Cancelled via cancel scope abc; reason: deadline exceeded"
                )

        mock_client.query = AsyncMock(side_effect=query_side_effect)

        async def receive() -> Any:
            yield _result_msg("recovered after cancel")

        mock_client.receive_messages = receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             patch("maestro.sdk.asyncio.current_task", side_effect=fake_current_task):
            result = await query_on_session(mock_client, "test")

        assert result.response == "recovered after cancel"
        assert call_count == 2
        assert cancel_counter[0] == 0

    @pytest.mark.asyncio
    async def test_external_cancel_propagates(self) -> None:
        from maestro.sdk import query_on_session

        mock_client = MagicMock()

        async def cancel_via_task(prompt: str) -> None:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

        mock_client.query = AsyncMock(side_effect=cancel_via_task)

        with pytest.raises(asyncio.CancelledError):
            await query_on_session(mock_client, "test")

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_propagates(self) -> None:
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(side_effect=KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            await query_on_session(mock_client, "test")

    @pytest.mark.asyncio
    async def test_dirty_cancel_counter_drained_on_entry(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from maestro.sdk import query_on_session

        counter = [2]

        def fake_cancelling() -> int:
            return counter[0]

        def fake_uncancel() -> int:
            counter[0] = max(0, counter[0] - 1)
            return counter[0]

        fake_task = MagicMock()
        fake_task.cancelling.side_effect = fake_cancelling
        fake_task.uncancel.side_effect = fake_uncancel

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg("ok")

        mock_client.receive_messages = receive

        with patch("maestro.sdk.asyncio.current_task", return_value=fake_task), \
             patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.WARNING, logger="maestro"):
            result = await query_on_session(mock_client, "test")

        assert result.response == "ok"
        assert fake_task.uncancel.call_count == 2
        assert counter[0] == 0
        assert "dirty cancellation counter" in caplog.text

    @pytest.mark.asyncio
    async def test_rate_limit_classification_retry(self) -> None:
        from maestro.sdk import query_on_session

        call_count = 0
        mock_client = MagicMock()

        async def query_side_effect(prompt: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("rate limit exceeded")

        mock_client.query = AsyncMock(side_effect=query_side_effect)

        async def receive() -> Any:
            yield _result_msg("ok after rate classify")

        mock_client.receive_messages = receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await query_on_session(mock_client, "test", caller="test")

        assert result.response == "ok after rate classify"
        assert call_count == 2
        sleep_val = mock_sleep.await_args_list[0][0][0]
        assert sleep_val >= 60.0

    @pytest.mark.asyncio
    async def test_rate_limit_classification_exhausted(self) -> None:
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(
            side_effect=RuntimeError("rate limit exceeded"),
        )

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "test")

        assert exc_info.value.category == ErrorCategory.RATE_LIMIT
        assert exc_info.value.retries_exhausted is True

    @pytest.mark.asyncio
    async def test_warning_throttle_with_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        sdk_dir = Path("/fake/sdk_session_throttle_log")
        sdk_mod.configure_sdk_logging(sdk_dir)

        _update_warning_throttle_state(True)

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg("ok")

        mock_client.receive_messages = receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock):
            result = await query_on_session(mock_client, "throttled", caller="t")

        assert result.response == "ok"
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Warning throttle" in content

    @pytest.mark.asyncio
    async def test_rate_limit_rejected_with_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        import time as _time_mod

        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        sdk_dir = Path("/fake/sdk_session_rate_limit_log")
        sdk_mod.configure_sdk_logging(sdk_dir)

        call_count = 0
        mock_client = MagicMock()

        async def combined_query(prompt: str) -> None:
            nonlocal call_count
            call_count += 1

        mock_client.query = AsyncMock(side_effect=combined_query)

        async def reject_receive() -> Any:
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="rejected",
                    resets_at=int(_time_mod.time()) + 5,
                ),
                uuid="u1",
                session_id="s1",
            )
            raise RuntimeError("rejected")

        mock_client.receive_messages = reject_receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "rate limited", caller="t")

        assert exc_info.value.category == ErrorCategory.RATE_LIMIT
        assert call_count == 1
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Rate limited after prompt accepted" in content
        assert "sleeping" not in content

    @pytest.mark.asyncio
    async def test_rate_limit_classification_with_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        sdk_dir = Path("/fake/sdk_session_classify_log")
        sdk_mod.configure_sdk_logging(sdk_dir)

        call_count = 0
        mock_client = MagicMock()

        async def query_side_effect(prompt: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("rate limit exceeded")

        mock_client.query = AsyncMock(side_effect=query_side_effect)

        async def receive() -> Any:
            yield _result_msg("ok")

        mock_client.receive_messages = receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock):
            result = await query_on_session(mock_client, "classify", caller="t")

        assert result.response == "ok"
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "Rate limited (classification)" in content

    @pytest.mark.asyncio
    async def test_session_logging_on_retry(
        self, mem_fs: dict[str, str],
    ) -> None:
        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        sdk_dir = Path("/fake/sdk_session_retry_log")
        sdk_mod.configure_sdk_logging(sdk_dir)

        call_count = 0
        mock_client = MagicMock()

        async def query_side_effect(prompt: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Command failed with exit code 1")

        mock_client.query = AsyncMock(side_effect=query_side_effect)

        async def receive() -> Any:
            yield _result_msg("ok")

        mock_client.receive_messages = receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock):
            result = await query_on_session(mock_client, "retry prompt", caller="t")

        assert result.response == "ok"
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "RETRY" in content
        assert "EXCEPTION" in content

    @pytest.mark.asyncio
    async def test_prompt_accepted_consume_failure_raises_not_retried(
        self,
    ) -> None:
        """A transient failure during ``_consume_messages`` — struck *after*
        the prompt was accepted — raises instead of retrying in place.

        Retrying in place would re-send the same prompt on the persistent
        session, duplicating it in the conversation history (prompt +
        partial response + prompt again), which can confuse the model — most
        of all when the aborted turn already emitted tool calls. The fix
        raises the categorized ``SDKError`` so the caller's resume-in-place
        recreates a fresh session; ``client.query`` is awaited exactly once.

        Contrast with ``test_retry_on_transient_error`` /
        ``test_session_logging_on_retry`` above, where ``client.query`` itself
        raises (pre-acceptance) and the cheap in-place retry is preserved.
        """
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            # Prompt already accepted (``client.query`` returned); a partial
            # response streams, then the consume phase crashes transiently.
            yield AssistantMessage(
                content=[TextBlock(text="partial response")],
                model="claude-sonnet-4-20250514",
            )
            raise RuntimeError("Command failed with exit code 1")

        mock_client.receive_messages = receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "test", caller="t")

        # The prompt was sent exactly once — no in-place re-send.
        assert mock_client.query.await_count == 1
        # CLI_CRASH is in _TRANSIENT_SDK_CATEGORIES, so the caller's
        # resume-in-place recreates the session with a fresh transcript.
        assert exc_info.value.category == ErrorCategory.CLI_CRASH

    @pytest.mark.asyncio
    async def test_prompt_accepted_internal_cancel_promoted_to_stale_session(
        self,
    ) -> None:
        """A *contained* internal-cancel after prompt acceptance recovers.

        A contained anyio internal-cancel (deadline exceeded) that strikes
        inside ``_consume_messages`` — *after* the prompt was accepted — is
        wrapped into a ``TimeoutError`` whose natural category is TIMEOUT.
        TIMEOUT is **not** in ``_TRANSIENT_SDK_CATEGORIES``, so without
        promotion the ``prompt_accepted`` gate would crash the loop on a
        transient the containment machinery exists to recover from. The fix
        promotes it to STALE_SESSION so the caller clears the persisted ids
        and retries on a fresh session, delivering the recovery the gate
        promises — instead of re-sending the prompt in place (#1216) or
        crashing (the pre-fix TIMEOUT surfaced loudly).

        Contrast with ``test_prompt_accepted_consume_failure_raises_not_retried``
        above, where a plain CLI crash keeps its natural (already-recoverable)
        CLI_CRASH category.
        """
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            # Prompt already accepted; a partial response streams, then an
            # anyio cancel-scope deadline fires. The BaseException block in
            # ``_query_on_session_impl`` contains it (current task is not
            # externally cancelled) and wraps it into a TimeoutError.
            yield AssistantMessage(
                content=[TextBlock(text="partial response")],
                model="claude-sonnet-4-20250514",
            )
            raise asyncio.CancelledError(
                "Cancelled via cancel scope 0xdead; reason: deadline exceeded",
            )

        mock_client.receive_messages = receive

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "test", caller="t")

        # The prompt was sent exactly once — no in-place re-send.
        assert mock_client.query.await_count == 1
        # Promoted from TIMEOUT so the caller's resume-in-place recovery
        # (STALE_SESSION is in _TRANSIENT_SDK_CATEGORIES) recreates the
        # session fresh rather than crashing the loop.
        assert exc_info.value.category == ErrorCategory.STALE_SESSION
        # The current task was left clean — the internal cancel never escaped.
        current = asyncio.current_task()
        assert current is not None
        assert current.cancelling() == 0

    @pytest.mark.asyncio
    async def test_stderr_attached_to_exception(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Session stderr lines are attached to the exception on failure."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        # Simulate stderr lines collected by the session callback.
        mock_client._maestro_stderr_lines = ["error: crash detail", "backtrace: ..."]
        mock_client.query = AsyncMock(
            side_effect=RuntimeError("Command failed with exit code 1"),
        )

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.ERROR), \
             pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "test")

        # Callback-captured stderr is attached to the SDKError.
        assert exc_info.value.stderr is not None
        assert "crash detail" in exc_info.value.stderr
        assert "backtrace" in exc_info.value.stderr
        # Lines should appear in ERROR-level log.
        assert any("stderr captured (session" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_stderr_bytes_decoded_in_sdk_error(self) -> None:
        """Bytes stderr on the inner exception is decoded for SDKError."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client._maestro_stderr_lines = []

        async def raise_with_bytes_stderr(prompt: str) -> None:
            exc = RuntimeError("Command failed with exit code 1")
            exc.stderr = b"byte error output"  # type: ignore[attr-defined]
            raise exc

        mock_client.query = AsyncMock(side_effect=raise_with_bytes_stderr)

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "test")

        assert exc_info.value.stderr == "byte error output"

    @pytest.mark.asyncio
    async def test_empty_stderr_lines_triggers_error_session(
        self, caplog: pytest.LogCaptureFixture, mem_fs: dict[str, str],
    ) -> None:
        """A session crash with zero captured stderr lines logs an ERROR.

        Includes the ``(session)`` marker so investigators can distinguish
        the session-path crash from the stateless-query path.  Also writes
        an EMPTY STDERR line to the session log when one is configured.
        """
        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        sdk_dir = Path("/fake/sdk_empty_stderr_session")
        sdk_mod.configure_sdk_logging(sdk_dir)

        mock_client = MagicMock()
        # Force the empty branch: getattr() on a MagicMock would otherwise
        # auto-generate a truthy attribute, so set the list explicitly.
        mock_client._maestro_stderr_lines = []
        mock_client.query = AsyncMock(
            side_effect=RuntimeError("Command failed with exit code 1"),
        )

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.ERROR, logger="maestro"), \
             pytest.raises(SDKError):
            await query_on_session(
                mock_client, "test", caller="sess_caller",
            )

        empty_records = [
            r for r in caplog.records
            if "empty captured stderr" in r.message
        ]
        assert empty_records, "Expected an empty-stderr ERROR log"
        first = empty_records[0]
        assert "session" in first.message
        assert "attempt=1" in first.message
        assert "caller=sess_caller" in first.message
        assert "exc_type=RuntimeError" in first.message
        # The session log should contain a matching EMPTY STDERR line.
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert written, "Expected a session log file"
        content = mem_fs[written[0]]
        assert "EMPTY STDERR on crash (session" in content
        assert "caller=sess_caller" in content
        assert "exc_type=RuntimeError" in content

    @pytest.mark.asyncio
    async def test_query_on_session_early_exit_calls_interrupt(self) -> None:
        """Early-exit predicate fires → client.interrupt() is awaited."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock()

        async def receive() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="enough text" * 20)],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("never reached")

        mock_client.receive_messages = receive

        result = await query_on_session(
            mock_client, "follow up",
            caller="introspect",
            complete_on=lambda _m, buf: len(buf) >= 100,
        )

        mock_client.interrupt.assert_awaited_once()
        assert result.response.startswith("enough text")

    @pytest.mark.asyncio
    async def test_query_on_session_early_exit_tolerates_interrupt_failure(
        self,
    ) -> None:
        """If client.interrupt() raises, the partial result is still returned."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock(side_effect=RuntimeError("torn down"))

        async def receive() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="payload" * 20)],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("never")

        mock_client.receive_messages = receive

        result = await query_on_session(
            mock_client, "x",
            complete_on=lambda _m, buf: len(buf) >= 50,
        )
        assert result.response.startswith("payload")

    @pytest.mark.asyncio
    async def test_query_on_session_stream_stall_propagates(self) -> None:
        """A STREAM_STALL raised by the consumer reaches the caller intact."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="hi")],
                model="claude-sonnet-4-20250514",
            )
            await asyncio.sleep(3600)
            yield _result_msg("never")

        mock_client.receive_messages = receive

        # Real ``asyncio`` primitives: the stream hangs past the full budget.
        with pytest.raises(SDKError) as ei:
            await query_on_session(
                mock_client, "x",
                stream_idle_timeout_seconds=0.1,
            )
        assert ei.value.category is ErrorCategory.STREAM_STALL

    @pytest.mark.asyncio
    async def test_query_on_session_early_exit_drains_buffer(self) -> None:
        """After early-exit + interrupt, leftover messages are drained from
        the shared buffer so the next ``client.query()`` does not read them
        before the new turn's response arrives."""
        from collections import deque

        from maestro.sdk import query_on_session

        # Single shared FIFO buffer simulates the anyio memory stream behind
        # ``client.receive_messages()``: each call returns a generator that
        # consumes from the same queue. Without the drain, turn 2's
        # consumer would read the leftover messages from turn 1 first.
        shared_buffer: deque[Any] = deque()
        # Turn 1 — ample text so the predicate fires on the first message.
        shared_buffer.append(AssistantMessage(
            content=[TextBlock(text="x" * 200)],
            model="claude-sonnet-4-20250514",
        ))
        # Stale turn-1 leftovers that the background reader pumped in
        # before the CLI honoured the interrupt.
        shared_buffer.append(AssistantMessage(
            content=[TextBlock(text="LEFTOVER")],
            model="claude-sonnet-4-20250514",
        ))
        shared_buffer.append(_result_msg("turn1-tail", session_id="turn1-sess"))

        async def receive() -> Any:
            while shared_buffer:
                yield shared_buffer.popleft()

        mock_client = MagicMock()
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock()
        mock_client.receive_messages = receive

        result1 = await query_on_session(
            mock_client, "turn 1", caller="test",
            complete_on=lambda _m, buf: len(buf) >= 100,
        )
        # Early-exit fired and interrupt was called.
        mock_client.interrupt.assert_awaited_once()
        assert result1.response.startswith("x")
        # The drain consumed the LEFTOVER assistant + turn1-tail result so
        # the buffer is empty before turn 2 is sent. Without the drain,
        # those two messages would still be sitting in the buffer.
        assert len(shared_buffer) == 0

        # Now seed the buffer with TURN 2 messages and call query_on_session
        # again. The result message session_id and result string come from
        # turn 2 only — the leftover turn1-tail with session_id=turn1-sess
        # was consumed by the drain, not by turn 2's consumer.
        shared_buffer.append(AssistantMessage(
            content=[TextBlock(text="turn-2-assistant")],
            model="claude-sonnet-4-20250514",
        ))
        shared_buffer.append(_result_msg("turn2-done", session_id="turn2-sess"))

        result2 = await query_on_session(
            mock_client, "turn 2", caller="test",
        )
        assert result2.response == "turn2-done"
        assert result2.session_id == "turn2-sess"

    @pytest.mark.asyncio
    async def test_query_on_session_drain_idle_timeout_exits(self) -> None:
        """When no new messages arrive after ``client.interrupt()``, the
        drain loop returns within the idle bound rather than hanging."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock()

        # First call: deliver one large assistant chunk + ResultMessage so
        # the consume returns naturally (no STREAM_STALL). The drain then
        # opens a fresh iterator that never yields.
        call_index = [0]

        async def first_iter() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="y" * 200)],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("turn1")

        async def empty_iter() -> Any:
            # Generator that never yields a value; awaiting __anext__ on it
            # will trip the drain's wait_for timeout. Suppress the unreachable
            # warning by including a yield in an unreachable branch.
            await asyncio.sleep(3600)
            yield _result_msg("never")

        def receive() -> Any:
            idx = call_index[0]
            call_index[0] += 1
            if idx == 0:
                return first_iter()
            return empty_iter()

        mock_client.receive_messages = receive

        # Patch asyncio.wait_for so the drain's wait_for raises TimeoutError
        # immediately. The consume loop still calls ``wait_for`` — once per
        # message, for the *whole* idle budget, with the heartbeat running
        # as a side timer — so both the consume and the drain reach this
        # fake; they are told apart by the timeout value, not call order.
        real_wait_for = asyncio.wait_for
        wait_for_call_count = [0]

        async def fake_wait_for(coro: Any, timeout: float) -> Any:
            wait_for_call_count[0] += 1
            # The drain uses a 2.0s timeout; the consume uses 180s default.
            # Distinguish by timeout value to avoid coupling to call order.
            if timeout == 2.0:
                coro.close()
                raise TimeoutError
            return await real_wait_for(coro, timeout)

        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            result = await query_on_session(
                mock_client, "follow up", caller="test",
                complete_on=lambda _m, buf: len(buf) >= 100,
            )

        assert result.response.startswith("y")
        # Both receive_messages calls happened: consume + drain.
        assert call_index[0] == 2
        # The drain timed out cleanly — wait_for was called at least twice
        # (once for the assistant message in consume, once for the drain).
        assert wait_for_call_count[0] >= 2

    @pytest.mark.asyncio
    async def test_query_on_session_drain_swallows_unexpected_exception(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If the drain iterator raises an unexpected exception, the drain
        breaks cleanly without surfacing it to the caller."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock()

        call_index = [0]

        async def first_iter() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="q" * 200)],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("turn1")

        async def boom_iter() -> Any:
            raise RuntimeError("something exploded mid-stream")
            yield  # unreachable; satisfies async-generator typing

        def receive() -> Any:
            idx = call_index[0]
            call_index[0] += 1
            if idx == 0:
                return first_iter()
            return boom_iter()

        mock_client.receive_messages = receive

        # Should not raise — the drain swallows the exception at debug.
        with caplog.at_level(logging.DEBUG, logger="maestro"):
            result = await query_on_session(
                mock_client, "follow up", caller="test",
                complete_on=lambda _m, buf: len(buf) >= 100,
            )
        assert result.response.startswith("q")
        assert call_index[0] == 2
        assert "Post-interrupt drain aborted (caller=test)" in caplog.text

    @pytest.mark.asyncio
    async def test_query_on_session_drain_writes_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        """When a session log is configured, the drain emits a log line
        recording the count of drained messages."""
        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        sdk_dir = Path("/fake/sdk_drain_log")
        sdk_mod.configure_sdk_logging(sdk_dir)
        mock_client = MagicMock()
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock()

        call_index = [0]

        async def first_iter() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="r" * 200)],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("turn1")

        async def leftovers_iter() -> Any:
            yield _result_msg("leftover-tail")

        def receive() -> Any:
            idx = call_index[0]
            call_index[0] += 1
            if idx == 0:
                return first_iter()
            return leftovers_iter()

        mock_client.receive_messages = receive

        await query_on_session(
            mock_client, "follow up", caller="test",
            complete_on=lambda _m, buf: len(buf) >= 100,
        )

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert written, "Expected a session log file"
        content = mem_fs[written[0]]
        assert "Drained 1 post-interrupt message(s)" in content

    @pytest.mark.asyncio
    async def test_query_on_session_drain_message_cap(self) -> None:
        """A misbehaving CLI that keeps yielding messages cannot wedge the
        drain — the message cap forces an exit even when ResultMessage
        never arrives."""
        import maestro.sdk as sdk_mod
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()
        mock_client.interrupt = AsyncMock()

        call_index = [0]
        flood_yield_count = [0]

        async def first_iter() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="z" * 200)],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("turn1")

        async def flood_iter() -> Any:
            # Yield a non-Result message indefinitely; the cap must stop it.
            while True:
                flood_yield_count[0] += 1
                yield AssistantMessage(
                    content=[TextBlock(text="flood")],
                    model="claude-sonnet-4-20250514",
                )

        def receive() -> Any:
            idx = call_index[0]
            call_index[0] += 1
            if idx == 0:
                return first_iter()
            return flood_iter()

        mock_client.receive_messages = receive

        result = await query_on_session(
            mock_client, "follow up", caller="test",
            complete_on=lambda _m, buf: len(buf) >= 100,
        )

        assert result.response.startswith("z")
        # The drain reached the flood iterator.
        assert call_index[0] == 2
        # The cap stopped consumption — exactly _INTERRUPT_DRAIN_MAX_MESSAGES
        # were drained, no more.
        assert flood_yield_count[0] == sdk_mod._INTERRUPT_DRAIN_MAX_MESSAGES
        assert sdk_mod._INTERRUPT_DRAIN_MAX_MESSAGES == 50


class TestWarningHoldStateMachine:
    """Tests for the warning-hold extension of the throttle state machine."""

    def setup_method(self) -> None:
        configure_sdk_warning_throttle(
            base_seconds=10, hysteresis=3,
            hold_threshold=3, hold_probe_interval=60,
        )

    def test_consecutive_max_warnings_increments(self) -> None:
        """Warnings at max multiplier increment the consecutive counter."""
        for _ in range(4):
            _update_warning_throttle_state(saw_warning=True)
        # 4th warning reaches max — counter is already 1.
        assert _get_warning_throttle_state_for_tests() == (16, 0, 1)
        for i in range(2, 5):
            _update_warning_throttle_state(saw_warning=True)
            _, _, consec = _get_warning_throttle_state_for_tests()
            assert consec == i

    def test_consecutive_max_warnings_resets_on_clean(self) -> None:
        """A single clean call resets the consecutive-max counter to 0."""
        for _ in range(4):
            _update_warning_throttle_state(saw_warning=True)
        _update_warning_throttle_state(saw_warning=True)
        assert _get_warning_throttle_state_for_tests()[2] == 2
        _update_warning_throttle_state(saw_warning=False)
        assert _get_warning_throttle_state_for_tests()[2] == 0

    def test_hold_not_active_below_max(self) -> None:
        """Hold is inactive when the multiplier has not reached max."""
        _update_warning_throttle_state(saw_warning=True)
        _update_warning_throttle_state(saw_warning=True)
        assert _get_warning_throttle_state_for_tests()[0] == 4
        assert not is_warning_hold_active()

    def test_hold_not_active_below_threshold(self) -> None:
        """Hold is inactive at max multiplier with fewer than threshold warnings."""
        for _ in range(5):
            _update_warning_throttle_state(saw_warning=True)
        # 4th reaches max (consec=1), 5th bumps to 2 — still below 3.
        assert _get_warning_throttle_state_for_tests() == (16, 0, 2)
        assert not is_warning_hold_active()

    def test_hold_active_at_threshold(self) -> None:
        """Hold activates once consecutive-max counter reaches threshold."""
        for _ in range(6):
            _update_warning_throttle_state(saw_warning=True)
        # 4th reaches max (1), 5th (2), 6th (3) — equals threshold.
        assert _get_warning_throttle_state_for_tests() == (16, 0, 3)
        assert is_warning_hold_active()

    def test_hold_not_active_when_warning_below_utilization_ceiling(self) -> None:
        """#4514: a window-level warning at 80% utilization slows calls but
        never parks the loop."""
        for _ in range(8):
            _update_warning_throttle_state(saw_warning=True, utilization=0.80)
        assert get_warning_throttle_multiplier() == 16
        assert _get_warning_throttle_state_for_tests()[2] == 0
        assert is_warning_hold_active() is False

    def test_hold_active_when_warning_at_or_above_ceiling(self) -> None:
        """Warnings at/above the utilization ceiling still trip the hold."""
        for _ in range(6):
            _update_warning_throttle_state(saw_warning=True, utilization=0.97)
        assert is_warning_hold_active() is True

    def test_hold_counter_resets_when_utilization_drops(self) -> None:
        """A sub-ceiling warning at max multiplier resets the hold counter."""
        for _ in range(5):
            _update_warning_throttle_state(saw_warning=True, utilization=0.99)
        assert _get_warning_throttle_state_for_tests()[2] == 2
        _update_warning_throttle_state(saw_warning=True, utilization=0.5)
        assert _get_warning_throttle_state_for_tests()[2] == 0

    def test_hold_legacy_path_when_utilization_missing(self) -> None:
        """No utilization reported keeps the pre-#4514 behaviour."""
        for _ in range(6):
            _update_warning_throttle_state(saw_warning=True, utilization=None)
        assert is_warning_hold_active() is True

    def test_configure_hold_min_utilization(self) -> None:
        """A custom ceiling takes effect."""
        configure_sdk_warning_throttle(
            base_seconds=10, hysteresis=3, hold_min_utilization=0.7,
        )
        for _ in range(6):
            _update_warning_throttle_state(saw_warning=True, utilization=0.75)
        assert is_warning_hold_active() is True

    def test_hold_resets_at_exposed(self) -> None:
        """The last warned ``resets_at`` is exposed and cleared on a clean call."""
        assert get_warning_hold_resets_at() is None
        _update_warning_throttle_state(saw_warning=True, resets_at=1_700_000_000)
        assert get_warning_hold_resets_at() == 1_700_000_000
        _update_warning_throttle_state(saw_warning=False)
        assert get_warning_hold_resets_at() is None

    def test_reset_wait_until_resets_at(self) -> None:
        """A future resets_at yields remaining + slack seconds."""
        _update_warning_throttle_state(saw_warning=True, resets_at=1000)
        with patch("maestro.sdk.time.time", return_value=400.0):
            assert get_warning_hold_reset_wait() == 610

    def test_reset_wait_capped(self) -> None:
        """A far-future resets_at is bounded by the reset-wait cap."""
        _update_warning_throttle_state(saw_warning=True, resets_at=10**10)
        with patch("maestro.sdk.time.time", return_value=0.0):
            assert get_warning_hold_reset_wait() == 6 * 3600

    def test_reset_wait_none_when_absent_or_past(self) -> None:
        """No resets_at, or one already elapsed, yields None."""
        assert get_warning_hold_reset_wait() is None
        _update_warning_throttle_state(saw_warning=True, resets_at=100)
        with patch("maestro.sdk.time.time", return_value=400.0):
            assert get_warning_hold_reset_wait() is None

    def test_hold_clears_on_single_clean(self) -> None:
        """One clean call resets the counter and deactivates the hold."""
        for _ in range(6):
            _update_warning_throttle_state(saw_warning=True)
        assert is_warning_hold_active()
        _update_warning_throttle_state(saw_warning=False)
        assert not is_warning_hold_active()

    def test_configure_hold_threshold(self) -> None:
        """Custom threshold value takes effect."""
        configure_sdk_warning_throttle(
            base_seconds=10, hysteresis=3,
            hold_threshold=2, hold_probe_interval=60,
        )
        for _ in range(4):
            _update_warning_throttle_state(saw_warning=True)
        # 4th reaches max → consec=1, below threshold=2.
        assert not is_warning_hold_active()
        _update_warning_throttle_state(saw_warning=True)
        # consec=2, meets threshold.
        assert is_warning_hold_active()

    def test_get_warning_hold_probe_interval(self) -> None:
        """Returns configured probe interval."""
        assert get_warning_hold_probe_interval() == 60
        configure_sdk_warning_throttle(
            base_seconds=10, hysteresis=3,
            hold_threshold=3, hold_probe_interval=120,
        )
        assert get_warning_hold_probe_interval() == 120


# ---------------------------------------------------------------------------
# IterationUsage serialization (resume-in-place support)
# ---------------------------------------------------------------------------


class TestIterationUsageSerialization:
    def test_to_dict_round_trip(self) -> None:
        from maestro.sdk import IterationUsage

        original = IterationUsage(
            iteration=42,
            started_at="2026-04-16T10:00:00+00:00",
            calls=3,
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=100,
            cache_read_tokens=50,
            total_cost_usd=0.15,
            per_caller={"contemplate": 2, "evolve": 1},
            total_turns=17,
            per_caller_turns={"contemplate": 5, "evolve": 12},
        )
        restored = IterationUsage.from_dict(original.to_dict())

        assert restored.iteration == original.iteration
        assert restored.started_at == original.started_at
        assert restored.calls == original.calls
        assert restored.input_tokens == original.input_tokens
        assert restored.output_tokens == original.output_tokens
        assert restored.cache_creation_tokens == original.cache_creation_tokens
        assert restored.cache_read_tokens == original.cache_read_tokens
        assert restored.total_cost_usd == original.total_cost_usd
        assert restored.per_caller == original.per_caller
        assert restored.total_turns == original.total_turns
        assert restored.per_caller_turns == original.per_caller_turns

    def test_from_dict_handles_missing_optional_fields(self) -> None:
        from maestro.sdk import IterationUsage

        minimal = {
            "iteration": 1,
            "started_at": "2026-04-16T10:00:00+00:00",
        }
        restored = IterationUsage.from_dict(minimal)

        assert restored.iteration == 1
        assert restored.calls == 0
        assert restored.per_caller == {}
        # Backward-compat: records written before the turn-telemetry fields
        # existed must still load with safe defaults.
        assert restored.total_turns == 0
        assert restored.per_caller_turns == {}

    def test_from_dict_skips_non_int_per_caller_turns_values(self) -> None:
        """Non-int-coercible per_caller_turns values are skipped silently,
        matching the behavior of per_caller."""
        from maestro.sdk import IterationUsage

        restored = IterationUsage.from_dict({
            "iteration": 7,
            "started_at": "2026-04-16T10:00:00+00:00",
            "per_caller_turns": {"good": 9, "bad": "nope"},
        })
        assert restored.per_caller_turns == {"good": 9}

    def test_to_dict_does_not_serialize_session_peak_turns(self) -> None:
        """``_session_peak_turns`` is transient bookkeeping for the live
        aggregator and must not leak into the JSONL record."""
        from maestro.sdk import IterationUsage

        usage = IterationUsage(
            iteration=1,
            started_at="2026-04-16T10:00:00+00:00",
        )
        usage._session_peak_turns["sess-a"] = 7
        d = usage.to_dict()
        assert "_session_peak_turns" not in d

    def test_from_dict_tolerates_non_dict_per_caller(self) -> None:
        """Corrupt per_caller fields don't crash loading."""
        from maestro.sdk import IterationUsage

        restored = IterationUsage.from_dict({
            "iteration": 5,
            "started_at": "2026-04-16T10:00:00+00:00",
            "per_caller": "not a dict",
        })
        assert restored.per_caller == {}

    def test_from_dict_tolerates_non_int_counts(self) -> None:
        """Corrupt numeric fields fall back to defaults without crashing."""
        from maestro.sdk import IterationUsage

        restored = IterationUsage.from_dict({
            "iteration": "not an int",
            "started_at": "2026-04-16T10:00:00+00:00",
            "calls": "xyz",
            "total_cost_usd": "bad",
        })
        assert restored.iteration == 0
        assert restored.calls == 0

    def test_from_dict_skips_non_int_per_caller_values(self) -> None:
        """Non-int-coercible per_caller values are skipped silently."""
        from maestro.sdk import IterationUsage

        restored = IterationUsage.from_dict({
            "iteration": 2,
            "started_at": "2026-04-16T10:00:00+00:00",
            "per_caller": {"good": 3, "bad": "not-a-number"},
        })
        assert restored.per_caller == {"good": 3}
        assert restored.total_cost_usd == 0.0


# ---------------------------------------------------------------------------
# QueryResult.session_id capture
# ---------------------------------------------------------------------------


class TestQueryResultSessionId:
    @pytest.mark.asyncio
    async def test_query_populates_session_id_from_result_message(self) -> None:
        """query() should copy ResultMessage.session_id into QueryResult."""
        async def gen(**kwargs: Any) -> Any:
            yield _result_msg("done", session_id="ses_abc123")

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query("hello")

        assert result.session_id == "ses_abc123"

    @pytest.mark.asyncio
    async def test_query_on_session_stashes_session_id_on_client(self) -> None:
        """query_on_session writes the ResultMessage.session_id sidecar."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()
        mock_client._maestro_last_session_id = None
        mock_client._maestro_resume_used = False

        async def receive() -> Any:
            yield _result_msg("ok", session_id="ses_xyz")

        mock_client.receive_messages = receive

        await query_on_session(mock_client, "prompt", caller="t")

        assert mock_client._maestro_last_session_id == "ses_xyz"


# ---------------------------------------------------------------------------
# create_session resume
# ---------------------------------------------------------------------------


class TestCreateSessionResume:
    @pytest.mark.asyncio
    async def test_sets_resume_option(self) -> None:
        from maestro.sdk import create_session

        captured_opts: list[Any] = []
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        def capture_client(options: Any = None, **kwargs: Any) -> Any:
            captured_opts.append(options)
            return mock_client

        with patch("maestro.sdk.ClaudeSDKClient", side_effect=capture_client):
            await create_session(resume_session_id="ses_saved")

        assert captured_opts[0].resume == "ses_saved"
        assert mock_client._maestro_resume_used is True
        assert mock_client._maestro_last_session_id is None

    @pytest.mark.asyncio
    async def test_connect_failure_falls_back_to_fresh_session(self) -> None:
        from maestro.sdk import create_session

        connect_attempts: list[Any] = []
        client_opts: list[Any] = []

        def make_client(options: Any = None, **kwargs: Any) -> Any:
            client_opts.append(options)
            c = MagicMock()
            async def connect(prompt: Any) -> None:
                connect_attempts.append(getattr(options, "resume", None))
                if len(connect_attempts) == 1:
                    raise RuntimeError("resume id unknown")
            c.connect = connect
            c.disconnect = AsyncMock()
            return c

        with patch(
            "maestro.sdk.ClaudeSDKClient", side_effect=make_client,
        ):
            client = await create_session(resume_session_id="ses_stale")

        # First attempt used resume, second did not.
        assert connect_attempts == ["ses_stale", None]
        assert client._maestro_resume_used is False

    @pytest.mark.asyncio
    async def test_connect_failure_without_resume_id_raises_sdkerror(self) -> None:
        """A no-resume connect failure surfaces as a classified SDKError (#4570)."""
        from maestro.sdk import SDKError, create_session

        def make_client(options: Any = None, **kwargs: Any) -> Any:
            c = MagicMock()
            c.connect = AsyncMock(side_effect=RuntimeError("cli died"))
            c.disconnect = AsyncMock()
            return c

        with patch("maestro.sdk.ClaudeSDKClient", side_effect=make_client), \
             pytest.raises(SDKError, match="cli died"):
            await create_session(resume_session_id=None)


# ---------------------------------------------------------------------------
# create_session CancelledError handling (#1643)
# ---------------------------------------------------------------------------


class TestCreateSessionCancelledError:
    """Prove that ``create_session`` catches ``asyncio.CancelledError``.

    ``CancelledError`` is a ``BaseException`` (Python 3.8+), so the current
    ``except Exception`` guard in ``create_session`` lets it escape unhandled.
    A residual ``async_generator_athrow`` task from SDK teardown can fire
    this into ``client.connect()`` (#1643).  The fix should catch it and
    either retry or convert it to a classifiable ``SDKError``.
    """

    @pytest.mark.asyncio
    async def test_cancelled_error_on_connect_does_not_propagate(self) -> None:
        """CancelledError during connect must not escape create_session."""
        from maestro.sdk import create_session

        mock_client = MagicMock()
        mock_client.connect = AsyncMock(
            side_effect=asyncio.CancelledError("athrow leak"),
        )
        mock_client.disconnect = AsyncMock()

        with (
            patch("maestro.sdk.ClaudeSDKClient", return_value=mock_client),
            pytest.raises(SDKError),
        ):
            await create_session("hello", system="sys")

    @pytest.mark.asyncio
    async def test_cancelled_error_on_connect_with_resume_does_not_propagate(
        self,
    ) -> None:
        """CancelledError during resumed connect must not escape either."""
        from maestro.sdk import create_session

        connect_calls = 0

        def make_client(options: Any = None, **kwargs: Any) -> Any:
            nonlocal connect_calls
            c = MagicMock()

            async def connect(prompt: Any) -> None:
                nonlocal connect_calls
                connect_calls += 1
                raise asyncio.CancelledError("athrow leak")

            c.connect = connect
            c.disconnect = AsyncMock()
            return c

        with (
            patch("maestro.sdk.ClaudeSDKClient", side_effect=make_client),
            pytest.raises(SDKError),
        ):
            await create_session(
                "hello",
                system="sys",
                resume_session_id="ses_old",
            )

    @pytest.mark.asyncio
    async def test_cancelled_error_drains_cancel_state(self) -> None:
        """The handler drains stacked cancels so they don't re-fire later."""
        from maestro.sdk import create_session

        mock_client = MagicMock()

        async def _connect_with_cancel(prompt: Any) -> None:
            # Simulate the SDK's internal cancel-scope machinery: bump
            # the task's cancelling counter before raising, exactly as
            # ``task.cancel()`` + ``await`` does in production.
            task = asyncio.current_task()
            assert task is not None
            task.cancel("athrow residual")
            # Yield so the cancel request is delivered.
            await asyncio.sleep(0)

        mock_client.connect = _connect_with_cancel
        mock_client.disconnect = AsyncMock()

        with (
            patch("maestro.sdk.ClaudeSDKClient", return_value=mock_client),
            pytest.raises(SDKError),
        ):
            await create_session("hello", system="sys")

        # After the handler runs, the current task must have zero
        # pending cancels — otherwise the next ``await`` would crash.
        task = asyncio.current_task()
        assert task is not None
        assert task.cancelling() == 0

    @pytest.mark.asyncio
    async def test_cancelled_error_during_pre_connect_drain_does_not_propagate(
        self,
    ) -> None:
        """CancelledError raised by the pre-connect drain must classify as CLI_CRASH.

        ``_drain_athrow_tasks`` deliberately lets ``CancelledError`` propagate
        so the caller's cancellation intent is preserved (sdk.py:147-154).
        Before #1696 the drain ran one line *above* the existing
        ``except asyncio.CancelledError`` guard in ``create_session``, so a
        residual ``async_generator_athrow`` firing into the drain's own
        ``asyncio.wait`` escaped unwrapped and crashed the loop. The fix
        widens the guard so the drain shares the connect-cancel path.
        """
        from maestro.sdk import create_session

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()

        leaked = asyncio.CancelledError("athrow leak from drain")

        with (
            patch("maestro.sdk.ClaudeSDKClient", return_value=mock_client),
            patch(
                "maestro.sdk._drain_athrow_tasks",
                new=AsyncMock(side_effect=leaked),
            ),
            pytest.raises(SDKError) as excinfo,
        ):
            await create_session("hello", system="sys")

        assert excinfo.value.category == ErrorCategory.CLI_CRASH
        assert isinstance(excinfo.value.original, asyncio.CancelledError)
        # ``client.connect`` must never be reached when the drain itself
        # raises — the bug manifested precisely because the drain ran
        # before the connect guard opened.
        mock_client.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_late_materializing_athrow_reaped_inside_failing_call(
        self,
    ) -> None:
        """A late-materializing athrow orphan born inside ``client.connect()``
        must be reaped before ``SDKError`` propagates (#1893).

        The pre-connect drain (#1648) snapshots the loop's task set
        before connect runs; orphans materialised *during* that connect
        slip past it. ``disconnect_session``'s post-child athrow drain
        runs first, but a residual orphan birthed inside connect can
        leak past it too. Without a post-cancel drain inside the
        failing ``create_session`` call, the orphan survives and
        re-fires ``CancelledError`` into the agent's resume-in-place
        retry, exhausting ``_MAX_CONSECUTIVE_CLI_CRASHES`` in
        milliseconds.
        """
        from maestro.sdk import _collect_athrow_tasks, create_session

        async def child() -> Any:
            try:
                yield 1
            except GeneratorExit:
                await asyncio.sleep(0)
                raise

        spawned: list[asyncio.Task[Any]] = []

        async def _connect_spawns_athrow_then_cancels(prompt: Any) -> None:
            inner = child()
            await inner.__anext__()
            spawned.append(asyncio.create_task(inner.aclose()))
            # Yield so the athrow task is registered in the loop's
            # task set before the cancel propagates.
            await asyncio.sleep(0)
            raise asyncio.CancelledError("late athrow leak")

        mock_client = MagicMock()
        mock_client.connect = _connect_spawns_athrow_then_cancels
        mock_client.disconnect = AsyncMock()

        with (
            patch("maestro.sdk.ClaudeSDKClient", return_value=mock_client),
            # Simulate the production scenario: ``disconnect_session``
            # runs but its post-child athrow drain fails to reap the
            # late-materializing orphan (the failure mode #1893
            # describes). Patching it to a no-op AsyncMock isolates
            # the new post-cancel drain as the only line of defence
            # so the assertion below proves *that* drain reaped the
            # orphan, not the disconnect drain.
            patch(
                "maestro.sdk.disconnect_session",
                new=AsyncMock(),
            ),
            pytest.raises(SDKError) as excinfo,
        ):
            await create_session("hello", system="sys")

        assert excinfo.value.category == ErrorCategory.CLI_CRASH
        # The post-cancel drain inside the failing call must have
        # reaped the late orphan; nothing should remain in the loop's
        # task set to bleed into the next create_session attempt.
        assert _collect_athrow_tasks() == []

    @pytest.mark.asyncio
    async def test_post_cancel_drain_logs_residual_pending_count(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When the post-cancel drain leaves pending tasks, log a
        ``pending_athrow_after_drain`` warning so an operator triaging
        a future repeat can tell, from the log line alone, that the
        new drain itself failed to reap the orphan (a third leak
        point further inside the SDK, #1893)."""
        from maestro.sdk import create_session

        mock_client = MagicMock()
        mock_client.connect = AsyncMock(
            side_effect=asyncio.CancelledError("late athrow leak"),
        )
        mock_client.disconnect = AsyncMock()

        with (
            patch("maestro.sdk.ClaudeSDKClient", return_value=mock_client),
            # Stub both drains so the post-cancel drain reports a
            # residual pending task; an AsyncMock returning a tuple
            # makes the diagnostic path deterministic without needing
            # a real orphan that the test's event loop might race.
            patch(
                "maestro.sdk._drain_athrow_tasks",
                new=AsyncMock(return_value=(0, 2)),
            ),
            caplog.at_level(logging.WARNING, logger="maestro"),
            pytest.raises(SDKError),
        ):
            await create_session("hello", system="sys")

        assert any(
            "pending_athrow_after_drain=2" in record.message
            for record in caplog.records
        ), (
            "expected post-cancel drain to log pending_athrow_after_drain "
            f"when its return value is non-zero; got: {[r.message for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# STALE_SESSION classification on first-turn failure
# ---------------------------------------------------------------------------


class TestStaleSessionClassification:
    @pytest.mark.asyncio
    async def test_first_turn_failure_with_resume_classified_stale(
        self,
    ) -> None:
        """A resumed session's first-turn failure → STALE_SESSION category."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(
            side_effect=RuntimeError("CLI exit code 1: session not found"),
        )
        mock_client._maestro_last_session_id = None
        mock_client._maestro_resume_used = True

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "test", caller="t")

        assert exc_info.value.category == ErrorCategory.STALE_SESSION

    @pytest.mark.asyncio
    async def test_unknown_on_resumed_first_turn_classified_stale(
        self,
    ) -> None:
        """An UNKNOWN-category first-turn failure still promotes to
        STALE_SESSION — e.g. a protocol ``MessageParseError`` with no
        exit-code or session hint."""
        from claude_agent_sdk._errors import MessageParseError

        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(
            side_effect=MessageParseError(
                "unexpected message type", {"type": "garbage"},
            ),
        )
        mock_client._maestro_last_session_id = None
        mock_client._maestro_resume_used = True

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "t", caller="c")

        assert exc_info.value.category == ErrorCategory.STALE_SESSION

    @pytest.mark.asyncio
    async def test_network_on_resumed_first_turn_not_promoted(self) -> None:
        """A NETWORK first-turn failure keeps its category — the resumed
        transcript is fine, the transport dropped (#1250)."""
        from claude_agent_sdk._errors import CLIConnectionError

        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(
            side_effect=CLIConnectionError("connection dropped"),
        )
        mock_client._maestro_last_session_id = None
        mock_client._maestro_resume_used = True

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "t", caller="c")

        assert exc_info.value.category == ErrorCategory.NETWORK

    @pytest.mark.asyncio
    async def test_auth_on_resumed_first_turn_not_promoted(self) -> None:
        """An AUTH first-turn failure keeps its category — a real
        credential problem must not masquerade as a stale session (#1250)."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(
            side_effect=RuntimeError("unauthorized: invalid api key"),
        )
        mock_client._maestro_last_session_id = None
        mock_client._maestro_resume_used = True

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "t", caller="c")

        assert exc_info.value.category == ErrorCategory.AUTH

    @pytest.mark.asyncio
    async def test_timeout_on_resumed_first_turn_not_promoted(self) -> None:
        """A TIMEOUT first-turn failure keeps its category — a transport
        timeout is not a stale transcript (#1250)."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(
            side_effect=RuntimeError("operation timed out"),
        )
        mock_client._maestro_last_session_id = None
        mock_client._maestro_resume_used = True

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "t", caller="c")

        assert exc_info.value.category == ErrorCategory.TIMEOUT

    @pytest.mark.asyncio
    async def test_second_turn_failure_not_classified_stale(self) -> None:
        """After a successful first turn, the flag is cleared — next failure
        is classified normally."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        # _maestro_resume_used starts True (simulating create_session with
        # resume_session_id) — the first successful turn should clear it.
        mock_client._maestro_last_session_id = None
        mock_client._maestro_resume_used = True

        async def receive_ok() -> Any:
            yield _result_msg("ok", session_id="ses_live")

        mock_client.query = AsyncMock()
        mock_client.receive_messages = receive_ok

        # First turn succeeds — flag should clear.
        result = await query_on_session(mock_client, "one")
        assert result.response == "ok"
        assert mock_client._maestro_resume_used is False

        # Second turn fails consistently — not stale (resume was proven
        # live). Reset the mock to raise every time.
        mock_client.query = AsyncMock(
            side_effect=RuntimeError("CLI exit code 1: something else"),
        )

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "two")

        assert exc_info.value.category != ErrorCategory.STALE_SESSION

    @pytest.mark.asyncio
    async def test_rate_limit_still_classified_rate_limit(self) -> None:
        """Even on a resumed session, rate-limit errors stay RATE_LIMIT."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock(
            side_effect=RuntimeError("429 too many requests rate_limit"),
        )
        mock_client._maestro_last_session_id = None
        mock_client._maestro_resume_used = True

        with patch("maestro.sdk.asyncio.sleep", new_callable=AsyncMock), pytest.raises(SDKError) as exc_info:
            await query_on_session(mock_client, "t", caller="c")

        assert exc_info.value.category == ErrorCategory.RATE_LIMIT
