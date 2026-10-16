"""Vendor-neutral provider contract for Maestro's reasoning surface.

Maestro's entire reasoning surface routes through four operations —
``query``, ``create_session``, ``query_on_session``, ``disconnect_session`` —
plus the ``QueryOptions`` / ``QueryResult`` data contracts around them
(see :mod:`maestro.sdk`). Proposal 058 makes the LLM vendor a configuration
choice rather than an architectural assumption by declaring those operations
as a :class:`Provider` protocol with an explicit :class:`Capabilities`
descriptor.

This module is the single neutral import point for the contract. Providers
implement :class:`Provider`; call sites hold an opaque :data:`Session` handle
and import :class:`QueryOptions` / :class:`QueryResult` / … from here, so no
loop module needs to name a vendor type.

Phase 1 Step 1 (proposal 058) lands the protocol, the capability descriptor,
the opaque session handle, and the contract re-exports. It is behaviour-inert:
nothing imports it yet, and the contract types are still *defined* in
:mod:`maestro.sdk` — this module only re-exports them so the eventual
selection seam (Phase 2) and concrete providers (Phases 3-4) have one neutral
surface to depend on.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from maestro.inproc_mcp import InprocTool

# Re-export the contract types from :mod:`maestro.sdk` so providers and call
# sites import them from this neutral module. The types remain *defined* in
# ``sdk.py`` for now (Phase 1 is a behaviour-preserving move); ``base`` is the
# stable address callers will migrate to. ``sdk.py`` does not import ``base``,
# so this re-export introduces no import cycle.
from maestro.sdk import (
    ErrorCategory,
    QueryOptions,
    QueryResult,
    QueryUsage,
    SDKError,
    ToolCall,
)

__all__ = [
    "Capabilities",
    "ErrorCategory",
    "Provider",
    "QueryOptions",
    "QueryResult",
    "QueryUsage",
    "SDKError",
    "Session",
    "ToolAdapter",
    "ToolCall",
    "ToolRegistry",
]

#: Vendor-neutral catalogue of in-process tools, keyed by logical server name.
#:
#: Each value is the ordered list of :class:`~maestro.inproc_mcp.InprocTool`
#: handlers a server exposes. The registry is the canonical surface; a
#: provider-specific :class:`ToolAdapter` materialises it into the wire shape
#: that provider's SDK expects (see :class:`ToolAdapter`).
ToolRegistry = dict[str, list[InprocTool]]


#: Opaque handle to a provider-managed conversation session.
#:
#: :meth:`Provider.create_session` returns one of these and
#: :meth:`Provider.query_on_session` / :meth:`Provider.disconnect_session`
#: consume it. The concrete object is provider-specific (the Anthropic
#: provider returns a ``claude_agent_sdk.ClaudeSDKClient``; other providers
#: return their own state object), so the contract keeps it opaque: call sites
#: receive it from ``create_session`` and pass it back unmodified, never
#: inspecting or naming the underlying vendor type. Typed as ``Any`` so a
#: vendor-specific session flows through the protocol without a cast on either
#: side while still reading as a distinct concept at the call site.
Session = Any


@dataclass(frozen=True)
class Capabilities:
    """What a provider can do, declared explicitly rather than inferred.

    Phases declare the capabilities they require and the loop validates the
    selected provider at startup, failing loud when a phase needs a capability
    the provider lacks (proposal 058, Phase 2). The descriptor is frozen so a
    provider's advertised capabilities cannot drift at runtime.

    The single "supports MCP" question is deliberately split into two flags:

    * ``in_process_tools`` — can the provider host Maestro's own Python tool
      handlers (the journal / self-awareness / memory / issues introspection
      servers plus messaging / email / telegram)? These are plain
      function-calling tools, portable to any function-calling provider.
    * ``remote_mcp`` — can the provider reach *external* MCP-protocol servers
      (Context7, Serena), which are transport-bound and need a client
      that speaks MCP?

    Conflating the two would wrongly treat the portable introspection tools as
    a Claude-only capability.
    """

    sessions: bool
    in_process_tools: bool
    remote_mcp: bool
    extended_thinking: bool
    long_context: bool
    structured_output: bool
    usage_metering: bool
    streaming: bool


@runtime_checkable
class Provider(Protocol):
    """The four operations Maestro's reasoning surface routes through.

    A concrete provider backs the four public functions in :mod:`maestro.sdk`
    (Phase 2 wires the registry delegation). The signatures reproduce today's
    ``sdk.py`` functions exactly so the move is behaviour-preserving; the only
    change is that ``query_on_session`` / ``disconnect_session`` traffic in the
    opaque :data:`Session` handle rather than a named vendor session type.

    ``@runtime_checkable`` enables structural ``isinstance`` checks (method
    presence only — it does not verify signatures); the registry and tests use
    it to confirm a candidate provides the protocol surface.
    """

    #: What this provider can do; validated against per-phase requirements at
    #: startup (proposal 058, Phase 2).
    capabilities: Capabilities

    async def query(
        self,
        prompt: str,
        *,
        options: QueryOptions | None = None,
        system: str | None = None,
        complete_on: Callable[[Any, str], bool] | None = None,
        stream_idle_timeout_seconds: float | None = None,
    ) -> QueryResult:
        """Run a one-shot query and return the final :class:`QueryResult`."""
        ...

    async def create_session(
        self,
        initial_prompt: str | None = None,
        *,
        options: QueryOptions | None = None,
        system: str | None = None,
        resume_session_id: str | None = None,
    ) -> Session:
        """Open a persistent session; the caller owns its lifecycle.

        When *initial_prompt* is ``None`` the session connects without sending
        a first message. When *resume_session_id* is provided the provider
        resumes prior conversation state where it can. Returns an opaque
        :data:`Session` handle the caller passes to :meth:`query_on_session`
        and :meth:`disconnect_session`.
        """
        ...

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
        """Send a follow-up message on an existing :data:`Session`.

        ``bypass_warning_throttle`` lets the gate-convergence fix loop opt out
        of the soft per-call warning-throttle sleep. Only the Anthropic
        provider drives that throttle; other providers accept and ignore it.
        """
        ...

    async def disconnect_session(self, session: Session) -> None:
        """Tear down a :data:`Session` opened by :meth:`create_session`."""
        ...


@runtime_checkable
class ToolAdapter(Protocol):
    """Materialise a vendor-neutral :data:`ToolRegistry` into a provider's wire shape.

    The registry is canonical; each provider supplies its own adapter that
    walks the registry and emits the pair the provider's session API expects:
    the list of qualified tool names to allow, and the dict of server configs
    to register. Keeping the transform in a single seam means the introspection
    handlers in :mod:`maestro.tools` stay portable to any function-calling
    provider — only the adapter needs to know the wire shape.
    """

    def materialize(
        self, registry: ToolRegistry,
    ) -> tuple[list[str], dict[str, Any]]:
        """Return ``(qualified_tool_names, mcp_servers_dict)`` for *registry*.

        The first element is the list of qualified names to pass to the
        provider's allow-list; the second is the dict the provider's session
        constructor consumes under its ``mcp_servers`` (or equivalent) keyword.
        """
        ...
