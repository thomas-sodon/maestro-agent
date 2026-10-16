"""Session rate-limit retries never re-send an accepted prompt (#4159).

Once ``client.query(prompt)`` has returned, the prompt is committed to the
persistent session's transcript. A rate-limit signal after that point — a
rejected ``RateLimitEvent`` or rate-limit stderr followed by a stream
exception — must raise ``SDKError(RATE_LIMIT)`` for resume-in-place rather
than sleeping and re-sending on the same client (#1216). Before acceptance
(``client.query`` itself raised) the in-place retry is preserved.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo, ResultMessage

from maestro.sdk import (
    TRANSIENT_SDK_CATEGORIES,
    ErrorCategory,
    SDKError,
    query_on_session,
)


def _rejected_event() -> RateLimitEvent:
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="rejected", resets_at=int(time.time()) + 600,
        ),
        uuid="u1",
        session_id="s1",
    )


def _result_msg(text: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=0.0,
        usage={},
        result=text,
    )


@pytest.mark.asyncio
async def test_rejected_event_after_acceptance_raises_rate_limit_without_resend() -> None:
    client = MagicMock()
    client.query = AsyncMock()

    async def receive() -> Any:
        yield _rejected_event()
        raise RuntimeError("Command failed with exit code 1")

    client.receive_messages = receive

    sleep = AsyncMock()
    with patch("maestro.sdk.asyncio.sleep", sleep), \
         pytest.raises(SDKError) as exc_info:
        await query_on_session(client, "go", caller="t")

    assert client.query.await_count == 1
    assert exc_info.value.category == ErrorCategory.RATE_LIMIT
    assert exc_info.value.retries_exhausted is False
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_stderr_rate_limit_after_acceptance_raises_without_resend() -> None:
    client = MagicMock()
    client.query = AsyncMock()
    client._maestro_stderr_lines = ["API Error: 429 rate limit exceeded"]

    async def receive() -> Any:
        raise RuntimeError("Command failed with exit code 1")
        yield  # pragma: no cover

    client.receive_messages = receive

    sleep = AsyncMock()
    with patch("maestro.sdk.asyncio.sleep", sleep), \
         pytest.raises(SDKError) as exc_info:
        await query_on_session(client, "go", caller="t")

    assert client.query.await_count == 1
    assert exc_info.value.category == ErrorCategory.RATE_LIMIT
    assert exc_info.value.stderr == "API Error: 429 rate limit exceeded"
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_stderr_bytes_decoded_on_accepted_rate_limit_raise() -> None:
    client = MagicMock()
    client.query = AsyncMock()
    client._maestro_stderr_lines = []

    async def receive() -> Any:
        yield _rejected_event()
        exc = RuntimeError("Command failed with exit code 1")
        exc.stderr = b"usage limit reached"  # type: ignore[attr-defined]
        raise exc

    client.receive_messages = receive

    with patch("maestro.sdk.asyncio.sleep", AsyncMock()), \
         pytest.raises(SDKError) as exc_info:
        await query_on_session(client, "go")

    assert client.query.await_count == 1
    assert exc_info.value.category == ErrorCategory.RATE_LIMIT
    assert exc_info.value.stderr == "usage limit reached"


@pytest.mark.asyncio
async def test_rejected_event_then_contained_cancel_does_not_resend() -> None:
    client = MagicMock()
    client.query = AsyncMock()

    async def receive() -> Any:
        yield _rejected_event()
        raise asyncio.CancelledError("deadline exceeded")

    client.receive_messages = receive

    sleep = AsyncMock()
    with patch("maestro.sdk.asyncio.sleep", sleep), \
         pytest.raises(SDKError) as exc_info:
        await query_on_session(client, "go", caller="t")

    assert client.query.await_count == 1
    assert exc_info.value.category in TRANSIENT_SDK_CATEGORIES
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_pre_acceptance_rate_limit_still_retries_in_place() -> None:
    client = MagicMock()
    client.query = AsyncMock(
        side_effect=[RuntimeError("rate limit exceeded"), None],
    )

    async def receive() -> Any:
        yield _result_msg("ok")

    client.receive_messages = receive

    sleep = AsyncMock()
    with patch("maestro.sdk.asyncio.sleep", sleep):
        result = await query_on_session(client, "go", caller="t")

    assert result.response == "ok"
    assert client.query.await_count == 2
    sleep.assert_awaited_once()
