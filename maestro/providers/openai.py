"""OpenAI reference :class:`~maestro.providers.base.Provider` (proposal 058, Phase 3 Step 9).

This is the second concrete provider — the one that proves the seam against a
genuinely different vendor. It maps Maestro's vendor-neutral
:class:`~maestro.providers.base.QueryOptions` onto an OpenAI **Responses API**
request and maps the response back onto a
:class:`~maestro.providers.base.QueryResult` / :class:`~maestro.providers.base.QueryUsage`.

The query path (Step 9) is the *one-shot* call:

* :meth:`OpenAIProvider.query` — build the request, call the client, map the
  result and usage. Cost is derived from token counts via the existing pricing
  table (:func:`maestro.sdk._estimate_cost_usd`), so a `[sdk.pricing.<model>]`
  entry for the OpenAI model name yields an auditable, ``estimated=True`` cost.
* :attr:`OpenAIProvider.capabilities` — the honest capability descriptor (see
  the class for the per-flag rationale).

**Sessions + error mapping (Step 11) land here.** The Responses API is stateless
HTTP: conversation continuity is the server-side *stored response chain*,
addressed by ``previous_response_id``. :meth:`OpenAIProvider.create_session`
returns an :class:`_OpenAISession` carrying the per-conversation request shape
(model / effort / output-format via the baked ``options`` plus the system
``instructions``) and a rolling ``previous_response_id``;
:meth:`OpenAIProvider.query_on_session` sends each follow-up turn threaded
through that id and advances it from ``response.id``. There is no live
connection, so :meth:`OpenAIProvider.disconnect_session` is a clean no-op — no
local transcript-replay shim is needed because ``previous_response_id`` is
natively supported. :func:`_classify_openai_error` maps ``openai`` exception
classes onto :class:`~maestro.providers.base.ErrorCategory`; both the one-shot
and session paths wrap failures into
:class:`~maestro.providers.base.SDKError` via :func:`_create_response`, which
owns **bounded transport retry** for ``TIMEOUT`` / ``NETWORK`` (the vendor
SDK's own retry loop is pinned off in :func:`_default_client`). A category that
reaches the loop from here is already exhausted: ``RATE_LIMIT`` drives the
loop's cooldown, and ``TIMEOUT`` / ``NETWORK`` / ``AUTH`` are terminal
fail-loud because ``maestro.agent._TRANSIENT_SDK_CATEGORIES`` deliberately
excludes them.

The OpenAI **tool adapter** (Step 10, :mod:`maestro.providers.openai_tool_adapter`)
materialises the in-process tool registry into OpenAI function tools and runs the
function-call dispatch loop, but it is *not yet wired into* :meth:`query` here —
the loop's call sites still pass Anthropic-materialised MCP servers through
``QueryOptions``, so threading the neutral registry to this provider is a
separate plumbing concern. Until that wiring exists, the provider is *not*
registered in :mod:`maestro.providers.registry` — selecting
``[sdk] provider = "openai"`` still fails loud at startup (the transitional
posture pinned by Step 6), so this whole module stays a standalone, test-pinned
seam exactly like the adapters shipped in Steps 2 and 10.

The ``openai`` package is an **optional** dependency: it is not in
``pyproject.toml``'s base install, so this module never imports it at module
top. The real client is built lazily on first use (:func:`_default_client`),
and tests inject a mocked client through :class:`OpenAIProvider`'s constructor,
so the whole module imports and is exercised without ``openai`` installed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from maestro import sdk
from maestro.providers.base import (
    Capabilities,
    ErrorCategory,
    QueryOptions,
    QueryResult,
    QueryUsage,
    SDKError,
    Session,
)

#: ``openai`` exception class name → :class:`ErrorCategory`. Keyed by class name
#: (walking the exception's MRO) rather than ``isinstance`` because ``openai`` is
#: an optional dependency not installed in the unit environment — name-based
#: matching lets the mapping be exercised with stand-in exception classes and
#: keeps this module import-clean without the package. Names are checked
#: most-derived first, so ``APITimeoutError`` wins over its
#: ``APIConnectionError`` base.
_OPENAI_ERROR_CATEGORY: dict[str, ErrorCategory] = {
    "RateLimitError": ErrorCategory.RATE_LIMIT,
    "AuthenticationError": ErrorCategory.AUTH,
    "PermissionDeniedError": ErrorCategory.AUTH,
    "APITimeoutError": ErrorCategory.TIMEOUT,
    "APIConnectionError": ErrorCategory.NETWORK,
}

#: Categories whose failures are *transport* failures — a timed-out or dropped
#: HTTP call, or a 5xx from the backend — and are therefore retryable right here
#: in the provider. The loop deliberately does not retry them (see
#: ``maestro.agent._TRANSIENT_SDK_CATEGORIES``), so this is the only retry
#: boundary the OpenAI path has.
_TRANSPORT_RETRY_CATEGORIES: frozenset[ErrorCategory] = frozenset({
    ErrorCategory.TIMEOUT,
    ErrorCategory.NETWORK,
})

#: Total attempts (first call + retries) a transport failure gets before the
#: ``SDKError`` surfaces to the caller as exhausted.
_TRANSPORT_MAX_ATTEMPTS = 3

#: Seconds of backoff between transport attempts, multiplied by the attempt
#: number so the wait grows linearly across the bounded attempt budget. The
#: *total* worst-case blocking is therefore quadratic in the attempt budget —
#: ``sum(1..N-1) * base`` seconds, 3.0s at the current bound — so raising
#: :data:`_TRANSPORT_MAX_ATTEMPTS` costs more wall time than it looks.
_TRANSPORT_BACKOFF_SECONDS = 1.0

#: Patchable seam for the backoff wait, so tests never sleep for real without
#: mutating the shared :mod:`asyncio` module.
_sleep = asyncio.sleep

_log = logging.getLogger("maestro.providers.openai")


def _classify_openai_error(exc: BaseException) -> ErrorCategory | None:
    """Map an ``openai`` exception onto an :class:`ErrorCategory`.

    Returns ``None`` for an exception that is *not* an ``openai`` error (no
    ``OpenAIError`` in its class hierarchy) so the caller re-raises it unwrapped
    rather than mislabelling an unrelated bug as a transient SDK failure. For a
    recognised ``openai`` error the name map wins; an unmapped status error falls
    back to its HTTP ``status_code`` (429 → rate limit, 401/403 → auth, 408 →
    timeout, 5xx → network), and anything else is :attr:`ErrorCategory.UNKNOWN`.
    """
    mro_names = [cls.__name__ for cls in type(exc).__mro__]
    if "OpenAIError" not in mro_names:
        return None
    for name in mro_names:
        category = _OPENAI_ERROR_CATEGORY.get(name)
        if category is not None:
            return category
    status = getattr(exc, "status_code", None)
    if status == 429:
        return ErrorCategory.RATE_LIMIT
    if status in (401, 403):
        return ErrorCategory.AUTH
    if status == 408:
        return ErrorCategory.TIMEOUT
    if isinstance(status, int) and status >= 500:
        return ErrorCategory.NETWORK
    return ErrorCategory.UNKNOWN


async def _create_response(
    client: Any, request: dict[str, Any], caller: str | None,
) -> Any:
    """Call ``responses.create`` and translate ``openai`` failures to ``SDKError``.

    A recognised ``openai`` exception is wrapped into an
    :class:`~maestro.providers.base.SDKError` carrying the classified
    :class:`ErrorCategory`. **This provider owns transport retry**: a
    :attr:`ErrorCategory.TIMEOUT` or :attr:`ErrorCategory.NETWORK` failure is
    re-tried here up to :data:`_TRANSPORT_MAX_ATTEMPTS` times with a short
    backoff, because the loop does not retry those categories at all. Every
    other category raises on the first attempt, and a non-``openai`` exception
    is re-raised untouched — it is a real bug, not a transient backend failure.

    ``responses.create`` is **not idempotent**: a call that timed out may still
    have been processed and billed server-side, so a retry can duplicate work.
    That is accepted deliberately — a lost turn strands the loop, which has no
    retry of its own for these categories — and every retry is logged at
    warning level so the duplication is visible rather than silent.

    A category that surfaces from here is therefore already *exhausted*:
    :attr:`ErrorCategory.RATE_LIMIT` drives the loop's cooldown, while
    ``TIMEOUT`` / ``NETWORK`` / ``AUTH`` are terminal fail-loud because
    ``maestro.agent._TRANSIENT_SDK_CATEGORIES`` deliberately excludes them.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await client.responses.create(**request)
        except Exception as exc:
            category = _classify_openai_error(exc)
            if category is None:
                raise
            if (
                category in _TRANSPORT_RETRY_CATEGORIES
                and attempt < _TRANSPORT_MAX_ATTEMPTS
            ):
                _log.warning(
                    "openai transport failure (%s) on attempt %d/%d for "
                    "caller=%s; retrying (create is not idempotent): %s",
                    category.value,
                    attempt,
                    _TRANSPORT_MAX_ATTEMPTS,
                    caller,
                    exc,
                )
                await _sleep(_TRANSPORT_BACKOFF_SECONDS * attempt)
                continue
            raise SDKError(
                str(exc) or type(exc).__name__,
                original=exc,
                category=category,
                caller=caller,
            ) from exc


@dataclass
class _OpenAISession:
    """Continuity state for an OpenAI Responses API conversation.

    The Responses API has no live connection: continuity is the server-side
    stored response chain, addressed by ``previous_response_id``. A session
    therefore holds the per-conversation request shape baked at creation
    (``options`` for model / effort / output-format, ``instructions`` for the
    system prompt) and the rolling ``previous_response_id`` advanced after every
    successful turn. The handle flows through the protocol as the opaque
    :data:`~maestro.providers.base.Session`.
    """

    client: Any
    options: QueryOptions | None = None
    instructions: str | None = None
    previous_response_id: str | None = None


def _default_client() -> Any:  # pragma: no cover - needs the optional openai dep installed
    """Build the real ``openai.AsyncOpenAI`` client (lazy, optional dependency).

    Imported here rather than at module top so the module loads without the
    optional ``openai`` package. Pragma-excluded because the unit suite always
    injects a mocked client and never installs ``openai``.

    ``max_retries=0`` pins the vendor SDK's own retry loop *off*: transport
    retry is owned by :func:`_create_response`, and leaving the default
    (``2``) in place would multiply the two budgets into up to nine HTTP
    attempts per call.
    """
    from openai import AsyncOpenAI

    return AsyncOpenAI(max_retries=0)


def _build_request(
    prompt: str,
    options: QueryOptions | None,
    system: str | None,
) -> dict[str, Any]:
    """Map a neutral query onto OpenAI Responses API ``create`` kwargs.

    Only the fields meaningful to a one-shot reasoning call are mapped:

    * ``prompt`` → ``input`` (the Responses API user input).
    * ``system`` → ``instructions`` (the Responses API system prompt slot).
    * ``options.model`` → ``model``.
    * ``options.effort`` → ``reasoning.effort`` — reasoning models expose effort
      rather than a thinking-token budget, which is why ``extended_thinking`` is
      ``False`` and ``task_budget_tokens`` is not mapped.
    * ``options.output_format`` → ``text.format`` (structured outputs).

    Tool / MCP fields (``allowed_tools`` / ``tools`` / ``mcp_servers`` /
    ``disallowed_tools`` / ``hooks``) are not mapped here — tools ride the OpenAI
    tool adapter and its dispatch loop (Step 10), which also enforces
    ``max_turns``. Anthropic-only fields (``betas`` / ``fallback_model``) have no
    Responses API analogue and are intentionally dropped.
    """
    request: dict[str, Any] = {"input": prompt}
    if system is not None:
        request["instructions"] = system
    if options is not None:
        if options.model is not None:
            request["model"] = options.model
        if options.effort is not None:
            request["reasoning"] = {"effort": options.effort}
        if options.output_format is not None:
            request["text"] = {"format": options.output_format}
    return request


def _usage_from_response(response: Any, caller: str | None) -> QueryUsage:
    """Map a Responses API ``response.usage`` onto :class:`QueryUsage`.

    OpenAI reports ``input_tokens`` / ``output_tokens`` / ``total_tokens`` and a
    nested ``input_tokens_details.cached_tokens`` for prompt-cache reads; it has
    no cache-*creation* counter, so that field stays ``0``. Cost is
    ``estimated`` from the configured pricing table — the Responses API returns
    token counts, not a dollar figure — so an operator who adds a
    ``[sdk.pricing.<openai-model>]`` row gets an auditable cost and unknown
    models leave ``total_cost_usd`` unset rather than fabricating ``$0``.

    Note the semantic gap between vendors. OpenAI's ``input_tokens`` is the
    *gross* total — it already contains the cached subset reported under
    ``input_tokens_details.cached_tokens``. Maestro's :class:`QueryUsage`
    follows the Anthropic split, where ``input_tokens`` is the *uncached*
    portion and ``cache_read_tokens`` is tracked separately and added in
    pricing/diagnostics. The two downstream consumers wired against that split
    are :func:`sdk._estimate_cost_usd` (``input_tokens * input_rate +
    cache_read_tokens * cache_read_rate``) and
    :func:`diagnostics.build_sdk_usage_stats` (``cache_read / (cache_read +
    input_tokens)``); plumbing OpenAI's gross input through verbatim would
    double-count cached tokens in cost and bias the cache-hit rate low. We
    subtract the cached subset here (clamped at zero) so both consumers see
    the same shape they were written against.
    """
    model = getattr(response, "model", None)
    raw = getattr(response, "usage", None)
    if raw is None:
        # No usage block (e.g. a stubbed/error response): report a zero-count
        # row tagged with the caller/model so telemetry stays attributable.
        return QueryUsage(caller=caller, model=model, provider="openai")
    details = getattr(raw, "input_tokens_details", None)
    cache_read = (
        sdk._coerce_int(getattr(details, "cached_tokens", 0))
        if details is not None
        else 0
    )
    raw_input_tokens = sdk._coerce_int(getattr(raw, "input_tokens", 0))
    usage = QueryUsage(
        input_tokens=max(raw_input_tokens - cache_read, 0),
        output_tokens=sdk._coerce_int(getattr(raw, "output_tokens", 0)),
        total_tokens=sdk._coerce_int(getattr(raw, "total_tokens", 0)),
        cache_read_tokens=cache_read,
        caller=caller,
        model=model,
        estimated=True,
        provider="openai",
    )
    usage.total_cost_usd = sdk._estimate_cost_usd(usage)
    return usage


def _result_from_response(
    response: Any, caller: str | None, *, structured_requested: bool = False,
) -> QueryResult:
    """Map a Responses API response onto :class:`QueryResult`.

    ``response.output_text`` is the convenience aggregation of all text output;
    ``response.output_parsed`` (present only for structured-output calls) carries
    the parsed object. ``response.id`` is the continuity token a follow-up turn
    resumes from via ``previous_response_id`` — Step 11 consumes it; recording it
    on ``session_id`` now keeps the field's provider-neutral meaning intact.

    ``structured_output`` is forwarded only when *structured_requested* (the call
    passed an ``output_format``); otherwise it stays ``_STRUCTURED_UNSET`` so an
    ordinary text call is not given a phantom ``None`` that the shared sentinel
    would misread as a validated ``null`` (#2533). ``output_parsed`` may itself be
    a validated ``None``, which is preserved verbatim when a schema was requested.
    """
    return QueryResult(
        response=getattr(response, "output_text", "") or "",
        usage=_usage_from_response(response, caller),
        structured_output=(
            getattr(response, "output_parsed", None)
            if structured_requested
            else sdk._STRUCTURED_UNSET
        ),
        session_id=getattr(response, "id", None),
    )


class OpenAIProvider:
    """OpenAI-backed :class:`~maestro.providers.base.Provider` (query path; Step 9).

    Stateless apart from a lazily-built client handle. The client is injected in
    tests (and may be injected in production to share a configured instance);
    when absent it is built on first use via :func:`_default_client`.
    """

    #: Honest capability descriptor for the Step 9 slice. The five flags the
    #: proposal names explicitly:
    #:
    #: * ``in_process_tools=True`` — the introspection / messaging tools are
    #:   portable function-calling tools; the OpenAI adapter that materialises
    #:   them landed in Step 10 (:mod:`maestro.providers.openai_tool_adapter`),
    #:   though it is not yet wired into :meth:`query` while the provider is
    #:   still unregistered and unreachable.
    #: * ``remote_mcp=False`` — reaching the external Context7/Serena MCP
    #:   servers from OpenAI is out of scope for v1.
    #: * ``long_context=False`` — no 1M-context analogue is wired.
    #: * ``extended_thinking=False`` — reasoning models expose ``effort``, not a
    #:   thinking-token budget.
    #: * ``usage_metering=True`` — the Responses API reports token usage.
    #:
    #: The three the proposal leaves unspecified take honest current-slice
    #: values: ``sessions=True`` (Step 11 implements the session ops over
    #: ``previous_response_id``), ``structured_output=True`` (Step 9 maps
    #: ``output_format`` → structured outputs), and ``streaming=False`` (the v1
    #: query is a single non-streaming call).
    capabilities = Capabilities(
        sessions=True,
        in_process_tools=True,
        remote_mcp=False,
        extended_thinking=False,
        long_context=False,
        structured_output=True,
        usage_metering=True,
        streaming=False,
    )

    def __init__(self, client: Any | None = None) -> None:
        """Hold an optional pre-built client; otherwise build it lazily."""
        self._client = client

    def _get_client(self) -> Any:
        """Return the client, building the real one on first use if none was injected."""
        if self._client is None:
            self._client = _default_client()
        return self._client

    async def query(
        self,
        prompt: str,
        *,
        options: QueryOptions | None = None,
        system: str | None = None,
        complete_on: Callable[[Any, str], bool] | None = None,
        stream_idle_timeout_seconds: float | None = None,
    ) -> QueryResult:
        """Run a one-shot Responses API query and map it to a :class:`QueryResult`.

        ``complete_on`` and ``stream_idle_timeout_seconds`` are streaming-only
        levers; the v1 query is a single non-streaming call (``streaming=False``)
        so they are accepted for protocol parity and ignored.
        """
        request = _build_request(prompt, options, system)
        caller = options.caller if options is not None else None
        client = self._get_client()
        response = await _create_response(client, request, caller)
        result = _result_from_response(
            response, caller,
            structured_requested=(
                options is not None and options.output_format is not None
            ),
        )
        # ``options.effort`` is what reached the model (``reasoning.effort``).
        result.usage.effort = options.effort if options is not None else None
        # Emit the same telemetry line / cost-sample / usage-sink the Anthropic
        # path emits, so per-call cost tracking works identically once selected.
        sdk._log_sdk_call(result.usage)
        return result

    async def _turn(
        self, session: _OpenAISession, prompt: str, caller: str | None,
    ) -> QueryResult:
        """Send one conversation turn and advance the session's continuity token.

        Builds the request from the session's baked ``options`` / ``instructions``
        (so the model / effort / output-format chosen at ``create_session`` apply
        to every turn), threads the prior ``previous_response_id`` when present,
        and records ``response.id`` back onto the session so the *next* turn
        continues the same server-side chain. Emits the same per-call usage
        telemetry the one-shot :meth:`query` does.
        """
        request = _build_request(prompt, session.options, session.instructions)
        if session.previous_response_id is not None:
            request["previous_response_id"] = session.previous_response_id
        response = await _create_response(session.client, request, caller)
        result = _result_from_response(
            response, caller,
            structured_requested=(
                session.options is not None
                and session.options.output_format is not None
            ),
        )
        # Every turn runs at the session's baked effort (proposal 140).
        result.usage.effort = (
            session.options.effort if session.options is not None else None
        )
        if result.session_id is not None:
            session.previous_response_id = result.session_id
        sdk._log_sdk_call(result.usage)
        return result

    async def create_session(
        self,
        initial_prompt: str | None = None,
        *,
        options: QueryOptions | None = None,
        system: str | None = None,
        resume_session_id: str | None = None,
    ) -> Session:
        """Open a Responses API conversation as an :class:`_OpenAISession`.

        When *initial_prompt* is ``None`` (Maestro's usual case — the first real
        prompt arrives via :meth:`query_on_session`) no request is sent; the
        session simply holds its baked request shape. When *resume_session_id* is
        provided it seeds ``previous_response_id`` so the conversation continues
        the prior server-side chain. When *initial_prompt* is given it is sent as
        the first turn to establish continuity (its reply text is discarded — the
        contract returns the :data:`Session`, not a :class:`QueryResult`).
        """
        session = _OpenAISession(
            client=self._get_client(),
            options=options,
            instructions=system,
            previous_response_id=resume_session_id,
        )
        if initial_prompt is not None:
            caller = options.caller if options is not None else None
            await self._turn(session, initial_prompt, caller)
        return session

    async def query_on_session(
        self,
        session: Session,
        prompt: str,
        *,
        caller: str | None = None,
        complete_on: Callable[[Any, str], bool] | None = None,
        stream_idle_timeout_seconds: float | None = None,
        bypass_warning_throttle: bool = False,
    ) -> QueryResult:
        """Send a follow-up turn on an :class:`_OpenAISession`.

        ``complete_on`` and ``stream_idle_timeout_seconds`` are streaming-only
        levers; the v1 turn is a single non-streaming call (``streaming=False``)
        so they are accepted for protocol parity and ignored.
        ``bypass_warning_throttle`` is likewise accepted for protocol parity and
        ignored — this provider does not drive the agent-SDK warning throttle.
        """
        return await self._turn(session, prompt, caller)

    async def disconnect_session(self, session: Session) -> None:
        """No-op: a Responses API conversation has no live connection to close.

        Continuity is server-side stored state addressed by
        ``previous_response_id`` (retained by OpenAI independently of any client
        handle), so there is nothing to tear down. Present for protocol parity
        with the Anthropic provider, whose sessions own a live CLI subprocess.
        """
        return None


__all__ = ["OpenAIProvider"]
