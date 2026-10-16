"""Tests for the OpenAI reference provider (proposal 058, Phase 3 Step 9).

The Step 9 slice is the one-shot query path: :class:`OpenAIProvider` maps a
neutral :class:`~maestro.providers.base.QueryOptions` onto an OpenAI Responses
API request and maps the response back onto a ``QueryResult`` / ``QueryUsage``.
These tests pin:

* structural ``Provider`` protocol conformance and the honest capability flags;
* per-field ``QueryOptions → request`` mapping, including ``effort →
  reasoning.effort`` and ``output_format → text.format``;
* ``response.usage → QueryUsage`` mapping, the cached-token path, the
  no-usage-block fallback, and cost estimation via the pricing table;
* the lazy client build (without the optional ``openai`` package installed);
* Step 11 sessions — continuity threaded and advanced via ``previous_response_id``,
  ``disconnect_session`` as a no-op;
* Step 11 error mapping — ``openai`` exception classes (and unmapped status
  errors) onto :class:`~maestro.providers.base.ErrorCategory`, wrapped as
  ``SDKError`` on both the one-shot and session paths.

The ``openai`` package is not installed in the unit environment; every test
injects a mocked client, and the real :func:`_default_client` is patched.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from maestro import sdk
from maestro.providers import base
from maestro.providers import openai as openai_provider
from maestro.providers.base import ErrorCategory, QueryOptions, SDKError
from maestro.providers.openai import (
    OpenAIProvider,
    _build_request,
    _classify_openai_error,
    _create_response,
    _result_from_response,
    _usage_from_response,
)


# A stand-in for the ``openai`` exception hierarchy: the package is not installed
# in the unit environment, and :func:`_classify_openai_error` matches by class
# name walking the MRO, so these mirror the real shapes (``OpenAIError`` at the
# root, ``APITimeoutError`` deriving from ``APIConnectionError``) faithfully.
class OpenAIError(Exception):
    """Root of the stand-in ``openai`` exception tree."""

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class APIError(OpenAIError):
    pass


class APIConnectionError(APIError):
    pass


class APITimeoutError(APIConnectionError):
    pass


class APIStatusError(APIError):
    pass


class RateLimitError(APIStatusError):
    pass


class AuthenticationError(APIStatusError):
    pass


class PermissionDeniedError(APIStatusError):
    pass


@pytest.fixture(autouse=True)
def _reset_pricing() -> Iterator[None]:
    """Keep the module-global pricing table empty unless a test sets it."""
    sdk.configure_sdk_pricing(None)
    try:
        yield
    finally:
        sdk.configure_sdk_pricing(None)


@pytest.fixture(autouse=True)
def no_sleep() -> Iterator[AsyncMock]:
    """Stub the provider's backoff seam so no test ever sleeps for real."""
    stub = AsyncMock()
    with patch.object(openai_provider, "_sleep", stub):
        yield stub


def _make_client(response: object) -> object:
    """Build a stub client whose ``responses.create`` returns *response*."""
    client = SimpleNamespace()
    client.responses = SimpleNamespace(create=AsyncMock(return_value=response))
    return client


def _usage(**fields: object) -> SimpleNamespace:
    """Build a Responses API ``usage``-shaped object."""
    return SimpleNamespace(**fields)


class TestProtocol:
    """Structural conformance and the honest Step 9 capability descriptor."""

    def test_satisfies_protocol(self) -> None:
        assert isinstance(OpenAIProvider(), base.Provider)

    def test_capabilities(self) -> None:
        caps = OpenAIProvider.capabilities
        assert caps.sessions is True
        assert caps.in_process_tools is True
        assert caps.remote_mcp is False
        assert caps.extended_thinking is False
        assert caps.long_context is False
        assert caps.structured_output is True
        assert caps.usage_metering is True
        assert caps.streaming is False


class TestBuildRequest:
    """Per-field ``QueryOptions → Responses API request`` mapping."""

    def test_minimal_prompt_only(self) -> None:
        assert _build_request("hello", None, None) == {"input": "hello"}

    def test_system_maps_to_instructions(self) -> None:
        req = _build_request("hi", None, "you are a test")
        assert req == {"input": "hi", "instructions": "you are a test"}

    def test_model_maps(self) -> None:
        req = _build_request("hi", QueryOptions(model="gpt-5"), None)
        assert req["model"] == "gpt-5"

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_effort_maps_to_reasoning_effort(self, effort: str) -> None:
        req = _build_request("hi", QueryOptions(effort=effort), None)
        assert req["reasoning"] == {"effort": effort}

    def test_output_format_maps_to_text_format(self) -> None:
        fmt = {"type": "json_schema", "name": "x", "schema": {"type": "object"}}
        req = _build_request("hi", QueryOptions(output_format=fmt), None)
        assert req["text"] == {"format": fmt}

    def test_all_fields_together(self) -> None:
        fmt = {"type": "json_schema"}
        options = QueryOptions(
            model="gpt-5",
            effort="high",
            output_format=fmt,
            caller="contemplate",
        )
        req = _build_request("prompt", options, "sys")
        assert req == {
            "input": "prompt",
            "instructions": "sys",
            "model": "gpt-5",
            "reasoning": {"effort": "high"},
            "text": {"format": fmt},
        }

    def test_anthropic_only_fields_are_dropped(self) -> None:
        """Tool / beta / budget fields have no Responses API analogue here."""
        options = QueryOptions(
            allowed_tools=["Read"],
            tools=["Bash"],
            mcp_servers={"github": {"command": "x"}},
            disallowed_tools=["Agent"],
            hooks={"PreToolUse": ["m"]},
            betas=["context-1m-2025-08-07"],
            fallback_model="gpt-4",
            task_budget_tokens=2000,
            max_turns=5,
        )
        assert _build_request("p", options, None) == {"input": "p"}


class TestUsageMapping:
    """``response.usage → QueryUsage`` mapping."""

    def test_token_fields_map(self) -> None:
        response = SimpleNamespace(
            model="gpt-5",
            usage=_usage(input_tokens=100, output_tokens=40, total_tokens=140),
        )
        usage = _usage_from_response(response, "contemplate")
        assert usage.input_tokens == 100
        assert usage.output_tokens == 40
        assert usage.total_tokens == 140
        assert usage.cache_creation_tokens == 0
        assert usage.caller == "contemplate"
        assert usage.model == "gpt-5"
        assert usage.estimated is True
        assert usage.provider == "openai"

    def test_cached_tokens_map_to_cache_read(self) -> None:
        # OpenAI's input_tokens is the gross total (includes the cached
        # subset). QueryUsage.input_tokens is the *uncached* portion in
        # Maestro's Anthropic-shaped split, so the cached count is
        # subtracted out and surfaced separately on cache_read_tokens.
        response = SimpleNamespace(
            model="gpt-5",
            usage=_usage(
                input_tokens=100,
                output_tokens=10,
                total_tokens=110,
                input_tokens_details=SimpleNamespace(cached_tokens=64),
            ),
        )
        usage = _usage_from_response(response, None)
        assert usage.cache_read_tokens == 64
        assert usage.input_tokens == 36

    def test_cached_subset_subtraction_clamps_at_zero(self) -> None:
        # Defensive: a malformed report where cached_tokens > input_tokens
        # must not produce a negative input_tokens (which would corrupt cost
        # estimation and the diagnostics cache-hit denominator).
        response = SimpleNamespace(
            model="gpt-5",
            usage=_usage(
                input_tokens=10,
                output_tokens=0,
                total_tokens=10,
                input_tokens_details=SimpleNamespace(cached_tokens=999),
            ),
        )
        usage = _usage_from_response(response, None)
        assert usage.input_tokens == 0
        assert usage.cache_read_tokens == 999

    def test_missing_usage_block_yields_zero_row(self) -> None:
        response = SimpleNamespace(model="gpt-5", usage=None)
        usage = _usage_from_response(response, "caller-x")
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.total_tokens == 0
        assert usage.caller == "caller-x"
        assert usage.model == "gpt-5"
        assert usage.total_cost_usd is None
        # Attribution (067 Step 2) rides the zero-count fallback row too.
        assert usage.provider == "openai"

    def test_cost_estimated_from_pricing_table(self) -> None:
        # OpenAI's usage.input_tokens is the *gross* prompt-token count and
        # already contains input_tokens_details.cached_tokens as a subset;
        # the cache-read line is the discounted rate applied to those
        # cached tokens, not an additive counter. The mapping must hand
        # _estimate_cost_usd the uncached portion (Anthropic split) so the
        # cached share isn't billed twice — once at the input rate and
        # again at the cache-read rate.
        sdk.configure_sdk_pricing(
            {"gpt-5": {"input": 1.0, "output": 2.0, "cache_read": 0.5}},
        )
        response = SimpleNamespace(
            model="gpt-5",
            usage=_usage(
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                total_tokens=2_000_000,
                input_tokens_details=SimpleNamespace(cached_tokens=400_000),
            ),
        )
        usage = _usage_from_response(response, None)
        # uncached = 1_000_000 - 400_000 = 600_000.
        # 600K*input + 1M*output + 400K*cache_read
        #   = 0.6 + 2.0 + 0.2 = 2.8 USD.
        assert usage.total_cost_usd == pytest.approx(2.8)

    def test_unknown_model_leaves_cost_unset(self) -> None:
        response = SimpleNamespace(
            model="gpt-unknown",
            usage=_usage(input_tokens=10, output_tokens=5, total_tokens=15),
        )
        usage = _usage_from_response(response, None)
        assert usage.total_cost_usd is None


class TestResultMapping:
    """``response → QueryResult`` mapping."""

    def test_response_text_and_session_id(self) -> None:
        response = SimpleNamespace(
            id="resp_123",
            output_text="the answer",
            model="gpt-5",
            usage=_usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )
        result = _result_from_response(response, "caller")
        assert result.response == "the answer"
        assert result.session_id == "resp_123"
        # No schema requested → the field stays unset, not a phantom None (#2533).
        assert result.structured_output is sdk._STRUCTURED_UNSET
        assert result.usage.caller == "caller"

    def test_structured_output_is_carried(self) -> None:
        parsed = {"verdict": "ok"}
        response = SimpleNamespace(
            id="resp_9",
            output_text="{}",
            output_parsed=parsed,
            model="gpt-5",
            usage=None,
        )
        result = _result_from_response(response, None, structured_requested=True)
        assert result.structured_output == parsed

    def test_structured_output_unset_when_not_requested(self) -> None:
        """``output_parsed`` is dropped when no schema was requested (#2533).

        A response can carry ``output_parsed`` while the call did not ask for a
        schema; the packing must leave the field unset so the shared sentinel is
        not fed a value that would masquerade as a validated result.
        """
        response = SimpleNamespace(
            id="resp_x", output_text="{}", output_parsed={"stray": 1},
            model="gpt-5", usage=None,
        )
        result = _result_from_response(response, None, structured_requested=False)
        assert result.structured_output is sdk._STRUCTURED_UNSET

    def test_structured_output_validated_null_is_carried(self) -> None:
        """A validated ``null`` (``output_parsed=None``) is preserved (#2533).

        When a schema was requested and the model answered ``null``, the produced
        ``None`` must be forwarded verbatim, not coerced to the unset sentinel.
        """
        response = SimpleNamespace(
            id="resp_n", output_text="null", output_parsed=None,
            model="gpt-5", usage=None,
        )
        result = _result_from_response(response, None, structured_requested=True)
        assert result.structured_output is None

    def test_none_output_text_coerces_to_empty(self) -> None:
        response = SimpleNamespace(
            id="resp_0", output_text=None, model="gpt-5", usage=None,
        )
        result = _result_from_response(response, None)
        assert result.response == ""


class TestQuery:
    """The end-to-end query path against a mocked client."""

    @pytest.mark.asyncio
    async def test_query_calls_client_and_maps_result(self) -> None:
        response = SimpleNamespace(
            id="resp_42",
            output_text="hi there",
            model="gpt-5",
            usage=_usage(input_tokens=20, output_tokens=5, total_tokens=25),
        )
        client = _make_client(response)
        provider = OpenAIProvider(client=client)

        result = await provider.query(
            "hello",
            options=QueryOptions(model="gpt-5", effort="low", caller="phase-a"),
            system="sys",
        )

        client.responses.create.assert_awaited_once_with(
            input="hello",
            instructions="sys",
            model="gpt-5",
            reasoning={"effort": "low"},
        )
        assert result.response == "hi there"
        assert result.session_id == "resp_42"
        assert result.usage.input_tokens == 20
        assert result.usage.caller == "phase-a"

    @pytest.mark.asyncio
    async def test_query_emits_usage_telemetry(self) -> None:
        response = SimpleNamespace(
            id="r", output_text="x", model="gpt-5",
            usage=_usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )
        provider = OpenAIProvider(client=_make_client(response))

        with patch.object(sdk, "_log_sdk_call") as log:
            result = await provider.query("p")

        log.assert_called_once_with(result.usage)

    @pytest.mark.asyncio
    async def test_query_forwards_structured_output_when_requested(self) -> None:
        """A one-shot query with ``output_format`` forwards ``output_parsed`` (#2533)."""
        parsed = {"verdict": "ok"}
        response = SimpleNamespace(
            id="r", output_text="{}", output_parsed=parsed, model="gpt-5",
            usage=None,
        )
        provider = OpenAIProvider(client=_make_client(response))

        result = await provider.query(
            "p",
            options=QueryOptions(
                output_format={"type": "json_schema", "schema": {"type": "object"}},
            ),
        )

        assert result.structured_output == parsed


class TestLazyClient:
    """The real client is built lazily and only once."""

    def test_get_client_builds_lazily_and_caches(self) -> None:
        sentinel = object()
        with patch.object(
            openai_provider, "_default_client", return_value=sentinel,
        ) as factory:
            provider = OpenAIProvider()
            assert provider._get_client() is sentinel
            assert provider._get_client() is sentinel
        factory.assert_called_once()

    def test_injected_client_is_used_directly(self) -> None:
        injected = object()
        provider = OpenAIProvider(client=injected)
        with patch.object(openai_provider, "_default_client") as factory:
            assert provider._get_client() is injected
        factory.assert_not_called()


class TestSessions:
    """Step 11 session ops: continuity over ``previous_response_id``."""

    @pytest.mark.asyncio
    async def test_create_session_without_prompt_sends_nothing(self) -> None:
        client = _make_client(SimpleNamespace())
        provider = OpenAIProvider(client=client)

        session = await provider.create_session(
            options=QueryOptions(model="gpt-5", effort="high"), system="sys",
        )

        client.responses.create.assert_not_called()
        assert session.previous_response_id is None
        assert session.instructions == "sys"
        assert session.options.model == "gpt-5"

    @pytest.mark.asyncio
    async def test_create_session_resume_seeds_previous_id(self) -> None:
        client = _make_client(SimpleNamespace())
        provider = OpenAIProvider(client=client)

        session = await provider.create_session(resume_session_id="resp_prior")

        client.responses.create.assert_not_called()
        assert session.previous_response_id == "resp_prior"

    @pytest.mark.asyncio
    async def test_create_session_with_initial_prompt_establishes_continuity(
        self,
    ) -> None:
        response = SimpleNamespace(id="resp_1", output_text="hi", model="gpt-5", usage=None)
        client = _make_client(response)
        provider = OpenAIProvider(client=client)

        session = await provider.create_session(
            "open the conversation",
            options=QueryOptions(model="gpt-5", caller="phase-a"),
            system="sys",
        )

        client.responses.create.assert_awaited_once_with(
            input="open the conversation", instructions="sys", model="gpt-5",
        )
        assert session.previous_response_id == "resp_1"

    @pytest.mark.asyncio
    async def test_query_on_session_threads_and_advances_previous_id(self) -> None:
        first = SimpleNamespace(id="resp_1", output_text="a", model="gpt-5", usage=None)
        second = SimpleNamespace(id="resp_2", output_text="b", model="gpt-5", usage=None)
        client = SimpleNamespace()
        client.responses = SimpleNamespace(
            create=AsyncMock(side_effect=[first, second]),
        )
        provider = OpenAIProvider(client=client)
        session = await provider.create_session(
            options=QueryOptions(model="gpt-5"), system="sys",
        )

        r1 = await provider.query_on_session(session, "turn one", caller="c1")
        r2 = await provider.query_on_session(session, "turn two", caller="c2")

        # First turn has no prior id; second turn threads the first turn's id.
        assert client.responses.create.await_args_list[0].kwargs == {
            "input": "turn one", "instructions": "sys", "model": "gpt-5",
        }
        assert client.responses.create.await_args_list[1].kwargs == {
            "input": "turn two", "instructions": "sys", "model": "gpt-5",
            "previous_response_id": "resp_1",
        }
        assert r1.session_id == "resp_1"
        assert r2.session_id == "resp_2"
        assert session.previous_response_id == "resp_2"

    @pytest.mark.asyncio
    async def test_query_on_session_forwards_structured_output_when_requested(
        self,
    ) -> None:
        """A session turn whose baked options carry ``output_format`` forwards
        ``output_parsed`` (#2533); the session path derives the request flag from
        ``session.options.output_format``."""
        parsed = {"verdict": "ok"}
        response = SimpleNamespace(
            id="r", output_text="{}", output_parsed=parsed, model="gpt-5",
            usage=None,
        )
        provider = OpenAIProvider(client=_make_client(response))
        session = await provider.create_session(
            options=QueryOptions(
                output_format={"type": "json_schema", "schema": {"type": "object"}},
            ),
        )

        result = await provider.query_on_session(session, "p")

        assert result.structured_output == parsed

    @pytest.mark.asyncio
    async def test_query_on_session_emits_usage_telemetry(self) -> None:
        response = SimpleNamespace(
            id="r", output_text="x", model="gpt-5",
            usage=_usage(input_tokens=1, output_tokens=1, total_tokens=2),
        )
        provider = OpenAIProvider(client=_make_client(response))
        session = await provider.create_session()

        with patch.object(sdk, "_log_sdk_call") as log:
            result = await provider.query_on_session(session, "p")

        log.assert_called_once_with(result.usage)

    @pytest.mark.asyncio
    async def test_disconnect_session_is_noop(self) -> None:
        client = _make_client(SimpleNamespace())
        provider = OpenAIProvider(client=client)
        session = await provider.create_session()

        assert await provider.disconnect_session(session) is None
        client.responses.create.assert_not_called()


class TestErrorMapping:
    """``openai`` exceptions map onto :class:`ErrorCategory` and wrap as ``SDKError``."""

    @pytest.mark.parametrize(
        ("exc", "category"),
        [
            (RateLimitError("slow down"), ErrorCategory.RATE_LIMIT),
            (APITimeoutError("timed out"), ErrorCategory.TIMEOUT),
            (AuthenticationError("bad key"), ErrorCategory.AUTH),
            (PermissionDeniedError("nope"), ErrorCategory.AUTH),
            (APIConnectionError("dropped"), ErrorCategory.NETWORK),
        ],
    )
    def test_named_exceptions_map(
        self, exc: Exception, category: ErrorCategory,
    ) -> None:
        assert _classify_openai_error(exc) == category

    @pytest.mark.parametrize(
        ("status", "category"),
        [
            (429, ErrorCategory.RATE_LIMIT),
            (401, ErrorCategory.AUTH),
            (403, ErrorCategory.AUTH),
            (408, ErrorCategory.TIMEOUT),
            (500, ErrorCategory.NETWORK),
            (503, ErrorCategory.NETWORK),
        ],
    )
    def test_unmapped_status_error_falls_back_to_status_code(
        self, status: int, category: ErrorCategory,
    ) -> None:
        # An APIStatusError whose class name is not in the map is classified by
        # its HTTP status code.
        assert _classify_openai_error(APIStatusError(status_code=status)) == category

    def test_unmapped_openai_error_without_status_is_unknown(self) -> None:
        assert _classify_openai_error(APIError()) == ErrorCategory.UNKNOWN

    def test_non_openai_exception_is_not_classified(self) -> None:
        # A plain bug is not an openai backend failure — returns None so the
        # caller re-raises it unwrapped rather than mislabelling it transient.
        assert _classify_openai_error(ValueError("boom")) is None

    @pytest.mark.asyncio
    async def test_create_response_retries_transport_failures_then_raises_exhausted(
        self, no_sleep: AsyncMock,
    ) -> None:
        # A transport failure is retried inside the provider (the loop does not
        # retry TIMEOUT/NETWORK) and surfaces as an exhausted SDKError.
        client = SimpleNamespace(
            responses=SimpleNamespace(
                create=AsyncMock(side_effect=APITimeoutError("timed out")),
            ),
        )
        with pytest.raises(SDKError) as excinfo:
            await _create_response(client, {"input": "p"}, "phase-a")

        assert client.responses.create.await_count == (
            openai_provider._TRANSPORT_MAX_ATTEMPTS
        )
        assert no_sleep.await_count == openai_provider._TRANSPORT_MAX_ATTEMPTS - 1
        assert excinfo.value.category == ErrorCategory.TIMEOUT
        assert excinfo.value.caller == "phase-a"

    @pytest.mark.asyncio
    async def test_create_response_returns_after_transient_transport_failure(
        self, no_sleep: AsyncMock,
    ) -> None:
        # A single connection drop is absorbed by the retry: the second attempt
        # succeeds and its response is returned.
        response = SimpleNamespace(id="resp-1")
        client = SimpleNamespace(
            responses=SimpleNamespace(
                create=AsyncMock(
                    side_effect=[APIConnectionError("dropped"), response],
                ),
            ),
        )
        result = await _create_response(client, {"input": "p"}, "phase-a")

        assert result is response
        assert client.responses.create.await_count == 2
        no_sleep.assert_awaited_once()

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (AuthenticationError("bad key"), SDKError),
            (RateLimitError("slow down"), SDKError),
            (APIError(), SDKError),
            (ValueError("boom"), ValueError),
        ],
    )
    @pytest.mark.asyncio
    async def test_create_response_does_not_retry_non_transport_categories(
        self, exc: Exception, expected: type[Exception], no_sleep: AsyncMock,
    ) -> None:
        # AUTH / RATE_LIMIT / UNKNOWN — and a non-openai bug — raise on the
        # first attempt with no backoff sleep.
        client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(side_effect=exc)),
        )
        with pytest.raises(expected):
            await _create_response(client, {"input": "p"}, "phase-a")

        assert client.responses.create.await_count == 1
        no_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_response_wraps_openai_error_as_sdkerror(self) -> None:
        client = SimpleNamespace(
            responses=SimpleNamespace(
                create=AsyncMock(side_effect=RateLimitError("429")),
            ),
        )
        with pytest.raises(SDKError) as excinfo:
            await _create_response(client, {"input": "p"}, "phase-a")
        assert excinfo.value.category == ErrorCategory.RATE_LIMIT
        assert excinfo.value.caller == "phase-a"
        assert isinstance(excinfo.value.original, RateLimitError)

    @pytest.mark.asyncio
    async def test_create_response_reraises_non_openai_error(self) -> None:
        client = SimpleNamespace(
            responses=SimpleNamespace(create=AsyncMock(side_effect=ValueError("bug"))),
        )
        with pytest.raises(ValueError, match="bug"):
            await _create_response(client, {"input": "p"}, None)

    @pytest.mark.asyncio
    async def test_query_wraps_openai_error(self) -> None:
        client = SimpleNamespace(
            responses=SimpleNamespace(
                create=AsyncMock(side_effect=AuthenticationError("bad key")),
            ),
        )
        provider = OpenAIProvider(client=client)
        with pytest.raises(SDKError) as excinfo:
            await provider.query("p", options=QueryOptions(caller="q"))
        assert excinfo.value.category == ErrorCategory.AUTH

    @pytest.mark.asyncio
    async def test_query_on_session_wraps_openai_error(self) -> None:
        client = SimpleNamespace(
            responses=SimpleNamespace(
                create=AsyncMock(side_effect=APITimeoutError("slow")),
            ),
        )
        provider = OpenAIProvider(client=client)
        session = await provider.create_session()
        # TIMEOUT is transport-retried inside the provider (the ``no_sleep``
        # fixture stubs the backoff); assert the exhausted category surfaces.
        with pytest.raises(SDKError) as excinfo:
            await provider.query_on_session(session, "p")
        assert excinfo.value.category == ErrorCategory.TIMEOUT
