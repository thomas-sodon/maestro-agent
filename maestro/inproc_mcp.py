"""In-process MCP server shim for Maestro's introspection tools.

Local replacement for ``claude_agent_sdk.create_sdk_mcp_server`` and ``@tool``.
Builds the same ``{"type": "sdk", "name": str, "instance": mcp.server.Server}``
wire shape the SDK client consumes, with no dependency on SDK-private helper
APIs.

:func:`create_inproc_mcp_server` targets the ``mcp`` 2.x low-level ``Server`` API
directly. The list/call handlers pass as ``on_list_tools=`` / ``on_call_tool=``
constructor kwargs (2.x removed the ``@server.list_tools()`` /
``@server.call_tool()`` decorators the 1.x shim used), the wire fields are
snake_case (``input_schema``, ``is_error``), and the unknown-tool and
handler-raise error results are built explicitly — 2.x dropped the auto
exception→error conversion the 1.x path leaned on, so a bare ``raise`` no longer
becomes an error result the client can read. The returned wire shape is
version-stable, so every shim consumer is untouched; this mirrors how the pinned
``claude-agent-sdk`` dispatches against the 2.x ``Server`` surface.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp.server import Server, ServerRequestContext
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ContentBlock,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

logger = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class InprocTool:
    """Metadata holder for a single in-process MCP tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler


def tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
) -> Callable[[Handler], InprocTool]:
    """Decorator that wraps an async handler into an :class:`InprocTool`."""

    def decorator(func: Handler) -> InprocTool:
        return InprocTool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=func,
        )

    return decorator


def _content_from_result(result: dict[str, Any]) -> tuple[list[ContentBlock], bool]:
    """Map a tool handler's result dict to ``(content blocks, is_error)``.

    The shim's single result-mapping seam: the ``type == "text"`` mapping, the
    unsupported-type warning, and the ``is_error`` read are defined here exactly
    once and shared by every ``on_call_tool`` dispatch.
    """
    content: list[ContentBlock] = []
    result_type = result.get("type")
    if result_type == "text" and "text" in result:
        content.append(TextContent(type="text", text=result["text"]))
    elif result_type is not None:
        logger.warning(
            "Unsupported content type %r in tool result, skipping",
            result_type,
        )
    return content, bool(result.get("is_error", False))


def create_inproc_mcp_server(
    name: str,
    tools: list[InprocTool] | None = None,
    version: str = "1.0.0",
) -> dict[str, Any]:
    """Build an in-process MCP server config consumable by the SDK client.

    The returned dict matches the shape the SDK's ``Query`` uses: it pulls
    ``config["instance"]`` and dispatches the tool-list / tool-call requests off
    the wrapped ``mcp`` 2.x ``Server``.
    """
    tool_list = list(tools or [])
    tool_map = {t.name: t for t in tool_list}

    # The catalog is built once at construction rather than per list-tools
    # request, mirroring the SDK's own static tool advertisement.
    catalog = [
        Tool(name=t.name, description=t.description, input_schema=t.input_schema)
        for t in tool_list
    ]

    async def _on_list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(tools=catalog)

    async def _on_call_tool(
        ctx: ServerRequestContext, params: CallToolRequestParams
    ) -> CallToolResult:
        tool_name = params.name
        if tool_name not in tool_map:
            # 2.x no longer auto-converts an unknown-tool raise into an error
            # result; build the explicit error the client reads.
            return CallToolResult(
                content=[TextContent(type="text", text=f"Tool {tool_name!r} not found")],
                is_error=True,
            )
        try:
            result = await tool_map[tool_name].handler(params.arguments or {})
        except Exception as exc:
            # 2.x dropped the auto exception→error conversion 1.x did for us;
            # surface the raise as an explicit error result (a typed return, not
            # a swallow — the caller sees ``is_error=True``).
            return CallToolResult(
                content=[TextContent(type="text", text=str(exc))],
                is_error=True,
            )
        content, is_error = _content_from_result(result)
        return CallToolResult(content=content, is_error=is_error)

    server = Server(
        name,
        version=version,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )
    return {"type": "sdk", "name": name, "instance": server}
