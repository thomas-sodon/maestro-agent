"""Session-path rejected rate limit raises RATE_LIMIT instead of success (#4046).

A persistent session breaks on the ``ResultMessage`` and its CLI stays alive, so
the stateless path's exception-driven sleep-until-reset hand-off never runs.
An unproductive completion following ``RateLimitEvent("rejected")`` must raise
a categorized ``SDKError(RATE_LIMIT)`` rather than return the quota notice.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    TextBlock,
)

from maestro.sdk import (
    ErrorCategory,
    QueryUsage,
    SDKError,
    ToolCall,
    _consume_messages,
    _is_unproductive_after_rejection,
    query_on_session,
)

_QUOTA = "You've hit your limit · resets 3pm"


def _result_msg(result: str = "done", **overrides: Any) -> ResultMessage:
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


def _rejected() -> RateLimitEvent:
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="rejected", resets_at=int(time.time()) + 600,
        ),
        uuid="u1",
        session_id="s1",
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)], model="claude-sonnet-4-20250514",
    )


_ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0}

_UNPRODUCTIVE_SHAPES = {
    "is_error": _result_msg(
        _QUOTA, is_error=True, usage=_ZERO_USAGE, total_cost_usd=0.0,
    ),
    "ghost": _result_msg(
        _QUOTA, num_turns=1, usage=_ZERO_USAGE, total_cost_usd=0.0,
    ),
    "zero_turn": _result_msg("", num_turns=0, usage=_ZERO_USAGE),
}


def _session_client(*messages: Any) -> MagicMock:
    client = MagicMock()
    client.query = AsyncMock()

    async def receive() -> Any:
        for msg in messages:
            yield msg

    client.receive_messages = receive
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_UNPRODUCTIVE_SHAPES))
async def test_session_rejected_unproductive_raises_rate_limit(shape: str) -> None:
    client = _session_client(
        _rejected(), _assistant(_QUOTA), _UNPRODUCTIVE_SHAPES[shape],
    )
    sleep = AsyncMock()
    with patch("maestro.sdk.asyncio.sleep", sleep), pytest.raises(SDKError) as exc:
        await query_on_session(client, "prompt", caller="update_self_model")
    assert exc.value.category is ErrorCategory.RATE_LIMIT
    sleep.assert_not_awaited()
    assert client.query.await_count == 1


@pytest.mark.asyncio
async def test_session_rejected_without_result_raises_rate_limit() -> None:
    async def gen() -> Any:
        yield _rejected()

    with pytest.raises(SDKError) as exc:
        await _consume_messages(gen(), "caller", None, break_on_result=True)
    assert exc.value.category is ErrorCategory.RATE_LIMIT


@pytest.mark.asyncio
async def test_session_rejected_then_productive_returns_normally() -> None:
    client = _session_client(
        _rejected(),
        _assistant("real work"),
        _result_msg(
            "real work",
            usage={"input_tokens": 100, "output_tokens": 50},
            total_cost_usd=0.01,
        ),
    )
    with patch("maestro.sdk.asyncio.sleep", AsyncMock()):
        result = await query_on_session(client, "prompt", caller="test")
    assert result.response == "real work"


@pytest.mark.asyncio
async def test_stateless_rejected_ghost_still_defers() -> None:
    async def gen() -> Any:
        yield _rejected()
        yield _UNPRODUCTIVE_SHAPES["ghost"]

    cr = await _consume_messages(gen(), "caller", None, break_on_result=False)
    assert cr.rejected_resets_at is not None


def _helper(**overrides: Any) -> bool:
    kwargs: dict[str, Any] = {
        "last": None,
        "early_exit": False,
        "text_buffer": "",
        "usage": QueryUsage(),
        "tool_calls": [],
        "structured_output_requested": False,
    }
    kwargs.update(overrides)
    return _is_unproductive_after_rejection(**kwargs)


def test_helper_early_exit_ghost_buffer_is_unproductive() -> None:
    assert _helper(early_exit=True, text_buffer=_QUOTA) is True


def test_helper_early_exit_real_text_is_productive() -> None:
    assert _helper(early_exit=True, text_buffer="partial answer") is False


def test_helper_structured_output_request_is_productive() -> None:
    last = _result_msg(
        "", usage={"input_tokens": 10, "output_tokens": 5}, total_cost_usd=0.01,
    )
    assert _helper(last=last, structured_output_requested=True) is False


def test_helper_empty_aggregate_is_unproductive() -> None:
    last = _result_msg(
        "", usage={"input_tokens": 10, "output_tokens": 5}, total_cost_usd=0.01,
    )
    assert _helper(last=last) is True


def test_helper_successful_tool_call_is_productive() -> None:
    last = _result_msg(
        "", usage={"input_tokens": 10, "output_tokens": 5}, total_cost_usd=0.01,
    )
    tool = ToolCall(name="Read", input={}, result="ok", is_error=False)
    assert _helper(last=last, tool_calls=[tool]) is False
