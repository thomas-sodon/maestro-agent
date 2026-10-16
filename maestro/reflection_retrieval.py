"""Bounded read-only retrieval surface for the priced reflection (proposal 132).

The priced reflection — :meth:`Consciousness.introspect`, driven each cycle by
the loop's contemplate phase on the persistent slot-A session — is the one place
Maestro forms and grades a falsifiable bet about itself and its own code. That
session is deliberately tool-free today, so the reflection can reason only over
whatever the loop *pushed* into its prompt; it has no way to *pull* the one fact
a given bet hinges on (does a symbol behave as claimed, did a PR actually land).
Closing that class means eventually granting the reflection a bounded,
structurally read-only retrieval surface: a curated subset of the in-process
introspection tools the loop already ships, never a shell, never a mutating
tool, with a hard tool-call budget and per-result caps.

This module is the single owner of that surface's *policy*: the closed
read-only allowlist, the mutating exclusions it must never expose, the hard
tool-call budget, and the per-result char cap — plus one pure constructor
(:func:`build_reflection_tool_set`) that filters the existing introspection
:data:`~maestro.providers.base.ToolRegistry` down to its read-only members in the
same shape the provider tool adapter already materialises, and one bounded
constructor (:func:`build_bounded_reflection_tool_set`) that composes over it,
wrapping every tool with the in-process call budget and per-result cap the
contemplation session enforces. Read-only by construction
(inclusion-by-allowlist, not exclusion-by-denylist, so an unrecognised tool is
excluded by default), no shell, no mutating tool, cost-bounded.

Pure leaf: zero SDK cost, no I/O, no runtime import beyond the standard library.
:data:`~maestro.providers.base.ToolRegistry` /
:class:`~maestro.inproc_mcp.InprocTool` are imported only under ``TYPE_CHECKING``
so the module never reaches an SDK seam — the structural form of "read-only by
construction, cannot push, merge, or write". The bounded constructor's only
production caller is ``MaestroAgent._build_contemplation_session_options``,
behind the default-off ``[loop] grounded_reflection_enabled`` flag.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from maestro.inproc_mcp import InprocTool
    from maestro.providers.base import ToolRegistry

#: The three mutating members of the in-process introspection servers that the
#: reflection surface must never expose: journal writes and issue
#: create/close. Named explicitly so the classification
#: tripwire in ``tests/test_reflection_retrieval.py`` reds the build if a new
#: mutating tool lands in ``maestro/tools.py`` unclassified.
MUTATING_INTROSPECTION_TOOLS: frozenset[str] = frozenset(
    {
        "write_journal_entry",
        "create_issue",
        "close_issue",
    }
)

#: The read-only members of the introspection registry the reflection may read (the
#: read-only tools of the five ``_build_agent_tools`` servers). The memory server
#: also declares ``read_task_artifact`` and ``search_evolution_plans``, but
#: ``_build_agent_tools`` filters those out via its ``_PREV_MEMORY_TOOLS`` subset,
#: so they are not in the registry and are not listed — the allowlist names
#: exactly what the registry can supply. Inclusion-by-allowlist: a tool not named
#: here is excluded by default, so a future addition (or a ``Bash``-named
#: impostor) can never slip onto the surface. The self-awareness server's two
#: conscious-workspace inspection tools (``read_conscious_moment`` /
#: ``read_conscious_access_profile``) are read-only but deliberately withheld
#: (proposal 149 Step 4): their observer is off in base, so they could only present
#: a stale or inert moment. They stay registered for the operator ``_mcp-serve``
#: surface until proposal 154 deletes the workspace.
READ_ONLY_REFLECTION_TOOLS: frozenset[str] = frozenset(
    {
        "read_journal",
        "search_journal",
        "read_self_model",
        "read_task_history",
        "read_evolution_plans",
        "list_open_issues",
        "get_diagnostic_snapshot",
        "get_recent_errors",
        "is_diagnostics_noteworthy",
    }
)

#: The hard per-reflection tool-call budget; a later increment derives the
#: session's ``max_turns`` from it. The priced reflection completes in one or two
#: turns today, and the budget must allow a handful of grounding reads without
#: doubling the cost of the loop's most expensive phase — four is a deliberate
#: small ceiling, not a round number.
MAX_REFLECTION_TOOL_CALLS: int = 4

#: The per-tool-result char cap, deliberately equal to ``_BASH_HARD_CAP`` in
#: ``maestro/executor.py`` (2000). Kept a literal rather than imported, because
#: importing ``executor`` would drag the SDK into this leaf; the unit test binds
#: the two values so they can never silently diverge.
REFLECTION_TOOL_RESULT_CHAR_CAP: int = 2000

#: The session ``max_turns`` fail-loud backstop, derived from
#: :data:`MAX_REFLECTION_TOOL_CALLS`. The *primary* budget is the in-process call
#: counter in :func:`build_bounded_reflection_tool_set`, which returns a graceful
#: ``is_error`` result once exhausted; ``max_turns`` only bounds a pathological
#: run, and exhausting it returns the CLI's ``error_max_turns`` terminal result
#: (non-transient — the very crash class this surface removes), so it must sit
#: well *above* the call budget: room for every budgeted read, an error turn per
#: over-budget attempt, the trailing text turn, and one spare.
REFLECTION_SESSION_MAX_TURNS: int = 2 * MAX_REFLECTION_TOOL_CALLS + 2


def build_reflection_tool_set(registry: ToolRegistry) -> ToolRegistry:
    """Filter *registry* down to its read-only reflection members.

    Walks *registry* in insertion order and returns a **new** registry whose
    per-server lists contain only the tools whose ``.name`` is in
    :data:`READ_ONLY_REFLECTION_TOOLS`. A server left with no read-only member is
    dropped rather than emitted empty. Inclusion-by-allowlist, not
    exclusion-by-denylist, so an unrecognised tool (a future addition, or a
    ``Bash``-named impostor) is excluded by default.

    The input registry and its lists are never mutated. Returning a
    :data:`~maestro.providers.base.ToolRegistry` (not a flat list) lets a later
    increment hand the result straight to the provider tool adapter's
    ``materialize(...)`` — the same path the executor uses — to obtain qualified
    names plus MCP servers for a session.
    """
    filtered: ToolRegistry = {}
    for server_name, tools in registry.items():
        read_only = [t for t in tools if t.name in READ_ONLY_REFLECTION_TOOLS]
        if read_only:
            filtered[server_name] = read_only
    return filtered


def _cap_reflection_result(result: dict[str, Any]) -> dict[str, Any]:
    """Truncate an over-cap ``text`` tool result; pass everything else through.

    Reproduces ``maestro/tools.py``'s ``read_task_artifact`` truncation idiom —
    ``text[:cap]`` plus a ``[truncated — N more chars]`` suffix — at
    :data:`REFLECTION_TOOL_RESULT_CHAR_CAP` so a grounding read cannot flood the
    priced reflection's context. A non-``text`` result (or an under-cap one) is
    returned unchanged, so an under-cap result is byte-identical to the
    handler's own output; other keys (e.g. ``is_error``) are preserved.
    """
    if result.get("type") != "text":
        return result
    text = result.get("text")
    if not isinstance(text, str) or len(text) <= REFLECTION_TOOL_RESULT_CHAR_CAP:
        return result
    suffix = f"\n\n[truncated — {len(text) - REFLECTION_TOOL_RESULT_CHAR_CAP} more chars]"
    return {**result, "text": text[:REFLECTION_TOOL_RESULT_CHAR_CAP] + suffix}


def build_bounded_reflection_tool_set(registry: ToolRegistry) -> ToolRegistry:
    """Filter *registry* to its read-only members and wrap each with the budget.

    Composes over :func:`build_reflection_tool_set` (so the read-only allowlist
    stays the single owner) and then, with **one shared call counter per
    invocation**, replaces each :class:`~maestro.inproc_mcp.InprocTool`'s handler
    via :func:`dataclasses.replace` with a wrapper that:

    * returns ``{"type": "text", "text": "<budget exhausted; finish the
      reflection now>", "is_error": True}`` — without invoking the underlying
      handler — once the counter reaches :data:`MAX_REFLECTION_TOOL_CALLS`, so an
      over-budget read is a graceful error result the model can recover from, not
      a loop-killing terminal error; and
    * caps a ``text`` result at :data:`REFLECTION_TOOL_RESULT_CHAR_CAP` (via
      :func:`_cap_reflection_result`), passing non-text results through.

    The counter is shared across every wrapped tool of *this* invocation because
    slot A carries two prompts per iteration (``introspect`` then
    ``update_self_model``) on one session, and tools are session-level: the
    budget bounds the iteration as a whole. A fresh invocation gets a fresh
    counter. The input registry and its lists are never mutated;
    ``build_reflection_tool_set`` already returns new lists, and
    :func:`dataclasses.replace` produces new frozen ``InprocTool`` instances.
    """
    base = build_reflection_tool_set(registry)
    calls_made = [0]  # one shared, mutable counter across every wrapped tool

    def _wrap(inner: InprocTool) -> InprocTool:
        underlying = inner.handler

        async def _bounded_handler(tool_input: Any) -> dict[str, Any]:
            if calls_made[0] >= MAX_REFLECTION_TOOL_CALLS:
                return {
                    "type": "text",
                    "text": "<budget exhausted; finish the reflection now>",
                    "is_error": True,
                }
            calls_made[0] += 1
            return _cap_reflection_result(await underlying(tool_input))

        return dataclasses.replace(inner, handler=_bounded_handler)

    return {
        server_name: [_wrap(t) for t in tools]
        for server_name, tools in base.items()
    }
