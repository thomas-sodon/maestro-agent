"""Tests for maestro.sdk — QueryResult and helpers.

``query()`` is covered in ``test_sdk_query.py`` and ``test_sdk_retry.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from maestro.sdk import (
    QueryResult,
    SDKError,
    SessionLogger,
    _truncate,
    configure_sdk_logging,
    configure_sdk_transcripts,
    message_has_tool_use,
    query,
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


class TestParseJson:
    def test_plain_json(self) -> None:
        result = QueryResult(response='{"key": "value"}')
        assert result.parse_json() == {"key": "value"}

    def test_json_in_code_block(self) -> None:
        result = QueryResult(
            response='Here is the update:\n```json\n{"key": "value"}\n```\nDone.',
        )
        assert result.parse_json() == {"key": "value"}

    def test_json_in_untagged_code_block(self) -> None:
        result = QueryResult(
            response='Result:\n```\n{"a": 1}\n```',
        )
        assert result.parse_json() == {"a": 1}

    def test_json_surrounded_by_text(self) -> None:
        result = QueryResult(
            response='Sure, here you go: {"ready": true, "reasoning": "yes"} hope that helps!',
        )
        assert result.parse_json() == {"ready": True, "reasoning": "yes"}

    def test_json_array(self) -> None:
        result = QueryResult(response='[{"step": "do it"}]')
        assert result.parse_json() == [{"step": "do it"}]

    def test_json_array_in_text(self) -> None:
        result = QueryResult(
            response='Here are the steps:\n[{"step": "a"}, {"step": "b"}]\nEnjoy!',
        )
        assert result.parse_json() == [{"step": "a"}, {"step": "b"}]

    def test_no_json_raises(self) -> None:
        result = QueryResult(response="No JSON here, just thoughts.")
        with pytest.raises(json.JSONDecodeError):
            result.parse_json()

    def test_empty_response_raises(self) -> None:
        result = QueryResult(response="")
        with pytest.raises(json.JSONDecodeError):
            result.parse_json()

    def test_object_with_nested_array_not_misextracted(self) -> None:
        """When an object contains arrays, parse_json must return the outer object,
        not the inner array.  This catches the bug where trying '[' before '{'
        in the last-resort extraction returns a nested array instead of the
        enclosing object."""
        result = QueryResult(
            response='Here is the plan: {"title": "Fix bug", "changes": ["a", "b"]} done!',
        )
        parsed = result.parse_json()
        assert isinstance(parsed, dict), (
            f"Expected dict, got {type(parsed).__name__}: {parsed}"
        )
        assert parsed["title"] == "Fix bug"
        assert parsed["changes"] == ["a", "b"]

    # -- from test_sdk_query.py TestParseJsonEdgeCases --

    def test_bracket_with_invalid_json_falls_through(self) -> None:
        """When brackets are found but content isn't valid JSON, continue to next."""
        result = QueryResult(response='text [not json] more {"key": "val"}')
        assert result.parse_json() == {"key": "val"}

    def test_only_invalid_brackets_raises(self) -> None:
        """When brackets are found but none contain valid JSON, raise."""
        result = QueryResult(response="some [not json] and {also not json}")
        with pytest.raises(json.JSONDecodeError):
            result.parse_json()

    # -- from test_bug_hunt.py --

    def test_whitespace_only_raises(self) -> None:
        """parse_json should raise JSONDecodeError on whitespace-only response."""
        result = QueryResult(response="   \n\n  ")
        with pytest.raises(json.JSONDecodeError):
            result.parse_json()

    def test_code_block_no_trailing_newline(self) -> None:
        """parse_json should handle code blocks where JSON is on last line before ```."""
        result = QueryResult(response='Result:\n```json\n{"key": "value"}```')
        assert result.parse_json() == {"key": "value"}

    # -- from test_bug_hunt_purpose_vcs.py --

    def test_multiple_separate_objects(self) -> None:
        """When response has multiple separate JSON objects, parse_json should
        still extract a valid one instead of raising JSONDecodeError."""
        result = QueryResult(
            response='Small: {"a": 1} and big: {"b": 2, "c": 3, "d": 4}',
        )
        data = result.parse_json()
        assert isinstance(data, dict)

    # -- from test_bug_hunt4.py TestParseJsonCodeBlockFallthrough --

    def test_invalid_json_in_code_block_with_valid_json_outside(self) -> None:
        """Bug 37: When a code block contains invalid JSON, parse_json should
        fall through to the bracket extraction fallback."""
        text = 'Here: ```json\nnot valid\n``` but also {"key": "value"}'
        result = QueryResult(response=text)
        assert result.parse_json() == {"key": "value"}

    def test_invalid_json_in_code_block_no_fallback(self) -> None:
        """Bug 37: When code block has invalid JSON and no valid JSON outside, raise."""
        text = '```json\nnot valid json\n```'
        result = QueryResult(response=text)
        with pytest.raises(json.JSONDecodeError):
            result.parse_json()

    # -- structured_output support --

    def test_structured_output_returned_directly(self) -> None:
        """When structured_output is set, parse_json returns it without text parsing."""
        data = {"key": "value", "count": 42}
        result = QueryResult(response="", structured_output=data)
        assert result.parse_json() == data

    def test_structured_output_takes_precedence(self) -> None:
        """structured_output takes precedence over response text."""
        result = QueryResult(
            response='{"wrong": true}',
            structured_output={"right": True},
        )
        assert result.parse_json() == {"right": True}

    def test_structured_output_unset_falls_back_to_text(self) -> None:
        """When structured_output is unset, parse_json falls back to text parsing.

        The default is now ``_STRUCTURED_UNSET`` (omitted here), not ``None`` —
        an explicit ``None`` means "produced a validated null" (#2533).
        """
        result = QueryResult(response='{"key": "value"}')
        assert result.parse_json() == {"key": "value"}

    def test_structured_output_produced_null_is_honored(self) -> None:
        """An explicit ``None`` is a validated ``null`` and is returned as-is.

        Regression for #2533: previously ``parse_json`` treated a produced
        ``None`` as "not set" and re-parsed the (non-null) response text; it must
        instead honor the validated ``null`` and return ``None``.
        """
        result = QueryResult(response='{"ignored": true}', structured_output=None)
        assert result.parse_json() is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result_msg(result: str = "done", **overrides: Any) -> ResultMessage:
    """Create a ResultMessage with sensible defaults."""
    defaults: dict[str, Any] = {
        "subtype": "result",
        "duration_ms": 100,
        "duration_api_ms": 80,
        "is_error": False,
        "num_turns": 1,
        "session_id": "s1",
        "result": result,
    }
    defaults.update(overrides)
    return ResultMessage(**defaults)


async def _fake_query(*messages: Any) -> Any:
    """Return an async-generator factory that yields *messages* in order."""
    async def _gen(**kwargs: Any) -> Any:
        for msg in messages:
            yield msg
    return _gen


# ---------------------------------------------------------------------------
# _coerce_int
# ---------------------------------------------------------------------------

class TestCoerceInt:
    def test_none_returns_zero(self) -> None:
        from maestro.sdk import _coerce_int

        assert _coerce_int(None) == 0

    def test_int_passthrough(self) -> None:
        from maestro.sdk import _coerce_int

        assert _coerce_int(42) == 42

    def test_string_int_coerced(self) -> None:
        from maestro.sdk import _coerce_int

        assert _coerce_int("17") == 17

    def test_unparseable_returns_zero(self) -> None:
        from maestro.sdk import _coerce_int

        assert _coerce_int("nope") == 0

    def test_typeerror_returns_zero(self) -> None:
        from maestro.sdk import _coerce_int

        assert _coerce_int(object()) == 0


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_text(self) -> None:
        assert _truncate("hello", 10) == "hello"

    def test_exact_length(self) -> None:
        assert _truncate("12345", 5) == "12345"

    def test_long_text(self) -> None:
        text = "a" * 100
        result = _truncate(text, 50)
        assert len(result) <= 50
        assert "[100 chars total]" in result

    def test_long_text_very_short_limit(self) -> None:
        """When max_len is too small for the suffix, just truncate."""
        text = "a" * 100
        result = _truncate(text, 10)
        assert len(result) <= 10
        assert result == "a" * 10

    # -- from test_bug_hunt9.py TestBug57TruncateMaxLenZero --

    def test_max_len_zero_returns_empty(self) -> None:
        """Bug 57: max_len=0 should return empty string."""
        result = _truncate("hello world", max_len=0)
        assert result == ""

    def test_negative_max_len_returns_empty(self) -> None:
        """Bug 57/39: Negative max_len should return empty string."""
        result = _truncate("hello world", max_len=-5)
        assert result == ""

    # -- from test_bug_hunt_batch10.py TestSdkTruncateZeroContent --

    def test_small_max_len_keeps_content(self) -> None:
        """Bug: sdk._truncate must include content, not just suffix."""
        result = _truncate("Hello world this is long text", max_len=20)
        assert len(result) <= 20
        assert result[0] == "H"

    # -- from test_bug_hunt_batch3.py TestSdkTruncate --

    def test_output_not_longer_than_max_len(self) -> None:
        """Bug 15: _truncate must not produce output longer than max_len."""
        text = "a" * 1000
        result = _truncate(text, 50)
        assert len(result) <= 50

    def test_small_max_len_within_limit(self) -> None:
        """Bug 15: Even small max_len must be respected."""
        text = "a" * 100
        result = _truncate(text, 10)
        assert len(result) <= 10

    def test_max_len_greater_than_text(self) -> None:
        text = "hello"
        result = _truncate(text, 100)
        assert result == "hello"

    def test_truncated_has_ellipsis_indicator(self) -> None:
        text = "a" * 1000
        result = _truncate(text, 50)
        assert "..." in result or "truncated" in result.lower() or len(result) < len(text)


# ---------------------------------------------------------------------------
# message_has_tool_use()
# ---------------------------------------------------------------------------

class TestMessageHasToolUse:
    def test_true_when_assistant_carries_tool_use(self) -> None:
        message = AssistantMessage(
            content=[
                TextBlock(text="let me check"),
                ToolUseBlock(id="tu1", name="read_journal", input={}),
            ],
            model="claude-sonnet-4-20250514",
        )
        assert message_has_tool_use(message) is True

    def test_false_for_text_only_assistant(self) -> None:
        message = AssistantMessage(
            content=[TextBlock(text="just prose")],
            model="claude-sonnet-4-20250514",
        )
        assert message_has_tool_use(message) is False

    def test_false_for_empty_content(self) -> None:
        message = AssistantMessage(content=[], model="claude-sonnet-4-20250514")
        assert message_has_tool_use(message) is False

    def test_false_for_non_assistant_message(self) -> None:
        user = UserMessage(
            content=[ToolResultBlock(tool_use_id="tu1", content="result")],
        )
        assert message_has_tool_use(user) is False


# ---------------------------------------------------------------------------
# SessionLogger
# ---------------------------------------------------------------------------


class TestSessionLogger:
    def test_creates_file(self, mem_fs: dict[str, str]) -> None:
        sdk_dir = Path("/fake/sdk")
        sl = SessionLogger(sdk_dir, caller="test")
        sl.log("test message")
        sl.close()

        # Find the written file in mem_fs
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "SDK session started" in content
        assert "test message" in content
        assert "SDK session ended" in content

    def test_path_format(self, mem_fs: dict[str, str]) -> None:
        sdk_dir = Path("/fake/sdk")
        sl = SessionLogger(sdk_dir, caller="test")
        # Path should be sdk_dir / YYYY-MM-DD / HH-MM-SS_<id>.log
        assert sl.path.suffix == ".log"
        assert sl.path.parent.parent == sdk_dir
        sl.close()

    def test_close_is_idempotent(self, mem_fs: dict[str, str]) -> None:
        sdk_dir = Path("/fake/sdk")
        sl = SessionLogger(sdk_dir, caller="test")
        sl.close()
        sl.close()  # Should not raise

    def test_context_manager(self, mem_fs: dict[str, str]) -> None:
        sdk_dir = Path("/fake/sdk")
        with SessionLogger(sdk_dir, caller="test") as sl:
            sl.log("inside context")

        # File should be closed after exiting context
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "inside context" in content
        assert "SDK session ended" in content

    # -- from test_bug_hunt4.py TestSessionLoggerLogAfterClose --

    def test_log_after_close_should_not_raise(self, mem_fs: dict[str, str]) -> None:
        """Bug 42: SessionLogger.log() on a closed file should not raise."""
        sdk_dir = Path("/fake/sdk_logs42")
        sl = SessionLogger(sdk_dir, caller="test")
        sl.close()
        sl.log("message after close")

    # -- from test_bug_hunt_batch10.py TestSessionLoggerWriteError --

    def test_write_error_suppressed(self, mem_fs: dict[str, str]) -> None:
        """Bug: SessionLogger.log must handle write errors gracefully."""
        with patch("maestro.sdk.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.strftime.side_effect = lambda fmt: (
                "2025-01-01" if "Y" in fmt else "12-00-00"
            )
            mock_now.isoformat.return_value = "2025-01-01T12:00:00"
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: mock_dt

            session = SessionLogger(Path("/tmp/sdk"), caller="test")

        # Make _file.write raise an IOError
        session._file = MagicMock()
        session._file.closed = False
        session._file.write.side_effect = OSError("disk full")

        # Should not raise
        session.log("test message")


class TestConfigureSdkLogging:
    def test_sets_global(self, monkeypatch: Any) -> None:
        import maestro.sdk as sdk_mod
        monkeypatch.setattr(sdk_mod, "_sdk_log_dir", None)
        configure_sdk_logging(Path("/tmp/sdk"))
        assert sdk_mod._sdk_log_dir == Path("/tmp/sdk")


class TestConfigureSdkTranscripts:
    def test_sets_global(self, monkeypatch: Any) -> None:
        """``configure_sdk_transcripts`` toggles the module-level flag."""
        import maestro.sdk as sdk_mod
        monkeypatch.setattr(sdk_mod, "_structured_transcripts_enabled", False)
        configure_sdk_transcripts(True)
        assert sdk_mod._structured_transcripts_enabled is True
        configure_sdk_transcripts(False)
        assert sdk_mod._structured_transcripts_enabled is False


class TestOpenSessionLogger:
    def test_none_when_logging_unconfigured(self, monkeypatch: Any) -> None:
        """No configured log dir means no logger — the caller writes nothing."""
        import maestro.sdk as sdk_mod
        monkeypatch.setattr(sdk_mod, "_sdk_log_dir", None)
        assert sdk_mod.open_session_logger("evolve") is None


class TestQuerySessionLogging:
    @pytest.mark.asyncio
    async def test_creates_session_log(self, mem_fs: dict[str, str]) -> None:
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_test")
        sdk_mod.configure_sdk_logging(sdk_dir)

        msg = _result_msg("hello", total_cost_usd=0.01)

        async def gen(**kwargs: Any) -> Any:
            yield msg

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test prompt")

        # A session log file should have been created
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "SDK session started" in content
        assert "Prompt: test prompt" in content
        assert "Query complete" in content
        assert "SDK session ended" in content

    @pytest.mark.asyncio
    async def test_no_session_log_when_unconfigured(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import maestro.sdk as sdk_mod

        monkeypatch.setattr(sdk_mod, "_sdk_log_dir", None)

        async def gen(**kwargs: Any) -> Any:
            yield _result_msg()

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test")

        # No session log files
        sdk_files = [
            k for k in mem_fs if "sdk" in k and k.endswith(".log")
        ]
        assert len(sdk_files) == 0

    @pytest.mark.asyncio
    async def test_session_log_closed_on_early_exception(
        self, mem_fs: dict[str, str],
    ) -> None:
        """If session_log.log() raises right after creation (before the try
        block), the file handle must still be closed."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_early")
        sdk_mod.configure_sdk_logging(sdk_dir)

        close_called = False
        original_close = SessionLogger.close

        def tracking_close(self: SessionLogger) -> None:
            nonlocal close_called
            close_called = True
            original_close(self)

        call_count = 0
        original_log = SessionLogger.log

        def exploding_log(self: SessionLogger, message: str) -> None:
            nonlocal call_count
            call_count += 1
            # Let the __init__ log succeed (call 1), blow up on the
            # Prompt log (call 2) which happens outside the try block.
            if call_count == 2:
                raise RuntimeError("log boom")
            original_log(self, message)

        with patch.object(SessionLogger, "log", exploding_log), \
             patch.object(SessionLogger, "close", tracking_close), \
             pytest.raises(RuntimeError, match="log boom"):
            await query(prompt="test")

        assert close_called, "SessionLogger.close() was never called"

    @pytest.mark.asyncio
    async def test_stderr_callback_writes_to_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        """When session logging is enabled and the SDK emits stderr via the
        callback, each line is mirrored into the per-session log file
        (sdk.py:503)."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_stderr_cb")
        sdk_mod.configure_sdk_logging(sdk_dir)

        async def gen(**kwargs: Any) -> Any:
            opts = kwargs.get("options")
            # Fire the stderr callback while the session log is active.
            assert opts is not None and opts.stderr is not None
            opts.stderr("warning: slow tool call")
            yield _result_msg("ok", total_cost_usd=0.0)

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="p")

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "CLI stderr: warning: slow tool call" in content

    @pytest.mark.asyncio
    async def test_stderr_dump_written_to_session_log_on_exception(
        self, mem_fs: dict[str, str],
    ) -> None:
        """On a non-transient SDK failure with collected stderr lines, the
        dump is written to the session log (sdk.py:659)."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_stderr_dump")
        sdk_mod.configure_sdk_logging(sdk_dir)

        async def gen(**kwargs: Any) -> Any:
            opts = kwargs.get("options")
            assert opts is not None and opts.stderr is not None
            opts.stderr("fatal: segfault in output handler")
            opts.stderr("trace: /tmp/core.1234")
            # ValueError is non-transient so retries do not mask this.
            raise ValueError("CLI crashed")
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             pytest.raises(SDKError):
            await query(prompt="p")

        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "CLI stderr dump:" in content
        assert "fatal: segfault in output handler" in content
        assert "trace: /tmp/core.1234" in content

    @pytest.mark.asyncio
    async def test_bytes_stderr_attribute_decoded_into_sdk_error(self) -> None:
        """When the original exception carries a ``bytes`` stderr attribute
        (and no stderr lines were collected via the callback, so the byte
        attribute is preserved), the decoded string propagates into
        ``SDKError.stderr`` (sdk.py:679)."""
        async def gen(**kwargs: Any) -> Any:
            # Do NOT call opts.stderr() — we want exc.stderr to remain bytes
            # through the except block without being overwritten by the joined
            # str form.
            exc = ValueError("fatal")
            exc.stderr = b"\xff\xfe binary payload"  # type: ignore[attr-defined]
            raise exc
            yield  # pragma: no cover

        with patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="p")

        # .stderr on the SDKError must be a str (decoded with replace), not bytes.
        sdk_err = exc_info.value
        assert isinstance(sdk_err.stderr, str)
        assert "binary payload" in sdk_err.stderr
        # The original exception's bytes attribute is still bytes.
        assert isinstance(sdk_err.original.stderr, bytes)  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_exception_logged_to_session(self, mem_fs: dict[str, str]) -> None:
        """When _query raises mid-stream, the exception should be in the session log."""
        import maestro.sdk as sdk_mod

        sdk_dir = Path("/fake/sdk_exc")
        sdk_mod.configure_sdk_logging(sdk_dir)

        async def gen(**kwargs: Any) -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="partial")],
                model="claude-sonnet-4-20250514",
            )
            # Use a non-transient exception so retries don't interfere.
            raise ValueError("bad model output")

        with patch("maestro.sdk._query", side_effect=gen), \
             pytest.raises(SDKError) as exc_info:
            await query(prompt="test")

        assert isinstance(exc_info.value.__cause__, ValueError)
        written = [k for k in mem_fs if k.startswith(str(sdk_dir))]
        assert len(written) == 1
        content = mem_fs[written[0]]
        assert "EXCEPTION (attempt 1): ValueError: bad model output" in content
        assert "SDK session ended" in content
