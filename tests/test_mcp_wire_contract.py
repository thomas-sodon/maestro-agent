"""End-to-end wire-contract tests for the in-process MCP server shim.

These tests stand up the shim server from sample tools and dispatch *real*
``list_tools`` / ``call_tool`` requests through an in-memory connected client
session (``connected_session`` in ``tests/conftest.py`` — no socket, no
subprocess), driving the server the way the SDK client does in production.

They pin the shim's dispatch behaviour (catalog shape, success text, unknown-tool
error, handler-raise error) as an executable contract validated against the real
``mcp`` library, so later work that changes the shim's internals — or bumps the
``mcp`` pin — has a red-on-break contract to move against instead of a red deploy.
"""

from __future__ import annotations

from typing import Any

import pytest

from maestro.inproc_mcp import InprocTool, create_inproc_mcp_server, tool
from tests.conftest import connected_session


def _make_tool(name: str) -> InprocTool:
    """Build a sample echo tool via the ``@tool`` decorator.

    Mirrors the ``_make_tool`` idiom in ``tests/test_inproc_mcp.py`` (there is no
    shared MCP test helper to reuse). The permissive ``msg`` schema lets valid
    arguments pass the server's default input validation.
    """

    @tool(
        name,
        f"echo: {name}",
        {"type": "object", "properties": {"msg": {"type": "string"}}},
    )
    async def handler(inp: Any) -> dict[str, Any]:
        return {"type": "text", "text": f"got:{inp.get('msg', '')}"}

    return handler


@pytest.mark.asyncio
async def test_list_tools_advertises_catalog_over_wire() -> None:
    """A real ``list_tools`` round-trip returns the registered catalog in order.

    Real-dispatch analogue of the private-handler ``list_tools`` test in
    ``tests/test_inproc_mcp.py``.
    """
    cfg = create_inproc_mcp_server("srv", tools=[_make_tool("alpha"), _make_tool("beta")])

    async with connected_session(cfg["instance"]) as client:
        result = await client.list_tools()

    assert [t.name for t in result.tools] == ["alpha", "beta"]
    assert [t.description for t in result.tools] == ["echo: alpha", "echo: beta"]


@pytest.mark.asyncio
async def test_call_tool_returns_text_result_over_wire() -> None:
    """A real ``call_tool`` round-trip returns the handler's text with no error."""
    cfg = create_inproc_mcp_server("srv", tools=[_make_tool("echo")])

    async with connected_session(cfg["instance"]) as client:
        result = await client.call_tool("echo", {"msg": "hi"})

    assert result.is_error is False
    assert len(result.content) == 1
    block = result.content[0]
    assert block.type == "text"
    assert block.text == "got:hi"


@pytest.mark.asyncio
async def test_call_tool_unknown_name_returns_error_over_wire() -> None:
    """An unknown tool name surfaces as an error result carrying ``not found``.

    2.x dropped the auto exception→error conversion 1.x did, so the shim builds
    this unknown-tool ``CallToolResult(is_error=True)`` explicitly; this pins that
    it reaches the client over the wire.
    """
    cfg = create_inproc_mcp_server("srv", tools=[_make_tool("only")])

    async with connected_session(cfg["instance"]) as client:
        result = await client.call_tool("missing", {})

    assert result.is_error is True
    assert "not found" in result.content[0].text


@pytest.mark.asyncio
async def test_call_tool_handler_raise_returns_error_over_wire() -> None:
    """A handler that raises surfaces as an error result over the wire.

    The other branch 2.x no longer auto-converts: the shim's ``try/except`` turns
    the raise into an explicit ``CallToolResult(is_error=True)``.
    """

    @tool("boom", "raises", {"type": "object", "properties": {}})
    async def boom(_: Any) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    cfg = create_inproc_mcp_server("srv", tools=[boom])

    async with connected_session(cfg["instance"]) as client:
        result = await client.call_tool("boom", {})

    assert result.is_error is True
