"""Tests for the concrete :class:`maestro.providers.anthropic.AnthropicProvider`.

Phase 1 Step 3 (proposal 058) lands a stateless wrapper whose four operations
delegate one-to-one to the private impl functions in :mod:`maestro.sdk`
(``_query_impl`` etc.; targeting the impls rather than the public delegators
avoids recursion now that those route back through the active provider — Phase 2
Step 5). These tests assert:

* the wrapper structurally satisfies the ``Provider`` protocol;
* its advertised capabilities are all ``True``;
* each method delegates with byte-identical arguments (and forwards the opaque
  session handle as the impl's first positional ``client``);
* a golden-dict pin of ``sdk._build_sdk_options`` fixes the move boundary so a
  future *physical* relocation of the SDK bodies into the provider stays
  behaviour-preserving;
* ``_classify_error`` maps each documented input to its expected
  ``ErrorCategory`` (guards that same future move).
"""

from __future__ import annotations

import errno
from unittest.mock import AsyncMock, patch

import pytest

from maestro import sdk
from maestro.providers import base
from maestro.providers.anthropic import AnthropicProvider
from maestro.providers.anthropic_tool_adapter import AnthropicToolAdapter
from maestro.sdk import (
    LONG_CONTEXT_BETA,
    ErrorCategory,
    QueryOptions,
    _build_sdk_options,
    _classify_error,
)


class TestProtocol:
    """The wrapper satisfies the contract surface."""

    def test_anthropic_provider_satisfies_protocol(self) -> None:
        """``isinstance`` against the runtime-checkable protocol (presence-only)."""
        assert isinstance(AnthropicProvider(), base.Provider)

    def test_capabilities_all_true(self) -> None:
        """Every one of the eight capability flags is advertised as ``True``."""
        caps = AnthropicProvider.capabilities
        assert caps.sessions is True
        assert caps.in_process_tools is True
        assert caps.remote_mcp is True
        assert caps.extended_thinking is True
        assert caps.long_context is True
        assert caps.structured_output is True
        assert caps.usage_metering is True
        assert caps.streaming is True

    def test_owns_anthropic_tool_adapter(self) -> None:
        """The provider exposes the Step-2 adapter for Phase 2 to reach."""
        assert isinstance(AnthropicProvider.tool_adapter, AnthropicToolAdapter)


class TestDelegation:
    """Each method forwards to the matching ``maestro.sdk`` function verbatim."""

    @pytest.mark.asyncio
    async def test_query_delegates(self) -> None:
        sentinel = object()
        provider = AnthropicProvider()
        opts = QueryOptions(caller="c")

        def _complete(_client: object, _text: str) -> bool:
            return True

        with patch.object(sdk, "_query_impl", AsyncMock(return_value=sentinel)) as mock:
            result = await provider.query(
                "hello",
                options=opts,
                system="sys",
                complete_on=_complete,
                stream_idle_timeout_seconds=12.0,
            )

        assert result is sentinel
        mock.assert_awaited_once_with(
            "hello",
            options=opts,
            system="sys",
            complete_on=_complete,
            stream_idle_timeout_seconds=12.0,
        )

    @pytest.mark.asyncio
    async def test_create_session_delegates(self) -> None:
        sentinel = object()
        provider = AnthropicProvider()
        opts = QueryOptions(caller="c")

        with patch.object(
            sdk, "_create_session_impl", AsyncMock(return_value=sentinel),
        ) as mock:
            result = await provider.create_session(
                "init",
                options=opts,
                system="sys",
                resume_session_id="sess-1",
            )

        assert result is sentinel
        mock.assert_awaited_once_with(
            "init",
            options=opts,
            system="sys",
            resume_session_id="sess-1",
        )

    @pytest.mark.asyncio
    async def test_query_on_session_forwards_session_positionally(self) -> None:
        sentinel = object()
        session = object()
        provider = AnthropicProvider()

        def _complete(_client: object, _text: str) -> bool:
            return False

        with patch.object(
            sdk, "_query_on_session_impl", AsyncMock(return_value=sentinel),
        ) as mock:
            result = await provider.query_on_session(
                session,
                "follow-up",
                caller="phase",
                complete_on=_complete,
                stream_idle_timeout_seconds=3.0,
            )

        assert result is sentinel
        # ``session`` is forwarded as the first positional ``client``.
        mock.assert_awaited_once_with(
            session,
            "follow-up",
            caller="phase",
            complete_on=_complete,
            stream_idle_timeout_seconds=3.0,
            bypass_warning_throttle=False,
        )

    @pytest.mark.asyncio
    async def test_disconnect_session_delegates(self) -> None:
        session = object()
        provider = AnthropicProvider()

        with patch.object(
            sdk, "_disconnect_session_impl", AsyncMock(return_value=None),
        ) as mock:
            result = await provider.disconnect_session(session)

        assert result is None
        mock.assert_awaited_once_with(session)


class TestGoldenSdkOptions:
    """Pin ``_build_sdk_options`` output — the stable move boundary.

    Pinning ``_build_sdk_options`` (not ``ClaudeAgentOptions(**opts)``) is
    deliberate: callers add a per-call ``stderr`` callback / ``extra_args``
    around the dict, but the dict itself is the byte-stable boundary a future
    physical move into the provider must reproduce.
    """

    def test_full_options_build_matches_golden(self) -> None:
        # The autouse runtime owner restores module defaults before each test;
        # pin the model explicitly so the build is determined solely by the
        # populated QueryOptions, not ambient configuration.
        sdk.configure_sdk_model(None)

        options = QueryOptions(
            allowed_tools=["Read", "Write"],
            tools=["Bash"],
            cwd="/tmp/work",
            max_turns=7,
            model="claude-opus-4-8",
            effort="high",
            output_format={"type": "json"},
            mcp_servers={"github": {"command": "x"}},
            disallowed_tools=["Agent", "Edit"],
            hooks={"PreToolUse": ["matcher"]},
            fallback_model="claude-sonnet-4-6",
            task_budget_tokens=50_000,
            betas=[LONG_CONTEXT_BETA],
            caller="golden-caller",
        )

        sdk_opts, caller = _build_sdk_options(options, "you are a test")

        # ``task_budget`` is a ``TaskBudget`` dataclass with structural
        # equality, so it compares directly inside the golden dict.
        assert sdk_opts == {
            "system_prompt": "you are a test",
            "allowed_tools": ["Read", "Write"],
            "tools": ["Bash"],
            "cwd": "/tmp/work",
            "max_turns": 7,
            "model": "claude-opus-4-8",
            "effort": "high",
            "output_format": {"type": "json"},
            "mcp_servers": {"github": {"command": "x"}},
            # Ungated shell built-ins are always unioned in (issue #4381).
            "disallowed_tools": ["Agent", "Edit", "Monitor"],
            "hooks": {"PreToolUse": ["matcher"]},
            "thinking": {"type": "adaptive"},
            "fallback_model": "claude-sonnet-4-6",
            "betas": [LONG_CONTEXT_BETA],
            "task_budget": sdk.TaskBudget(total=50_000),
            "permission_mode": "bypassPermissions",
            "include_partial_messages": True,
        }
        assert caller == "golden-caller"


def _bare_network_oserror(err_no: int) -> OSError:
    """Bare ``OSError`` with a network errno assigned post-construction.

    The two-arg ``OSError(errno, msg)`` constructor auto-maps to
    ``ConnectionError`` subclasses, which would dodge the errno arm (#405).
    """
    exc = OSError("transport fault")
    exc.errno = err_no
    return exc


class TestClassifyErrorRegressionTable:
    """``_classify_error`` mapping pinned for the future physical move."""

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (RuntimeError("Invalid API key"), ErrorCategory.AUTH),
            (RuntimeError("unauthorized"), ErrorCategory.AUTH),
            (RuntimeError("authentication failed"), ErrorCategory.AUTH),
            (RuntimeError("permission denied"), ErrorCategory.AUTH),
            (RuntimeError("rate limit exceeded"), ErrorCategory.RATE_LIMIT),
            (RuntimeError("HTTP 429"), ErrorCategory.RATE_LIMIT),
            (RuntimeError("too many requests"), ErrorCategory.RATE_LIMIT),
            (RuntimeError("request timeout"), ErrorCategory.TIMEOUT),
            (RuntimeError("connection timed out"), ErrorCategory.TIMEOUT),
            (RuntimeError("deadline exceeded"), ErrorCategory.TIMEOUT),
            (RuntimeError("connect ETIMEDOUT 104.18.32.47:443"), ErrorCategory.TIMEOUT),
            (ConnectionError("reset"), ErrorCategory.NETWORK),
            (ConnectionResetError("reset"), ErrorCategory.NETWORK),
            (BrokenPipeError("pipe"), ErrorCategory.NETWORK),
            (RuntimeError("connection refused"), ErrorCategory.NETWORK),
            (RuntimeError("connection reset by peer"), ErrorCategory.NETWORK),
            (RuntimeError("connect ECONNREFUSED 104.18.32.47:443"), ErrorCategory.NETWORK),
            (RuntimeError("read ECONNRESET"), ErrorCategory.NETWORK),
            (RuntimeError("write EPIPE"), ErrorCategory.NETWORK),
            (RuntimeError("socket hang up"), ErrorCategory.NETWORK),
            (RuntimeError("getaddrinfo ENOTFOUND api.anthropic.com"), ErrorCategory.NETWORK),
            (RuntimeError("getaddrinfo EAI_AGAIN api.anthropic.com"), ErrorCategory.NETWORK),
            (_bare_network_oserror(errno.ECONNRESET), ErrorCategory.NETWORK),
            (_bare_network_oserror(errno.EPIPE), ErrorCategory.NETWORK),
            (_bare_network_oserror(errno.ECONNREFUSED), ErrorCategory.NETWORK),
            (RuntimeError("exit code 1"), ErrorCategory.CLI_CRASH),
            (ValueError("bad json"), ErrorCategory.UNKNOWN),
        ],
    )
    def test_classify_error_mapping(
        self, exc: Exception, expected: ErrorCategory,
    ) -> None:
        assert _classify_error(exc) == expected
