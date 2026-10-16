"""Generic transient retries stay bounded after stderr rate limits (#4124).

The stderr-keyword rate-limit arm may advance the shared ``attempt`` counter
past ``_MAX_RETRIES``; the generic transient arm must still treat
``_MAX_RETRIES`` as a ceiling rather than an exact match, and its backoff must
never exceed the designed ladder.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import CLIConnectionError

from maestro.sdk import (
    _MAX_RETRIES,
    _RETRY_BACKOFF_FACTOR,
    _RETRY_BASE_DELAY,
    SDKError,
    query,
    query_on_session,
)

_MAX_GENERIC_DELAY = _RETRY_BASE_DELAY * _RETRY_BACKOFF_FACTOR ** (_MAX_RETRIES - 1)


def _rate_limit_exc() -> RuntimeError:
    exc = RuntimeError("Command failed with exit code 1")
    exc.stderr = "rate limit exceeded"  # type: ignore[attr-defined]
    return exc


def _transient_exc() -> CLIConnectionError:
    return CLIConnectionError("529 overloaded")


def _sequence(rate_limit_calls: set[int]) -> tuple[list[int], Any]:
    """Return a call counter and a raiser: rate limit on listed calls, else transient."""
    calls = [0]

    def next_exc() -> Exception:
        calls[0] += 1
        if calls[0] in rate_limit_calls:
            return _rate_limit_exc()
        return _transient_exc()

    return calls, next_exc


def _generic_sleeps(sleep: AsyncMock) -> list[float]:
    # Generic backoff delays are the only sleeps at or below the ladder cap;
    # rate-limit sleeps start at a much larger base.
    return [
        c.args[0] for c in sleep.await_args_list if c.args[0] <= _MAX_GENERIC_DELAY
    ]


@pytest.mark.asyncio
async def test_query_generic_transient_after_stderr_rate_limits_exhausts() -> None:
    calls, next_exc = _sequence({1, 2, 3, 4})

    async def gen(**kwargs: Any) -> Any:
        raise next_exc()
        yield  # pragma: no cover

    sleep = AsyncMock()
    with patch("maestro.sdk._query", side_effect=gen), \
         patch("asyncio.sleep", sleep), \
         pytest.raises(SDKError) as exc_info:
        await query(prompt="test")

    assert exc_info.value.retries_exhausted is True
    assert calls[0] == 5
    assert _generic_sleeps(sleep) == []


@pytest.mark.asyncio
async def test_query_rate_limit_after_generic_transients_exhausts() -> None:
    calls, next_exc = _sequence({4})

    async def gen(**kwargs: Any) -> Any:
        raise next_exc()
        yield  # pragma: no cover

    sleep = AsyncMock()
    with patch("maestro.sdk._query", side_effect=gen), \
         patch("asyncio.sleep", sleep), \
         pytest.raises(SDKError) as exc_info:
        await query(prompt="test")

    assert exc_info.value.retries_exhausted is True
    assert exc_info.value.retries_attempted == _MAX_RETRIES + 1
    assert calls[0] == 5
    assert max(_generic_sleeps(sleep)) == _MAX_GENERIC_DELAY


@pytest.mark.asyncio
async def test_session_generic_transient_after_stderr_rate_limits_exhausts() -> None:
    calls, next_exc = _sequence({1, 2, 3, 4})

    client = MagicMock()
    client._maestro_stderr_lines = []

    async def raise_next(*args: Any, **kwargs: Any) -> None:
        exc = next_exc()
        # The session path reads CLI stderr from the client's capture buffer.
        client._maestro_stderr_lines.append(getattr(exc, "stderr", "overloaded"))
        raise exc

    client.query = AsyncMock(side_effect=raise_next)
    sleep = AsyncMock()
    with patch("maestro.sdk.asyncio.sleep", sleep), \
         pytest.raises(SDKError) as exc_info:
        await query_on_session(client, "prompt", caller="test")

    assert exc_info.value.retries_exhausted is True
    assert client.query.await_count == 5
    assert calls[0] == 5
    assert _generic_sleeps(sleep) == []


@pytest.mark.asyncio
async def test_session_rate_limit_after_generic_transients_exhausts() -> None:
    calls, next_exc = _sequence({4})

    client = MagicMock()
    client._maestro_stderr_lines = []

    async def raise_next(*args: Any, **kwargs: Any) -> None:
        exc = next_exc()
        client._maestro_stderr_lines.append(getattr(exc, "stderr", "overloaded"))
        raise exc

    client.query = AsyncMock(side_effect=raise_next)
    sleep = AsyncMock()
    with patch("maestro.sdk.asyncio.sleep", sleep), \
         patch("maestro.sdk.logger") as log, \
         pytest.raises(SDKError) as exc_info:
        await query_on_session(client, "prompt", caller="test")

    assert exc_info.value.retries_exhausted is True
    assert exc_info.value.retries_attempted == _MAX_RETRIES + 1
    assert calls[0] == 5
    assert max(_generic_sleeps(sleep)) == _MAX_GENERIC_DELAY
    exhausted = [
        c for c in log.error.call_args_list if "failed after" in c.args[0]
    ]
    assert exhausted[0].args[1] == _MAX_RETRIES + 1
