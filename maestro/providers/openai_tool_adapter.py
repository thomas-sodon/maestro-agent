"""OpenAI-side materialiser for the vendor-neutral tool registry (proposal 058 Step 10).

Maps a :data:`~maestro.providers.base.ToolRegistry` onto OpenAI **Responses API**
function tools and runs the tool-call dispatch loop, so Maestro's in-process
introspection / messaging handlers (journal, self-awareness, memory, issues,
messaging, email, telegram) run natively on OpenAI without any change to the
handler logic in :mod:`maestro.tools`.

Two halves, mirroring the Anthropic adapter (:mod:`maestro.providers.anthropic_tool_adapter`):

* :meth:`OpenAIToolAdapter.materialize` — the registry → wire-shape transform.
  Where the Anthropic adapter emits ``{"type": "sdk", ...}`` MCP servers, the
  OpenAI adapter emits flat Responses API function-tool specs
  (``{"type": "function", "name", "description", "parameters"}``). The OpenAI
  function namespace is flat (one tool per name, no per-server grouping), so a
  tool's wire name is qualified ``<server>__<tool>`` to stay collision-free
  across servers — the same disambiguation the Anthropic adapter gets for free
  from its ``mcp__<server>__<tool>`` convention. The JSON Schema in
  ``InprocTool.input_schema`` maps 1:1 onto the function ``parameters``.
* :meth:`OpenAIToolAdapter.handler_map` + :meth:`OpenAIToolAdapter.dispatch` —
  the dispatch loop. ``handler_map`` keys each qualified name onto its unchanged
  handler; ``dispatch`` decodes one Responses API ``function_call`` item's JSON
  arguments, awaits the handler, and packs the ``{"type": "text", "text": ...}``
  return into the ``function_call_output`` message OpenAI expects on the next
  turn. The adapter is stateless and never imports ``openai`` — it only shapes
  data — so it is exercised without the optional dependency installed.

This adapter is not yet wired into :meth:`maestro.providers.openai.OpenAIProvider.query`
(the OpenAI provider stays unregistered until sessions land in Step 11); it is a
standalone, test-pinned seam, exactly like the Anthropic adapter shipped in Step 2.
"""

from __future__ import annotations

import json
from typing import Any

from maestro.inproc_mcp import Handler, InprocTool
from maestro.providers.base import ToolRegistry


def _qualified_name(server_name: str, tool: InprocTool) -> str:
    """Return the collision-free wire name for *tool* under *server_name*.

    OpenAI's function namespace is flat, so two servers exposing a same-named
    tool would clash if unqualified. ``<server>__<tool>`` mirrors the
    disambiguation the Anthropic ``mcp__<server>__<tool>`` convention provides.
    """
    return f"{server_name}__{tool.name}"


def _function_tool_spec(qualified_name: str, tool: InprocTool) -> dict[str, Any]:
    """Map an :class:`InprocTool` onto a Responses API function-tool spec.

    The ``parameters`` field is the tool's ``input_schema`` verbatim — plain
    JSON Schema maps 1:1 onto OpenAI's function parameters.
    """
    return {
        "type": "function",
        "name": qualified_name,
        "description": tool.description,
        "parameters": tool.input_schema,
    }


class OpenAIToolAdapter:
    """Materialise a tool registry into OpenAI function tools and dispatch calls."""

    def materialize(
        self, registry: ToolRegistry,
    ) -> tuple[list[str], dict[str, Any]]:
        """Return ``(qualified_tool_names, function_tool_specs_by_name)`` for *registry*.

        The first element is the flat allow-list of qualified names, in
        registry-insertion order (server-first, then tool order within the
        server) so the output is deterministic. The second is the function-tool
        specs keyed by the same qualified name — OpenAI's analogue of the
        Anthropic adapter's ``mcp_servers`` dict; pass ``list(specs.values())``
        as the Responses API ``tools=`` payload.
        """
        qualified_names: list[str] = []
        specs: dict[str, Any] = {}
        for server_name, tools in registry.items():
            for t in tools:
                qualified = _qualified_name(server_name, t)
                qualified_names.append(qualified)
                specs[qualified] = _function_tool_spec(qualified, t)
        return qualified_names, specs

    def handler_map(self, registry: ToolRegistry) -> dict[str, Handler]:
        """Map each qualified tool name onto its unchanged in-process handler.

        Keys match :meth:`materialize`'s names exactly, so a ``function_call``
        the model emits against a registered tool resolves to its handler.
        """
        handlers: dict[str, Handler] = {}
        for server_name, tools in registry.items():
            for t in tools:
                handlers[_qualified_name(server_name, t)] = t.handler
        return handlers

    async def dispatch(
        self, call: Any, handlers: dict[str, Handler],
    ) -> dict[str, Any]:
        """Run one Responses API ``function_call`` and return its tool-result message.

        *call* is a Responses API ``function_call`` output item exposing
        ``name`` (the qualified tool name), ``arguments`` (a JSON-encoded string,
        empty for a no-arg call), and ``call_id`` (the correlation token). The
        decoded arguments are passed to the unchanged handler; the handler's
        ``{"type": "text", "text": ...}`` return is packed into the
        ``function_call_output`` message OpenAI consumes on the next turn.

        An unknown tool name fails loud rather than silently dropping the call —
        the same posture as the in-process MCP server's own dispatch.
        """
        name = call.name
        if name not in handlers:
            raise ValueError(f"Tool {name!r} not found")
        raw_arguments = call.arguments or ""
        arguments = json.loads(raw_arguments) if raw_arguments else {}
        result = await handlers[name](arguments)
        return {
            "type": "function_call_output",
            "call_id": call.call_id,
            "output": result.get("text", ""),
        }

    async def run_tool_calls(
        self, response: Any, handlers: dict[str, Handler],
    ) -> list[dict[str, Any]]:
        """Dispatch every ``function_call`` in one model *response*, in order.

        This is the loop body for a single turn: it walks ``response.output``,
        dispatches each ``function_call`` item, and returns the resulting
        ``function_call_output`` messages to feed back into the next request.
        Non-tool output items (text, reasoning) are skipped.
        """
        outputs: list[dict[str, Any]] = []
        for item in getattr(response, "output", None) or []:
            if getattr(item, "type", None) == "function_call":
                outputs.append(await self.dispatch(item, handlers))
        return outputs


__all__ = ["OpenAIToolAdapter"]
