"""Session-connect failures surface as classified ``SDKError`` (#4570).

Lives in its own module because ``tests/test_sdk.py`` is over its size band.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ProcessError

from maestro.sdk import ErrorCategory, SDKError, create_session, is_environment_fault


def _client_factory(*errors: BaseException) -> Any:
    """Return a ClaudeSDKClient stand-in whose Nth client's connect raises errors[N]."""
    queue = list(errors)

    def make_client(options: Any = None, **_: Any) -> MagicMock:
        client = MagicMock()
        client.connect = AsyncMock(side_effect=queue.pop(0))
        client.disconnect = AsyncMock()
        return client

    return make_client


@pytest.mark.asyncio
async def test_connect_process_error_137_without_resume_is_environment_fault() -> None:
    raw = ProcessError("killed", exit_code=137)
    with patch("maestro.sdk.ClaudeSDKClient", side_effect=_client_factory(raw)), \
            pytest.raises(SDKError) as excinfo:
        await create_session(resume_session_id=None)
    exc = excinfo.value
    assert exc.exit_code == 137
    assert is_environment_fault(exc) is True
    assert exc.original is raw
    assert exc.__cause__ is raw
    assert exc.session_reusable is False


@pytest.mark.asyncio
async def test_connect_process_error_1_without_resume_is_cli_crash() -> None:
    factory = _client_factory(ProcessError("crashed", exit_code=1))
    with patch("maestro.sdk.ClaudeSDKClient", side_effect=factory), \
            pytest.raises(SDKError) as excinfo:
        await create_session(resume_session_id=None)
    assert excinfo.value.category == ErrorCategory.CLI_CRASH


@pytest.mark.asyncio
async def test_connect_initialize_timeout_is_timeout() -> None:
    factory = _client_factory(Exception("Control request timeout: initialize"))
    with patch("maestro.sdk.ClaudeSDKClient", side_effect=factory), \
            pytest.raises(SDKError) as excinfo:
        await create_session(resume_session_id=None)
    assert excinfo.value.category == ErrorCategory.TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [137, 1])
async def test_resume_fallback_fresh_connect_failure_is_classified(exit_code: int) -> None:
    fresh = ProcessError("fresh failed", exit_code=exit_code)
    factory = _client_factory(RuntimeError("stale resume"), fresh)
    with patch("maestro.sdk.ClaudeSDKClient", side_effect=factory), \
            pytest.raises(SDKError) as excinfo:
        await create_session(resume_session_id="ses_x")
    exc = excinfo.value
    assert exc.exit_code == exit_code
    assert exc.original is fresh
    assert exc.__cause__ is fresh


@pytest.mark.asyncio
async def test_resume_fallback_fresh_oom_is_environment_fault() -> None:
    factory = _client_factory(
        RuntimeError("stale resume"), ProcessError("killed", exit_code=137),
    )
    with patch("maestro.sdk.ClaudeSDKClient", side_effect=factory), \
            pytest.raises(SDKError) as excinfo:
        await create_session(resume_session_id="ses_x")
    assert is_environment_fault(excinfo.value) is True


@pytest.mark.asyncio
async def test_resume_fallback_fresh_crash_is_cli_crash() -> None:
    factory = _client_factory(
        RuntimeError("stale resume"), ProcessError("crashed", exit_code=1),
    )
    with patch("maestro.sdk.ClaudeSDKClient", side_effect=factory), \
            pytest.raises(SDKError) as excinfo:
        await create_session(resume_session_id="ses_x")
    assert excinfo.value.category == ErrorCategory.CLI_CRASH


@pytest.mark.asyncio
async def test_connect_failure_attaches_captured_stderr() -> None:
    def make_client(options: Any = None, **_: Any) -> MagicMock:
        async def _connect(prompt: Any = None) -> None:
            options.stderr("fatal: heap exhausted")
            raise ProcessError("killed", exit_code=137, stderr="Check stderr output")

        client = MagicMock()
        client.connect = _connect
        client.disconnect = AsyncMock()
        return client

    with patch("maestro.sdk.ClaudeSDKClient", side_effect=make_client), \
            pytest.raises(SDKError) as excinfo:
        await create_session(resume_session_id=None)
    stderr = excinfo.value.stderr
    assert stderr is not None
    assert "fatal: heap exhausted" in stderr


@pytest.mark.asyncio
async def test_connect_bytes_stderr_is_decoded() -> None:
    raw = RuntimeError("boom")
    raw.stderr = b"bytes err"  # type: ignore[attr-defined]
    with patch("maestro.sdk.ClaudeSDKClient", side_effect=_client_factory(raw)), \
            pytest.raises(SDKError) as excinfo:
        await create_session(resume_session_id=None)
    assert excinfo.value.stderr == "bytes err"


@pytest.mark.asyncio
async def test_connect_sdkerror_passes_through_unwrapped() -> None:
    pre = SDKError("stalled", category=ErrorCategory.STREAM_STALL)
    with patch("maestro.sdk.ClaudeSDKClient", side_effect=_client_factory(pre)), \
            pytest.raises(SDKError) as excinfo:
        await create_session(resume_session_id=None)
    assert excinfo.value is pre
