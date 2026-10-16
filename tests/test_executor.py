"""Tests for Executor — prompt construction and step execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.config import Config
from maestro.executor import (
    _BASH_HARD_CAP,
    _BASH_HARD_CAP_LONG_CONTEXT,
    Executor,
    StepResult,
    _compress_bash_output,
    _detect_tool_loop,
    _is_productive,
)
from maestro.memory import PlanStep
from maestro.sdk import (
    LONG_CONTEXT_BETA,
    ErrorCategory,
    QueryResult,
    SDKError,
    ToolCall,
)


def _make_step(description: str = "Do the thing") -> PlanStep:
    return PlanStep(step_id="step-1", description=description)


@pytest.mark.asyncio
async def test_executor_prompt_no_double_blank_lines_with_artifacts_dir() -> None:
    """No double blank lines appear in the executor prompt when artifacts_dir is provided."""
    executor = Executor()
    step = _make_step("Analyse the codebase")

    captured_prompt: list[str] = []

    async def fake_query(**kwargs: object) -> QueryResult:  # type: ignore[return]
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return QueryResult(response="done")

    with patch("maestro.executor.query", side_effect=fake_query):
        await executor.execute(
            step,
            workspace="/workspace",
            identity_context="",
            artifacts_dir="/workspace/artifacts/task-123",
        )

    assert captured_prompt, "query was never called"
    prompt = captured_prompt[0]

    # No double blank lines (three consecutive newlines = two blank lines in a row)
    assert "\n\n\n" not in prompt, (
        "Executor prompt contains a double blank line when artifacts_dir is provided"
    )

    # Artifacts directory line is present
    assert "Artifacts directory:" in prompt
    assert "/workspace/artifacts/task-123" in prompt

    # Task header is present
    assert "Task: Analyse the codebase" in prompt


@pytest.mark.asyncio
async def test_executor_prompt_no_double_blank_lines_without_artifacts_dir() -> None:
    """No double blank lines appear in the executor prompt when artifacts_dir is absent."""
    executor = Executor()
    step = _make_step("Write a report")

    captured_prompt: list[str] = []

    async def fake_query(**kwargs: object) -> QueryResult:  # type: ignore[return]
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return QueryResult(response="done")

    with patch("maestro.executor.query", side_effect=fake_query):
        await executor.execute(
            step,
            workspace="/workspace",
            identity_context="",
            artifacts_dir=None,
        )

    assert captured_prompt, "query was never called"
    prompt = captured_prompt[0]

    assert "\n\n\n" not in prompt
    assert "Artifacts directory:" not in prompt
    assert "Task: Write a report" in prompt


async def _capture_executor_prompt(artifacts_dir: str | None) -> str:
    """Helper: run executor and return the prompt string passed to query."""
    executor = Executor()
    step = _make_step("Check something")
    captured: list[str] = []

    async def fake_query(**kwargs: object) -> QueryResult:  # type: ignore[return]
        captured.append(str(kwargs.get("prompt", "")))
        return QueryResult(response="ok")

    with patch("maestro.executor.query", side_effect=fake_query):
        await executor.execute(
            step,
            workspace="/workspace",
            identity_context="",
            artifacts_dir=artifacts_dir,
        )
    return captured[0]


@pytest.mark.asyncio
async def test_executor_prompt_consistent_spacing_with_artifacts() -> None:
    """Workspace line is followed by exactly one blank line when artifacts_dir is set."""
    prompt = await _capture_executor_prompt("/tmp/artifacts")
    lines = prompt.split("\n")
    workspace_idx = next(
        i for i, line in enumerate(lines) if line.startswith("Execute this task")
    )
    assert lines[workspace_idx + 1] == "", "Expected blank line after workspace line"
    assert lines[workspace_idx + 2] != "", "Got double blank line after workspace"


@pytest.mark.asyncio
async def test_executor_prompt_consistent_spacing_without_artifacts() -> None:
    """Workspace line is followed by exactly one blank line when artifacts_dir is None."""
    prompt = await _capture_executor_prompt(None)
    lines = prompt.split("\n")
    workspace_idx = next(
        i for i, line in enumerate(lines) if line.startswith("Execute this task")
    )
    assert lines[workspace_idx + 1] == "", "Expected blank line after workspace line"
    assert lines[workspace_idx + 2] != "", "Got double blank line after workspace"


@pytest.mark.asyncio
async def test_executor_empty_step_description_skips_api_call() -> None:
    """Executor returns failure immediately when step description is empty."""
    executor = Executor()
    step = _make_step("")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        result = await executor.execute(
            step, workspace="/workspace", identity_context=""
        )

    mock_q.assert_not_called()
    assert result.success is False
    assert result.error is not None


@pytest.mark.asyncio
async def test_executor_returns_step_result_on_success() -> None:
    """Executor wraps SDK response in a successful StepResult."""
    executor = Executor()
    step = _make_step("List files")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response="Listed 42 files")
        result = await executor.execute(
            step, workspace="/workspace", identity_context=""
        )

    assert result.success is True
    assert result.output == "Listed 42 files"
    assert result.error is None


# ---------------------------------------------------------------------------
# Telegram tool integration
# ---------------------------------------------------------------------------


def _make_telegram_mcp_servers() -> dict[str, Any]:
    """Return mcp_servers dict with a fake telegram server registered."""
    from maestro.telegram_tools import SERVER_KEY
    return {SERVER_KEY: MagicMock()}


@pytest.mark.asyncio
async def test_build_telegram_tool_names_present() -> None:
    """Tool names are returned when the telegram server key is in mcp_servers."""
    from maestro.telegram_tools import TOOL_NAMES
    executor = Executor(mcp_servers=_make_telegram_mcp_servers())
    names = executor._build_telegram_tool_names()
    assert names == list(TOOL_NAMES)


@pytest.mark.asyncio
async def test_build_telegram_tool_names_absent() -> None:
    """Empty list when telegram server is not registered."""
    executor = Executor(mcp_servers={"other": MagicMock()})
    names = executor._build_telegram_tool_names()
    assert names == []


@pytest.mark.asyncio
async def test_build_telegram_tool_names_no_mcp() -> None:
    """Empty list when no mcp_servers at all."""
    executor = Executor()
    names = executor._build_telegram_tool_names()
    assert names == []


@pytest.mark.asyncio
async def test_executor_prompt_includes_telegram_line() -> None:
    """Telegram tools description appears in prompt when configured."""
    executor = Executor(mcp_servers=_make_telegram_mcp_servers())
    step = _make_step("Send update")
    captured: list[str] = []

    async def fake_query(**kwargs: object) -> QueryResult:  # type: ignore[return]
        captured.append(str(kwargs.get("prompt", "")))
        return QueryResult(response="done")

    with patch("maestro.executor.query", side_effect=fake_query):
        await executor.execute(step, workspace="/w", identity_context="")

    assert captured
    assert "telegram_send" in captured[0]
    assert "telegram_read_messages" in captured[0]


@pytest.mark.asyncio
async def test_executor_prompt_excludes_telegram_line_when_absent() -> None:
    """No telegram mention when telegram is not configured."""
    executor = Executor()
    step = _make_step("Do work")
    captured: list[str] = []

    async def fake_query(**kwargs: object) -> QueryResult:  # type: ignore[return]
        captured.append(str(kwargs.get("prompt", "")))
        return QueryResult(response="done")

    with patch("maestro.executor.query", side_effect=fake_query):
        await executor.execute(step, workspace="/w", identity_context="")

    assert captured
    assert "telegram_send" not in captured[0]


@pytest.mark.asyncio
async def test_executor_prompt_distinguishes_messaging_and_telegram() -> None:
    """Dual-configured prompt distinguishes the durable messaging tools from
    the transient Telegram tools and steers durable sends to
    send_message_to_thomas."""
    executor = Executor(
        notifier=MagicMock(),
        message_store=MagicMock(),
        mcp_servers=_make_telegram_mcp_servers(),
    )
    step = _make_step("Send an update")
    captured: list[str] = []

    async def fake_query(**kwargs: object) -> QueryResult:  # type: ignore[return]
        captured.append(str(kwargs.get("prompt", "")))
        return QueryResult(response="done")

    with (
        patch("maestro.executor.query", side_effect=fake_query),
        patch("maestro.tools.create_inproc_mcp_server", return_value="mock_server"),
    ):
        await executor.execute(step, workspace="/w", identity_context="")

    assert captured
    prompt = captured[0]
    # Both tool sets are registered on a dual-configured executor.
    assert "send_message_to_thomas" in prompt
    assert "telegram_send" in prompt
    # Messaging tools are described as the durable, recorded channel.
    assert "durable, recorded channel" in prompt
    assert "persists" in prompt
    # Telegram tools are described as transient / not recorded.
    assert "NOT recorded" in prompt
    # The prompt steers durable messages to send_message_to_thomas.
    assert "prefer send_message_to_thomas" in prompt
    # The existing double-blank-line spacing guard still holds.
    assert "\n\n\n" not in prompt


@pytest.mark.asyncio
async def test_executor_telegram_tools_in_allowed_tools() -> None:
    """Telegram tool names appear in allowed_tools passed to SDK query."""
    from maestro.telegram_tools import TOOL_NAMES
    executor = Executor(mcp_servers=_make_telegram_mcp_servers())
    step = _make_step("Check messages")
    captured_opts: list[Any] = []

    async def fake_query(**kwargs: object) -> QueryResult:  # type: ignore[return]
        captured_opts.append(kwargs.get("options"))
        return QueryResult(response="done")

    with patch("maestro.executor.query", side_effect=fake_query):
        await executor.execute(step, workspace="/w", identity_context="")

    assert captured_opts
    opts = captured_opts[0]
    for tool_name in TOOL_NAMES:
        assert tool_name in opts.allowed_tools


# ---------------------------------------------------------------------------
# SDK error propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_sdk_error_propagates() -> None:
    """SDKError propagates out of execute() instead of being caught."""
    executor = Executor()
    step = _make_step("Failing step")

    sdk_err = SDKError(
        "Command failed with exit code 1",
        stderr="fatal: segfault in handler",
        category=ErrorCategory.CLI_CRASH,
        retries_attempted=3,
        retries_exhausted=True,
    )

    with patch("maestro.executor.query", new_callable=AsyncMock, side_effect=sdk_err), \
         pytest.raises(SDKError) as exc_info:
        await executor.execute(
            step, workspace="/workspace", identity_context="",
        )

    assert "segfault in handler" in exc_info.value.detail
    assert "exit code 1" in str(exc_info.value)
    assert exc_info.value.retries_exhausted


# ---------------------------------------------------------------------------
# Prior steps context injection
# ---------------------------------------------------------------------------


async def _capture_prompt_with_prior_steps(
    prior_steps: list[PlanStep] | None,
    artifacts_dir: str | None = None,
) -> str:
    """Helper: run executor with prior_steps and return the prompt."""
    executor = Executor()
    step = _make_step("Next step")
    captured: list[str] = []

    async def fake_query(**kwargs: object) -> QueryResult:  # type: ignore[return]
        captured.append(str(kwargs.get("prompt", "")))
        return QueryResult(response="ok")

    with patch("maestro.executor.query", side_effect=fake_query):
        await executor.execute(
            step,
            workspace="/workspace",
            identity_context="",
            artifacts_dir=artifacts_dir,
            prior_steps=prior_steps,
        )
    return captured[0]


@pytest.mark.asyncio
async def test_prior_steps_appear_in_prompt() -> None:
    """Completed prior steps are injected into the executor prompt."""
    prior = [
        PlanStep(step_id="s1", description="Research API", status="done", result="Found REST endpoints"),
    ]
    prompt = await _capture_prompt_with_prior_steps(prior)
    assert "Prior steps completed:" in prompt
    assert "Research API" in prompt
    assert "Found REST endpoints" in prompt


@pytest.mark.asyncio
async def test_no_prior_context_when_none() -> None:
    """No prior context section when prior_steps is None."""
    prompt = await _capture_prompt_with_prior_steps(None)
    assert "Prior steps completed:" not in prompt


@pytest.mark.asyncio
async def test_no_prior_context_when_empty() -> None:
    """No prior context section when prior_steps is an empty list."""
    prompt = await _capture_prompt_with_prior_steps([])
    assert "Prior steps completed:" not in prompt


@pytest.mark.asyncio
async def test_prior_step_result_truncated_to_200_chars() -> None:
    """Per-step result is truncated to 200 characters."""
    long_result = "x" * 300
    prior = [
        PlanStep(step_id="s1", description="Long step", status="done", result=long_result),
    ]
    prompt = await _capture_prompt_with_prior_steps(prior)
    # The full 300-char result should NOT appear
    assert long_result not in prompt
    # But the first 200 chars should
    assert "x" * 200 in prompt


@pytest.mark.asyncio
async def test_prior_context_capped_at_2000_chars() -> None:
    """Total prior context is capped at approximately 2000 characters."""
    # Each step contributes ~210 chars (description + 200-char result)
    prior = [
        PlanStep(step_id=f"s{i}", description=f"Step {i}", status="done", result="R" * 200)
        for i in range(20)
    ]
    prompt = await _capture_prompt_with_prior_steps(prior)
    # Extract the prior context section
    assert "Prior steps completed:" in prompt
    start = prompt.index("Prior steps completed:")
    end = prompt.index("Task:")
    prior_section = prompt[start:end]
    assert len(prior_section) <= 2200  # 2000 budget + header + trailing newlines


@pytest.mark.asyncio
async def test_prior_step_description_truncated_to_200_chars() -> None:
    """Per-step description is truncated to 200 characters."""
    long_description = "d" * 300
    prior = [
        PlanStep(step_id="s1", description=long_description, status="done", result="short"),
    ]
    prompt = await _capture_prompt_with_prior_steps(prior)
    # The full 300-char description should NOT appear
    assert long_description not in prompt
    # But the first 200 chars should
    assert "d" * 200 in prompt


@pytest.mark.asyncio
async def test_oversized_prior_step_does_not_drop_later_steps() -> None:
    """A single over-budget entry does not skip subsequent prior steps."""
    # Ten normal-sized steps each contribute ~210 chars; after them the
    # 2000-char budget is exhausted. The 11th step has a description that
    # blows the remaining budget, and the 12th step is short enough to fit
    # if the loop continues rather than breaks.
    prior = [
        PlanStep(
            step_id=f"s{i}",
            description=f"Normal step {i}",
            status="done",
            result="R" * 200,
        )
        for i in range(10)
    ]
    prior.append(
        PlanStep(
            step_id="oversized",
            description="O" * 200,
            status="done",
            result="O" * 200,
        )
    )
    prior.append(
        PlanStep(
            step_id="tail",
            description="TailMarker",
            status="done",
            result="tail-result",
        )
    )
    prompt = await _capture_prompt_with_prior_steps(prior)
    # The short trailing step must appear even though the oversized entry
    # came before it and could not fit in the remaining budget.
    assert "TailMarker" in prompt


@pytest.mark.asyncio
async def test_no_triple_newlines_with_artifacts_and_prior_steps() -> None:
    """No triple newlines when both artifacts_dir and prior_steps are present."""
    prior = [
        PlanStep(step_id="s1", description="Step 1", status="done", result="Done"),
    ]
    prompt = await _capture_prompt_with_prior_steps(prior, artifacts_dir="/tmp/art")
    assert "\n\n\n" not in prompt
    assert "Artifacts directory:" in prompt
    assert "Prior steps completed:" in prompt
    assert "Task:" in prompt


@pytest.mark.asyncio
async def test_prior_step_none_result_handled() -> None:
    """Steps with None result show 'No result' instead of crashing."""
    prior = [
        PlanStep(step_id="s1", description="Silent step", status="done", result=None),
    ]
    prompt = await _capture_prompt_with_prior_steps(prior)
    assert "No result" in prompt


@pytest.mark.asyncio
async def test_executor_non_sdk_error_uses_str() -> None:
    """Non-SDKError exceptions produce str(e) in StepResult.error."""
    executor = Executor()
    step = _make_step("Another failing step")

    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=RuntimeError("something unexpected"),
    ):
        result = await executor.execute(
            step, workspace="/workspace", identity_context="",
        )

    assert result.success is False
    assert result.error == "something unexpected"
    assert result.error_category is None
    assert result.exit_code is None
    assert result.sdk_error is None


# ---------------------------------------------------------------------------
# StepResult — structured error context (Phase 2 plumbing)
# ---------------------------------------------------------------------------


def test_step_result_default_fields_are_none() -> None:
    """New structured error fields default to None on a plain StepResult."""
    ok = StepResult(success=True, output="done")
    assert ok.error_category is None
    assert ok.exit_code is None
    assert ok.sdk_error is None

    failed = StepResult(success=False, output="", error="boom")
    assert failed.error_category is None
    assert failed.exit_code is None
    assert failed.sdk_error is None


def test_step_result_from_sdk_error_classmethod() -> None:
    """from_sdk_error populates error/category/exit_code/sdk_error."""
    exc = SDKError(
        "Command failed with exit code 1",
        stderr="fatal",
        category=ErrorCategory.CLI_CRASH,
        exit_code=1,
        retries_attempted=2,
    )
    result = StepResult.from_sdk_error(exc)
    assert result.success is False
    assert result.output == ""
    assert result.error == str(exc)
    assert result.error_category is ErrorCategory.CLI_CRASH
    assert result.exit_code == 1
    assert result.sdk_error is exc


# ---------------------------------------------------------------------------
# Tool loop detection
# ---------------------------------------------------------------------------


class TestDetectToolLoop:
    def test_distinct_calls_no_loop(self) -> None:
        calls = [
            ToolCall(name="Read", input={"path": "/a"}, result="content a"),
            ToolCall(name="Write", input={"path": "/b"}, result="ok"),
            ToolCall(name="Bash", input={"cmd": "ls"}, result="files"),
        ]
        detected, msg = _detect_tool_loop(calls, threshold=3)
        assert detected is False
        assert msg is None

    def test_consecutive_identical_triggers(self) -> None:
        tc = ToolCall(name="Read", input={"path": "/a"}, result="same")
        calls = [tc, tc, tc]
        detected, msg = _detect_tool_loop(calls, threshold=3)
        assert detected is True
        assert msg is not None
        assert "Read" in msg
        assert "3" in msg

    def test_interleaved_not_flagged(self) -> None:
        a = ToolCall(name="Read", input={"path": "/a"}, result="x")
        b = ToolCall(name="Write", input={"path": "/b"}, result="y")
        calls = [a, b, a, b, a, b]
        detected, _ = _detect_tool_loop(calls, threshold=3)
        assert detected is False

    def test_different_inputs_not_flagged(self) -> None:
        calls = [
            ToolCall(name="Read", input={"path": f"/{i}"}, result="ok")
            for i in range(5)
        ]
        detected, _ = _detect_tool_loop(calls, threshold=3)
        assert detected is False

    def test_below_threshold_not_flagged(self) -> None:
        tc = ToolCall(name="Bash", input={"cmd": "ls"}, result="files")
        calls = [tc, tc]
        detected, _ = _detect_tool_loop(calls, threshold=3)
        assert detected is False

    def test_too_few_calls(self) -> None:
        tc = ToolCall(name="Bash", input={"cmd": "ls"}, result="files")
        detected, _ = _detect_tool_loop([tc], threshold=3)
        assert detected is False

    def test_none_result_handled(self) -> None:
        tc = ToolCall(name="Read", input={"path": "/a"}, result=None)
        calls = [tc, tc, tc]
        detected, _msg = _detect_tool_loop(calls, threshold=3)
        assert detected is True

    # -- Edge case 1: hash collision across different tools ----------------
    def test_different_tools_same_input_result_no_collision(self) -> None:
        """Two different tool names with identical input and result must NOT
        collide because the fingerprint includes ``tc.name``."""
        shared_input: dict[str, Any] = {"path": "/same"}
        shared_result = "identical output"
        a = ToolCall(name="Read", input=shared_input, result=shared_result)
        b = ToolCall(name="Write", input=shared_input, result=shared_result)
        # Alternate so each tool repeats 3 times total but never
        # consecutively with the *same* fingerprint.
        calls = [a, b, a, b, a, b]
        detected, _ = _detect_tool_loop(calls, threshold=3)
        assert detected is False

    def test_same_tool_same_input_result_triggers(self) -> None:
        """Sanity counterpart: same tool name + same input/result DOES
        trigger, proving name is part of the fingerprint."""
        tc = ToolCall(
            name="Read", input={"path": "/same"}, result="identical output",
        )
        calls = [tc, tc, tc]
        detected, msg = _detect_tool_loop(calls, threshold=3)
        assert detected is True
        assert msg is not None

    # -- Edge case 2: identical serialisation ⇒ same fingerprint -----------
    def test_identical_serialisation_triggers(self) -> None:
        """Dicts that differ only in insertion order serialize identically
        (``sort_keys=True``) and therefore share a fingerprint.  Detection
        must fire when the same logical input repeats."""
        input_a: dict[str, Any] = {"x": 1, "y": 2}
        input_b: dict[str, Any] = {"y": 2, "x": 1}
        # They serialize to the same JSON with sort_keys — verify.
        import json
        assert json.dumps(input_a, sort_keys=True) == json.dumps(
            input_b, sort_keys=True,
        )
        calls = [
            ToolCall(name="Bash", input=input_a, result="ok"),
            ToolCall(name="Bash", input=input_b, result="ok"),
            ToolCall(name="Bash", input=input_a, result="ok"),
        ]
        detected, msg = _detect_tool_loop(calls, threshold=3)
        assert detected is True
        assert msg is not None
        assert "Bash" in msg

    # -- Edge case 3: threshold boundary -----------------------------------
    def test_exactly_at_threshold_triggers(self) -> None:
        """A streak of exactly ``threshold`` identical calls triggers."""
        tc = ToolCall(name="Grep", input={"q": "x"}, result="found")
        for threshold in (2, 3, 5):
            calls = [tc] * threshold
            detected, msg = _detect_tool_loop(calls, threshold=threshold)
            assert detected is True, f"threshold={threshold}"
            assert msg is not None

    def test_one_below_threshold_does_not_trigger(self) -> None:
        """A streak of ``threshold - 1`` identical calls must NOT trigger."""
        tc = ToolCall(name="Grep", input={"q": "x"}, result="found")
        for threshold in (2, 3, 5):
            calls = [tc] * (threshold - 1)
            detected, _ = _detect_tool_loop(calls, threshold=threshold)
            assert detected is False, f"threshold={threshold}"

    # -- Edge case 4: interleaved repeating / non-repeating ----------------
    def test_interleaved_non_repeating_resets_streak(self) -> None:
        """A single different call in the middle resets the streak, so
        ``threshold + 1`` total repeats with an interruption should NOT
        trigger."""
        tc = ToolCall(name="Read", input={"path": "/x"}, result="data")
        other = ToolCall(name="Write", input={"path": "/y"}, result="ok")
        threshold = 3
        # threshold - 1 repeats, then an interruption, then threshold - 1
        # again.  Total identical calls = 2*(threshold-1) = 4 > threshold,
        # but the consecutive streak never reaches threshold.
        calls = [tc] * (threshold - 1) + [other] + [tc] * (threshold - 1)
        detected, _ = _detect_tool_loop(calls, threshold=threshold)
        assert detected is False

    def test_interleaved_streak_resumes_after_reset(self) -> None:
        """After an interruption, if the streak reaches threshold again on
        the *second* run, detection fires."""
        tc = ToolCall(name="Read", input={"path": "/x"}, result="data")
        other = ToolCall(name="Write", input={"path": "/y"}, result="ok")
        threshold = 3
        # 1 repeat, interruption, then threshold repeats → fires.
        calls = [tc] + [other] + [tc] * threshold
        detected, msg = _detect_tool_loop(calls, threshold=threshold)
        assert detected is True
        assert msg is not None

    # -- Edge case 7: fingerprint with None/missing result -----------------
    def test_none_result_produces_valid_fingerprint(self) -> None:
        """A ToolCall with ``result=None`` hashes the empty string and still
        produces a deterministic fingerprint.  Three consecutive calls with
        the same name, input, and ``result=None`` must trigger detection."""
        tc = ToolCall(name="Bash", input={"cmd": "true"}, result=None)
        calls = [tc, tc, tc]
        detected, msg = _detect_tool_loop(calls, threshold=3)
        assert detected is True
        assert msg is not None
        assert "Bash" in msg

    def test_none_vs_empty_string_result_same_fingerprint(self) -> None:
        """``result=None`` is coerced to ``""`` before hashing, so a call
        with ``result=None`` and one with ``result=""`` share a fingerprint
        and count toward the same streak."""
        tc_none = ToolCall(name="Read", input={"path": "/a"}, result=None)
        tc_empty = ToolCall(name="Read", input={"path": "/a"}, result="")
        calls = [tc_none, tc_empty, tc_none]
        detected, msg = _detect_tool_loop(calls, threshold=3)
        assert detected is True
        assert msg is not None

    def test_none_result_different_inputs_no_trigger(self) -> None:
        """Consecutive calls with ``result=None`` but different inputs must
        NOT trigger — the input hash differentiates them."""
        calls = [
            ToolCall(name="Read", input={"path": f"/{i}"}, result=None)
            for i in range(5)
        ]
        detected, _ = _detect_tool_loop(calls, threshold=3)
        assert detected is False


# ---------------------------------------------------------------------------
# _is_productive unit tests
# ---------------------------------------------------------------------------


class TestIsProductive:
    def test_productive_with_response(self) -> None:
        """A QueryResult with non-empty response is productive."""
        result = QueryResult(response="I did the thing")
        assert _is_productive(result) is True

    def test_productive_tool_only_success(self) -> None:
        """No response + at least one non-error tool call → productive."""
        result = QueryResult(
            response="",
            tool_calls=[
                ToolCall(name="Write", input={"path": "/x"}, result="ok", is_error=False),
            ],
        )
        assert _is_productive(result) is True

    def test_unproductive_error_tools_only(self) -> None:
        """No response + all tool calls have is_error=True → unproductive."""
        result = QueryResult(
            response="",
            tool_calls=[
                ToolCall(name="Bash", input={"cmd": "x"}, result="fail", is_error=True),
                ToolCall(name="Read", input={"path": "/y"}, result="err", is_error=True),
            ],
        )
        assert _is_productive(result) is False

    def test_productive_mixed_error_and_success_tools(self) -> None:
        """No response + mixed error/success tools → productive (any success suffices)."""
        result = QueryResult(
            response="",
            tool_calls=[
                ToolCall(name="Bash", input={"cmd": "x"}, result="fail", is_error=True),
                ToolCall(name="Write", input={"path": "/a"}, result="ok", is_error=False),
            ],
        )
        assert _is_productive(result) is True

    def test_unproductive_no_response_no_tools(self) -> None:
        """No response + empty tool list → unproductive."""
        result = QueryResult(response="", tool_calls=[])
        assert _is_productive(result) is False


# ---------------------------------------------------------------------------
# Additional _detect_tool_loop edge cases
# ---------------------------------------------------------------------------


class TestDetectToolLoopEdgeCases:
    def test_empty_list_returns_false(self) -> None:
        """Empty tool-call list → not a loop."""
        detected, msg = _detect_tool_loop([], threshold=3)
        assert detected is False
        assert msg is None

    def test_same_input_different_result_no_loop(self) -> None:
        """Identical name+input but distinct results → distinct fingerprints → no loop."""
        calls = [
            ToolCall(name="Read", input={"path": "/a"}, result=f"content-{i}")
            for i in range(3)
        ]
        detected, msg = _detect_tool_loop(calls, threshold=3)
        assert detected is False
        assert msg is None

    def test_streak_resets_then_triggers_later(self) -> None:
        """Streak resets on a different call, then a later streak triggers detection."""
        a = ToolCall(name="Read", input={"path": "/a"}, result="same-a")
        b = ToolCall(name="Write", input={"path": "/b"}, result="same-b")
        # [A, A, B, B, B] — A's streak=2 (below 3), then B's streak=3 (triggers)
        calls = [a, a, b, b, b]
        detected, msg = _detect_tool_loop(calls, threshold=3)
        assert detected is True
        assert msg is not None
        assert "Write" in msg


@pytest.mark.asyncio
async def test_executor_loop_detection_integration() -> None:
    """Executor returns loop_detected=True when tool calls repeat."""
    tc = ToolCall(name="Read", input={"path": "/stuck"}, result="same")
    executor = Executor(loop_detect_repeats=3)
    step = _make_step("Do something")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(
            response="I tried reading the file",
            tool_calls=[tc, tc, tc],
        )
        result = await executor.execute(
            step, workspace="/workspace", identity_context="",
        )

    assert result.success is False
    assert result.loop_detected is True
    assert "loop detected" in (result.error or "")
    # Synthetic failure — not an SDK error, so structured fields stay None.
    assert result.error_category is None
    assert result.exit_code is None
    assert result.sdk_error is None


@pytest.mark.asyncio
async def test_executor_no_loop_when_distinct_tools() -> None:
    """Executor returns success when tool calls are varied."""
    calls = [
        ToolCall(name="Read", input={"path": "/a"}, result="a"),
        ToolCall(name="Write", input={"path": "/b"}, result="ok"),
        ToolCall(name="Bash", input={"cmd": "ls"}, result="files"),
    ]
    executor = Executor(loop_detect_repeats=3)
    step = _make_step("Do something")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(
            response="Done",
            tool_calls=calls,
        )
        result = await executor.execute(
            step, workspace="/workspace", identity_context="",
        )

    assert result.success is True
    assert result.loop_detected is False


# ---------------------------------------------------------------------------
# Edge case 8: loop detection is step-scoped, not session-scoped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_detection_does_not_carry_across_execute_calls() -> None:
    """Tool calls from a prior ``execute()`` call must NOT influence loop
    detection in a subsequent call.  This proves that replanned steps start
    with a clean slate — ``_process_step_result`` inspects only the
    ``result.tool_calls`` returned by the current query."""
    tc = ToolCall(name="Read", input={"path": "/stuck"}, result="same")
    executor = Executor(loop_detect_repeats=3)

    # Step 1 returns 2 identical tool calls — below the threshold of 3.
    step_a = _make_step("Step A")
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(
            response="partial progress",
            tool_calls=[tc, tc],  # 2 repeats, threshold=3 → no trigger
        )
        result_a = await executor.execute(
            step_a, workspace="/w", identity_context="",
        )

    assert result_a.success is True
    assert result_a.loop_detected is False

    # Step 2 also returns 2 identical tool calls.  If tool_calls from
    # step 1 leaked into step 2, the combined 4 repeats would trigger
    # the threshold of 3.  Detection must NOT fire.
    step_b = _make_step("Step B (replanned)")
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(
            response="also partial",
            tool_calls=[tc, tc],  # 2 repeats, threshold=3 → no trigger
        )
        result_b = await executor.execute(
            step_b, workspace="/w", identity_context="",
        )

    assert result_b.success is True
    assert result_b.loop_detected is False


@pytest.mark.asyncio
async def test_loop_detection_fires_within_single_step_only() -> None:
    """Counterpart: loop detection fires when the threshold is met within
    a single ``execute()`` call, confirming it works in isolation."""
    tc = ToolCall(name="Read", input={"path": "/stuck"}, result="same")
    executor = Executor(loop_detect_repeats=3)

    # Step 1 is clean.
    step_a = _make_step("Clean step")
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(
            response="ok",
            tool_calls=[tc],  # only 1
        )
        result_a = await executor.execute(
            step_a, workspace="/w", identity_context="",
        )

    assert result_a.success is True
    assert result_a.loop_detected is False

    # Step 2 independently hits threshold.
    step_b = _make_step("Loopy step")
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(
            response="stuck",
            tool_calls=[tc, tc, tc],  # 3 repeats = threshold → fire
        )
        result_b = await executor.execute(
            step_b, workspace="/w", identity_context="",
        )

    assert result_b.success is False
    assert result_b.loop_detected is True
    assert "loop detected" in (result_b.error or "")


# ---------------------------------------------------------------------------
# Bash output compression
# ---------------------------------------------------------------------------


class TestCompressBashOutput:
    def test_ansi_strip(self) -> None:
        output = "\x1b[31mERROR\x1b[0m: something broke"
        result = _compress_bash_output(output)
        assert "\x1b[" not in result
        assert "ERROR" in result
        assert "something broke" in result

    def test_progress_glyphs_stripped(self) -> None:
        output = "Progress: ████████░░░░ 60%"
        result = _compress_bash_output(output)
        assert "█" not in result
        assert "░" not in result
        assert "60%" in result

    def test_long_equals_collapsed(self) -> None:
        output = "header\n" + "=" * 40 + "\ncontent"
        result = _compress_bash_output(output)
        assert "=" * 40 not in result
        assert "=...=" in result
        assert "header" in result
        assert "content" in result

    def test_long_plus_collapsed(self) -> None:
        output = "+" * 20 + " border"
        result = _compress_bash_output(output)
        assert "+" * 20 not in result
        assert "+...+" in result

    def test_line_collapse(self) -> None:
        output = "\n".join(["same line"] * 50)
        result = _compress_bash_output(output)
        # One copy of the original line survives, plus a marker for the rest.
        assert result.count("same line") == 1
        assert "[line repeated 49 more times]" in result

    def test_line_collapse_preserves_repeated_error_line(self) -> None:
        # A run of identical diagnostics must stay legible: the original error
        # text survives the collapse alongside the repeat-count marker.
        output = "\n".join(["ERROR: connection refused"] * 40)
        result = _compress_bash_output(output)
        assert "ERROR: connection refused" in result
        assert "[line repeated 39 more times]" in result

    def test_line_collapse_marker_singular_for_pair(self) -> None:
        # A run of exactly two identical lines keeps one copy and a
        # grammatically singular marker ("1 more time", not "1 more times").
        output = "same line\nsame line\nother"
        result = _compress_bash_output(output)
        assert result.count("same line") == 1
        assert "[line repeated 1 more time]" in result
        assert "1 more times" not in result

    def test_no_collapse_for_distinct_lines(self) -> None:
        output = "line a\nline b\nline c"
        result = _compress_bash_output(output)
        assert "line a" in result
        assert "line b" in result
        assert "line c" in result
        assert "[line repeated" not in result

    def test_hard_cap(self) -> None:
        output = "x" * 10_000
        result = _compress_bash_output(output)
        assert len(result) <= 2000
        assert "chars elided" in result

    def test_hard_cap_preserves_head_and_tail(self) -> None:
        head = "HEAD_MARKER " + "a" * 788
        tail = "b" * 788 + " TAIL_MARKER"
        middle = "m" * 5000
        output = head + middle + tail
        result = _compress_bash_output(output)
        assert "HEAD_MARKER" in result
        assert "TAIL_MARKER" in result

    def test_pkg_manager_error_preference(self) -> None:
        lines = ["pip install somepackage"]
        lines.extend([f"Downloading file {i}" for i in range(50)])
        lines.insert(25, "WARNING: something odd")
        lines.insert(35, "error: build failed")
        lines.extend(["Installing collected packages", "Successfully installed foo", ""])
        output = "\n".join(lines)
        result = _compress_bash_output(output)
        assert "pip install somepackage" in result
        assert "error: build failed" in result
        assert "WARNING: something odd" in result
        assert "Successfully installed foo" in result
        assert "Downloading file 10" not in result

    def test_pkg_manager_short_output_kept(self) -> None:
        output = "pip install foo\nCollecting foo\nInstalled\nDone"
        result = _compress_bash_output(output)
        assert "pip install foo" in result
        assert "Done" in result

    def test_empty_string(self) -> None:
        assert _compress_bash_output("") == ""

    def test_none_like_empty(self) -> None:
        assert _compress_bash_output("") == ""

    def test_short_output_unchanged(self) -> None:
        output = "hello world"
        assert _compress_bash_output(output) == "hello world"


@pytest.mark.asyncio
async def test_bash_compression_applied_in_execute() -> None:
    """Bash tool results are compressed; non-Bash results are untouched."""
    bash_tc = ToolCall(
        name="Bash", input={"cmd": "ls"},
        result="\x1b[31mred\x1b[0m output",
    )
    read_tc = ToolCall(
        name="Read", input={"path": "/a"},
        result="\x1b[31mshould stay\x1b[0m",
    )
    executor = Executor()
    step = _make_step("Run stuff")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(
            response="Done",
            tool_calls=[bash_tc, read_tc],
        )
        result = await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert result.success is True
    # Bash tool had ANSI stripped
    assert "\x1b[" not in bash_tc.result
    assert "red" in bash_tc.result
    # Read tool was untouched
    assert "\x1b[31m" in read_tc.result


# ---------------------------------------------------------------------------
# Session reuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_end_session_lifecycle() -> None:
    """start_session connects a client without an initial prompt, end_session disconnects it."""
    executor = Executor()
    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.disconnect = AsyncMock()

    with patch("maestro.executor.create_session", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_client
        await executor.start_session("/w", "identity")

    assert executor._session_client is mock_client
    assert executor._session_step_count == 0
    # No initial prompt — the session is opened without a user message
    call_args = mock_create.call_args
    assert call_args.kwargs.get("system") == "identity"

    await executor.end_session()
    mock_client.disconnect.assert_awaited_once()
    assert executor._session_client is None
    assert executor._session_step_count == 0


@pytest.mark.asyncio
async def test_start_session_threads_disallowed_tools() -> None:
    """A configured disallow list reaches the step session's QueryOptions.

    This is the executor (run_task) half of the read-only external-MCP trust
    boundary — the denied serena exec/write tools must ride the step session
    so they cannot bypass the Bash-scoped action gate (proposal 084 Step 4).
    """
    deny = ["Agent", "mcp__serena__execute_shell_command"]
    executor = Executor(disallowed_tools=deny)
    mock_client = MagicMock()

    with patch("maestro.executor.create_session", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_client
        await executor.start_session("/w", "identity")

    options = mock_create.call_args.kwargs["options"]
    assert options.disallowed_tools == deny


@pytest.mark.asyncio
async def test_start_session_default_disallowed_tools_is_none() -> None:
    """With no disallow list the step session carries None — byte-identical."""
    executor = Executor()
    mock_client = MagicMock()

    with patch("maestro.executor.create_session", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_client
        await executor.start_session("/w", "identity")

    options = mock_create.call_args.kwargs["options"]
    assert options.disallowed_tools is None


def test_step_query_options_threads_disallowed_tools() -> None:
    """The stateless step path forwards the disallow list too."""
    deny = ["Agent", "mcp__serena__create_text_file"]
    executor = Executor(disallowed_tools=deny)
    options = executor._step_query_options("/w")
    assert options.disallowed_tools == deny


def test_step_query_options_default_disallowed_tools_is_none() -> None:
    """No disallow list → None on the stateless step path (byte-identical)."""
    executor = Executor()
    options = executor._step_query_options("/w")
    assert options.disallowed_tools is None


@pytest.mark.asyncio
async def test_end_session_noop_when_no_session() -> None:
    """end_session is safe to call when no session exists."""
    executor = Executor()
    await executor.end_session()  # should not raise


@pytest.mark.asyncio
async def test_end_session_routes_through_disconnect_session() -> None:
    """``end_session()`` must hand off to ``disconnect_session`` (#1719).

    The executor's session is opened via ``create_session()``, so it is
    subject to the same SDK orphan-task leak as every other session
    boundary. Bypassing ``disconnect_session`` would leave residual
    ``Query._child_tasks`` cancels free to fire ``CancelledError`` into
    ``agent.run``'s later awaits — exactly the crash class proposal 055
    eliminates."""
    executor = Executor()
    client = MagicMock()
    executor._session_client = client
    executor._session_step_count = 5

    with patch(
        "maestro.executor.disconnect_session", new_callable=AsyncMock,
    ) as mock_disconnect:
        await executor.end_session()

    mock_disconnect.assert_awaited_once_with(client)
    assert executor._session_client is None
    assert executor._session_step_count == 0


@pytest.mark.asyncio
async def test_execute_with_session_reuse() -> None:
    """When use_session=True and a session is active, query_on_session is called."""
    executor = Executor()
    step = _make_step("Do something")

    mock_client = MagicMock()
    executor._session_client = mock_client
    executor._session_step_count = 0

    with patch(
        "maestro.executor.query_on_session",
        new_callable=AsyncMock,
        return_value=QueryResult(response="session result"),
    ) as mock_qos:
        result = await executor.execute(
            step, workspace="/w", identity_context="",
            use_session=True,
        )

    assert result.success is True
    assert result.output == "session result"
    mock_qos.assert_awaited_once()
    assert executor._session_step_count == 1


@pytest.mark.asyncio
async def test_execute_session_fence_message() -> None:
    """Follow-up steps (step_count > 0) in a session get a fence prefix."""
    executor = Executor()
    step = _make_step("Second step")

    mock_client = MagicMock()
    executor._session_client = mock_client
    executor._session_step_count = 1  # one step already executed

    captured_prompt: list[str] = []

    async def capture_qos(client: Any, prompt: str, **kw: Any) -> QueryResult:
        captured_prompt.append(prompt)
        return QueryResult(response="ok")

    with patch("maestro.executor.query_on_session", side_effect=capture_qos):
        await executor.execute(
            step, workspace="/w", identity_context="",
            use_session=True,
        )

    assert captured_prompt
    assert captured_prompt[0].startswith("=== NEW STEP ===")


@pytest.mark.asyncio
async def test_execute_session_first_step_no_fence() -> None:
    """The first step in a session (step_count == 0) uses query_on_session without a fence."""
    executor = Executor()
    step = _make_step("First step")

    executor._session_client = MagicMock()
    executor._session_step_count = 0

    captured_prompt: list[str] = []

    async def capture_qos(client: Any, prompt: str, **kw: Any) -> QueryResult:
        captured_prompt.append(prompt)
        return QueryResult(response="ok")

    with patch("maestro.executor.query_on_session", side_effect=capture_qos):
        await executor.execute(
            step, workspace="/w", identity_context="",
            use_session=True,
        )

    assert captured_prompt
    assert not captured_prompt[0].startswith("=== NEW STEP ===")


@pytest.mark.asyncio
async def test_execute_no_session_falls_back_to_stateless() -> None:
    """When use_session=False, regular query() is used."""
    executor = Executor()
    step = _make_step("Do thing")

    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        return_value=QueryResult(response="stateless"),
    ) as mock_query:
        result = await executor.execute(
            step, workspace="/w", identity_context="",
            use_session=False,
        )

    assert result.success is True
    assert result.output == "stateless"
    mock_query.assert_awaited_once()


def test_executor_default_tool_stream_idle_matches_config() -> None:
    """The constructor default tracks the Config default, not a stale literal.

    Production wires the value from ``Config`` in ``cli.py``, but an Executor
    built without that wiring must not fall back to a net tighter than the
    largest budget a single gate run may spend (``selfcheck_max_timeout``) —
    otherwise a gate-running step's stream is declared stalled before the suite
    can answer.
    """
    config = Config()
    executor = Executor()

    assert (
        executor._tool_stream_idle_timeout_seconds
        == config.sdk_tool_stream_idle_timeout_seconds
    )
    assert (
        executor._tool_stream_idle_timeout_seconds > config.selfcheck_max_timeout
    )


@pytest.mark.asyncio
async def test_execute_passes_tool_stream_idle_timeout_stateless() -> None:
    """Stateless execute() forwards the configured tool idle timeout to query()."""
    executor = Executor(tool_stream_idle_timeout_seconds=555.0)
    step = _make_step("Do thing")

    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        return_value=QueryResult(response="ok"),
    ) as mock_query:
        await executor.execute(
            step, workspace="/w", identity_context="",
            use_session=False,
        )

    assert mock_query.call_args.kwargs["stream_idle_timeout_seconds"] == 555.0


@pytest.mark.asyncio
async def test_execute_stateless_routes_run_task_phase() -> None:
    """Stateless execute() tags the query with phase="run_task".

    The executor backs the run_task phase, so the stateless fallback (single-step
    or all-parallel plans where ``use_session`` is False) must carry the phase
    exactly like ``start_session`` does — otherwise a ``[sdk] run_task_provider``
    override resolves via ``get_phase_provider(None)`` and is silently ignored.
    """
    executor = Executor()
    step = _make_step("Do thing")

    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        return_value=QueryResult(response="ok"),
    ) as mock_query:
        await executor.execute(
            step, workspace="/w", identity_context="",
            use_session=False,
        )

    assert mock_query.call_args.kwargs["phase"] == "run_task"


@pytest.mark.asyncio
async def test_execute_passes_tool_stream_idle_timeout_session() -> None:
    """Session-reuse execute() forwards the configured tool idle timeout to query_on_session()."""
    executor = Executor(tool_stream_idle_timeout_seconds=555.0)
    executor._session_client = MagicMock()
    executor._session_step_count = 0
    step = _make_step("Do thing")

    with patch(
        "maestro.executor.query_on_session",
        new_callable=AsyncMock,
        return_value=QueryResult(response="ok"),
    ) as mock_qos:
        await executor.execute(
            step, workspace="/w", identity_context="",
            use_session=True,
        )

    assert mock_qos.call_args.kwargs["stream_idle_timeout_seconds"] == 555.0


@pytest.mark.asyncio
async def test_execute_session_fallback_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """use_session=True with no active session logs a warning and falls back to stateless."""
    executor = Executor()
    step = _make_step("Orphan step")
    assert executor._session_client is None

    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        return_value=QueryResult(response="fallback"),
    ):
        import logging
        with caplog.at_level(logging.WARNING, logger="maestro"):
            result = await executor.execute(
                step, workspace="/w", identity_context="",
                use_session=True,
            )

    assert result.success is True
    assert result.output == "fallback"
    assert any("no active session" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_execute_session_loop_detection() -> None:
    """Loop detection still works in session mode."""
    executor = Executor(loop_detect_repeats=2)
    step = _make_step("Loopy step")

    mock_client = MagicMock()
    executor._session_client = mock_client
    executor._session_step_count = 0

    repeated_tc = ToolCall(name="Bash", input={"command": "ls"}, result="same")
    with patch(
        "maestro.executor.query_on_session",
        new_callable=AsyncMock,
        return_value=QueryResult(
            response="stuck",
            tool_calls=[repeated_tc, repeated_tc],
        ),
    ):
        result = await executor.execute(
            step, workspace="/w", identity_context="",
            use_session=True,
        )

    assert result.success is False
    assert result.loop_detected is True


@pytest.mark.asyncio
async def test_execute_session_bash_compression() -> None:
    """Bash output compression still applies in session mode."""
    executor = Executor()
    step = _make_step("Run command")

    mock_client = MagicMock()
    executor._session_client = mock_client
    executor._session_step_count = 0

    tc = ToolCall(name="Bash", input={"command": "ls"}, result="\x1b[31mred\x1b[0m text")
    with patch(
        "maestro.executor.query_on_session",
        new_callable=AsyncMock,
        return_value=QueryResult(response="done", tool_calls=[tc]),
    ):
        result = await executor.execute(
            step, workspace="/w", identity_context="",
            use_session=True,
        )

    assert result.success is True
    assert "\x1b[" not in tc.result


@pytest.mark.asyncio
async def test_start_session_ends_existing_first() -> None:
    """Starting a new session disconnects any existing one."""
    executor = Executor()

    old_client = MagicMock()
    old_client.disconnect = AsyncMock()
    executor._session_client = old_client

    new_client = MagicMock()
    new_client.connect = AsyncMock()

    with patch("maestro.executor.create_session", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = new_client
        await executor.start_session("/w", "identity")

    old_client.disconnect.assert_awaited_once()
    assert executor._session_client is new_client


@pytest.mark.asyncio
async def test_start_session_passes_merged_mcp_servers() -> None:
    """start_session passes merged MCP servers to create_session."""
    executor = Executor()
    new_client = MagicMock()

    with (
        patch("maestro.executor.create_session", new_callable=AsyncMock) as mock_create,
        patch.object(executor, "_merged_mcp_servers", return_value={"builtin": {}}),
        patch.object(executor, "_all_tool_names", return_value=[]),
    ):
        mock_create.return_value = new_client
        await executor.start_session("/w", "identity")

    call_kwargs = mock_create.call_args
    opts = call_kwargs.kwargs.get("options") or call_kwargs[1].get("options")
    assert "builtin" in opts.mcp_servers


@pytest.mark.asyncio
async def test_start_session_routes_run_task_phase() -> None:
    """start_session tags the session with phase="run_task".

    The executor backs the run_task phase, so the step-reuse session must
    carry the phase exactly like the stateless fallback does (see
    ``test_execute_stateless_routes_run_task_phase``) — otherwise a
    ``[sdk] run_task_provider`` override would silently route every
    multi-step run_task plan to the active provider instead.
    """
    executor = Executor()
    new_client = MagicMock()

    with patch("maestro.executor.create_session", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = new_client
        await executor.start_session("/w", "identity")

    assert mock_create.call_args.kwargs["phase"] == "run_task"


@pytest.mark.asyncio
async def test_end_session_suppresses_disconnect_error() -> None:
    """Disconnect errors are logged but don't propagate.

    ``disconnect_session`` is contracted not to raise ``Exception``
    (its inner ``contextlib.suppress(Exception)`` swallows
    ``client.disconnect()`` failures), but ``end_session`` keeps a
    defensive ``try/except Exception:`` around it so a contract
    regression cannot crash the executor. This test pins that
    safety-net branch by forcing ``disconnect_session`` itself to
    raise."""
    executor = Executor()
    client = MagicMock()
    executor._session_client = client
    executor._session_step_count = 3

    with patch(
        "maestro.executor.disconnect_session",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        await executor.end_session()

    assert executor._session_client is None
    assert executor._session_step_count == 0


def test_serena_reaches_executor_merged_mcp_set_when_enabled() -> None:
    """Serena flows ``Config.mcp_servers_config()`` → ``_merged_mcp_servers()``.

    The plan-step-5 read-only seam has two halves: the deny list (asserted by
    ``test_start_session_threads_disallowed_tools``) and the server itself
    reaching the run_task step session. This pins the second — when
    ``[mcp] serena_enabled`` is on the server lands in the executor's merged MCP
    set (so steps can call its retrieval tools), and is absent when off, the
    byte-identical default.
    """
    on = Config(serena_mcp_enabled=True)
    merged_on = Executor(mcp_servers=on.mcp_servers_config())._merged_mcp_servers()
    assert merged_on is not None
    assert "serena" in merged_on

    off = Config(serena_mcp_enabled=False)
    merged_off = Executor(mcp_servers=off.mcp_servers_config())._merged_mcp_servers()
    # Off + nothing else enabled → no MCP servers at all, so certainly no serena.
    assert merged_off is None


def test_tool_cache_computed_once() -> None:
    """_all_tool_names and _merged_mcp_servers cache their results after first call."""
    executor = Executor()
    names1 = executor._all_tool_names()
    cache1 = executor._cached_tool_names
    mcp1 = executor._merged_mcp_servers()
    assert executor._tool_cache_built is True

    names2 = executor._all_tool_names()
    mcp2 = executor._merged_mcp_servers()
    # _all_tool_names now hands out a defensive copy, so the returned lists are
    # equal by value but not the same object; the underlying cache list is what
    # stays stable across calls (proving it is computed exactly once).
    assert names1 == names2
    assert executor._cached_tool_names is cache1
    assert mcp1 is mcp2


def test_all_tool_names_returns_defensive_copy() -> None:
    """_all_tool_names hands out a fresh copy, so caller mutation cannot corrupt
    the shared tool-name cache.

    The bad state this guards against: a consumer mutating the returned list in
    place (SDK option assembly, a provider adapter, or future code filtering
    ``allowed_tools``) leaks into ``_cached_tool_names`` — built once per Executor
    lifetime — silently corrupting the tool set for every subsequent step.
    """
    executor = Executor()
    names = executor._all_tool_names()
    expected = list(names)
    cache = executor._cached_tool_names

    # Mutate the returned list in place; this must not touch the cache.
    names.append("MUTATED")
    names.clear()

    fresh = executor._all_tool_names()
    assert fresh == expected
    assert "MUTATED" not in fresh
    # The cache object itself is unchanged, still holding the full tool set.
    assert executor._cached_tool_names is cache
    assert executor._cached_tool_names == expected


def test_messaging_server_created_once_across_steps() -> None:
    """_build_step_prompt reuses the cached messaging tool names instead of
    rebuilding the messaging MCP server on every step.

    ``create_messaging_server`` should run at most once per Executor lifetime —
    once when the tool cache is first populated. Before the fix, each
    ``_build_step_prompt`` call re-ran ``_build_messaging_options`` (and thus the
    server factory), so two step prompts plus the initial cache build made three
    calls; now it is one.
    """
    with patch("maestro.executor.create_messaging_server") as make_server:
        executor = Executor(notifier=MagicMock(), message_store=MagicMock())
        executor._all_tool_names()
        prompt1 = executor._build_step_prompt(
            _make_step("First step"),
            workspace="/workspace",
            artifacts_dir=None,
            prior_steps=None,
        )
        prompt2 = executor._build_step_prompt(
            _make_step("Second step"),
            workspace="/workspace",
            artifacts_dir=None,
            prior_steps=None,
        )

    assert make_server.call_count == 1
    assert "send_message_to_thomas" in prompt1
    assert "send_message_to_thomas" in prompt2


def _executor_with_all_tool_descriptions() -> Executor:
    """Build an Executor whose four tool-capability description blocks all fire."""
    executor = Executor(notifier=MagicMock(), message_store=MagicMock())
    executor._ensure_tool_cache()
    # Force every capability family on so all four description blocks render
    # on a first-step prompt.
    executor._cached_messaging_tool_names = ["send_message_to_thomas"]
    executor._cached_agent_tool_names = ["write_journal_entry"]
    import maestro.executor as executor_mod
    executor._mcp_servers = {
        executor_mod.EMAIL_SERVER_KEY: object(),
        executor_mod.TELEGRAM_SERVER_KEY: object(),
    }
    return executor


def test_step_prompt_introspection_line_omits_conscious_tools() -> None:
    """Proposal 149 Step 4: the introspection hint no longer advertises the
    conscious-workspace inspection tools."""
    executor = _executor_with_all_tool_descriptions()

    prompt = executor._build_step_prompt(
        _make_step("x"),
        workspace="/workspace",
        artifacts_dir=None,
        prior_steps=None,
        is_session_followup=False,
    )

    assert "You have access to introspection tools" in prompt
    assert "read_self_model" in prompt
    assert "read_conscious_moment" not in prompt
    assert "read_conscious_access_profile" not in prompt
    assert "current attention" not in prompt


def test_step_prompt_followup_omits_tool_capability_descriptions() -> None:
    """On a session-attached followup step the redundant tool-capability
    description blocks are dropped (the session already carries them from the
    first step); on the first step they are present. The tool set itself is
    unchanged."""
    executor = _executor_with_all_tool_descriptions()
    step = _make_step("Do the followup work")

    first = executor._build_step_prompt(
        step,
        workspace="/workspace",
        artifacts_dir=None,
        prior_steps=None,
        is_session_followup=False,
    )
    followup = executor._build_step_prompt(
        step,
        workspace="/workspace",
        artifacts_dir=None,
        prior_steps=None,
        is_session_followup=True,
    )

    # Criterion 2: first step carries every description block.
    assert "introspection tools" in first
    assert "email tools available" in first
    assert "Telegram tools" in first
    assert "send_message_to_thomas persists" in first

    # Criterion 1: followup step omits all of them.
    assert "introspection tools" not in followup
    assert "email tools available" not in followup
    assert "Telegram tools" not in followup
    assert "send_message_to_thomas persists" not in followup

    # Criterion 4: followup keeps the fence and the task body.
    assert "=== NEW STEP ===" in followup
    assert "Task: Do the followup work" in followup

    # Criterion 3: tool availability is untouched by the followup trim.
    # Assert against the concrete tool-name set the helper produces (base
    # tools plus the two messaging MCP names) rather than a self-comparison,
    # so a regression that dropped or added a tool during prompt building
    # would actually fail.
    expected_tools = [
        "Read",
        "Write",
        "Edit",
        "Bash",
        "Glob",
        "Grep",
        "mcp__messaging__send_message_to_thomas",
        "mcp__messaging__read_messages_from_thomas",
    ]
    assert executor._all_tool_names() == expected_tools
    assert executor._step_query_options("/workspace").allowed_tools == expected_tools


# ---------------------------------------------------------------------------
# Empty-response retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_response_retry_succeeds_on_second_attempt() -> None:
    """First STREAM_STALL triggers a same-session retry; second attempt
    returns a productive QueryResult and the step succeeds. ``sdk.py``
    raises STREAM_STALL on unproductive completions, so the executor's
    retry path is now exception-driven rather than predicate-on-result."""
    executor = Executor(empty_response_retries=1)
    step = _make_step("Maybe empty")

    side_effect: list[Any] = [
        SDKError("unproductive", category=ErrorCategory.STREAM_STALL),
        QueryResult(response="finally answered"),
    ]
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=side_effect,
    ) as mock_q:
        result = await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert result.success is True
    assert result.output == "finally answered"
    assert mock_q.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "SDK stream idle for 30.0s without a new message",
        "SDK stream ended without ResultMessage",
    ],
)
async def test_stream_stall_session_unusable_skips_same_session_retry(
    message: str,
) -> None:
    """A stall that poisoned the session (idle timeout, or a stream that
    ended with no ResultMessage because the subprocess exited) must not be
    retried on the same session: the retry's write would die as NETWORK
    and crash the loop. The original STREAM_STALL propagates instead so
    resume-in-place recreates the session."""
    executor = Executor(empty_response_retries=1)
    executor._session_client = MagicMock()
    step = _make_step("Stalls")

    side_effect: list[Any] = [
        SDKError(
            message,
            category=ErrorCategory.STREAM_STALL,
            session_reusable=False,
        ),
        SDKError("dead transport", category=ErrorCategory.NETWORK),
    ]
    with patch(
        "maestro.executor.query_on_session",
        new_callable=AsyncMock,
        side_effect=side_effect,
    ) as mock_qos, pytest.raises(SDKError) as ei:
        await executor.execute(
            step, workspace="/w", identity_context="", use_session=True,
        )

    assert ei.value.category is ErrorCategory.STREAM_STALL
    assert ei.value.session_reusable is False
    assert mock_qos.await_count == 1


@pytest.mark.asyncio
async def test_stream_stall_session_unusable_still_retried_stateless() -> None:
    """On the stateless path each attempt is a fresh ``query()``, so a
    ``session_reusable=False`` stall is still cheaply retried."""
    executor = Executor(empty_response_retries=1)
    step = _make_step("Stalls stateless")

    side_effect: list[Any] = [
        SDKError(
            "SDK stream ended without ResultMessage",
            category=ErrorCategory.STREAM_STALL,
            session_reusable=False,
        ),
        SDKError("again", category=ErrorCategory.STREAM_STALL),
    ]
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=side_effect,
    ) as mock_q, pytest.raises(SDKError):
        await executor.execute(
            step, workspace="/w", identity_context="", use_session=False,
        )

    assert mock_q.await_count == 2


@pytest.mark.asyncio
async def test_empty_response_retry_exhausted_propagates_stream_stall() -> None:
    """When every attempt raises STREAM_STALL, the executor exhausts its
    same-session retry budget and re-raises so ``agent.py``'s resume-in-place
    path can cycle the session and try again."""
    executor = Executor(empty_response_retries=2)
    step = _make_step("Always empty")

    sdk_err = SDKError("unproductive", category=ErrorCategory.STREAM_STALL)
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=sdk_err,
    ) as mock_q, pytest.raises(SDKError) as ei:
        await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert ei.value.category is ErrorCategory.STREAM_STALL
    # Initial attempt + 2 retries = 3 total calls.
    assert mock_q.await_count == 3


@pytest.mark.asyncio
async def test_empty_response_retries_zero_disables_retry() -> None:
    """empty_response_retries=0 propagates STREAM_STALL on the first
    occurrence — no same-session retry."""
    executor = Executor(empty_response_retries=0)
    step = _make_step("Empty-once")

    sdk_err = SDKError("unproductive", category=ErrorCategory.STREAM_STALL)
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=sdk_err,
    ) as mock_q, pytest.raises(SDKError) as ei:
        await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert ei.value.category is ErrorCategory.STREAM_STALL
    assert mock_q.await_count == 1


@pytest.mark.asyncio
async def test_non_empty_failure_is_not_retried() -> None:
    """Loop-detected failures have a non-empty response and must not be retried."""
    repeated_tc = ToolCall(name="Read", input={"path": "/x"}, result="same")
    executor = Executor(empty_response_retries=3, loop_detect_repeats=3)
    step = _make_step("Loopy step")

    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        return_value=QueryResult(
            response="I'm stuck",
            tool_calls=[repeated_tc, repeated_tc, repeated_tc],
        ),
    ) as mock_q:
        result = await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert result.success is False
    assert result.loop_detected is True
    assert mock_q.await_count == 1


@pytest.mark.asyncio
async def test_step_with_only_successful_tool_calls_succeeds() -> None:
    """Empty assistant text + non-error tool calls is treated as productive."""
    executor = Executor(empty_response_retries=1)
    step = _make_step("Edit then Read")

    tool_calls = [
        ToolCall(name="Edit", input={"path": "/x"}, result="ok", is_error=False),
        ToolCall(name="Read", input={"path": "/x"}, result="ok", is_error=False),
    ]
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        return_value=QueryResult(response="", tool_calls=tool_calls),
    ) as mock_q:
        result = await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert result.success is True
    assert result.error is None
    assert "completed 2 tool call(s)" in result.output
    # Tool names are listed alphabetically.
    assert "Edit, Read" in result.output
    # No retry — productive on the first attempt.
    assert mock_q.await_count == 1
    # Non-Bash tool calls contribute no executed commands.
    assert result.bash_commands == []


@pytest.mark.asyncio
async def test_bash_commands_captured_from_tool_inputs() -> None:
    """StepResult.bash_commands holds the Bash tool-call command inputs.

    The executed-command record the CI-failing red-read bind keys on — sourced
    from the tool-call inputs (what actually ran), not the assistant prose in
    ``output``.
    """
    executor = Executor(empty_response_retries=1)
    step = _make_step("Run the gate")

    tool_calls = [
        ToolCall(
            name="Bash",
            input={"command": "python3.12 -m pytest tests/ -q"},
            result="passed",
            is_error=False,
        ),
        ToolCall(name="Read", input={"path": "/x"}, result="ok", is_error=False),
        ToolCall(
            name="Bash",
            input={"command": "ruff check maestro/"},
            result="clean",
            is_error=False,
        ),
    ]
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        return_value=QueryResult(
            response="Fixed the failing assertion.", tool_calls=tool_calls,
        ),
    ):
        result = await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert result.success is True
    # Only Bash inputs, in order; the Read call is excluded.
    assert result.bash_commands == [
        "python3.12 -m pytest tests/ -q",
        "ruff check maestro/",
    ]
    # The narration itself carries no gate token — proving the record is the
    # tool inputs, not the assistant prose.
    assert "pytest" not in result.output


@pytest.mark.asyncio
async def test_bash_command_missing_or_nonstring_input_skipped() -> None:
    """A Bash call with an absent or non-string ``command`` is skipped safely."""
    executor = Executor(empty_response_retries=1)
    step = _make_step("Malformed bash")

    tool_calls = [
        ToolCall(name="Bash", input={}, result="", is_error=False),
        ToolCall(name="Bash", input={"command": 123}, result="", is_error=False),
        ToolCall(
            name="Bash",
            input={"command": "mypy maestro/"},
            result="ok",
            is_error=False,
        ),
    ]
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        return_value=QueryResult(response="done", tool_calls=tool_calls),
    ):
        result = await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert result.bash_commands == ["mypy maestro/"]


@pytest.mark.asyncio
async def test_step_with_only_failed_tool_calls_retries_then_propagates() -> (
    None
):
    """Empty text + only failed tool calls is unproductive — sdk.py raises
    STREAM_STALL on this shape; the executor retries on the same session
    once before propagating to ``agent.py``'s resume-in-place handler."""
    executor = Executor(empty_response_retries=1)
    step = _make_step("All tools error")

    sdk_err = SDKError("unproductive", category=ErrorCategory.STREAM_STALL)
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=sdk_err,
    ) as mock_q, pytest.raises(SDKError) as ei:
        await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert ei.value.category is ErrorCategory.STREAM_STALL
    # Initial attempt + 1 retry = 2 calls.
    assert mock_q.await_count == 2


@pytest.mark.asyncio
async def test_truly_empty_propagates_after_retries() -> None:
    """No text and no tool calls — sdk.py classifies this as STREAM_STALL;
    the executor retries on the same session once, then propagates."""
    executor = Executor(empty_response_retries=1)
    step = _make_step("Truly empty")

    sdk_err = SDKError("unproductive", category=ErrorCategory.STREAM_STALL)
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=sdk_err,
    ) as mock_q, pytest.raises(SDKError) as ei:
        await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert ei.value.category is ErrorCategory.STREAM_STALL
    assert mock_q.await_count == 2


@pytest.mark.asyncio
async def test_sdk_error_propagates_without_retry() -> None:
    """SDKError raised on the first attempt propagates immediately — no retry."""
    executor = Executor(empty_response_retries=3)
    step = _make_step("Crash step")

    sdk_err = SDKError(
        "Command failed with exit code 1",
        stderr="boom",
        category=ErrorCategory.CLI_CRASH,
    )

    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=sdk_err,
    ) as mock_q, pytest.raises(SDKError):
        await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert mock_q.await_count == 1


# ---------------------------------------------------------------------------
# Edge case 5: empty-response retry counter resets between steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_counter_resets_between_steps() -> None:
    """The per-step retry counter (local ``attempt`` in ``execute``) resets
    for each call.  Two sequential steps each hit STREAM_STALL on attempt 1
    and succeed on attempt 2 — both must succeed, proving the retry budget
    is not shared across steps."""
    executor = Executor(empty_response_retries=1)

    stall = SDKError("unproductive", category=ErrorCategory.STREAM_STALL)

    # Step 1: STREAM_STALL → success
    step_a = _make_step("Step A")
    side_effect_a: list[Any] = [
        stall,
        QueryResult(response="A done"),
    ]
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=side_effect_a,
    ) as mock_q:
        result_a = await executor.execute(
            step_a, workspace="/w", identity_context="",
        )

    assert result_a.success is True
    assert result_a.output == "A done"
    assert mock_q.await_count == 2

    # Step 2: same pattern — STREAM_STALL → success.
    # If the retry counter leaked from step 1, this would propagate the
    # STREAM_STALL instead of retrying.
    step_b = _make_step("Step B")
    side_effect_b: list[Any] = [
        stall,
        QueryResult(response="B done"),
    ]
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=side_effect_b,
    ) as mock_q:
        result_b = await executor.execute(
            step_b, workspace="/w", identity_context="",
        )

    assert result_b.success is True
    assert result_b.output == "B done"
    assert mock_q.await_count == 2


@pytest.mark.asyncio
async def test_retry_budget_exhausts_per_step_independently() -> None:
    """Two sequential steps: the first exhausts the retry budget
    (STREAM_STALL on every attempt) while the second gets a fresh
    budget and succeeds on its retry, confirming independent counters."""
    executor = Executor(empty_response_retries=1)

    stall = SDKError("unproductive", category=ErrorCategory.STREAM_STALL)

    # Step 1: exhaust the budget — initial + 1 retry = 2 STREAM_STALL raises
    step_a = _make_step("Step A always stalls")
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=stall,
    ) as mock_q, pytest.raises(SDKError) as ei:
        await executor.execute(
            step_a, workspace="/w", identity_context="",
        )

    assert ei.value.category is ErrorCategory.STREAM_STALL
    assert mock_q.await_count == 2  # initial + 1 retry

    # Step 2: STREAM_STALL on attempt 1, succeeds on attempt 2.
    # Must succeed because the budget is fresh.
    step_b = _make_step("Step B succeeds on retry")
    side_effect_b: list[Any] = [
        stall,
        QueryResult(response="B recovered"),
    ]
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=side_effect_b,
    ) as mock_q:
        result_b = await executor.execute(
            step_b, workspace="/w", identity_context="",
        )

    assert result_b.success is True
    assert result_b.output == "B recovered"
    assert mock_q.await_count == 2


# ---------------------------------------------------------------------------
# Edge case 6: STREAM_STALL on tool-only completions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_errored_tool_calls_propagates_stream_stall() -> None:
    """When there is no assistant text and every tool call errored, sdk.py
    raises STREAM_STALL (the completion is unproductive).  The executor must
    propagate this after exhausting its retry budget."""
    executor = Executor(empty_response_retries=1)
    step = _make_step("All tools error")

    # sdk.py classifies this shape as STREAM_STALL before the executor
    # sees a QueryResult, so mock both attempts raising.
    stall = SDKError("unproductive", category=ErrorCategory.STREAM_STALL)
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=stall,
    ) as mock_q, pytest.raises(SDKError) as ei:
        await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert ei.value.category is ErrorCategory.STREAM_STALL
    # 1 initial + 1 retry = 2 total.
    assert mock_q.await_count == 2


@pytest.mark.asyncio
async def test_mixed_tool_calls_with_one_success_is_productive() -> None:
    """A completion with no assistant text but at least one non-error tool
    call is productive — sdk.py does NOT raise STREAM_STALL, so the executor
    receives a normal ``QueryResult``.  The step should succeed with a
    synthesised output summary."""
    executor = Executor(empty_response_retries=1)
    step = _make_step("Mixed tools")

    tool_calls = [
        ToolCall(name="Bash", input={"cmd": "ls"}, result="error", is_error=True),
        ToolCall(name="Read", input={"path": "/a"}, result="ok", is_error=False),
        ToolCall(name="Write", input={"path": "/b"}, result="fail", is_error=True),
    ]
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        return_value=QueryResult(response="", tool_calls=tool_calls),
    ) as mock_q:
        result = await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert result.success is True
    # Synthesised output lists only successful tool calls.
    assert "completed 1 tool call(s)" in result.output
    assert "Read" in result.output
    # No retry — productive on the first attempt.
    assert mock_q.await_count == 1


@pytest.mark.asyncio
async def test_all_errored_no_text_not_productive() -> None:
    """Directly verify the ``_is_productive`` predicate: empty response
    + only errored tool calls → not productive; empty response + at least
    one successful tool call → productive."""
    from maestro.executor import _is_productive

    all_error = QueryResult(
        response="",
        tool_calls=[
            ToolCall(name="Bash", input={"cmd": "rm"}, result="err", is_error=True),
            ToolCall(name="Read", input={"path": "x"}, result="err", is_error=True),
        ],
    )
    assert _is_productive(all_error) is False

    one_ok = QueryResult(
        response="",
        tool_calls=[
            ToolCall(name="Bash", input={"cmd": "rm"}, result="err", is_error=True),
            ToolCall(name="Read", input={"path": "x"}, result="ok", is_error=False),
        ],
    )
    assert _is_productive(one_ok) is True

    # With assistant text, always productive regardless of tool outcomes.
    text_present = QueryResult(
        response="Some text",
        tool_calls=[
            ToolCall(name="Bash", input={"cmd": "rm"}, result="err", is_error=True),
        ],
    )
    assert _is_productive(text_present) is True


# ---------------------------------------------------------------------------
# Long-context opt-in (proposal-derived: targeted 1M-context at three sites)
# ---------------------------------------------------------------------------


def test_compress_bash_output_default_caps_short_at_2k() -> None:
    """Default cap stays at 2 KB so the standard executor path is unchanged."""
    output = "X" * 30_000
    result = _compress_bash_output(output)
    assert len(result) < 5_000
    assert "elided" in result


def test_compress_bash_output_long_caps_preserve_30kb() -> None:
    """With long-context caps, a 30 KB output survives without elision."""
    output = "X" * 30_000
    result = _compress_bash_output(
        output,
        hard_cap=_BASH_HARD_CAP_LONG_CONTEXT,
        head=20_000,
        tail=20_000,
    )
    assert len(result) >= 29_000
    assert "elided" not in result


def test_bash_compression_docs_name_both_policies() -> None:
    """The documented Bash cap names both the standard and long-context policies."""
    doc = Path(__file__).resolve().parent.parent / "docs" / "executor-details.md"
    text = doc.read_text(encoding="utf-8")
    start = text.index("## Bash Output Compression")
    end = text.find("\n## ", start + 1)
    section = text[start:end]
    assert "`_BASH_HARD_CAP`" in section
    assert "`_BASH_HARD_CAP_LONG_CONTEXT`" in section
    assert "2000 chars" not in section


@pytest.mark.asyncio
async def test_bash_output_uses_long_cap_when_long_context() -> None:
    """Executor with long_context_model flips compression to the larger cap."""
    big_output = "X" * 30_000

    bash_tc = ToolCall(name="Bash", input={"cmd": "build"}, result=big_output)
    executor_long = Executor(long_context_model="claude-opus-4-7[1m]")
    executor_std = Executor()
    step = _make_step("Run the build")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response="ok", tool_calls=[bash_tc])
        await executor_long.execute(step, workspace="/w", identity_context="")
    assert len(bash_tc.result) >= 29_000

    bash_tc2 = ToolCall(name="Bash", input={"cmd": "build"}, result=big_output)
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response="ok", tool_calls=[bash_tc2])
        await executor_std.execute(step, workspace="/w", identity_context="")
    assert len(bash_tc2.result) < _BASH_HARD_CAP + 200


@pytest.mark.asyncio
async def test_executor_session_uses_long_context_options() -> None:
    """start_session passes model + betas when configured."""
    executor = Executor(
        long_context_model="claude-opus-4-7[1m]",
    )
    mock_client = MagicMock()
    mock_client.disconnect = AsyncMock()
    with patch(
        "maestro.executor.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = mock_client
        await executor.start_session("/w", "identity")
    options = mock_create.call_args.kwargs["options"]
    assert options.model == "claude-opus-4-7[1m]"
    assert options.betas == [LONG_CONTEXT_BETA]


@pytest.mark.asyncio
async def test_executor_session_default_no_long_context_fields() -> None:
    """Without long_context_model the session opens with the SDK defaults."""
    executor = Executor()
    mock_client = MagicMock()
    mock_client.disconnect = AsyncMock()
    with patch(
        "maestro.executor.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = mock_client
        await executor.start_session("/w", "identity")
    options = mock_create.call_args.kwargs["options"]
    assert options.model is None
    assert options.betas is None


@pytest.mark.asyncio
async def test_executor_stateless_step_uses_long_context_options() -> None:
    """The stateless _step_query_options path also flips when configured."""
    executor = Executor(
        long_context_model="claude-opus-4-7[1m]",
    )
    step = _make_step("Run something")
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response="ok")
        await executor.execute(step, workspace="/w", identity_context="")
    options = mock_q.call_args.kwargs["options"]
    assert options.model == "claude-opus-4-7[1m]"
    assert options.betas == [LONG_CONTEXT_BETA]


# ---------------------------------------------------------------------------
# Tool-gating guard wiring (issue #2280)
# ---------------------------------------------------------------------------


def _pretooluse_hook(options: object) -> object:
    """Extract the Bash ``PreToolUse`` hook callback from a QueryOptions."""
    hooks = options.hooks  # type: ignore[attr-defined]
    matcher = hooks["PreToolUse"][0]
    assert matcher.matcher == "Bash"
    return matcher.hooks[0]


async def _hook_decision(hook: object, command: str) -> str:
    """Invoke *hook* as the SDK would and return the permission decision."""
    out = await hook(  # type: ignore[operator]
        {"tool_name": "Bash", "tool_input": {"command": command}}, None, None,
    )
    if not out:
        return "allow"
    return out["hookSpecificOutput"]["permissionDecision"]


@pytest.mark.asyncio
async def test_start_session_wires_tool_guard_keyed_on_allowed_branches() -> None:
    """start_session attaches a PreToolUse Bash hook keyed on the allowed set.

    The hook must deny a PR-opening Bash command and allow a push to the
    run's own task branch.
    """
    executor = Executor()
    mock_client = MagicMock()
    with patch(
        "maestro.executor.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = mock_client
        await executor.start_session(
            "/w", "identity",
            allowed_branches=frozenset({"maestro/task/xyz"}),
        )
    hook = _pretooluse_hook(mock_create.call_args.kwargs["options"])
    assert await _hook_decision(hook, "gh pr create") == "deny"
    assert await _hook_decision(
        hook, "git push origin maestro/task/xyz",
    ) == "allow"


@pytest.mark.asyncio
async def test_stateless_step_wires_tool_guard_keyed_on_allowed_branches() -> None:
    """_step_query_options attaches a hook keyed on the passed branch set."""
    executor = Executor()
    step = _make_step("Run something")
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response="ok")
        await executor.execute(
            step, workspace="/w", identity_context="",
            allowed_branches=frozenset({"maestro/task/xyz"}),
        )
    hook = _pretooluse_hook(mock_q.call_args.kwargs["options"])
    assert await _hook_decision(hook, "git push origin main") == "deny"


@pytest.mark.asyncio
async def test_execute_threads_multi_branch_owned_set_to_gate() -> None:
    """A genuine multi-branch owned set authorizes a push to *any* member —
    the widened gate the ``run()`` producer feeds (proposal 093)."""
    executor = Executor()
    step = _make_step("Run something")
    owned = frozenset({"maestro/task/xyz", "maestro/evolve/abc"})
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response="ok")
        await executor.execute(
            step, workspace="/w", identity_context="",
            allowed_branches=owned,
        )
    hook = _pretooluse_hook(mock_q.call_args.kwargs["options"])
    assert await _hook_decision(
        hook, "git push origin maestro/evolve/abc",
    ) == "allow"
    assert await _hook_decision(
        hook, "git push origin maestro/task/xyz",
    ) == "allow"
    assert await _hook_decision(hook, "git push origin main") == "deny"


@pytest.mark.asyncio
async def test_stateless_step_threads_current_branch_to_gate() -> None:
    """proposal 099: ``execute`` forwards the run's ``current_branch`` to the
    gate so a bare ``HEAD`` push resolves to it and is allowed."""
    executor = Executor()
    step = _make_step("Run something")
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response="ok")
        await executor.execute(
            step, workspace="/w", identity_context="",
            allowed_branches=frozenset({"maestro/evolve/abc"}),
            current_branch="maestro/evolve/abc",
        )
    hook = _pretooluse_hook(mock_q.call_args.kwargs["options"])
    assert hook.current_branch == "maestro/evolve/abc"
    assert await _hook_decision(hook, "git push origin HEAD") == "allow"
    assert await _hook_decision(hook, "git push") == "allow"


@pytest.mark.asyncio
async def test_start_session_threads_current_branch_to_gate() -> None:
    """proposal 099: ``start_session`` forwards the run's ``current_branch`` so
    the session's gate resolves a bare ``HEAD`` push; ``main`` stays denied."""
    executor = Executor()
    mock_client = MagicMock()
    with patch(
        "maestro.executor.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = mock_client
        await executor.start_session(
            "/w", "identity",
            allowed_branches=frozenset({"maestro/evolve/abc"}),
            current_branch="maestro/evolve/abc",
        )
    hook = _pretooluse_hook(mock_create.call_args.kwargs["options"])
    assert hook.current_branch == "maestro/evolve/abc"
    assert await _hook_decision(hook, "git push origin HEAD") == "allow"
    assert await _hook_decision(hook, "git push origin main") == "deny"


@pytest.mark.asyncio
async def test_execute_without_current_branch_denies_bare_head() -> None:
    """The default (branch-fallback) path injects no current branch, so a bare
    ``HEAD`` push stays fail-closed — byte-identical to the pre-099 gate."""
    executor = Executor()
    step = _make_step("Run something")
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response="ok")
        await executor.execute(
            step, workspace="/w", identity_context="",
            allowed_branches=frozenset({"maestro/evolve/abc"}),
        )
    hook = _pretooluse_hook(mock_q.call_args.kwargs["options"])
    assert hook.current_branch is None
    assert await _hook_decision(hook, "git push origin HEAD") == "deny"


# ---------------------------------------------------------------------------
# Loop detection threshold edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_detect_repeats_below_2_disables_detection() -> None:
    """loop_detect_repeats=1 disables detection at the processing level.

    The ``_process_step_result`` method gates loop detection on
    ``self._loop_detect_repeats >= 2``, so values below 2 must allow
    any number of identical tool calls through without triggering.
    """
    repeated_tc = ToolCall(name="Read", input={"path": "/x"}, result="same")
    executor = Executor(loop_detect_repeats=1)
    step = _make_step("Allowed repetition")

    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(
            response="Finished checking",
            tool_calls=[repeated_tc, repeated_tc, repeated_tc, repeated_tc, repeated_tc],
        )
        result = await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert result.success is True
    assert result.loop_detected is False


# ---------------------------------------------------------------------------
# STREAM_STALL retry — session mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_stall_retry_session_mode() -> None:
    """STREAM_STALL retry works identically through the session-mode branch.

    First attempt raises STREAM_STALL; second attempt returns a productive
    QueryResult. Verifies the retry path uses query_on_session correctly.
    """
    executor = Executor(empty_response_retries=1)
    step = _make_step("Session retry step")

    mock_client = MagicMock()
    executor._session_client = mock_client
    executor._session_step_count = 0

    side_effect: list[Any] = [
        SDKError("unproductive", category=ErrorCategory.STREAM_STALL),
        QueryResult(response="ok"),
    ]
    with patch(
        "maestro.executor.query_on_session",
        new_callable=AsyncMock,
        side_effect=side_effect,
    ) as mock_qos:
        result = await executor.execute(
            step, workspace="/w", identity_context="",
            use_session=True,
        )

    assert result.success is True
    assert result.output == "ok"
    assert mock_qos.await_count == 2


# ---------------------------------------------------------------------------
# Mixed error categories during retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_error_categories_during_retry() -> None:
    """Non-STREAM_STALL error on attempt 2 propagates immediately.

    First attempt raises STREAM_STALL (retryable), second attempt raises
    CLI_CRASH (non-retryable). The CLI_CRASH must propagate without
    further retries.
    """
    executor = Executor(empty_response_retries=2)
    step = _make_step("Mixed errors")

    side_effect: list[Any] = [
        SDKError("unproductive", category=ErrorCategory.STREAM_STALL),
        SDKError("crashed", category=ErrorCategory.CLI_CRASH),
    ]
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=side_effect,
    ) as mock_q, pytest.raises(SDKError) as ei:
        await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert ei.value.category is ErrorCategory.CLI_CRASH
    assert mock_q.await_count == 2


# ---------------------------------------------------------------------------
# Negative empty_response_retries normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_empty_response_retries_normalizes_to_zero() -> None:
    """empty_response_retries=-1 normalizes to 0 via max(0, ...).

    STREAM_STALL propagates on the first call with no retries attempted.
    """
    executor = Executor(empty_response_retries=-1)
    step = _make_step("Negative retries")

    sdk_err = SDKError("unproductive", category=ErrorCategory.STREAM_STALL)
    with patch(
        "maestro.executor.query",
        new_callable=AsyncMock,
        side_effect=sdk_err,
    ) as mock_q, pytest.raises(SDKError) as ei:
        await executor.execute(
            step, workspace="/w", identity_context="",
        )

    assert ei.value.category is ErrorCategory.STREAM_STALL
    assert mock_q.await_count == 1


# ---------------------------------------------------------------------------
# Import-time binding of tool modules (regression for #2240)
# ---------------------------------------------------------------------------


def test_executor_imports_tool_modules_at_import_time() -> None:
    """Tool modules are bound at import time, not lazily after a checkout.

    The loop process checks out arbitrary PR/task branches at runtime. If
    executor deferred these ``maestro.*`` imports to function bodies, the import
    would resolve against the checked-out working tree's source and could raise
    ImportError on API drift. Promoting them to module level binds the symbols
    against the interpreter's startup source, removing the race (#2240).
    """
    import sys

    import maestro.email_tools as email_tools
    import maestro.executor as executor_mod
    import maestro.telegram_tools as telegram_tools
    import maestro.tools as tools

    # The three tool modules load eagerly with maestro.executor.
    assert "maestro.email_tools" in sys.modules
    assert "maestro.telegram_tools" in sys.modules
    assert "maestro.tools" in sys.modules

    # The promoted names are module attributes — the deterministic anchor that
    # fails at CI time if any import drifts back into a function body.
    assert executor_mod.EMAIL_SERVER_KEY == email_tools.SERVER_KEY
    assert executor_mod.EMAIL_TOOL_NAMES == email_tools.TOOL_NAMES
    assert executor_mod.TELEGRAM_SERVER_KEY == telegram_tools.SERVER_KEY
    assert executor_mod.TELEGRAM_TOOL_NAMES == telegram_tools.TOOL_NAMES
    assert executor_mod.create_messaging_server is tools.create_messaging_server


@pytest.mark.asyncio
async def test_execute_threads_push_remotes_to_gate() -> None:
    """proposal 157 W1: ``execute`` forwards ``allowed_push_remotes``; default ``None``."""
    executor = Executor()
    step = _make_step("Run something")
    remotes = frozenset({"origin", "acme/tool"})
    with patch("maestro.executor.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response="ok")
        await executor.execute(
            step, workspace="/w", identity_context="",
            allowed_branches=frozenset({"maestro/task/t1"}),
            allowed_push_remotes=remotes,
        )
        hook = _pretooluse_hook(mock_q.call_args.kwargs["options"])
        assert hook.allowed_push_remotes == remotes
        assert await _hook_decision(hook, "git push upstream maestro/task/t1") == "deny"
        await executor.execute(
            step, workspace="/w", identity_context="",
            allowed_branches=frozenset({"maestro/task/t1"}),
        )
        assert _pretooluse_hook(mock_q.call_args.kwargs["options"]).allowed_push_remotes is None


@pytest.mark.asyncio
async def test_start_session_threads_push_remotes_to_gate() -> None:
    executor = Executor()
    remotes = frozenset({"origin"})
    with patch(
        "maestro.executor.create_session", new_callable=AsyncMock,
    ) as mock_create:
        mock_create.return_value = MagicMock()
        await executor.start_session(
            "/w", "identity",
            allowed_branches=frozenset({"maestro/task/t1"}),
            allowed_push_remotes=remotes,
        )
    hook = _pretooluse_hook(mock_create.call_args.kwargs["options"])
    assert hook.allowed_push_remotes == remotes
