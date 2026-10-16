"""query() happy paths and transient-error classification; split out of
``tests/test_sdk.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    StreamEvent,
    TaskNotificationMessage,
    TaskProgressMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from maestro import sdk as sdk_module
from maestro.sdk import (
    ErrorCategory,
    QueryOptions,
    SDKError,
    _extract_error_detail,
    _is_transient,
    configure_sdk_model,
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
# query()
# ---------------------------------------------------------------------------

class TestQuery:
    @pytest.mark.asyncio
    async def test_basic_result(self) -> None:
        msg = _result_msg("hello world")

        async def gen(**kwargs: Any) -> Any:
            yield msg

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test")

        assert result.response == "hello world"
        assert result.tool_calls == []
        assert result.usage.num_turns == 1

    @pytest.mark.asyncio
    async def test_no_result_message(self) -> None:
        async def gen(**kwargs: Any) -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="thinking out loud")],
                model="claude-sonnet-4-20250514",
            )

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test")

        assert result.response == ""
        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_tool_call_flow(self) -> None:
        assistant = AssistantMessage(
            content=[ToolUseBlock(id="tu1", name="Bash", input={"command": "ls"})],
            model="claude-sonnet-4-20250514",
        )
        user = UserMessage(
            content=[ToolResultBlock(tool_use_id="tu1", content="file.txt")],
        )
        result_msg = _result_msg("done")

        async def gen(**kwargs: Any) -> Any:
            yield assistant
            yield user
            yield result_msg

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test")

        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc.name == "Bash"
        assert tc.input == {"command": "ls"}
        assert tc.result == "file.txt"
        assert tc.is_error is False

    @pytest.mark.asyncio
    async def test_tool_error(self) -> None:
        assistant = AssistantMessage(
            content=[ToolUseBlock(id="tu1", name="Bash", input={"command": "bad"})],
            model="claude-sonnet-4-20250514",
        )
        user = UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu1", content="command failed", is_error=True
                )
            ],
        )

        async def gen(**kwargs: Any) -> Any:
            yield assistant
            yield user
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test")

        assert result.tool_calls[0].is_error is True
        assert result.tool_calls[0].result == "command failed"

    @pytest.mark.asyncio
    async def test_thinking_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        assistant = AssistantMessage(
            content=[ThinkingBlock(thinking="deep thoughts here", signature="sig")],
            model="claude-sonnet-4-20250514",
        )

        async def gen(**kwargs: Any) -> Any:
            yield assistant
            yield _result_msg()

        with caplog.at_level(logging.INFO, logger="maestro"), patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        # First ThinkingBlock per batch is INFO so long think-only spans
        # stay visible in maestro.log even at INFO-only log levels.
        records = [r for r in caplog.records if "SDK thinking" in r.message]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert "deep thoughts here" in records[0].message

    @pytest.mark.asyncio
    async def test_thinking_first_info_rest_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Multiple ThinkingBlocks in one batch: first INFO, rest DEBUG."""
        assistant = AssistantMessage(
            content=[
                ThinkingBlock(thinking="first thought", signature="s1"),
                ThinkingBlock(thinking="second thought", signature="s2"),
                ThinkingBlock(thinking="third thought", signature="s3"),
            ],
            model="claude-sonnet-4-20250514",
        )

        async def gen(**kwargs: Any) -> Any:
            yield assistant
            yield _result_msg()

        with caplog.at_level(logging.DEBUG, logger="maestro"), patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        records = [r for r in caplog.records if "SDK thinking" in r.message]
        assert len(records) == 3
        assert records[0].levelno == logging.INFO
        assert records[1].levelno == logging.DEBUG
        assert records[2].levelno == logging.DEBUG

    @pytest.mark.asyncio
    async def test_text_first_info_rest_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Multiple TextBlocks in one batch: first INFO, rest DEBUG."""
        assistant = AssistantMessage(
            content=[
                TextBlock(text="intro sentence"),
                TextBlock(text="middle sentence"),
            ],
            model="claude-sonnet-4-20250514",
        )

        async def gen(**kwargs: Any) -> Any:
            yield assistant
            yield _result_msg()

        with caplog.at_level(logging.DEBUG, logger="maestro"), patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        records = [r for r in caplog.records if "SDK text" in r.message]
        assert len(records) == 2
        assert records[0].levelno == logging.INFO
        assert records[1].levelno == logging.DEBUG

    @pytest.mark.asyncio
    async def test_progress_updates(self) -> None:
        progress = TaskProgressMessage(
            subtype="task_progress",
            data={},
            task_id="t1",
            description="working",
            usage={"total_tokens": 500, "tool_uses": 2, "duration_ms": 1000},
            uuid="u1",
            session_id="s1",
            last_tool_name="Bash",
        )

        async def gen(**kwargs: Any) -> Any:
            yield progress
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test")

        # ResultMessage overwrites duration_ms, but tokens/tool_uses come from progress
        assert result.usage.total_tokens == 500
        assert result.usage.tool_uses == 2

    @pytest.mark.asyncio
    async def test_result_usage(self) -> None:
        msg = _result_msg(
            total_cost_usd=0.0123,
            num_turns=3,
            duration_ms=5000,
        )

        async def gen(**kwargs: Any) -> Any:
            yield msg

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test")

        assert result.usage.total_cost_usd == 0.0123
        assert result.usage.num_turns == 3
        assert result.usage.duration_ms == 5000

    @pytest.mark.asyncio
    async def test_task_notification_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
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

        with caplog.at_level(logging.INFO, logger="maestro"), patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        assert "SDK task completed: all done" in caplog.text

    @pytest.mark.asyncio
    async def test_rate_limit_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="allowed_warning"),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield event
            yield _result_msg()

        with caplog.at_level(logging.WARNING, logger="maestro"), patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test")

        assert "SDK rate limit: allowed_warning" in caplog.text
        assert result.rate_limit_events == ["allowed_warning"]

    @pytest.mark.asyncio
    async def test_warning_utilization_and_resets_at_threaded(self) -> None:
        """#4514: peak warned utilization and *its own* window's resets_at
        reach the throttle state; a lower or bare warning leaves them."""
        events = [
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed_warning", utilization=0.9, resets_at=100,
                ),
                uuid="u1", session_id="s1",
            ),
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed_warning", utilization=0.8, resets_at=200,
                ),
                uuid="u2", session_id="s1",
            ),
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(status="allowed_warning"),
                uuid="u3", session_id="s1",
            ),
        ]

        async def gen(**kwargs: Any) -> Any:
            for e in events:
                yield e
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("maestro.sdk._update_warning_throttle_state") as upd:
            await query(prompt="test")

        upd.assert_called_once_with(True, 0.9, 100)

    @pytest.mark.asyncio
    async def test_warning_resets_at_without_utilization_threaded(self) -> None:
        """A warning reporting only resets_at still threads it through."""
        events = [
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed_warning", resets_at=300,
                ),
                uuid="u1", session_id="s1",
            ),
        ]

        async def gen(**kwargs: Any) -> Any:
            for e in events:
                yield e
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("maestro.sdk._update_warning_throttle_state") as upd:
            await query(prompt="test")

        upd.assert_called_once_with(True, None, 300)

    @pytest.mark.asyncio
    async def test_rate_limit_warning_logs_window_detail(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning event's window fields reach the WARNING record (#4515)."""
        event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                rate_limit_type="five_hour",
                utilization=0.82,
                resets_at=1760000000,
                overage_status="allowed",
            ),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield event
            yield _result_msg()

        with caplog.at_level(logging.WARNING, logger="maestro"), patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        expected = (
            "SDK rate limit: allowed_warning type=five_hour utilization=0.82"
            " resets_at=1760000000 overage_status=allowed"
        )
        assert expected in messages

    @pytest.mark.asyncio
    async def test_rate_limit_warning_omits_none_fields(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bare event logs only its status — no ``None`` or stray separators."""
        event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="allowed_warning"),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield event
            yield _result_msg()

        with caplog.at_level(logging.WARNING, logger="maestro"), patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert "SDK rate limit: allowed_warning" in messages

    @pytest.mark.asyncio
    async def test_rate_limit_allowed_logged_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Benign ``"allowed"`` events must not pollute the WARNING stream,
        or the diagnostic keyword scan will falsely report a rate limit."""
        event = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="allowed"),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield event
            yield _result_msg()

        with caplog.at_level(logging.DEBUG, logger="maestro"), patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test")

        # Message is captured at DEBUG level, never WARNING.
        debug_hit = any(
            r.levelno == logging.DEBUG and "SDK rate limit: allowed" in r.getMessage()
            for r in caplog.records
        )
        warning_hit = any(
            r.levelno >= logging.WARNING and "SDK rate limit" in r.getMessage()
            for r in caplog.records
        )
        assert debug_hit
        assert not warning_hit
        assert result.rate_limit_events == ["allowed"]

    @pytest.mark.asyncio
    async def test_build_sdk_options_enables_partial_messages(self) -> None:
        """``_build_sdk_options`` must set ``include_partial_messages=True`` so
        the SDK emits ``StreamEvent`` deltas during long think-only spans.
        Without this, ``maestro.log`` falls silent for tens of seconds during
        ``reflect``/``propose_plan`` and looks indistinguishable from a hang."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].include_partial_messages is True

    @pytest.mark.asyncio
    async def test_stream_event_thinking_delta_emits_info(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``thinking_delta`` stream event emits an INFO line so the log
        shows a heartbeat during a long think-only span."""
        event = StreamEvent(
            uuid="u1",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "delta": {
                    "type": "thinking_delta",
                    "thinking": "live thinking heartbeat",
                },
            },
        )

        async def gen(**kwargs: Any) -> Any:
            yield event
            yield _result_msg()

        # Pin monotonic clock so the throttle gate fires deterministically.
        monkeypatch.setattr("time.monotonic", lambda: 100.0)

        with caplog.at_level(logging.INFO, logger="maestro"), patch(
            "maestro.sdk._query", side_effect=gen,
        ):
            await query(prompt="test")

        records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "SDK thinking" in r.message
        ]
        assert len(records) == 1
        assert "live thinking heartbeat" in records[0].message

    @pytest.mark.asyncio
    async def test_stream_event_text_delta_emits_info(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``text_delta`` stream event also emits an INFO line."""
        event = StreamEvent(
            uuid="u1",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "live text heartbeat"},
            },
        )

        async def gen(**kwargs: Any) -> Any:
            yield event
            yield _result_msg()

        monkeypatch.setattr("time.monotonic", lambda: 100.0)

        with caplog.at_level(logging.INFO, logger="maestro"), patch(
            "maestro.sdk._query", side_effect=gen,
        ):
            await query(prompt="test")

        records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "SDK text" in r.message
        ]
        assert len(records) == 1
        assert "live text heartbeat" in records[0].message

    @pytest.mark.asyncio
    async def test_stream_event_throttle_silences_inside_window(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Deltas inside the throttle window must NOT emit a second INFO line,
        but every delta must still reach the per-session log file."""
        events = [
            StreamEvent(
                uuid=f"u{i}",
                session_id="s1",
                event={
                    "type": "content_block_delta",
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": f"chunk {i}",
                    },
                },
            )
            for i in range(3)
        ]

        async def gen(**kwargs: Any) -> Any:
            for ev in events:
                yield ev
            yield _result_msg()

        # Clock advances 1s per call — well inside the 10s gate, so only the
        # first delta emits an INFO line.  ``count(...)`` is unbounded so any
        # incidental ``time.monotonic`` callers in the SDK stack don't exhaust it.
        from itertools import count
        clock_steps = count(start=100.0, step=1.0)
        monkeypatch.setattr("time.monotonic", lambda: next(clock_steps))

        # Enable session logging so the per-session file path is exercised.
        # Patch via monkeypatch so the global is auto-restored after the test.
        monkeypatch.setattr("maestro.sdk._sdk_log_dir", tmp_path)

        with caplog.at_level(logging.INFO, logger="maestro"), patch(
            "maestro.sdk._query", side_effect=gen,
        ):
            await query(prompt="test")

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "SDK thinking" in r.message
        ]
        assert len(info_records) == 1
        assert "chunk 0" in info_records[0].message

        # Every delta should have been written to the session log file.
        log_files = list((tmp_path).rglob("*.log"))
        assert log_files, "session log file was not created"
        contents = log_files[0].read_text()
        assert "Thinking delta: chunk 0" in contents
        assert "Thinking delta: chunk 1" in contents
        assert "Thinking delta: chunk 2" in contents

    @pytest.mark.asyncio
    async def test_stream_event_throttle_releases_after_window(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once the throttle window elapses, the next delta emits INFO again."""
        events = [
            StreamEvent(
                uuid=f"u{i}",
                session_id="s1",
                event={
                    "type": "content_block_delta",
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": f"chunk {i}",
                    },
                },
            )
            for i in range(2)
        ]

        async def gen(**kwargs: Any) -> Any:
            for ev in events:
                yield ev
            yield _result_msg()

        # 100s -> emit; 115s -> 15s elapsed > 10s gate -> emit again.  Use
        # ``count`` so incidental ``time.monotonic`` calls in the SDK stack
        # don't exhaust a fixed-size iterator.
        from itertools import count
        clock_steps = count(start=100.0, step=15.0)
        monkeypatch.setattr("time.monotonic", lambda: next(clock_steps))

        with caplog.at_level(logging.INFO, logger="maestro"), patch(
            "maestro.sdk._query", side_effect=gen,
        ):
            await query(prompt="test")

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "SDK thinking" in r.message
        ]
        assert len(info_records) == 2

    @pytest.mark.asyncio
    async def test_stream_event_non_delta_silent(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``message_start``, ``content_block_stop``, and other non-delta
        events must not emit any log line — only thinking/text deltas count."""
        event = StreamEvent(
            uuid="u1",
            session_id="s1",
            event={"type": "message_start"},
        )

        async def gen(**kwargs: Any) -> Any:
            yield event
            yield _result_msg()

        monkeypatch.setattr("time.monotonic", lambda: 100.0)

        with caplog.at_level(logging.DEBUG, logger="maestro"), patch(
            "maestro.sdk._query", side_effect=gen,
        ):
            await query(prompt="test")

        assert not any(
            "SDK thinking" in r.message or "SDK text" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_stream_event_per_kind_throttle_thinking_then_text(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The thinking→text transition always emits INFO for the first text
        delta even when it falls within the shared heartbeat window of the
        last thinking delta — per-kind throttle ensures independence."""
        thinking_event = StreamEvent(
            uuid="u1",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "delta": {
                    "type": "thinking_delta",
                    "thinking": "reasoning about stuff",
                },
            },
        )
        text_event = StreamEvent(
            uuid="u2",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "output begins"},
            },
        )

        async def gen(**kwargs: Any) -> Any:
            yield thinking_event
            yield text_event
            yield _result_msg()

        # Clock advances 1s — well within the 10s window.  Under a shared
        # throttle the text delta would be suppressed; per-kind throttle
        # ensures both emit.
        from itertools import count
        clock_steps = count(start=100.0, step=1.0)
        monkeypatch.setattr("time.monotonic", lambda: next(clock_steps))

        with caplog.at_level(logging.INFO, logger="maestro"), patch(
            "maestro.sdk._query", side_effect=gen,
        ):
            await query(prompt="test")

        thinking_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "SDK thinking" in r.message
        ]
        text_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "SDK text" in r.message
        ]
        assert len(thinking_records) == 1
        assert "reasoning about stuff" in thinking_records[0].message
        assert len(text_records) == 1
        assert "output begins" in text_records[0].message

    @pytest.mark.asyncio
    async def test_stream_event_same_kind_still_throttled(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Multiple deltas of the same kind within the heartbeat window are
        still suppressed — per-kind throttle only lifts the cross-kind gate."""
        events = [
            StreamEvent(
                uuid=f"u{i}",
                session_id="s1",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": f"text chunk {i}"},
                },
            )
            for i in range(3)
        ]

        async def gen(**kwargs: Any) -> Any:
            for ev in events:
                yield ev
            yield _result_msg()

        # 1s steps — all within the 10s window.
        from itertools import count
        clock_steps = count(start=100.0, step=1.0)
        monkeypatch.setattr("time.monotonic", lambda: next(clock_steps))

        with caplog.at_level(logging.INFO, logger="maestro"), patch(
            "maestro.sdk._query", side_effect=gen,
        ):
            await query(prompt="test")

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "SDK text" in r.message
        ]
        # Only the first text delta should emit; the rest are within window.
        assert len(info_records) == 1
        assert "text chunk 0" in info_records[0].message

    @pytest.mark.asyncio
    async def test_stream_event_first_of_each_kind_always_emits(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The first delta of each kind always emits regardless of how
        recently the other kind emitted — the initial 0.0 per-kind value
        guarantees this."""
        # Text first, then thinking — reverse order from typical model output.
        text_event = StreamEvent(
            uuid="u1",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "first text"},
            },
        )
        thinking_event = StreamEvent(
            uuid="u2",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "delta": {
                    "type": "thinking_delta",
                    "thinking": "first thinking",
                },
            },
        )

        async def gen(**kwargs: Any) -> Any:
            yield text_event
            yield thinking_event
            yield _result_msg()

        # 0.5s apart — well within window.
        from itertools import count
        clock_steps = count(start=100.0, step=0.5)
        monkeypatch.setattr("time.monotonic", lambda: next(clock_steps))

        with caplog.at_level(logging.INFO, logger="maestro"), patch(
            "maestro.sdk._query", side_effect=gen,
        ):
            await query(prompt="test")

        text_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "SDK text" in r.message
        ]
        thinking_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "SDK thinking" in r.message
        ]
        assert len(text_records) == 1
        assert "first text" in text_records[0].message
        assert len(thinking_records) == 1
        assert "first thinking" in thinking_records[0].message

    @pytest.mark.asyncio
    async def test_empty_allowed_tools_forwarded(self) -> None:
        """QueryOptions(allowed_tools=[]) must forward an empty list to the
        ``ClaudeAgentOptions`` layer.  Note that this only proves the maestro
        forwarding step is correct — ``claude-agent-sdk`` 's
        ``subprocess_cli.py:196`` then silently drops the empty list via a
        truthiness check, so the CLI does *not* end up gating tools off.
        Tool-less sessions must use ``tools=[]`` instead (see
        ``test_empty_tools_forwarded``)."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(allowed_tools=[]))

        # The SDK should receive options with an explicit empty allowed_tools list.
        assert "options" in captured_kwargs
        assert captured_kwargs["options"].allowed_tools == []

    @pytest.mark.asyncio
    async def test_empty_tools_forwarded(self) -> None:
        """QueryOptions(tools=[]) must forward an empty list to
        ``ClaudeAgentOptions.tools``.  Unlike ``allowed_tools=[]``, the SDK's
        CLI argument constructor handles ``tools=[]`` correctly — emitting
        ``--tools ""`` — so this is the canonical shape for gating tools off
        in tool-less sessions."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(tools=[]))

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].tools == []

    @pytest.mark.asyncio
    async def test_tools_unset_not_forwarded(self) -> None:
        """When ``QueryOptions.tools`` is left at its default ``None``, the
        forwarder must not set ``tools`` on ``ClaudeAgentOptions`` at all,
        leaving the SDK to use its default base set."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions())

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].tools is None

    @pytest.mark.asyncio
    async def test_all_none_query_options_only_sets_stderr(self) -> None:
        """QueryOptions() with all-None fields should only set the stderr
        callback on SDK options (for CLI error capture)."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions())

        # stderr callback is always injected for CLI error capture.
        assert "options" in captured_kwargs
        assert captured_kwargs["options"].stderr is not None

    @pytest.mark.asyncio
    async def test_query_options_cwd_forwarded(self) -> None:
        """QueryOptions.cwd should be forwarded to ClaudeAgentOptions,
        even when it is the only non-None field."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(
                prompt="test",
                options=QueryOptions(cwd="/only-cwd"),
            )

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].cwd == "/only-cwd"

    @pytest.mark.asyncio
    async def test_query_options_max_turns_forwarded(self) -> None:
        """QueryOptions.max_turns should be forwarded to ClaudeAgentOptions."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(
                prompt="test",
                options=QueryOptions(allowed_tools=["Read"], max_turns=5),
            )

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].max_turns == 5

    @pytest.mark.asyncio
    async def test_system_prompt_forwarded(self) -> None:
        """The system kwarg should be forwarded to the SDK."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", system="You are helpful.")

        assert captured_kwargs["options"].system_prompt == "You are helpful."

    @pytest.mark.asyncio
    async def test_no_options_only_sets_stderr(self) -> None:
        """When options=None, SDK options should only contain the stderr
        callback, permission_mode, and the default disallowed_tools."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].stderr is not None
        assert captured_kwargs["options"].disallowed_tools == ["Agent", "Monitor"]
        assert captured_kwargs["prompt"] == "test"

    @pytest.mark.asyncio
    async def test_max_turns_zero_forwarded(self) -> None:
        """max_turns=0 is falsy but semantically meaningful (hard stop).
        It must be forwarded, not silently dropped."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(max_turns=0))

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].max_turns == 0

    @pytest.mark.asyncio
    async def test_query_options_model_forwarded(self) -> None:
        """QueryOptions.model should be forwarded to ClaudeAgentOptions."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(model="claude-sonnet-4-5-20250514"))

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].model == "claude-sonnet-4-5-20250514"

    @pytest.mark.asyncio
    async def test_query_options_effort_forwarded(self) -> None:
        """QueryOptions.effort should be forwarded to ClaudeAgentOptions."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(effort="low"))

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].effort == "low"

    @pytest.mark.asyncio
    async def test_configure_sdk_model_substitutes_when_model_unset(self) -> None:
        """configure_sdk_model() default substitutes when QueryOptions.model is None."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        try:
            configure_sdk_model("claude-opus-4-6")
            with patch("maestro.sdk._query", side_effect=gen):
                await query(prompt="test", options=QueryOptions(effort="high"))
        finally:
            configure_sdk_model(None)

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].model == "claude-opus-4-6"
        assert captured_kwargs["options"].effort == "high"

    @pytest.mark.asyncio
    async def test_explicit_model_wins_over_configured_default(self) -> None:
        """An explicit options.model overrides the configured default."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        try:
            configure_sdk_model("claude-opus-4-6")
            with patch("maestro.sdk._query", side_effect=gen):
                await query(
                    prompt="test",
                    options=QueryOptions(
                        model="claude-sonnet-4-6", effort="high",
                    ),
                )
        finally:
            configure_sdk_model(None)

        assert captured_kwargs["options"].model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_configure_sdk_model_applies_across_effort_levels(self) -> None:
        """The configured default applies regardless of effort level."""
        try:
            configure_sdk_model("claude-opus-4-6")
            for effort in ("low", "medium", "high"):
                captured_kwargs: dict[str, Any] = {}

                async def gen(
                    captured: dict[str, Any] = captured_kwargs,
                    **kwargs: Any,
                ) -> Any:
                    captured.update(kwargs)
                    yield _result_msg("ok")

                with patch("maestro.sdk._query", side_effect=gen):
                    await query(prompt="test", options=QueryOptions(effort=effort))

                assert captured_kwargs["options"].model == "claude-opus-4-6"
                assert captured_kwargs["options"].effort == effort
        finally:
            configure_sdk_model(None)

    @pytest.mark.asyncio
    async def test_configure_sdk_model_none_is_noop(self) -> None:
        """configure_sdk_model(None) and ("") leave model unset by default."""
        # Empty string normalises to None.
        configure_sdk_model("")
        try:
            captured_kwargs: dict[str, Any] = {}

            async def gen(**kwargs: Any) -> Any:
                captured_kwargs.update(kwargs)
                yield _result_msg("ok")

            with patch("maestro.sdk._query", side_effect=gen):
                await query(prompt="test", options=QueryOptions(effort="high"))

            assert "options" in captured_kwargs
            # When no override is configured, model should be None in
            # the forwarded ClaudeAgentOptions (SDK picks its default).
            assert captured_kwargs["options"].model is None
        finally:
            configure_sdk_model(None)

    @pytest.mark.asyncio
    async def test_query_options_output_format_forwarded(self) -> None:
        """QueryOptions.output_format should be forwarded to ClaudeAgentOptions."""
        captured_kwargs: dict[str, Any] = {}
        schema = {"type": "json_schema", "schema": {"type": "object"}}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(output_format=schema))

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].output_format == schema

    @pytest.mark.asyncio
    async def test_structured_output_propagated(self) -> None:
        """structured_output from ResultMessage should appear in QueryResult.

        The packing forwards the SDK value only when a schema was requested, so
        the query must set ``output_format`` (#2533).
        """
        data = {"parsed": True}
        schema = {"type": "json_schema", "schema": {"type": "object"}}

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg("", structured_output=data)

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(
                prompt="test", options=QueryOptions(output_format=schema),
            )

        assert result.structured_output == data

    @pytest.mark.asyncio
    async def test_structured_output_unset_without_output_format(self) -> None:
        """A no-schema call leaves ``structured_output`` unset, not a phantom None.

        Without an ``output_format`` the packing must not forward the SDK's
        ``structured_output`` — otherwise an ordinary text call would acquire a
        ``None`` the sentinel misreads as a validated ``null`` (#2533).
        """
        async def gen(**kwargs: Any) -> Any:
            yield _result_msg("some text")

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test", options=QueryOptions())

        assert result.structured_output is sdk_module._STRUCTURED_UNSET

    @pytest.mark.asyncio
    async def test_structured_null_requested_turn_is_productive(
        self, mem_fs: Any,
    ) -> None:
        """A requested-schema turn producing a validated ``null`` is productive.

        Regression for #2533: a completed, non-error turn that requested
        structured output and produced a validated ``null`` (empty text, no tool
        calls, ``ResultMessage.structured_output`` left at ``None``) must not be
        misclassified as an unproductive ``STREAM_STALL``; the honored ``null``
        is returned by ``parse_json``.
        """
        from maestro.sdk import _consume_messages, _finalize_usage

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg("", structured_output=None)

        with patch("asyncio.sleep", new=AsyncMock()):
            cr = await _consume_messages(
                gen(), "planner", None, structured_output_requested=True,
            )
        result = _finalize_usage(cr, None)
        assert result.structured_output is None
        assert result.parse_json() is None

    @pytest.mark.asyncio
    async def test_unproductive_no_schema_turn_still_stalls(
        self, mem_fs: Any,
    ) -> None:
        """A textless, tool-less turn with no schema requested still stalls.

        The request-aware guard must not swallow a genuinely unproductive
        completion on the ordinary (no ``output_format``) path (#2533).
        """
        from maestro.sdk import _consume_messages

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg("", structured_output=None)

        with (
            patch("asyncio.sleep", new=AsyncMock()),
            pytest.raises(SDKError) as exc_info,
        ):
            await _consume_messages(
                gen(), "planner", None, structured_output_requested=False,
            )
        assert exc_info.value.category == ErrorCategory.STREAM_STALL

    @pytest.mark.asyncio
    async def test_mcp_servers_forwarded(self) -> None:
        """QueryOptions.mcp_servers should be forwarded to ClaudeAgentOptions."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        mcp = {"github": {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "x"}}}
        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(mcp_servers=mcp))

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].mcp_servers == mcp

    @pytest.mark.asyncio
    async def test_new_options_none_only_sets_stderr(self) -> None:
        """New QueryOptions fields that are None should not be forwarded,
        but stderr callback and default disallowed_tools are always present."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions())

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].stderr is not None
        assert captured_kwargs["options"].disallowed_tools == ["Agent", "Monitor"]

    @pytest.mark.asyncio
    async def test_agent_tool_disallowed_by_default(self) -> None:
        """Agent tool must be blocked by default to prevent sub-agent
        fan-out that exhausts rate limits."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(
                prompt="test",
                options=QueryOptions(allowed_tools=["Read"]),
            )

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].disallowed_tools == ["Agent", "Monitor"]

    @pytest.mark.asyncio
    async def test_disallowed_tools_overridable(self) -> None:
        """Callers can pass disallowed_tools=[] to allow everything."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(
                prompt="test",
                options=QueryOptions(disallowed_tools=[]),
            )

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].disallowed_tools == ["Monitor"]

    @pytest.mark.asyncio
    async def test_custom_disallowed_tools_forwarded(self) -> None:
        """A custom disallowed_tools list should be forwarded as-is."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(
                prompt="test",
                options=QueryOptions(disallowed_tools=["Agent", "Bash"]),
            )

        assert "options" in captured_kwargs
        assert captured_kwargs["options"].disallowed_tools == ["Agent", "Bash", "Monitor"]

    # -- from test_sdk_query.py --

    @pytest.mark.asyncio
    async def test_returns_last_result_message(self) -> None:
        """query() consumes the async generator and returns the last ResultMessage."""
        msg = _result_msg("final answer")

        async def gen(**kwargs: Any) -> Any:
            yield msg

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test")

        assert result.response == "final answer"

    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        """query() returns empty response when no ResultMessage is yielded."""
        async def empty_gen(**kwargs: Any) -> Any:
            return
            yield  # make it an async generator

        with patch("maestro.sdk._query", side_effect=empty_gen):
            result = await query(prompt="test")

        assert result.response == ""

    @pytest.mark.asyncio
    async def test_none_result(self) -> None:
        """A ResultMessage with ``result=None`` and no productive tool calls
        is unproductive — the broad aggregate check classifies it as
        STREAM_STALL so the iteration-level resume-in-place path retries on
        a fresh session rather than returning an empty response. Was
        historically asserted as ``response == ""``; the broaden-detection
        evolution moved that case under STREAM_STALL."""
        msg = _result_msg(result=None)  # type: ignore[arg-type]

        async def gen(**kwargs: Any) -> Any:
            yield msg

        with patch("maestro.sdk._query", side_effect=gen), \
             pytest.raises(SDKError) as ei:
            await query(prompt="test")

        assert ei.value.category is ErrorCategory.STREAM_STALL
        assert "unproductive" in str(ei.value)

    # -- from test_bug_hunt14.py TestBug98QueryIgnoresResultMessageUsage --

    @pytest.mark.asyncio
    async def test_total_tokens_from_result_message_usage(self) -> None:
        """Bug 98: query() should use ResultMessage.usage for token counts
        when no TaskProgressMessage is received."""
        result_msg_real = ResultMessage(
            subtype="result",
            duration_ms=500,
            duration_api_ms=400,
            is_error=False,
            num_turns=1,
            session_id="test-session",
            total_cost_usd=0.01,
            usage={"total_tokens": 1500, "tool_uses": 3, "duration_ms": 500},
            result="Hello",
        )

        async def mock_query_gen(**kwargs: Any) -> Any:
            yield result_msg_real

        with patch("maestro.sdk._query", side_effect=mock_query_gen), \
             patch("maestro.sdk._sdk_log_dir", None):
            result = await query("test prompt")

        assert result.usage.total_tokens == 1500
        assert result.usage.tool_uses == 3

    @pytest.mark.asyncio
    async def test_drains_generator_past_result_message(self) -> None:
        """Regression for PR #1299 deploy rollback 2026-04-21.

        Stateless ``query()`` must drain the underlying async generator to
        ``StopAsyncIteration`` so the SDK's internal anyio scope finalizes
        inside the caller's task. Breaking early on ``ResultMessage`` leaves
        the generator for GC, whose ``async_generator_athrow`` cleanup
        finalizes the scope from whatever task is live at GC time — leaking
        ``CancelledError`` into unrelated awaits (observed aborting a
        Telegram status-update during the next iteration's startup).
        """
        drained = asyncio.Event()

        async def gen(**_kwargs: Any) -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="ok")],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("ok")
            drained.set()

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="hi")

        assert result.response == "ok"
        assert drained.is_set(), (
            "Generator was broken out of prematurely; SDK anyio scope will "
            "leak cancels on GC."
        )

    @pytest.mark.asyncio
    async def test_complete_on_early_exit_closes_generator_in_caller_task(
        self,
    ) -> None:
        """Regression for the same lifecycle invariant as PR #1299, on the
        ``complete_on`` early-exit path (sdk.py:1452 ``break``).

        The drain path proves the generator finishes inside the caller's
        task. The early-exit path can't drain — the predicate fired and we
        ``break``. The fix hoists ``_query()`` into a local and calls
        ``await gen.aclose()`` in a ``finally`` so the SDK's anyio scope
        teardown runs synchronously here. Without it the generator survives
        until GC, whose ``async_generator_athrow`` finalizes the scope from
        whatever task is alive at GC time and leaks ``CancelledError`` into
        an unrelated ``await`` (observed crashing ``review_artifacts``).
        """
        closed_in_caller_task = asyncio.Event()
        caller_task: asyncio.Task[Any] | None = None

        async def gen(**_kwargs: Any) -> Any:
            nonlocal caller_task
            caller_task = asyncio.current_task()
            try:
                yield AssistantMessage(
                    content=[TextBlock(text="enough")],
                    model="claude-sonnet-4-20250514",
                )
                # Would arrive after the early-exit ``break`` — generator
                # is suspended here when aclose() throws GeneratorExit.
                yield _result_msg("ok")  # pragma: no cover
            except GeneratorExit:
                assert asyncio.current_task() is caller_task, (
                    "aclose() must run in the caller's task — that is "
                    "the whole point of the fix"
                )
                closed_in_caller_task.set()
                raise

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(
                prompt="hi",
                complete_on=lambda _m, buf: bool(buf),
            )

        assert result.response == "enough"
        assert closed_in_caller_task.is_set(), (
            "Generator was not closed inside the caller's task; SDK anyio "
            "scope teardown will leak CancelledError on GC from an "
            "unrelated task."
        )

    @pytest.mark.asyncio
    async def test_complete_on_early_exit_drains_residual_athrow_tasks(
        self,
    ) -> None:
        """Regression for #1648.

        ``aclose()`` on the early-exit path can leave residual
        ``async_generator_athrow`` tasks pending in the loop (the SDK's
        anyio cancel-scope teardown spawns child athrow tasks for inner
        generators that complete asynchronously after ``aclose()`` returns).
        Without a drain, those pending tasks fire ``CancelledError`` into
        whatever ``await`` is alive at the moment of finalization — in
        production, the next phase's ``client.connect``. ``query()`` must
        drain them inside the caller's task before returning so the cancel
        cannot leak forward across phases.
        """
        from maestro.sdk import _collect_athrow_tasks

        spawned_child: dict[str, asyncio.Task[Any]] = {}

        async def gen(**_kwargs: Any) -> Any:
            try:
                yield AssistantMessage(
                    content=[TextBlock(text="enough")],
                    model="claude-sonnet-4-20250514",
                )
                yield _result_msg("ok")  # pragma: no cover — early-exit
            except GeneratorExit:
                # Spawn a residual athrow-shaped task during aclose, mirroring
                # the SDK anyio teardown that motivates this fix. The child
                # yields once before re-raising so it is observable in the
                # async_generator_athrow state when the drain begins.
                async def child() -> Any:
                    try:
                        yield 1
                    except GeneratorExit:
                        await asyncio.sleep(0)
                        raise

                inner = child()
                await inner.__anext__()
                spawned_child["task"] = asyncio.create_task(inner.aclose())
                raise

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(
                prompt="hi",
                complete_on=lambda _m, buf: bool(buf),
            )

        assert result.response == "enough"
        assert "task" in spawned_child, (
            "Test setup: residual athrow task was never spawned during aclose."
        )
        assert spawned_child["task"].done(), (
            "Residual athrow task was not complete on query() return; the "
            "drain did not await it (scheduler-ordering luck would not pin "
            "this contract)."
        )
        assert _collect_athrow_tasks() == [], (
            "Residual async_generator_athrow tasks survived query() return; "
            "drain in finally did not run or did not wait for completion. "
            "These pending tasks would fire CancelledError into the next "
            "phase's await (#1648)."
        )

    @pytest.mark.asyncio
    async def test_residual_athrow_drain_does_not_mask_consume_failure(
        self,
    ) -> None:
        """The drain ``await`` in ``query()``'s ``finally`` must not poison
        the existing ``BaseException`` containment at ``maestro/sdk.py:2099``.

        If ``_consume_messages`` raises while a residual athrow task is
        pending, the drain must wait for it without re-raising — the
        original exception, not a drain-induced ``CancelledError``, must
        surface.
        """
        async def child() -> Any:
            try:
                yield 1
            except GeneratorExit:
                await asyncio.sleep(0)
                raise

        inner = child()
        await inner.__anext__()
        athrow_task = asyncio.create_task(inner.aclose())

        async def gen(**_kwargs: Any) -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="x")],
                model="claude-sonnet-4-20250514",
            )
            raise ValueError("consume boom")

        try:
            with patch("maestro.sdk._query", side_effect=gen), \
                 pytest.raises(SDKError) as exc_info:
                await query(prompt="hi")
        finally:
            with contextlib.suppress(
                asyncio.CancelledError, GeneratorExit, RuntimeError,
            ):
                await athrow_task

        # Original exception preserved: the drain did not swallow or
        # transmute the ValueError into a CancelledError or anything else.
        assert isinstance(exc_info.value.original, ValueError)
        assert "consume boom" in str(exc_info.value.original)
        assert not isinstance(
            exc_info.value.original, asyncio.CancelledError,
        )


# ---------------------------------------------------------------------------
# _is_transient
# ---------------------------------------------------------------------------


class TestIsTransient:
    @pytest.mark.parametrize(
        "exc, expected",
        [
            (RuntimeError("Command failed with exit code 1"), True),
            (Exception("Command failed with exit code 1 (exit code: 1)"), True),
            (ConnectionError("connection reset"), True),
            (BrokenPipeError(), True),
            (RuntimeError("authentication failed exit code 1"), False),
            (RuntimeError("invalid api key exit code 1"), False),
            (RuntimeError("permission denied exit code 1"), False),
            (RuntimeError("Bash tool permission denied exit code 1"), True),
            (ValueError("bad json"), False),
            (RuntimeError("something else"), False),
            (TypeError("wrong type"), False),
        ],
        ids=[
            "exit_code_runtime",
            "exit_code_base_exception",
            "connection_error",
            "broken_pipe",
            "authentication_failed_keyword",
            "invalid_api_key_keyword",
            "permission_denied_keyword",
            "tool_deny_is_not_auth",
            "value_error",
            "other_runtime",
            "type_error",
        ],
    )
    def test_classification(self, exc: Exception, expected: bool) -> None:
        assert _is_transient(exc) is expected

    def test_connection_error_with_auth_in_hostname(self) -> None:
        """ConnectionError with 'auth' in hostname should be transient (network error)."""
        exc = ConnectionError("Failed to connect to auth.claude.ai:443")
        assert _is_transient(exc) is True

    def test_connection_error_with_auth_server_timeout(self) -> None:
        """ConnectionError mentioning 'auth server' should be transient."""
        exc = ConnectionError("auth server timeout")
        assert _is_transient(exc) is True

    def test_oserror_with_connection_errno(self) -> None:
        exc = OSError(errno.ECONNREFUSED, "Connection refused")
        assert _is_transient(exc) is True

    def test_oserror_with_pipe_errno(self) -> None:
        exc = OSError(errno.EPIPE, "Broken pipe")
        assert _is_transient(exc) is True

    def test_oserror_with_unrelated_errno(self) -> None:
        exc = OSError(errno.ENOENT, "No such file")
        assert _is_transient(exc) is False

    def test_internal_cancel_timeout_is_transient(self) -> None:
        """TimeoutError from contained internal anyio cancel is transient."""
        exc = TimeoutError(
            "SDK call cancelled by internal scope: "
            "Cancelled via cancel scope 0x7faf861ef410; reason: deadline exceeded",
        )
        assert _is_transient(exc) is True

    def test_plain_timeout_not_transient(self) -> None:
        """A regular TimeoutError (not from cancel containment) is not transient."""
        assert _is_transient(TimeoutError("connection timed out")) is False

    def test_structured_exit_code_1_is_transient(self) -> None:
        """ProcessError with exit_code=1 is transient (generic failure, worth retrying)."""
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError("subprocess died", exit_code=1)
        assert _is_transient(exc) is True

    @pytest.mark.parametrize("code", [2, 126, 127, 137])
    def test_structured_non_transient_exit_codes(self, code: int) -> None:
        """Permanent-failure exit codes skip retries."""
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError("cli failed", exit_code=code)
        assert _is_transient(exc) is False

    def test_structured_exit_code_preempts_string_fallback(self) -> None:
        """Structured exit_code takes priority over 'exit code' keyword in message.

        The ProcessError message contains 'exit code: 137' (auto-appended by the
        SDK), but exit_code=137 must classify as non-transient regardless.
        """
        from claude_agent_sdk._errors import ProcessError

        exc = ProcessError("oom killed", exit_code=137)
        assert "exit code" in str(exc).lower()
        assert _is_transient(exc) is False

    def test_cli_not_found_is_not_transient(self) -> None:
        """Missing CLI binary is a permanent install/config failure — no retry."""
        from claude_agent_sdk._errors import CLINotFoundError

        assert _is_transient(CLINotFoundError()) is False

    def test_message_parse_error_is_not_transient(self) -> None:
        """SDK/CLI protocol mismatch is permanent — no retry."""
        from claude_agent_sdk._errors import MessageParseError

        assert _is_transient(MessageParseError("unknown type", None)) is False

    def test_cli_connection_error_is_transient(self) -> None:
        """CLIConnectionError is network-ish and worth retrying."""
        from claude_agent_sdk._errors import CLIConnectionError

        assert _is_transient(CLIConnectionError("connection dropped")) is True


# ---------------------------------------------------------------------------
# _extract_error_detail
# ---------------------------------------------------------------------------


class TestExtractErrorDetail:
    def test_plain_exception(self) -> None:
        exc = ValueError("bad input")
        result = _extract_error_detail(exc)
        assert result == "ValueError: bad input"

    def test_exception_with_stderr_bytes(self) -> None:
        exc = RuntimeError("failed")
        exc.stderr = b"some error output\n"  # type: ignore[attr-defined]
        result = _extract_error_detail(exc)
        assert "RuntimeError: failed" in result
        assert "stderr: some error output" in result

    def test_exception_with_stderr_string(self) -> None:
        exc = RuntimeError("failed")
        exc.stderr = "string stderr"  # type: ignore[attr-defined]
        result = _extract_error_detail(exc)
        assert "stderr: string stderr" in result

    def test_exception_with_empty_stderr(self) -> None:
        exc = RuntimeError("failed")
        exc.stderr = ""  # type: ignore[attr-defined]
        result = _extract_error_detail(exc)
        assert result == "RuntimeError: failed"
        assert "stderr" not in result

    def test_exception_with_none_stderr(self) -> None:
        exc = RuntimeError("failed")
        exc.stderr = None  # type: ignore[attr-defined]
        result = _extract_error_detail(exc)
        assert result == "RuntimeError: failed"
