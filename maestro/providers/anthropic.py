"""Concrete :class:`~maestro.providers.base.Provider` backed by ``maestro.sdk``.

Proposal 058 makes the LLM vendor a configuration choice. This module lands the
first concrete provider: :class:`AnthropicProvider`, a thin, stateless wrapper
whose four operations delegate one-to-one to the private impl functions in
:mod:`maestro.sdk` (``_query_impl`` / ``_create_session_impl`` /
``_query_on_session_impl`` / ``_disconnect_session_impl``).

The provider targets the *private impls*, not the public ``sdk.query`` etc.:
since Phase 2 Step 5 those public functions are thin delegators that route
through :func:`maestro.providers.registry.get_active_provider`, so calling them
from here would recurse (``sdk.query → provider.query → sdk.query → …``) while
``AnthropicProvider`` is the active provider. The impls are the real bodies.

Phase 1 Step 3 is deliberately a *delegating* wrapper, not a physical move of
the ``claude_agent_sdk`` bodies. ``sdk.py``'s impl functions read ~30 module
globals (throttle, semaphore, pricing, ``configure_sdk_*`` state) and are
patched by fully-qualified name across the test suite; relocating their bodies
here would either fork that state or break every patch site. The physical
relocation is a later dedicated refactor. The wrapper therefore holds no state
of its own: all configuration lives in :mod:`maestro.sdk` and is read
transitively through these delegations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# ``maestro.sdk`` is imported as a *module* (not ``from maestro.sdk import
# _query_impl``) for two reasons:
#   1. Delegation resolves the live module attribute at call time, so
#      ``patch("maestro.sdk._query_impl", ...)`` (and the three sibling impls)
#      stays effective when the provider is exercised in tests.
#   2. The attribute-style call ``sdk._query_impl(...)`` is an ``ast.Attribute``,
#      which the session-attachment AST guard (``tests/
#      test_structural_guards.py``) never flags — only a bare ``query()`` is.
# ``sdk.py``'s public ``query``/etc. now delegate back to the active provider
# (Phase 2 Step 5), so this wrapper targets the private ``_*_impl`` bodies to
# avoid the recursion ``sdk.query → provider.query → sdk.query``. That wiring
# lives in ``sdk.py`` as a *lazy* ``from maestro.providers.registry import
# get_active_provider`` inside each function body: a module-top import there
# would create the cycle ``sdk → providers.registry → providers.anthropic →
# sdk`` (``base.py`` already imports ``sdk`` at module top).
import maestro.sdk as sdk
from maestro.providers.anthropic_tool_adapter import AnthropicToolAdapter
from maestro.providers.base import (
    Capabilities,
    QueryOptions,
    QueryResult,
    Session,
)


class AnthropicProvider:
    """Anthropic-backed :class:`~maestro.providers.base.Provider`.

    Each method is a one-to-one delegation to the matching public function in
    :mod:`maestro.sdk`, so behaviour is byte-identical to calling ``sdk.*``
    directly. The class is stateless — it snapshots no module globals — so the
    throttle / semaphore / pricing state stays single-sourced in
    :mod:`maestro.sdk`.
    """

    #: Everything Maestro's Anthropic backend can do today. Frozen descriptor;
    #: Phase 2 validates these flags against per-phase requirements at startup.
    capabilities = Capabilities(
        sessions=True,
        in_process_tools=True,
        remote_mcp=True,
        extended_thinking=True,
        long_context=True,
        structured_output=True,
        usage_metering=True,
        streaming=True,
    )

    #: The adapter that materialises a neutral ``ToolRegistry`` into the SDK
    #: wire shape (already landed in Step 2). Exposed here so Phase 2 can reach
    #: it through the active provider rather than importing the adapter directly.
    tool_adapter = AnthropicToolAdapter()

    async def query(
        self,
        prompt: str,
        *,
        options: QueryOptions | None = None,
        system: str | None = None,
        complete_on: Callable[[Any, str], bool] | None = None,
        stream_idle_timeout_seconds: float | None = None,
    ) -> QueryResult:
        """Delegate to :func:`maestro.sdk._query_impl`."""
        return await sdk._query_impl(
            prompt,
            options=options,
            system=system,
            complete_on=complete_on,
            stream_idle_timeout_seconds=stream_idle_timeout_seconds,
        )

    async def create_session(
        self,
        initial_prompt: str | None = None,
        *,
        options: QueryOptions | None = None,
        system: str | None = None,
        resume_session_id: str | None = None,
    ) -> Session:
        """Delegate to :func:`maestro.sdk._create_session_impl`.

        Returns a real ``claude_agent_sdk.ClaudeSDKClient`` today; callers hold
        it as the opaque :data:`~maestro.providers.base.Session` handle.
        """
        return await sdk._create_session_impl(
            initial_prompt,
            options=options,
            system=system,
            resume_session_id=resume_session_id,
        )

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
        """Delegate to :func:`maestro.sdk._query_on_session_impl`.

        The protocol names the handle ``session``; ``sdk._query_on_session_impl``
        takes it as the first positional ``client``, so it is forwarded
        positionally. ``bypass_warning_throttle`` forwards through so the
        gate-convergence fix loop skips the session-path warning-throttle sleep.
        """
        return await sdk._query_on_session_impl(
            session,
            prompt,
            caller=caller,
            complete_on=complete_on,
            stream_idle_timeout_seconds=stream_idle_timeout_seconds,
            bypass_warning_throttle=bypass_warning_throttle,
        )

    async def disconnect_session(self, session: Session) -> None:
        """Delegate to :func:`maestro.sdk._disconnect_session_impl`."""
        await sdk._disconnect_session_impl(session)
