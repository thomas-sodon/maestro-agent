"""SDK telemetry, message consumption, usage finalisation and early-exit cost
estimation; split out of ``tests/test_sdk.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
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
    SessionLogger,
    configure_sdk_concurrency,
    configure_sdk_fallback_model,
    configure_sdk_task_budget,
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
# SDK telemetry — caller label, cache token capture, structured log lines
# ---------------------------------------------------------------------------


class TestSdkTelemetry:
    @pytest.mark.asyncio
    async def test_caller_recorded_on_usage(self) -> None:
        """QueryOptions.caller flows into QueryUsage.caller."""
        msg = _result_msg("ok")

        async def gen(**kwargs: Any) -> Any:
            yield msg

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(
                prompt="test", options=QueryOptions(caller="contemplate"),
            )

        assert result.usage.caller == "contemplate"

    @pytest.mark.asyncio
    async def test_caller_default_none(self) -> None:
        """When QueryOptions has no caller, usage.caller is None."""
        msg = _result_msg("ok")

        async def gen(**kwargs: Any) -> Any:
            yield msg

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query(prompt="test", options=QueryOptions())

        assert result.usage.caller is None

    @pytest.mark.asyncio
    async def test_caller_not_forwarded_to_sdk_options(self) -> None:
        """The caller label is telemetry only — must not flow into SDK options."""
        captured: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(caller="reflect"))

        # The SDK options object should not carry a 'caller' attr.
        opts = captured.get("options")
        assert opts is None or not hasattr(opts, "caller")

    @pytest.mark.asyncio
    async def test_cache_tokens_from_result_message(self) -> None:
        """Cache token splits are extracted from ResultMessage.usage."""
        result_msg = ResultMessage(
            subtype="result",
            duration_ms=100,
            duration_api_ms=80,
            is_error=False,
            num_turns=1,
            session_id="s1",
            total_cost_usd=0.05,
            usage={
                "total_tokens": 1500,
                "tool_uses": 0,
                "duration_ms": 100,
                "input_tokens": 200,
                "output_tokens": 300,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 800,
            },
            result="hi",
        )

        async def gen(**kwargs: Any) -> Any:
            yield result_msg

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query("test")

        assert result.usage.input_tokens == 200
        assert result.usage.output_tokens == 300
        assert result.usage.cache_creation_tokens == 1000
        assert result.usage.cache_read_tokens == 800

    @pytest.mark.asyncio
    async def test_cache_tokens_default_zero_when_missing(self) -> None:
        """Older mocks without cache keys still work — defaults are 0."""
        msg = _result_msg("ok")  # uses default usage dict (no cache fields)

        async def gen(**kwargs: Any) -> Any:
            yield msg

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query("test")

        assert result.usage.input_tokens == 0
        assert result.usage.output_tokens == 0
        assert result.usage.cache_creation_tokens == 0
        assert result.usage.cache_read_tokens == 0

    @pytest.mark.asyncio
    async def test_cache_tokens_from_progress_message(self) -> None:
        """Cache token splits also come from TaskProgressMessage.usage."""
        progress = TaskProgressMessage(
            subtype="task_progress",
            data={},
            task_id="t1",
            description="working",
            usage={
                "total_tokens": 500,
                "tool_uses": 1,
                "duration_ms": 200,
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 250,
                "cache_read_input_tokens": 100,
            },
            uuid="u1",
            session_id="s1",
            last_tool_name="Read",
        )

        async def gen(**kwargs: Any) -> Any:
            yield progress
            # Empty result.usage — verify progress values survive.
            yield ResultMessage(
                subtype="result",
                duration_ms=200,
                duration_api_ms=150,
                is_error=False,
                num_turns=1,
                session_id="s1",
                total_cost_usd=0.001,
                result="done",
            )

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query("test")

        assert result.usage.input_tokens == 100
        assert result.usage.cache_read_tokens == 100
        assert result.usage.cache_creation_tokens == 250

    @pytest.mark.asyncio
    async def test_multiple_progress_messages_last_snapshot_wins(self) -> None:
        """Progress usage is cumulative: the last snapshot wins, not the sum."""
        first = TaskProgressMessage(
            subtype="task_progress",
            data={},
            task_id="t1",
            description="working",
            usage={
                "total_tokens": 500,
                "tool_uses": 1,
                "duration_ms": 200,
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 250,
                "cache_read_input_tokens": 100,
            },
            uuid="u1",
            session_id="s1",
            last_tool_name="Read",
        )
        second = TaskProgressMessage(
            subtype="task_progress",
            data={},
            task_id="t1",
            description="working",
            usage={
                "total_tokens": 1200,
                "tool_uses": 2,
                "duration_ms": 500,
                "input_tokens": 300,
                "output_tokens": 120,
                "cache_creation_input_tokens": 250,
                "cache_read_input_tokens": 400,
            },
            uuid="u2",
            session_id="s1",
            last_tool_name="Read",
        )

        async def gen(**kwargs: Any) -> Any:
            yield first
            yield second
            # No usage on the ResultMessage — the progress snapshots survive.
            yield ResultMessage(
                subtype="result",
                duration_ms=500,
                duration_api_ms=400,
                is_error=False,
                num_turns=2,
                session_id="s1",
                total_cost_usd=0.002,
                result="done",
            )

        with patch("maestro.sdk._query", side_effect=gen):
            result = await query("test")

        # Last snapshot wins (plain assignment), not per-key accumulation.
        # The four per-key captures are guarded and are what a ``+=`` regression
        # would touch; an accumulating ``+=`` would yield 400/170/500/500 for
        # input/output/cache-creation/cache-read. These asserts pin the last
        # snapshot instead. ``total_tokens`` is captured by a separate
        # unconditional assignment, so it stays at the last snapshot (1200)
        # regardless — its assert confirms capture, not per-key accumulation.
        assert result.usage.input_tokens == 300
        assert result.usage.output_tokens == 120
        assert result.usage.cache_creation_tokens == 250
        assert result.usage.cache_read_tokens == 400
        assert result.usage.total_tokens == 1200

    @pytest.mark.asyncio
    async def test_structured_log_emitted_on_success(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One ``SDK call:`` log line is emitted per successful query."""
        msg = _result_msg(
            "ok",
            total_cost_usd=0.0042,
            usage={
                "total_tokens": 100,
                "tool_uses": 0,
                "duration_ms": 50,
                "input_tokens": 30,
                "output_tokens": 20,
                "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 0,
            },
        )

        async def gen(**kwargs: Any) -> Any:
            yield msg

        with caplog.at_level(logging.INFO, logger="maestro"), \
             patch("maestro.sdk._query", side_effect=gen):
            await query("test", options=QueryOptions(caller="contemplate"))

        sdk_lines = [r for r in caplog.records if r.message.startswith("SDK call:")]
        assert len(sdk_lines) == 1
        text = sdk_lines[0].getMessage()
        assert "caller=contemplate" in text
        assert "tokens_in=30" in text
        assert "tokens_out=20" in text
        assert "cache_read=70" in text
        assert "cost=0.0042" in text

    @pytest.mark.asyncio
    async def test_structured_log_emitted_when_no_result_message(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SDK call line is also emitted on the empty-stream code path."""
        async def gen(**kwargs: Any) -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="thinking")],
                model="claude-sonnet-4-20250514",
            )

        with caplog.at_level(logging.INFO, logger="maestro"), \
             patch("maestro.sdk._query", side_effect=gen):
            await query("test", options=QueryOptions(caller="distill_findings"))

        sdk_lines = [r for r in caplog.records if r.message.startswith("SDK call:")]
        assert len(sdk_lines) == 1
        assert "caller=distill_findings" in sdk_lines[0].getMessage()

    @pytest.mark.asyncio
    async def test_rate_limit_structured_log(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Rate limit events emit a structured ``SDK rate-limited:`` line."""
        rejected = RateLimitEvent(
            rate_limit_info=RateLimitInfo(status="rejected", resets_at=9999999999),
            uuid="u1",
            session_id="s1",
        )

        async def gen(**kwargs: Any) -> Any:
            yield rejected
            raise RuntimeError("upstream rate limit")

        with caplog.at_level(logging.WARNING, logger="maestro"), \
             patch("maestro.sdk._query", side_effect=gen), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.time", return_value=9999999980.0), \
             pytest.raises(SDKError):
            await query("test", options=QueryOptions(caller="reflect"))

        rl_lines = [
            r for r in caplog.records
            if r.getMessage().startswith("SDK rate-limited:")
        ]
        assert len(rl_lines) >= 1
        text = rl_lines[0].getMessage()
        assert "caller=reflect" in text
        assert "attempt=1" in text
        assert "resets_in_s=" in text


class TestCostTracking:
    """Tests for cost tracking integration in _log_sdk_call."""

    def test_configure_cost_tracking_sets_global(self) -> None:
        import maestro.sdk as _sdk
        from maestro.cost_tracking import JsonlCostSampleStore
        from maestro.sdk import configure_cost_tracking
        backend = JsonlCostSampleStore(Path("/tmp/test_cost.jsonl"))
        configure_cost_tracking(backend)
        assert _sdk._cost_sample_backend is backend

    def test_log_sdk_call_writes_cost_sample(self, mem_fs: dict[str, str]) -> None:
        from maestro.cost_tracking import JsonlCostSampleStore
        from maestro.sdk import QueryUsage, _log_sdk_call, configure_cost_tracking
        cost_path = Path("/fake/sdk/cost_samples.jsonl")
        configure_cost_tracking(JsonlCostSampleStore(cost_path))
        usage = QueryUsage(
            total_tokens=1200,
            tool_uses=1,
            duration_ms=2000,
            total_cost_usd=0.05,
            num_turns=1,
            input_tokens=1000,
            output_tokens=200,
            cache_creation_tokens=50,
            cache_read_tokens=100,
            caller="test_caller",
            attempts=1,
        )
        _log_sdk_call(usage)
        content = mem_fs[str(cost_path)]
        lines = content.strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["cost_usd"] == 0.05
        assert data["caller"] == "test_caller"
        assert data["input_tokens"] == 1000
        assert data["output_tokens"] == 200
        # No provider stamp on the hand-rolled usage → honest null.
        assert data["provider"] is None

    def test_log_sdk_call_persists_provider_stamp(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The producing provider rides into the cost sample (067 Step 2)."""
        from maestro.cost_tracking import JsonlCostSampleStore
        from maestro.sdk import QueryUsage, _log_sdk_call, configure_cost_tracking
        cost_path = Path("/fake/sdk/cost_samples.jsonl")
        configure_cost_tracking(JsonlCostSampleStore(cost_path))
        _log_sdk_call(QueryUsage(caller="evolve", provider="cli_paced"))
        data = json.loads(mem_fs[str(cost_path)].strip())
        assert data["provider"] == "cli_paced"

    def test_log_sdk_call_handles_oserror_on_write(self, mem_fs: dict[str, str]) -> None:
        from maestro.cost_tracking import JsonlCostSampleStore
        from maestro.sdk import QueryUsage, _log_sdk_call, configure_cost_tracking
        cost_path = Path("/fake/sdk/cost_samples.jsonl")
        configure_cost_tracking(JsonlCostSampleStore(cost_path))
        usage = QueryUsage(
            total_tokens=100,
            tool_uses=0,
            duration_ms=500,
            total_cost_usd=0.01,
            num_turns=1,
            input_tokens=80,
            output_tokens=20,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            caller="oserr",
            attempts=1,
        )
        with patch(
            "maestro.cost_tracking.append_cost_sample",
            side_effect=OSError("disk full"),
        ):
            # Should not raise — the OSError is caught and logged
            _log_sdk_call(usage)

    def test_log_sdk_call_handles_sqlite_error_on_write(self) -> None:
        """A SQLite backend's ``sqlite3.Error`` on append is swallowed too.

        Cost-sample telemetry is best-effort: now that the backend may be the
        SQLite store, ``_log_sdk_call`` catches ``sqlite3.Error`` alongside
        ``OSError`` so a DB write failure never crashes the SDK result path.
        """
        import sqlite3
        from unittest.mock import MagicMock

        from maestro.sdk import QueryUsage, _log_sdk_call, configure_cost_tracking
        backend = MagicMock()
        backend.append.side_effect = sqlite3.OperationalError("db is locked")
        configure_cost_tracking(backend)
        # Should not raise — the sqlite3.Error is caught and logged.
        _log_sdk_call(QueryUsage(caller="sqlite_err"))
        backend.append.assert_called_once()

    def test_log_sdk_call_succeeds_when_not_configured(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import maestro.sdk as _sdk
        from maestro.sdk import QueryUsage, _log_sdk_call
        monkeypatch.setattr(_sdk, "_cost_sample_backend", None)
        usage = QueryUsage(
            total_tokens=100,
            tool_uses=0,
            duration_ms=500,
            total_cost_usd=0.01,
            num_turns=1,
            input_tokens=80,
            output_tokens=20,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            caller="noop",
            attempts=1,
        )
        # Should not raise
        _log_sdk_call(usage)


class TestConcurrencySemaphore:
    """Tests for the shared concurrency semaphore in sdk.query()."""

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self) -> None:
        """With semaphore=1, two concurrent queries must serialize."""
        configure_sdk_concurrency(1)

        order: list[str] = []
        call_count = 0

        async def slow_query(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            my_id = str(call_count)
            order.append(f"start-{my_id}")
            await asyncio.sleep(0.05)
            order.append(f"end-{my_id}")
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=slow_query):
            await asyncio.gather(
                query(prompt="a"),
                query(prompt="b"),
            )

        # With semaphore=1 the second query cannot start until the first
        # finishes, so the order must be strictly sequential.
        assert order == ["start-1", "end-1", "start-2", "end-2"]

    @pytest.mark.asyncio
    async def test_semaphore_released_on_exception(self) -> None:
        """Semaphore must be released even when query() raises."""
        configure_sdk_concurrency(1)

        async def failing_query(**kwargs: Any) -> Any:
            raise RuntimeError("boom")
            yield  # pragma: no cover — make it an async generator

        with patch("maestro.sdk._query", side_effect=failing_query), \
             pytest.raises(SDKError):
            await query(prompt="fail")

        # Semaphore must be available for the next call.
        import maestro.sdk as _sdk
        assert _sdk._concurrency_semaphore is not None
        # Acquire should succeed immediately (not blocked).
        acquired = _sdk._concurrency_semaphore._value  # type: ignore[attr-defined]
        assert acquired == 1

    @pytest.mark.asyncio
    async def test_none_semaphore_allows_unlimited(self) -> None:
        """When semaphore is None, concurrent queries run freely."""
        assert sdk_module._concurrency_semaphore is None

        order: list[str] = []
        call_count = 0

        async def fast_query(**kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            my_id = str(call_count)
            order.append(f"start-{my_id}")
            await asyncio.sleep(0.05)
            order.append(f"end-{my_id}")
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=fast_query):
            await asyncio.gather(
                query(prompt="a"),
                query(prompt="b"),
            )

        # Without a semaphore both should start before either finishes.
        assert order[0] == "start-1"
        assert order[1] == "start-2"


class TestConcurrencySemaphoreLocalCapture:
    """The acquire/release pair must operate on the same semaphore object,
    even if the module-level ``_concurrency_semaphore`` is reassigned
    mid-flight (e.g. by ``configure_sdk_concurrency()`` or the test reset
    helper). Pre-fix, the ``finally:`` block read the global at release
    time, so a mid-flight rebind would release a fresh semaphore B while
    leaking a permit on the original A. See issue #1163.
    """

    @pytest.mark.asyncio
    async def test_query_releases_acquired_semaphore_not_rebound_global(
        self,
    ) -> None:
        import maestro.sdk as _sdk

        configure_sdk_concurrency(1)
        sem_a = _sdk._concurrency_semaphore
        assert sem_a is not None

        async def rebind_mid_flight(**kwargs: Any) -> Any:
            # Reassign the module-level semaphore to a brand-new instance B
            # in between the acquire (already happened) and the release
            # (in the outer finally:).
            configure_sdk_concurrency(1)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=rebind_mid_flight):
            await query(prompt="rebind")

        sem_b = _sdk._concurrency_semaphore
        assert sem_b is not None
        assert sem_b is not sem_a

        # The release must have landed on A (acquired permit returned), not B
        # (which was never acquired). _value is the count of available permits.
        assert sem_a._value == 1  # type: ignore[attr-defined]
        assert sem_b._value == 1  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_query_on_session_releases_acquired_semaphore_not_rebound_global(
        self,
    ) -> None:
        import maestro.sdk as _sdk
        from maestro.sdk import query_on_session

        configure_sdk_concurrency(1)
        sem_a = _sdk._concurrency_semaphore
        assert sem_a is not None

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive_rebind_mid_flight() -> Any:
            # Same race as above, applied to the session-attached path.
            configure_sdk_concurrency(1)
            yield _result_msg("ok")

        mock_client.receive_messages = receive_rebind_mid_flight

        await query_on_session(mock_client, "rebind", caller="t")

        sem_b = _sdk._concurrency_semaphore
        assert sem_b is not None
        assert sem_b is not sem_a

        assert sem_a._value == 1  # type: ignore[attr-defined]
        assert sem_b._value == 1  # type: ignore[attr-defined]


class TestNativeEffort:
    """Tests for SDK-native effort and adaptive-thinking passthrough."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    async def test_effort_forwarded_directly(self, effort: str) -> None:
        """Effort parameter is passed to ClaudeAgentOptions without manual thinking mapping."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(effort=effort))

        opts = captured_kwargs["options"]
        assert opts.effort == effort
        assert opts.thinking == {"type": "adaptive"}

    @pytest.mark.asyncio
    async def test_effort_sends_adaptive_thinking_not_budget(self) -> None:
        """An effort-bearing call turns thinking on via adaptive, never a budget."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(effort="high"))

        opts = captured_kwargs["options"]
        assert opts.thinking == {"type": "adaptive"}
        assert opts.max_thinking_tokens is None

    @pytest.mark.asyncio
    async def test_no_effort_sends_no_thinking_option(self) -> None:
        """When effort is None, neither thinking option is set."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions())

        opts = captured_kwargs["options"]
        assert opts.thinking is None
        assert opts.max_thinking_tokens is None

    @pytest.mark.asyncio
    async def test_session_with_effort_sends_adaptive_thinking(self) -> None:
        """Session creation with effort carries adaptive thinking too."""
        from maestro.sdk import create_session

        captured_opts: list[Any] = []
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        def capture_client(options: Any = None, **kwargs: Any) -> Any:
            captured_opts.append(options)
            return mock_client

        with patch("maestro.sdk.ClaudeSDKClient", side_effect=capture_client):
            await create_session("hello", options=QueryOptions(effort="medium"))

        assert len(captured_opts) == 1
        assert captured_opts[0].thinking == {"type": "adaptive"}
        assert captured_opts[0].max_thinking_tokens is None


class TestFallbackModel:
    """Tests for fallback model configuration."""

    @pytest.mark.asyncio
    async def test_global_fallback_forwarded(self) -> None:
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        configure_sdk_fallback_model("claude-sonnet-4-6")
        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(effort="high"))

        assert captured_kwargs["options"].fallback_model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_per_call_fallback_overrides_global(self) -> None:
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        configure_sdk_fallback_model("claude-sonnet-4-6")
        with patch("maestro.sdk._query", side_effect=gen):
            await query(
                prompt="test",
                options=QueryOptions(fallback_model="claude-haiku-4-5"),
            )

        assert captured_kwargs["options"].fallback_model == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_no_fallback_when_unconfigured(self) -> None:
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions())

        assert captured_kwargs["options"].fallback_model is None

    @pytest.mark.asyncio
    async def test_empty_string_disables_fallback(self) -> None:
        configure_sdk_fallback_model("")
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions())

        assert captured_kwargs["options"].fallback_model is None


class TestTaskBudget:
    """Tests for per-call task budget configuration."""

    @pytest.mark.asyncio
    async def test_global_task_budget_forwarded(self) -> None:
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        configure_sdk_task_budget(50000)
        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions())

        assert captured_kwargs["options"].task_budget == {"total": 50000}

    @pytest.mark.asyncio
    async def test_per_call_task_budget_overrides_global(self) -> None:
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        configure_sdk_task_budget(50000)
        with patch("maestro.sdk._query", side_effect=gen):
            await query(
                prompt="test",
                options=QueryOptions(task_budget_tokens=30000),
            )

        assert captured_kwargs["options"].task_budget == {"total": 30000}

    @pytest.mark.asyncio
    async def test_zero_budget_not_set(self) -> None:
        """Zero task_budget_tokens (disabled) should not set task_budget."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        configure_sdk_task_budget(0)
        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions())

        assert captured_kwargs["options"].task_budget is None

    @pytest.mark.asyncio
    async def test_none_budget_not_set(self) -> None:
        """When both per-call and global are zero/None, task_budget is not set."""
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(task_budget_tokens=None))

        assert captured_kwargs["options"].task_budget is None

    @pytest.mark.asyncio
    async def test_negative_budget_not_set(self) -> None:
        """A negative per-call budget disables task_budget, like zero.

        Negatives are the branch that separates a truthiness-plus-comparison
        guard from the comparison alone: ``-1`` is truthy, so only the ``> 0``
        test rejects it. Pinning it here keeps the single-comparison guard in
        :func:`maestro.sdk._build_sdk_options` honest.
        """
        captured_kwargs: dict[str, Any] = {}

        async def gen(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            yield _result_msg("ok")

        configure_sdk_task_budget(0)
        with patch("maestro.sdk._query", side_effect=gen):
            await query(prompt="test", options=QueryOptions(task_budget_tokens=-1))

        assert captured_kwargs["options"].task_budget is None


# ---------------------------------------------------------------------------
# _consume_messages / _finalize_usage
# ---------------------------------------------------------------------------

class TestConsumeMessages:
    @pytest.mark.asyncio
    async def test_basic_result_message(self) -> None:
        from maestro.sdk import _consume_messages, _finalize_usage

        msg = _result_msg("hello")

        async def gen() -> Any:
            yield msg

        cr = await _consume_messages(gen(), "test_caller", None)
        assert cr.last is not None
        assert cr.last.result == "hello"
        assert cr.tool_calls == []

        result = _finalize_usage(cr, None)
        assert result.response == "hello"
        assert result.usage.num_turns == 1
        # Provider attribution (067 Step 2): every row finalized on the
        # Anthropic impl path is stamped with its producing provider.
        assert result.usage.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_early_exit_row_stamps_provider(self) -> None:
        """The no-ResultMessage early path stamps attribution too."""
        from maestro.sdk import _consume_messages, _finalize_usage

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="thinking")],
                model="claude-sonnet-4-20250514",
            )

        cr = await _consume_messages(gen(), None, None)
        result = _finalize_usage(cr, None)
        assert result.usage.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_no_result_returns_empty(self) -> None:
        from maestro.sdk import _consume_messages, _finalize_usage

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="thinking")],
                model="claude-sonnet-4-20250514",
            )

        cr = await _consume_messages(gen(), None, None)
        assert cr.last is None
        result = _finalize_usage(cr, None)
        assert result.response == ""
        # No predicate fired (cr.early_exit is False): a genuine empty result,
        # no productive turn ran, so the early-exit turn floor (#1288) does not
        # apply and num_turns stays at the QueryUsage default of 0.
        assert cr.early_exit is False
        assert result.usage.num_turns == 0

    @pytest.mark.asyncio
    async def test_rate_limit_tracked(self) -> None:
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="allowed_warning",
                    resets_at=None,
                ),
                uuid="u1",
                session_id="s1",
            )
            yield _result_msg("done")

        cr = await _consume_messages(gen(), None, None)
        assert "allowed_warning" in cr.rate_limits

    @pytest.mark.asyncio
    async def test_rejected_resets_at_captured(self) -> None:
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="rejected",
                    resets_at=1234567890,
                ),
                uuid="u1",
                session_id="s1",
            )
            yield _result_msg("done")

        cr = await _consume_messages(gen(), None, None)
        assert cr.rejected_resets_at == 1234567890

    @pytest.mark.asyncio
    async def test_tool_calls_collected(self) -> None:
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
                model="claude-sonnet-4-20250514",
            )
            yield UserMessage(
                content=[ToolResultBlock(tool_use_id="t1", content="file.txt")],
            )
            yield _result_msg("done")

        cr = await _consume_messages(gen(), None, None)
        assert len(cr.tool_calls) == 1
        assert cr.tool_calls[0].name == "Bash"
        assert cr.tool_calls[0].result == "file.txt"

    @pytest.mark.asyncio
    async def test_usage_from_progress_message(self) -> None:
        from maestro.sdk import QueryUsage, _consume_messages

        async def gen() -> Any:
            yield TaskProgressMessage(
                subtype="progress",
                data={},
                task_id="t1",
                description="step",
                usage={"total_tokens": 500, "tool_uses": 2, "duration_ms": 50},
                uuid="u1",
                session_id="s1",
                last_tool_name="Bash",
            )
            yield _result_msg("done")

        usage = QueryUsage(caller="test")
        cr = await _consume_messages(gen(), "test", None, usage=usage)
        assert cr.usage.total_tokens == 500
        assert cr.usage.tool_uses == 2

    @pytest.mark.asyncio
    async def test_complete_on_predicate_short_circuits(self) -> None:
        """When the predicate fires, consumption stops before ResultMessage."""
        from maestro.sdk import _consume_messages, _finalize_usage

        consumed: list[Any] = []

        async def gen() -> Any:
            msg = AssistantMessage(
                content=[TextBlock(text="substantial reply" * 50)],
                model="claude-sonnet-4-20250514",
            )
            consumed.append(msg)
            yield msg
            consumed.append("would-yield-result")
            yield _result_msg("never reached")

        def predicate(_msg: Any, buf: str) -> bool:
            return len(buf) >= 100

        cr = await _consume_messages(
            gen(), "test", None, complete_on=predicate,
        )
        assert cr.early_exit is True
        assert cr.last is None
        # Predicate fired on first AssistantMessage; the generator never
        # advanced to the second item.
        assert consumed == [consumed[0]]

        result = _finalize_usage(cr, None)
        assert result.response.startswith("substantial reply")
        assert result.usage.total_cost_usd is None
        # A turn ran even though no ResultMessage arrived to report it, so the
        # early-exit branch stamps a floor of 1 (#1288) — otherwise early-exit
        # callers (introspect, update_self_model) credit zero turns downstream.
        assert result.usage.num_turns == 1

    @pytest.mark.asyncio
    async def test_early_exit_preserves_nonzero_turns(self) -> None:
        """The early-exit turn floor never clobbers a real count (#1288)."""
        from maestro.sdk import QueryUsage, _ConsumeResult, _finalize_usage

        cr = _ConsumeResult(
            last=None,
            tool_calls=[],
            usage=QueryUsage(num_turns=5),
            rate_limits=[],
            rejected_resets_at=None,
            early_exit=True,
        )
        result = _finalize_usage(cr, None)
        # num_turns was already non-zero, so the == 0 guard does not fire and
        # the existing count is left intact rather than reset to the floor of 1.
        assert result.usage.num_turns == 5

    @pytest.mark.asyncio
    async def test_early_exit_propagates_session_id(self) -> None:
        """The early-exit row carries its session so the stamped turn enters
        the downstream per-session peak-dedupe (#1288)."""
        from maestro.sdk import QueryUsage, _ConsumeResult, _finalize_usage

        cr = _ConsumeResult(
            last=None,
            tool_calls=[],
            usage=QueryUsage(),
            rate_limits=[],
            rejected_resets_at=None,
            early_exit=True,
            last_session_id="s1",
        )
        result = _finalize_usage(cr, None)
        # Mirrors the normal path: the usage row's session_id matches the
        # QueryResult's, so _on_query_usage routes through the session-aware
        # branch instead of the one-shot sum branch that would double-count a
        # later cumulative ResultMessage on the same session.
        assert result.usage.session_id == "s1"
        assert result.session_id == "s1"

    @pytest.mark.asyncio
    async def test_complete_on_false_does_not_short_circuit(self) -> None:
        """Predicate returning False keeps the loop running to ResultMessage."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="short")],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("done")

        cr = await _consume_messages(
            gen(), "test", None,
            complete_on=lambda _m, _b: False,
        )
        assert cr.early_exit is False
        assert cr.last is not None
        assert cr.last.result == "done"

    @pytest.mark.asyncio
    async def test_heartbeat_does_not_destroy_stream(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The mid-budget heartbeat must not cancel the pending __anext__().

        Runs against the *real* ``asyncio`` primitives: the regression this
        guards is that ``asyncio.wait_for`` cancels the awaitable it wraps,
        finalizing the single-use SDK generator and turning any tool call
        quiet for longer than half the budget into a bogus STREAM_STALL.
        """
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            # A tool call is left open across the heartbeat boundary — the
            # exact production shape (pending_tools=1, last_message kind
            # assistant_tool_use) the stalls carried.
            yield AssistantMessage(
                content=[ToolUseBlock(id="tu1", name="Bash", input={})],
                model="claude-sonnet-4-20250514",
            )
            # Longer than half the budget (0.5s), shorter than the full
            # 1.0s — a 200ms margin on each side so a loaded runner's
            # scheduling jitter cannot flip the outcome either way.
            await asyncio.sleep(0.7)
            yield _result_msg("done")

        with caplog.at_level(logging.WARNING, logger="maestro"):
            cr = await _consume_messages(
                gen(), "test", None,
                stream_idle_timeout_seconds=1.0,
                break_on_result=True,
            )

        assert cr.last is not None
        assert cr.last.result == "done"
        heartbeats = [
            r.getMessage() for r in caplog.records
            if "awaiting remainder" in r.getMessage()
        ]
        assert len(heartbeats) == 1
        assert "pending_tools=1" in heartbeats[0]
        assert "last_message=assistant_tool_use" in heartbeats[0]

    @pytest.mark.asyncio
    async def test_full_budget_exhaustion_reports_idle_not_stream_end(
        self,
    ) -> None:
        """A genuinely dead stream reports the idle timeout, not a clean close."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="hi")],
                model="claude-sonnet-4-20250514",
            )
            await asyncio.sleep(3600)
            yield _result_msg("never")

        with pytest.raises(SDKError) as exc_info:
            await _consume_messages(
                gen(), "test", None,
                stream_idle_timeout_seconds=0.2,
                break_on_result=True,
            )

        assert exc_info.value.category == ErrorCategory.STREAM_STALL
        assert "SDK stream idle for" in str(exc_info.value)
        assert "ended without ResultMessage" not in str(exc_info.value)
        assert exc_info.value.session_reusable is False

    @pytest.mark.asyncio
    async def test_stream_idle_timeout_raises_stream_stall(self) -> None:
        """When wait_for fires its timeout the consumer raises STREAM_STALL."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            # Yield one assistant message then hang forever.
            yield AssistantMessage(
                content=[TextBlock(text="hi")],
                model="claude-sonnet-4-20250514",
            )
            await asyncio.sleep(3600)
            yield _result_msg("never")

        seen = [0]

        async def fake_wait_for(coro: Any, timeout: float) -> Any:
            # First message: deliver it; second wait: synthesize a TimeoutError.
            if seen[0] == 0:
                seen[0] += 1
                return await coro
            coro.close()
            raise TimeoutError("stream idle")

        with patch("asyncio.wait_for", side_effect=fake_wait_for), \
             pytest.raises(SDKError) as ei:
            await _consume_messages(
                gen(), "test", None,
                stream_idle_timeout_seconds=0.5,
            )
        assert ei.value.category is ErrorCategory.STREAM_STALL

    @pytest.mark.asyncio
    async def test_stream_idle_timeout_disabled_when_zero(self) -> None:
        """Passing 0 disables the wait_for() guard entirely."""
        from maestro.sdk import _consume_messages

        called: list[float] = []

        async def fake_wait_for(coro: Any, timeout: float) -> Any:
            called.append(timeout)
            return await coro

        async def gen() -> Any:
            yield _result_msg("ok")

        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            cr = await _consume_messages(
                gen(), "test", None,
                stream_idle_timeout_seconds=0.0,
            )
        assert cr.last is not None
        assert called == []  # wait_for() never invoked

    @pytest.mark.asyncio
    async def test_stream_idle_module_default_used_when_none(self) -> None:
        """When no per-call value is given, the module default is honored."""
        from maestro.sdk import (
            _consume_messages,
            configure_sdk_stream_idle_timeout,
        )

        configure_sdk_stream_idle_timeout(7.0)
        try:
            captured: list[float] = []

            async def fake_wait_for(coro: Any, timeout: float) -> Any:
                captured.append(timeout)
                return await coro

            async def gen() -> Any:
                yield _result_msg("ok")

            with patch("asyncio.wait_for", side_effect=fake_wait_for):
                await _consume_messages(gen(), "test", None)
            # The whole 7s module default guards the single ``__anext__()``
            # await; the mid-budget heartbeat is a side timer, not a
            # shortened deadline.
            assert captured and captured[0] == 7.0
        finally:
            configure_sdk_stream_idle_timeout(None)

    @pytest.mark.asyncio
    async def test_heartbeat_failure_surfaces_loudly(self) -> None:
        """A raising heartbeat timer fails loud, not as a GC-time warning.

        The timer runs as a side task, so an exception inside it would
        otherwise be swallowed until asyncio reports "exception was never
        retrieved" at collection time. The consumer retrieves it instead.
        """
        from maestro.sdk import _consume_messages

        sl = MagicMock()
        sl.log.side_effect = RuntimeError("session log is wedged")

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="hi")],
                model="claude-sonnet-4-20250514",
            )
            await asyncio.sleep(3600)
            yield _result_msg("never")

        with pytest.raises(RuntimeError, match="session log is wedged"):
            await _consume_messages(
                gen(), "introspect", sl,
                stream_idle_timeout_seconds=0.2,
            )

    @pytest.mark.asyncio
    async def test_stream_stall_writes_to_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        """STREAM_STALL path writes a diagnostic line to the SessionLogger."""
        from maestro.sdk import _consume_messages

        sl = SessionLogger(Path("/fake/sdk_stall"), caller="test")
        try:
            async def gen() -> Any:
                yield AssistantMessage(
                    content=[TextBlock(text="hi")],
                    model="claude-sonnet-4-20250514",
                )
                await asyncio.sleep(3600)
                yield _result_msg("never")

            # Real asyncio primitives, so the mid-budget heartbeat and the
            # terminal stall are both written by the code under test.
            with pytest.raises(SDKError):
                await _consume_messages(
                    gen(), "introspect", sl,
                    stream_idle_timeout_seconds=0.2,
                )
        finally:
            sl.close()
        log_content = mem_fs[next(iter(mem_fs))]
        assert "Heartbeat: idle 0.1s" in log_content
        assert "last_message=assistant_text" in log_content
        assert "STREAM STALL" in log_content
        assert "caller=introspect" in log_content

    @pytest.mark.asyncio
    async def test_timeout_error_propagates_when_idle_timeout_disabled(
        self,
    ) -> None:
        """TimeoutError from the iterator propagates as-is when idle timeout
        is disabled (stream_idle_timeout_seconds=0.0), rather than being
        wrapped in SDKError(STREAM_STALL)."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="first")],
                model="claude-sonnet-4-20250514",
            )
            # Simulate a TimeoutError from deep inside the SDK transport.
            raise TimeoutError("transport timeout")

        with pytest.raises(TimeoutError, match="transport timeout"):
            await _consume_messages(
                gen(), "test", None,
                stream_idle_timeout_seconds=0.0,
            )

    @pytest.mark.asyncio
    async def test_assistant_message_session_id_captured(self) -> None:
        """A populated AssistantMessage.session_id is recorded on the result."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="hi")],
                model="claude-sonnet-4-20250514",
                session_id="sess-abc-123",
            )
            yield _result_msg("done", session_id="sess-abc-123")

        cr = await _consume_messages(gen(), "test", None)
        assert cr.last_session_id == "sess-abc-123"

    @pytest.mark.asyncio
    async def test_early_exit_writes_to_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Early-exit predicate path writes a diagnostic line to SessionLogger."""
        from maestro.sdk import _consume_messages

        sl = SessionLogger(Path("/fake/sdk_early"), caller="test")
        try:
            async def gen() -> Any:
                yield AssistantMessage(
                    content=[TextBlock(text="payload" * 30)],
                    model="claude-sonnet-4-20250514",
                )
                yield _result_msg("never")

            cr = await _consume_messages(
                gen(), "introspect", sl,
                complete_on=lambda _m, buf: len(buf) >= 100,
            )
            assert cr.early_exit is True
        finally:
            sl.close()
        log_content = mem_fs[next(iter(mem_fs))]
        assert "Early exit" in log_content

    @pytest.mark.asyncio
    async def test_query_propagates_stream_stall_sdk_error(self) -> None:
        """query() must re-raise pre-categorized SDKError without retry."""
        async def gen(**kwargs: Any) -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="hi")],
                model="claude-sonnet-4-20250514",
            )
            await asyncio.sleep(3600)
            yield _result_msg("never")

        # Real ``asyncio`` primitives: the generator hangs past the whole
        # 0.1s budget, so the consumer's own idle guard fires.
        with patch("maestro.sdk._query", side_effect=gen), \
             pytest.raises(SDKError) as ei:
            await query(prompt="x", stream_idle_timeout_seconds=0.1)
        assert ei.value.category is ErrorCategory.STREAM_STALL

    @pytest.mark.asyncio
    async def test_stream_stall_fingerprint_for_assistant_text(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """STREAM_STALL after an assistant text message tags ``assistant_text``."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="hi")],
                model="claude-sonnet-4-20250514",
            )
            await asyncio.sleep(3600)
            yield _result_msg("never")

        # Deliver the first message, then fire TimeoutError for every
        # subsequent wait_for so the heartbeat's inner wait and the
        # second-half wait both time out.
        seen = [0]

        async def fake_wait_for(coro: Any, timeout: float) -> Any:
            if seen[0] == 0:
                seen[0] += 1
                return await coro
            coro.close()
            raise TimeoutError

        caplog.set_level(logging.WARNING, logger="maestro")
        with patch("asyncio.wait_for", side_effect=fake_wait_for), \
             pytest.raises(SDKError) as ei:
            await _consume_messages(
                gen(), "test", None,
                stream_idle_timeout_seconds=0.5,
            )
        assert ei.value.category is ErrorCategory.STREAM_STALL
        # Fingerprint must be in the SDKError message so the non-transient
        # path's ``rejection_reason`` (derived from ``exc.detail``) carries it.
        assert "last_message=assistant_text" in str(ei.value)
        assert "pending_tools=0" in str(ei.value)
        # And in the terminal warning log line.
        stall_logs = [
            r for r in caplog.records
            if "STREAM_STALL" in r.getMessage()
        ]
        assert stall_logs, "expected a STREAM_STALL warning log"
        assert "last_message=assistant_text" in stall_logs[-1].getMessage()
        assert "pending_tools=0" in stall_logs[-1].getMessage()

    @pytest.mark.asyncio
    async def test_stream_stall_fingerprint_for_pending_tool(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A pending tool call surfaces as ``assistant_tool_use`` + pending>=1."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[
                    ToolUseBlock(id="t1", name="Bash", input={"cmd": "ls"}),
                ],
                model="claude-sonnet-4-20250514",
            )
            await asyncio.sleep(3600)
            yield _result_msg("never")

        seen = [0]

        async def fake_wait_for(coro: Any, timeout: float) -> Any:
            if seen[0] == 0:
                seen[0] += 1
                return await coro
            coro.close()
            raise TimeoutError

        caplog.set_level(logging.WARNING, logger="maestro")
        with patch("asyncio.wait_for", side_effect=fake_wait_for), \
             pytest.raises(SDKError) as ei:
            await _consume_messages(
                gen(), "test", None,
                stream_idle_timeout_seconds=0.5,
            )
        assert ei.value.category is ErrorCategory.STREAM_STALL
        assert "last_message=assistant_tool_use" in str(ei.value)
        assert "pending_tools=1" in str(ei.value)

    @pytest.mark.asyncio
    async def test_empty_completion_raises_stream_stall(self) -> None:
        """A ResultMessage with num_turns=0 and zero tokens is a dropped
        upstream connection — classify it as STREAM_STALL so the
        iteration-level resume-in-place path retries with a fresh
        session rather than demoting the plan."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="",
                num_turns=0,
                usage={"input_tokens": 0, "output_tokens": 0},
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "execute_step", None)
        assert ei.value.category is ErrorCategory.STREAM_STALL
        assert ei.value.caller == "execute_step"
        assert "empty completion" in str(ei.value)
        assert "num_turns=0" in str(ei.value)
        assert ei.value.session_reusable is True
        assert "tokens=0" in str(ei.value)

    @pytest.mark.asyncio
    async def test_empty_completion_writes_to_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The empty-completion STREAM_STALL path writes a diagnostic
        line to the SessionLogger (parallel to the idle-timer stall)."""
        from maestro.sdk import _consume_messages

        sl = SessionLogger(Path("/fake/sdk_empty_completion"), caller="test")
        try:
            async def gen() -> Any:
                yield _result_msg(
                    result="",
                    num_turns=0,
                    usage={"input_tokens": 0, "output_tokens": 0},
                )

            with pytest.raises(SDKError):
                await _consume_messages(gen(), "execute_step", sl)
        finally:
            sl.close()
        log_content = mem_fs[next(iter(mem_fs))]
        assert "STREAM_STALL: empty completion" in log_content
        assert "caller=execute_step" in log_content

    @pytest.mark.asyncio
    async def test_empty_completion_after_rate_limit_rejection_does_not_raise(
        self,
    ) -> None:
        """A zero-token completion *following* a rejected rate-limit event
        must stay in the RATE_LIMIT retry path, not be reclassified as
        STREAM_STALL."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="rejected", resets_at=1743890400,
                ),
                uuid="u1",
                session_id="s1",
            )
            yield _result_msg(
                result="",
                num_turns=0,
                usage={"input_tokens": 0, "output_tokens": 0},
            )

        cr = await _consume_messages(gen(), "execute_step", None)
        assert cr.rejected_resets_at == 1743890400
        assert cr.last is not None
        assert cr.last.num_turns == 0

    @pytest.mark.asyncio
    async def test_normal_completion_not_classified_as_empty(self) -> None:
        """Sanity check: a non-empty ResultMessage returns normally."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                "real response",
                num_turns=1,
                usage={"input_tokens": 100, "output_tokens": 50},
            )

        cr = await _consume_messages(gen(), "execute_step", None)
        assert cr.last is not None
        assert cr.last.result == "real response"

    @pytest.mark.asyncio
    async def test_query_on_session_propagates_empty_completion_stream_stall(
        self,
    ) -> None:
        """query_on_session must propagate STREAM_STALL from an empty
        completion (mirror of ``test_query_propagates_stream_stall_sdk_error``
        for the session path)."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg(
                result="",
                num_turns=0,
                usage={"input_tokens": 0, "output_tokens": 0},
            )

        mock_client.receive_messages = receive

        with pytest.raises(SDKError) as ei:
            await query_on_session(
                mock_client, "follow up", caller="execute_step",
            )
        assert ei.value.category is ErrorCategory.STREAM_STALL

    @pytest.mark.asyncio
    async def test_no_result_message_with_break_on_result_raises_stream_stall(
        self,
    ) -> None:
        """A persistent-session stream that closes without ever yielding a
        ResultMessage is an upstream disconnect — classify as STREAM_STALL so
        the iteration-level resume-in-place path retries with a fresh
        session rather than letting an empty QueryResult propagate to a
        JSON-parsing caller and crash the loop."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[ThinkingBlock(thinking="…", signature="sig")],
                model="claude-sonnet-4-20250514",
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(
                gen(), "update_self_model", None, break_on_result=True,
            )
        assert ei.value.category is ErrorCategory.STREAM_STALL
        assert ei.value.caller == "update_self_model"
        assert "without ResultMessage" in str(ei.value)
        assert ei.value.session_reusable is False

    @pytest.mark.asyncio
    async def test_no_result_message_without_break_on_result_returns_empty(
        self,
    ) -> None:
        """The stateless ``query()`` path (``break_on_result=False``) must
        keep returning a ``_ConsumeResult`` with ``last=None`` when the stream
        ends without a ResultMessage — the underlying generator is supposed
        to drain to ``StopAsyncIteration`` inside the caller's task."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[ThinkingBlock(thinking="…", signature="sig")],
                model="claude-sonnet-4-20250514",
            )

        cr = await _consume_messages(gen(), "query_caller", None)
        assert cr.last is None

    @pytest.mark.asyncio
    async def test_unproductive_aggregate_raises_stream_stall(self) -> None:
        """A completion with non-zero turns/tokens but no assistant text and
        all-erroring tool calls is unproductive — classify as STREAM_STALL so
        the iteration-level resume-in-place path retries on a fresh session."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="t1", name="Bash", input={"command": "boom"},
                    ),
                ],
                model="claude-sonnet-4-20250514",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="t1", content="failed", is_error=True,
                    ),
                ],
            )
            yield _result_msg(
                result="",
                num_turns=1,
                usage={"input_tokens": 100, "output_tokens": 50},
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "execute_step", None)
        assert ei.value.category is ErrorCategory.STREAM_STALL
        assert ei.value.caller == "execute_step"
        assert "unproductive" in str(ei.value)
        assert "pending_tools=" in str(ei.value)
        assert ei.value.session_reusable is True

    @pytest.mark.asyncio
    async def test_unproductive_aggregate_writes_to_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Unproductive-aggregate STREAM_STALL writes a diagnostic line with
        the broad-case reason to the SessionLogger."""
        from maestro.sdk import _consume_messages

        sl = SessionLogger(Path("/fake/sdk_unproductive"), caller="test")
        try:
            async def gen() -> Any:
                yield _result_msg(
                    result="",
                    num_turns=1,
                    usage={"input_tokens": 50, "output_tokens": 10},
                )

            with pytest.raises(SDKError):
                await _consume_messages(gen(), "execute_step", sl)
        finally:
            sl.close()
        log_content = mem_fs[next(iter(mem_fs))]
        assert "STREAM_STALL: empty completion" in log_content
        assert "reason=unproductive aggregate" in log_content
        assert "caller=execute_step" in log_content

    @pytest.mark.asyncio
    async def test_aggregate_check_skipped_during_rate_limit_rejected(
        self,
    ) -> None:
        """A rejected rate-limit event preserves the RATE_LIMIT retry path
        even when the resulting completion is otherwise unproductive."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="rejected", resets_at=1743890400,
                ),
                uuid="u1",
                session_id="s1",
            )
            yield _result_msg(
                result="",
                num_turns=1,
                usage={"input_tokens": 50, "output_tokens": 10},
            )

        cr = await _consume_messages(gen(), "execute_step", None)
        assert cr.rejected_resets_at == 1743890400
        assert cr.last is not None

    @pytest.mark.asyncio
    async def test_aggregate_check_skipped_on_early_exit(self) -> None:
        """An early-exit predicate returning partial buffer must not raise
        STREAM_STALL even when the buffer is empty — the caller deliberately
        opted out of waiting for a ResultMessage."""
        from maestro.sdk import _consume_messages, _finalize_usage

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="")],
                model="claude-sonnet-4-20250514",
            )
            yield _result_msg("never reached")

        # Predicate fires immediately on the first AssistantMessage, breaking
        # the loop before the ResultMessage arrives.
        cr = await _consume_messages(
            gen(), "introspect", None,
            complete_on=lambda _msg, _buf: True,
        )
        assert cr.early_exit is True
        assert cr.last is None
        result = _finalize_usage(cr, None)
        assert result.response == ""

    @pytest.mark.asyncio
    async def test_productive_text_does_not_raise(self) -> None:
        """A non-empty assistant text in the ResultMessage is productive."""
        from maestro.sdk import _consume_messages, _finalize_usage

        async def gen() -> Any:
            yield _result_msg(
                "real response",
                num_turns=1,
                usage={"input_tokens": 100, "output_tokens": 50},
            )

        cr = await _consume_messages(gen(), "execute_step", None)
        assert cr.last is not None
        result = _finalize_usage(cr, None)
        assert result.response == "real response"

    @pytest.mark.asyncio
    async def test_productive_tool_call_does_not_raise(self) -> None:
        """An empty assistant text with at least one non-error tool call
        is productive — late steps often skip the wrap-up sentence after a
        long tool run, and treating those as failures wastes the budget."""
        from maestro.sdk import _consume_messages, _finalize_usage

        async def gen() -> Any:
            yield AssistantMessage(
                content=[
                    ToolUseBlock(
                        id="t1", name="Edit", input={"path": "/x"},
                    ),
                ],
                model="claude-sonnet-4-20250514",
            )
            yield UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="t1", content="ok", is_error=False,
                    ),
                ],
            )
            yield _result_msg(
                result="",
                num_turns=1,
                usage={"input_tokens": 100, "output_tokens": 50},
            )

        cr = await _consume_messages(gen(), "execute_step", None)
        assert cr.last is not None
        assert len(cr.tool_calls) == 1
        assert cr.tool_calls[0].is_error is False
        result = _finalize_usage(cr, None)
        assert result.response == ""
        assert result.tool_calls[0].name == "Edit"

    @pytest.mark.asyncio
    async def test_stream_heartbeat_warning_at_half_budget(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A silent stream emits a mid-budget heartbeat WARNING before STREAM_STALL."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            # No messages ever — first half times out, heartbeat fires,
            # second half times out, STREAM_STALL raised.
            if False:
                yield _result_msg("never")
            await asyncio.sleep(3600)

        # Real asyncio primitives — a patched ``wait_for`` is exactly what
        # hid #3877 — with a budget small enough for a unit test.
        caplog.set_level(logging.WARNING, logger="maestro")
        with pytest.raises(SDKError) as ei:
            await _consume_messages(
                gen(), "introspect", None,
                stream_idle_timeout_seconds=0.2,
            )
        assert ei.value.category is ErrorCategory.STREAM_STALL

        heartbeat_logs = [
            r for r in caplog.records
            if "stream quiet" in r.getMessage()
        ]
        assert heartbeat_logs, "expected a mid-budget heartbeat warning"
        # Heartbeat fingerprint — no messages yet, so last_message=none.
        msg = heartbeat_logs[-1].getMessage()
        assert "last_message=none" in msg
        assert "pending_tools=0" in msg
        assert "caller=introspect" in msg

    @pytest.mark.asyncio
    async def test_rate_limit_ghost_completion_raises_rate_limit(self) -> None:
        """A zero-usage ResultMessage whose text is a CLI-synthesized
        rate-limit notice (``num_turns >= 1``, zero tokens, zero cost) is a
        ghost completion — the model was never invoked. Classify it as
        RATE_LIMIT at the SDK boundary so the resume-in-place path handles it,
        rather than letting the prose flow on and crash a JSON-bound caller."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="You've hit your limit · resets 6pm (UTC)",
                num_turns=1,
                usage={"input_tokens": 0, "output_tokens": 0},
                total_cost_usd=0.0,
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "update_self_model", None)
        assert ei.value.category is ErrorCategory.RATE_LIMIT
        assert ei.value.caller == "update_self_model"
        assert "rate-limit ghost completion" in str(ei.value)

    @pytest.mark.asyncio
    async def test_rate_limit_prose_with_nonzero_usage_not_classified(
        self,
    ) -> None:
        """The zero-usage guard is load-bearing: a real, paid model reply that
        merely *discusses* a rate limit (non-zero tokens/cost) must pass
        through unmolested even when its text contains a ghost phrase."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="As an aside: when you hit your limit the API "
                "returns a 429.",
                num_turns=1,
                usage={"input_tokens": 120, "output_tokens": 30},
                total_cost_usd=0.01,
            )

        cr = await _consume_messages(gen(), "update_self_model", None)
        assert cr.last is not None
        assert cr.last.result.startswith("As an aside")

    @pytest.mark.asyncio
    async def test_terminal_529_error_result_routes_to_rate_limit(
        self,
    ) -> None:
        """Regression for #2870: when the CLI exhausts its internal retries
        against a ``529 Overloaded`` burst it returns a *terminal*
        ResultMessage with ``is_error=True`` / ``api_error_status=529`` whose
        ``.result`` is the overload notice — reporting zero tokens but a small
        non-zero cost (billed for the failed retries). The non-zero cost
        defeats the zero-cost ghost guard, so under the old code this flowed on
        as a productive QueryResult and crashed the first JSON-bound caller on
        ``parse_json``. It must instead route into the transient RATE_LIMIT
        path at the SDK boundary so resume-in-place handles it."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="API Error: 529 Overloaded. This is a server-side "
                "issue; please retry shortly.",
                is_error=True,
                api_error_status=529,
                num_turns=1,
                usage={"input_tokens": 0, "output_tokens": 0},
                total_cost_usd=0.0037,
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "update_self_model", None)
        assert ei.value.category is ErrorCategory.RATE_LIMIT
        assert ei.value.caller == "update_self_model"
        assert "terminal error result" in str(ei.value)

    @pytest.mark.asyncio
    async def test_terminal_429_error_result_routes_to_rate_limit(
        self,
    ) -> None:
        """A retries-exhausted ``429 Too Many Requests`` surfaces as a terminal
        ``is_error`` ResultMessage whose prose ("Too Many Requests") is *not* in
        ``QUOTA_KEYWORDS`` and whose status is outside the 5xx band. Without the
        retryable-4xx band it would classify as the non-transient UNKNOWN and
        hard-crash the loop rather than resume in place — it must route into the
        transient RATE_LIMIT path just like the 529 overload case."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="API Error: 429 Too Many Requests. Please retry later.",
                is_error=True,
                api_error_status=429,
                num_turns=1,
                usage={"input_tokens": 0, "output_tokens": 0},
                total_cost_usd=0.0021,
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "update_self_model", None)
        assert ei.value.category is ErrorCategory.RATE_LIMIT
        assert ei.value.caller == "update_self_model"
        assert "terminal error result" in str(ei.value)

    @pytest.mark.asyncio
    async def test_paid_reply_with_overload_prose_not_routed(self) -> None:
        """Guard against over-broad classification (complements
        ``test_nonzero_cost_is_not_ghost``): a genuine *paid* reply
        (``is_error=False``, non-zero tokens/cost) that merely mentions an
        overload must pass through as a productive completion — the new
        ``is_error`` branch keys on the SDK's terminal error state, not the
        prose, so it never fires here."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="As an aside: the API can return 529 Overloaded "
                "during peak load.",
                is_error=False,
                num_turns=1,
                usage={"input_tokens": 120, "output_tokens": 30},
                total_cost_usd=0.01,
            )

        cr = await _consume_messages(gen(), "update_self_model", None)
        assert cr.last is not None
        assert cr.last.result.startswith("As an aside")

    @pytest.mark.asyncio
    async def test_terminal_non_transient_error_result_fails_loud(
        self,
    ) -> None:
        """A terminal ``is_error`` result with a *non-transient* status (a 4xx
        client error) must surface loudly as a categorized SDKError rather than
        flowing on to ``parse_json`` as a mislabeled success — but must NOT be
        classified as the transient RATE_LIMIT (it is not in
        ``_TRANSIENT_SDK_CATEGORIES``), so the loop still dies hard on
        genuinely non-transient failures (fail-loud invariant)."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="API Error: 400 Bad Request.",
                is_error=True,
                api_error_status=400,
                num_turns=1,
                usage={"input_tokens": 5, "output_tokens": 0},
                total_cost_usd=0.001,
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "update_self_model", None)
        # UNKNOWN is deliberately absent from ``_TRANSIENT_SDK_CATEGORIES``
        # (maestro.agent), so the loop dies hard rather than resuming in place.
        assert ei.value.category is ErrorCategory.UNKNOWN
        assert "terminal error result" in str(ei.value)

    @pytest.mark.asyncio
    async def test_terminal_error_result_without_status_fails_loud(
        self,
    ) -> None:
        """A terminal ``is_error`` result with no HTTP status at all (e.g. a
        max-turns / execution error) has no transient signal, so it routes to
        the non-transient UNKNOWN category and crashes loudly rather than being
        delivered as a productive completion."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="Error: reached the maximum number of turns.",
                is_error=True,
                api_error_status=None,
                num_turns=3,
                usage={"input_tokens": 40, "output_tokens": 0},
                total_cost_usd=0.002,
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "update_self_model", None)
        assert ei.value.category is ErrorCategory.UNKNOWN
        assert "terminal error result" in str(ei.value)

    @pytest.mark.asyncio
    async def test_terminal_error_result_names_subtype_and_errors(
        self,
    ) -> None:
        """#4286: a structured-output-exhaustion terminal error names its
        subtype and an errors head, so ``last_sdk_error`` is not a bare
        ``category=unknown`` with no cause; #4535: it classifies as the
        typed non-transient ``STRUCTURED_OUTPUT_INVALID``."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="",
                is_error=True,
                api_error_status=None,
                subtype="error_max_structured_output_retries",
                errors=["Output does not match required schema: bad"],
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "dream", None)
        assert ei.value.category is ErrorCategory.STRUCTURED_OUTPUT_INVALID
        text = str(ei.value)
        assert "subtype=error_max_structured_output_retries" in text
        assert "Output does not match required schema" in text

    @pytest.mark.asyncio
    async def test_terminal_error_other_subtype_stays_unknown(self) -> None:
        """#4535: the structured-output category is subtype-scoped — any
        other non-transient terminal error still classifies ``UNKNOWN``."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="",
                is_error=True,
                api_error_status=None,
                subtype="error_during_execution",
                errors=["Tool execution failed unexpectedly"],
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "propose_plan", None)
        assert ei.value.category is ErrorCategory.UNKNOWN

    @pytest.mark.asyncio
    async def test_structured_output_exhaustion_transient_keeps_precedence(
        self,
    ) -> None:
        """#4535: a transient overload status outranks the structured-output
        subtype, so the resume-in-place path still owns it."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="",
                is_error=True,
                api_error_status=529,
                subtype="error_max_structured_output_retries",
                errors=["Overloaded"],
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "propose_plan", None)
        assert ei.value.category is ErrorCategory.RATE_LIMIT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["errors", "result"])
    async def test_terminal_error_result_detail_head_is_capped(
        self, field: str,
    ) -> None:
        """An oversized ``errors`` / ``result`` payload embeds only a capped
        head in the raised text (context-bloat-cap invariant)."""
        from maestro.sdk import _TERMINAL_ERROR_DETAIL_MAX, _consume_messages

        payload = "x" * (_TERMINAL_ERROR_DETAIL_MAX * 5)
        overrides: dict[str, Any] = {
            "is_error": True, "api_error_status": None, "subtype": "error_during_execution",
        }
        if field == "errors":
            overrides["errors"] = [payload]
            result = ""
        else:
            result = payload

        async def gen() -> Any:
            yield _result_msg(result=result, **overrides)

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "dream", None)
        text = str(ei.value)
        assert f"{field}='x" in text
        assert "x" * _TERMINAL_ERROR_DETAIL_MAX in text
        assert "x" * (_TERMINAL_ERROR_DETAIL_MAX + 1) not in text

    @pytest.mark.asyncio
    async def test_terminal_overload_prose_without_status_routes_to_rate_limit(
        self,
    ) -> None:
        """Ledger r1: a transient overload that surfaces *without* an HTTP
        status (``api_error_status=None``) — e.g. an older CLI, or a non-HTTP
        transient — must still route to RATE_LIMIT via the overload-prose
        fallback in ``.result``, not fail loud as UNKNOWN. Otherwise a
        genuinely transient overload would crash the loop instead of resuming
        in place."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="API Error: Overloaded. Please retry shortly.",
                is_error=True,
                api_error_status=None,
                num_turns=1,
                usage={"input_tokens": 0, "output_tokens": 0},
                total_cost_usd=0.0037,
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "update_self_model", None)
        assert ei.value.category is ErrorCategory.RATE_LIMIT
        assert "terminal error result" in str(ei.value)

    @pytest.mark.asyncio
    async def test_terminal_overload_in_errors_list_routes_to_rate_limit(
        self,
    ) -> None:
        """Ledger r1: the overload-prose fallback also scans the structured
        ``.errors`` list — a status-less terminal error whose only overload
        signal lives in ``errors`` still routes to the transient RATE_LIMIT
        path."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield _result_msg(
                result="Request failed.",
                errors=["upstream returned Overloaded after 5 retries"],
                is_error=True,
                api_error_status=None,
                num_turns=1,
                usage={"input_tokens": 0, "output_tokens": 0},
                total_cost_usd=0.0037,
            )

        with pytest.raises(SDKError) as ei:
            await _consume_messages(gen(), "update_self_model", None)
        assert ei.value.category is ErrorCategory.RATE_LIMIT
        assert "terminal error result" in str(ei.value)

    @pytest.mark.asyncio
    async def test_rate_limit_ghost_after_rejection_takes_rejected_path(
        self,
    ) -> None:
        """A ghost-shaped completion *following* a rejected rate-limit event
        must stay on the existing sleep-until-reset path (rejected_resets_at
        set, no raise), not be reclassified by the new ghost-completion
        branch — confirming the ``rejected_resets_at is None`` guard."""
        from maestro.sdk import _consume_messages

        async def gen() -> Any:
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="rejected", resets_at=1743890400,
                ),
                uuid="u1",
                session_id="s1",
            )
            yield _result_msg(
                result="You've hit your limit · resets 6pm (UTC)",
                num_turns=1,
                usage={"input_tokens": 0, "output_tokens": 0},
                total_cost_usd=0.0,
            )

        cr = await _consume_messages(gen(), "update_self_model", None)
        assert cr.rejected_resets_at == 1743890400
        assert cr.last is not None

    @pytest.mark.asyncio
    async def test_rate_limit_ghost_completion_writes_to_session_log(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The ghost-completion path writes a diagnostic line with a distinct
        reason so the shape is greppable in post-mortem."""
        from maestro.sdk import _consume_messages

        sl = SessionLogger(Path("/fake/sdk_ghost_completion"), caller="test")
        try:
            async def gen() -> Any:
                yield _result_msg(
                    result="You've hit your limit · resets 6pm (UTC)",
                    num_turns=1,
                    usage={"input_tokens": 0, "output_tokens": 0},
                    total_cost_usd=0.0,
                )

            with pytest.raises(SDKError):
                await _consume_messages(gen(), "update_self_model", sl)
        finally:
            sl.close()
        log_content = mem_fs[next(iter(mem_fs))]
        # The diagnostic must name the category actually raised (RATE_LIMIT),
        # never the STREAM_STALL framing the shared helper used before the
        # ghost-completion path threaded its own category through.
        assert "RATE_LIMIT: empty completion" in log_content
        assert "STREAM_STALL" not in log_content
        assert "reason=rate-limit ghost completion" in log_content
        assert "caller=update_self_model" in log_content

    @pytest.mark.asyncio
    async def test_query_on_session_propagates_ghost_completion_rate_limit(
        self,
    ) -> None:
        """query_on_session must propagate the RATE_LIMIT category from a ghost
        completion (mirror of the empty-completion STREAM_STALL propagation
        test); the pre-categorized SDKError is re-raised without
        reclassification so the loop's resume-in-place path sees RATE_LIMIT."""
        from maestro.sdk import query_on_session

        mock_client = MagicMock()
        mock_client.query = AsyncMock()

        async def receive() -> Any:
            yield _result_msg(
                result="You've hit your limit · resets 6pm (UTC)",
                num_turns=1,
                usage={"input_tokens": 0, "output_tokens": 0},
                total_cost_usd=0.0,
            )

        mock_client.receive_messages = receive

        with pytest.raises(SDKError) as ei:
            await query_on_session(
                mock_client, "follow up", caller="update_self_model",
            )
        assert ei.value.category is ErrorCategory.RATE_LIMIT


# ---------------------------------------------------------------------------
# Shared early-exit predicates
# ---------------------------------------------------------------------------


class TestJsonComplete:
    def test_empty_buffer_is_not_complete(self) -> None:
        from maestro.sdk import json_complete

        assert json_complete(None, "") is False
        assert json_complete(None, "   ") is False

    def test_raw_json_object(self) -> None:
        from maestro.sdk import json_complete

        assert json_complete(None, '{"ready": true}') is True

    def test_raw_json_array(self) -> None:
        from maestro.sdk import json_complete

        assert json_complete(None, '[1, 2, 3]') is True

    def test_json_inside_fenced_block(self) -> None:
        from maestro.sdk import json_complete

        assert json_complete(
            None, '```json\n{"a": 1}\n```',
        ) is True

    def test_json_wrapped_in_prose(self) -> None:
        from maestro.sdk import json_complete

        assert json_complete(
            None,
            'Here is the answer:\n{"ready": true, "reasoning": "done"}\nDone.',
        ) is True

    def test_incomplete_brace_is_not_complete(self) -> None:
        from maestro.sdk import json_complete

        assert json_complete(None, '{"ready":') is False
        assert json_complete(None, 'no json here') is False

    def test_truncated_json_is_not_complete(self) -> None:
        """Mid-stream buffer with only an opening brace must not fire."""
        from maestro.sdk import json_complete

        assert json_complete(None, '{"partial_key":"val') is False

    def test_fenced_block_with_invalid_json_falls_through(self) -> None:
        """Fenced block containing garbage must not fire."""
        from maestro.sdk import json_complete

        # Fenced block matches but content is not parseable, and there's
        # no bracketed JSON anywhere either.
        assert json_complete(None, "```json\nnot valid\n```") is False

    def test_bracketed_garbage_falls_through(self) -> None:
        """Outer-brace span with invalid JSON inside must not fire."""
        from maestro.sdk import json_complete

        assert json_complete(None, "{this is not json}") is False
        assert json_complete(None, "[not, valid, @#$]") is False

    def test_multiple_objects_fires_on_last_span(self) -> None:
        """Multi-object buffer fires via the narrowest-span fallback.

        The widest span (first ``{`` → last ``}``) fails to parse, so the
        predicate must fall back to the last ``{`` → last ``}`` span — the
        same dual-start recovery :meth:`QueryResult.parse_json` uses — instead
        of returning False and burning the full stream-idle timeout.
        """
        from maestro.sdk import json_complete

        assert json_complete(None, 'Draft: {"a": 1}\n\nFinal: {"b": 2}') is True

    def test_fires_in_lockstep_with_parse_json_on_multi_object(self) -> None:
        """A buffer that fires the predicate is one parse_json also parses."""
        from maestro.sdk import QueryResult, json_complete

        buffer = 'Draft: {"a": 1}\n\nFinal: {"b": 2}'
        assert json_complete(None, buffer) is True
        assert QueryResult(response=buffer).parse_json() == {"b": 2}


class TestCharsComplete:
    def test_returns_same_callable_for_same_threshold(self) -> None:
        from maestro.sdk import chars_complete

        assert chars_complete(100) is chars_complete(100)

    def test_threshold_boundary(self) -> None:
        from maestro.sdk import chars_complete

        predicate = chars_complete(50)
        assert predicate(None, "x" * 49) is False
        assert predicate(None, "x" * 50) is True
        assert predicate(None, "x" * 51) is True

    def test_zero_threshold_fires_on_any_text(self) -> None:
        from maestro.sdk import chars_complete

        predicate = chars_complete(0)
        assert predicate(None, "") is True
        assert predicate(None, "a") is True

    def test_negative_threshold_rejected(self) -> None:
        from maestro.sdk import chars_complete

        with pytest.raises(ValueError, match="min_chars"):
            chars_complete(-1)


# ---------------------------------------------------------------------------
# Cost estimation on the early-exit predicate path
# ---------------------------------------------------------------------------

class TestEstimateCostUsd:
    """Pure-function tests for ``_estimate_cost_usd`` and ``configure_sdk_pricing``."""

    def setup_method(self) -> None:
        from maestro.sdk import configure_sdk_pricing

        configure_sdk_pricing(None)

    def teardown_method(self) -> None:
        from maestro.sdk import configure_sdk_pricing

        configure_sdk_pricing(None)

    def test_unknown_model_returns_none_and_warns_once(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from maestro.sdk import QueryUsage, _estimate_cost_usd

        usage = QueryUsage(model="claude-mystery-9", input_tokens=1000)
        with caplog.at_level("WARNING", logger="maestro"):
            assert _estimate_cost_usd(usage) is None
            # Second call for the same model is silent — the per-process
            # warned-set deduplicates so we never spam.
            assert _estimate_cost_usd(usage) is None
        warnings = [r for r in caplog.records if "claude-mystery-9" in r.message]
        assert len(warnings) == 1

    def test_no_model_returns_none_silently(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from maestro.sdk import QueryUsage, _estimate_cost_usd

        usage = QueryUsage(model=None, input_tokens=1000)
        with caplog.at_level("WARNING", logger="maestro"):
            assert _estimate_cost_usd(usage) is None
        # No model means no expectation that we *should* know pricing —
        # don't pollute the log.
        assert not [r for r in caplog.records if "pricing" in r.message]

    def test_known_model_computes_cost(self) -> None:
        from maestro.sdk import QueryUsage, _estimate_cost_usd, configure_sdk_pricing

        configure_sdk_pricing({
            "claude-opus-4-7": {
                "input": 15.0,
                "output": 75.0,
                "cache_creation": 18.75,
                "cache_read": 1.5,
            },
        })
        usage = QueryUsage(
            model="claude-opus-4-7",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=2000,
            cache_read_tokens=4000,
        )
        # (1000 * 15 + 500 * 75 + 2000 * 18.75 + 4000 * 1.5) / 1_000_000
        # = (15000 + 37500 + 37500 + 6000) / 1_000_000 = 0.096
        assert _estimate_cost_usd(usage) == pytest.approx(0.096)

    def test_long_context_tier_lookup_uses_full_model_string(self) -> None:
        """``[1m]`` suffix flows through the pricing lookup unchanged.

        ``_estimate_cost_usd`` keys by ``usage.model`` exactly; the
        long-context tier must hit its own pricing row, not collapse
        onto the standard ``claude-opus-4-7`` rate.
        """
        from maestro.sdk import QueryUsage, _estimate_cost_usd, configure_sdk_pricing

        configure_sdk_pricing({
            "claude-opus-4-7": {
                "input": 15.0, "output": 75.0,
                "cache_creation": 18.75, "cache_read": 1.5,
            },
            "claude-opus-4-7[1m]": {
                "input": 30.0, "output": 150.0,
                "cache_creation": 37.5, "cache_read": 3.0,
            },
        })
        usage = QueryUsage(
            model="claude-opus-4-7[1m]",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_tokens=2000,
            cache_read_tokens=4000,
        )
        # 2x the standard rate: 0.096 * 2 = 0.192
        assert _estimate_cost_usd(usage) == pytest.approx(0.192)

    def test_partial_pricing_table_uses_zero_for_missing_rates(self) -> None:
        from maestro.sdk import QueryUsage, _estimate_cost_usd, configure_sdk_pricing

        configure_sdk_pricing({
            "model-x": {"input": 10.0, "output": 20.0},
        })
        usage = QueryUsage(
            model="model-x",
            input_tokens=1_000_000,
            output_tokens=500_000,
            cache_read_tokens=999_999,
        )
        # cache_read rate is 0 -> contributes nothing.
        assert _estimate_cost_usd(usage) == pytest.approx(10.0 + 10.0)

    def test_configure_with_none_clears_table(self) -> None:
        from maestro.sdk import QueryUsage, _estimate_cost_usd, configure_sdk_pricing

        configure_sdk_pricing({"m": {"input": 1.0}})
        configure_sdk_pricing(None)
        usage = QueryUsage(model="m", input_tokens=1_000_000)
        assert _estimate_cost_usd(usage) is None

    def test_long_context_beta_routes_to_1m_pricing_row(self) -> None:
        """Long-context calls must hit the ``[1m]`` pricing row, not the standard one.

        Anthropic enables the 1M-token context window via the
        ``context-1m-2025-08-07`` beta header on the standard model
        identifier — there is no separate ``claude-opus-4-7[1m]`` model
        the user can set in ``[models] long_context`` (Anthropic would
        reject it). So the only path to the 1M-tier row is the SDK
        layer's ``_label_long_context_pricing`` post-processor, which
        appends the ``[1m]`` suffix when ``LONG_CONTEXT_BETA`` is in
        ``options.betas``. Without the post-processor, ``message.model``
        would carry the real model name, the lookup would land on the
        standard row, and 1M-tier calls would silently cost 2x what the
        estimator reports.
        """
        from maestro.sdk import (
            LONG_CONTEXT_BETA,
            QueryUsage,
            _estimate_cost_usd,
            _label_long_context_pricing,
            configure_sdk_pricing,
        )

        # Explicit, distinguishable tiers so the assertion proves the
        # routing to the ``[1m]`` row, independent of published rates.
        configure_sdk_pricing({
            "claude-opus-4-7": {"input": 15.0, "output": 75.0},
            "claude-opus-4-7[1m]": {"input": 30.0, "output": 150.0},
        })

        # Real Anthropic model name as it would arrive on
        # ``AssistantMessage.model``.
        real_model = "claude-opus-4-7"

        # Without the long-context beta, the label is unchanged and
        # pricing falls on the standard 1x row.
        assert _label_long_context_pricing(real_model, None) == real_model
        assert _label_long_context_pricing(real_model, []) == real_model
        assert (
            _label_long_context_pricing(real_model, ["unrelated-beta"])
            == real_model
        )

        # With the long-context beta, the suffix is appended so the
        # estimator hits the 1M-tier row.
        labeled = _label_long_context_pricing(real_model, [LONG_CONTEXT_BETA])
        assert labeled == "claude-opus-4-7[1m]"

        # Idempotent: a model that already carries the suffix is
        # returned unchanged (defends against double-application if
        # the post-processor is invoked twice on the same path).
        assert (
            _label_long_context_pricing(labeled, [LONG_CONTEXT_BETA])
            == labeled
        )
        # ``None`` model is left as-is.
        assert _label_long_context_pricing(None, [LONG_CONTEXT_BETA]) is None

        # End-to-end: the labeled model lands on the 1M-tier row,
        # delivering the documented 2x rate (was a silent
        # underestimate before the post-processor existed).
        usage = QueryUsage(
            model=labeled,
            input_tokens=1_000_000,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
        )
        cost = _estimate_cost_usd(usage)
        assert cost == 30.0, (
            f"1M-tier underestimated: expected $30, got ${cost}"
        )

    @pytest.mark.asyncio
    async def test_consume_messages_applies_long_context_label(self) -> None:
        """``_consume_messages`` rewrites ``usage.model`` when betas include the 1M flag.

        The real Anthropic ``AssistantMessage.model`` carries the
        un-suffixed model identifier; the post-processor in
        ``_consume_messages`` is what makes the lookup hit the 1M-tier
        row of ``[sdk.pricing]``.
        """
        from maestro.sdk import (
            LONG_CONTEXT_BETA,
            QueryUsage,
            _consume_messages,
        )

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="ok")],
                model="claude-opus-4-7",
            )
            yield _result_msg("done")

        usage = QueryUsage(caller="test")
        cr = await _consume_messages(
            gen(), "test", None,
            usage=usage,
            betas=[LONG_CONTEXT_BETA],
        )
        assert cr.usage.model == "claude-opus-4-7[1m]"

        # Without the beta, the real model name passes through unchanged.
        usage2 = QueryUsage(caller="test")

        async def gen2() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="ok")],
                model="claude-opus-4-7",
            )
            yield _result_msg("done")

        cr2 = await _consume_messages(
            gen2(), "test", None,
            usage=usage2,
            betas=None,
        )
        assert cr2.usage.model == "claude-opus-4-7"


class TestEarlyExitCostEmission:
    """Integration tests: predicate firing populates cost via the estimator."""

    def setup_method(self) -> None:
        from maestro.sdk import configure_sdk_pricing

        configure_sdk_pricing(None)

    def teardown_method(self) -> None:
        from maestro.sdk import configure_sdk_pricing

        configure_sdk_pricing(None)

    @pytest.mark.asyncio
    async def test_finalize_uses_estimator_when_pricing_configured(self) -> None:
        from maestro.sdk import (
            QueryUsage,
            _consume_messages,
            _finalize_usage,
            configure_sdk_pricing,
        )

        configure_sdk_pricing({
            "claude-opus-4-7": {"input": 15.0, "output": 75.0},
        })

        async def gen() -> Any:
            yield TaskProgressMessage(
                subtype="task_progress",
                data={},
                task_id="t1",
                description="working",
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 500,
                },
                uuid="u1",
                session_id="s1",
                last_tool_name=None,
            )
            yield AssistantMessage(
                content=[TextBlock(text="x" * 200)],
                model="claude-opus-4-7",
            )
            yield _result_msg("never")

        usage = QueryUsage(caller="test", model="claude-opus-4-7")
        cr = await _consume_messages(
            gen(), "test", None, usage=usage,
            complete_on=lambda _m, buf: len(buf) >= 100,
        )
        assert cr.early_exit is True
        result = _finalize_usage(cr, None)
        # (1000 * 15 + 500 * 75) / 1_000_000 = 0.0525
        assert result.usage.total_cost_usd == pytest.approx(0.0525)
        assert result.usage.estimated is True

    @pytest.mark.asyncio
    async def test_finalize_skips_estimator_without_model(self) -> None:
        """The pre-existing predicate test still sees zero/None cost when no model is set."""
        from maestro.sdk import _consume_messages, _finalize_usage

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="x" * 200)],
                model="claude-opus-4-7",
            )

        cr = await _consume_messages(
            gen(), "test", None,
            complete_on=lambda _m, buf: len(buf) >= 100,
        )
        result = _finalize_usage(cr, None)
        assert result.usage.total_cost_usd is None
        assert result.usage.estimated is False

    @pytest.mark.asyncio
    async def test_finalize_skips_estimator_for_unknown_model(self) -> None:
        from maestro.sdk import (
            QueryUsage,
            _consume_messages,
            _finalize_usage,
            configure_sdk_pricing,
        )

        configure_sdk_pricing({"some-other-model": {"input": 1.0}})

        async def gen() -> Any:
            yield TaskProgressMessage(
                subtype="task_progress", data={}, task_id="t1",
                description="working",
                usage={"input_tokens": 1000, "output_tokens": 500},
                uuid="u1", session_id="s1", last_tool_name=None,
            )
            yield AssistantMessage(
                content=[TextBlock(text="x" * 200)],
                model="claude-mystery",
            )

        usage = QueryUsage(caller="test", model="claude-mystery")
        cr = await _consume_messages(
            gen(), "test", None, usage=usage,
            complete_on=lambda _m, buf: len(buf) >= 100,
        )
        result = _finalize_usage(cr, None)
        # No pricing entry for ``claude-mystery`` -> cost stays unset,
        # ``estimated`` stays false (audit truth).
        assert result.usage.total_cost_usd is None
        assert result.usage.estimated is False

    @pytest.mark.asyncio
    async def test_non_early_exit_keeps_reported_cost(self) -> None:
        """Regression: the non-early-exit branch still reads cost from ResultMessage."""
        from maestro.sdk import (
            QueryUsage,
            _consume_messages,
            _finalize_usage,
            configure_sdk_pricing,
        )

        configure_sdk_pricing({
            "claude-opus-4-7": {"input": 15.0, "output": 75.0},
        })

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="hi")],
                model="claude-opus-4-7",
            )
            yield _result_msg("done", total_cost_usd=0.01)

        usage = QueryUsage(caller="test", model="claude-opus-4-7")
        cr = await _consume_messages(gen(), "test", None, usage=usage)
        result = _finalize_usage(cr, None)
        assert result.usage.total_cost_usd == pytest.approx(0.01)
        # The estimator never ran — this is a reported figure.
        assert result.usage.estimated is False

    @pytest.mark.asyncio
    async def test_early_exit_uses_assistant_message_usage(self) -> None:
        """Pure-text early-exit path picks up usage and model from AssistantMessage."""
        from maestro.sdk import (
            QueryUsage,
            _consume_messages,
            _finalize_usage,
            configure_sdk_pricing,
        )

        configure_sdk_pricing({
            "claude-opus-4-7": {"input": 15.0, "output": 75.0},
        })

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="x" * 200)],
                model="claude-opus-4-7",
                usage={"input_tokens": 1000, "output_tokens": 500},
            )

        usage = QueryUsage(caller="test")
        cr = await _consume_messages(
            gen(), "test", None, usage=usage,
            complete_on=lambda _m, buf: len(buf) >= 100,
        )
        assert cr.early_exit is True
        result = _finalize_usage(cr, None)
        assert result.usage.input_tokens == 1000
        assert result.usage.output_tokens == 500
        assert result.usage.model == "claude-opus-4-7"
        # (1000 * 15 + 500 * 75) / 1_000_000 = 0.0525
        assert result.usage.total_cost_usd == pytest.approx(0.0525)
        assert result.usage.estimated is True

    @pytest.mark.asyncio
    async def test_early_exit_assistant_usage_captures_cache_tokens(
        self,
    ) -> None:
        """Cache creation/read splits are extracted from AssistantMessage.usage."""
        from maestro.sdk import (
            QueryUsage,
            _consume_messages,
            _finalize_usage,
            configure_sdk_pricing,
        )

        configure_sdk_pricing({
            "claude-opus-4-7": {"input": 15.0, "output": 75.0},
        })

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="x" * 200)],
                model="claude-opus-4-7",
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 400,
                    "cache_read_input_tokens": 250,
                },
            )

        cr = await _consume_messages(
            gen(), "test", None, usage=QueryUsage(caller="test"),
            complete_on=lambda _m, buf: len(buf) >= 100,
        )
        assert cr.early_exit is True
        result = _finalize_usage(cr, None)
        assert result.usage.cache_creation_tokens == 400
        assert result.usage.cache_read_tokens == 250

    @pytest.mark.asyncio
    async def test_early_exit_overwrites_with_latest_assistant_usage(
        self,
    ) -> None:
        """AssistantMessage.usage is cumulative — the latest message wins."""
        from maestro.sdk import (
            QueryUsage,
            _consume_messages,
            _finalize_usage,
            configure_sdk_pricing,
        )

        configure_sdk_pricing({
            "claude-opus-4-7": {"input": 15.0, "output": 75.0},
        })

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="x" * 60)],
                model="claude-opus-4-7",
                usage={"input_tokens": 100, "output_tokens": 50},
            )
            yield AssistantMessage(
                content=[TextBlock(text="x" * 60)],
                model="claude-opus-4-7",
                usage={"input_tokens": 250, "output_tokens": 200},
            )

        cr = await _consume_messages(
            gen(), "test", None, usage=QueryUsage(caller="test"),
            complete_on=lambda _m, buf: len(buf) >= 100,
        )
        assert cr.early_exit is True
        result = _finalize_usage(cr, None)
        assert result.usage.input_tokens == 250
        assert result.usage.output_tokens == 200

    @pytest.mark.asyncio
    async def test_assistant_usage_overrides_requested_model_on_fallback(
        self,
    ) -> None:
        """When the SDK falls back to a secondary model, AssistantMessage.model wins."""
        from maestro.sdk import (
            QueryUsage,
            _consume_messages,
            _finalize_usage,
            configure_sdk_pricing,
        )

        configure_sdk_pricing({
            "claude-opus-4-7": {"input": 15.0, "output": 75.0},
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
        })

        async def gen() -> Any:
            yield AssistantMessage(
                content=[TextBlock(text="x" * 200)],
                model="claude-sonnet-4-6",
                usage={"input_tokens": 1000, "output_tokens": 500},
            )

        usage = QueryUsage(caller="test", model="claude-opus-4-7")
        cr = await _consume_messages(
            gen(), "test", None, usage=usage,
            complete_on=lambda _m, buf: len(buf) >= 100,
        )
        result = _finalize_usage(cr, None)
        assert result.usage.model == "claude-sonnet-4-6"
        # Sonnet pricing: (1000 * 3 + 500 * 15) / 1_000_000 = 0.0105
        assert result.usage.total_cost_usd == pytest.approx(0.0105)
        assert result.usage.estimated is True
