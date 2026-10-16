"""Anthropic-side materialiser for the vendor-neutral tool registry.

Maps a :data:`~maestro.providers.base.ToolRegistry` into the
``{"type": "sdk", "name", "instance"}`` MCP wire shape the Claude Agent SDK
consumes plus the ``mcp__<server>__<tool>`` qualified-name list it needs in
its allow-list. The adapter is a pure transform — no handler logic moves and
no behaviour changes for the production registry.

When a non-Anthropic provider arrives, it ships its own adapter that
materialises the same registry into its own function-tool spec without
touching the introspection handlers in :mod:`maestro.tools`.
"""

from __future__ import annotations

from typing import Any

from maestro.inproc_mcp import create_inproc_mcp_server
from maestro.providers.base import ToolRegistry


class AnthropicToolAdapter:
    """Materialise a tool registry into the Claude Agent SDK wire shape."""

    def materialize(
        self, registry: ToolRegistry,
    ) -> tuple[list[str], dict[str, Any]]:
        """Return ``(qualified_tool_names, mcp_servers_dict)`` for *registry*.

        Qualified names follow ``mcp__<server>__<tool>`` and are emitted in
        registry-insertion order (server-first, then tool order within the
        server) so the produced list is deterministic and trivial to
        regression-pin.
        """
        qualified_names: list[str] = []
        mcp_servers: dict[str, Any] = {}
        for server_name, tools in registry.items():
            mcp_servers[server_name] = create_inproc_mcp_server(
                name=server_name, tools=list(tools),
            )
            for t in tools:
                qualified_names.append(f"mcp__{server_name}__{t.name}")
        return qualified_names, mcp_servers
