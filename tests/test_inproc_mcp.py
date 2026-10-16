"""Tests for the local in-process MCP server shim (maestro.inproc_mcp)."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

import pytest
from mcp.server import Server
from packaging.requirements import Requirement

from maestro.inproc_mcp import InprocTool, create_inproc_mcp_server, tool
from tests.conftest import connected_session


def test_mcp_dependency_requires_2x() -> None:
    """The ``mcp`` pin must require the 2.x low-level API the shim targets.

    ``mcp`` 2.0.0 rebuilt the low-level ``mcp.server.Server`` — the decorators
    the 1.x shim used are gone, handlers pass as ``on_list_tools=`` /
    ``on_call_tool=`` constructor kwargs, and the wire fields are snake_case.
    :func:`create_inproc_mcp_server` is now written against that 2.x surface, so
    the pin must *include* the resolved 2.x version and *exclude* the retired 1.x
    line. The emergency ``<2`` cap this inverts is gone (proposal 121). The
    resolved version is read from ``constraints.txt`` rather than hardcoded, so
    a bump that raises the floor past a stale literal does not strand the guard.
    """
    repo_root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    specs = [
        Requirement(dep)
        for dep in data["project"]["dependencies"]
        if Requirement(dep).name == "mcp"
    ]
    assert len(specs) == 1, "expected exactly one mcp dependency specifier"
    resolved = [
        str(spec.version)
        for raw in (repo_root / "constraints.txt").read_text(encoding="utf-8").splitlines()
        if raw.strip() and not raw.strip().startswith("#")
        and Requirement(raw.strip()).name == "mcp"
        for spec in Requirement(raw.strip()).specifier
        if spec.operator == "=="
    ]
    assert len(resolved) == 1, "expected exactly one exact mcp pin in constraints.txt"
    assert resolved[0].startswith("2.")
    assert specs[0].specifier.contains(resolved[0]) is True
    assert specs[0].specifier.contains("1.27.0") is False


def _make_tool(name: str = "echo") -> InprocTool:
    @tool(
        name,
        f"echo: {name}",
        {"type": "object", "properties": {"msg": {"type": "string"}}},
    )
    async def handler(inp: Any) -> dict[str, Any]:
        return {"type": "text", "text": f"got:{inp.get('msg', '')}"}

    return handler


def test_tool_decorator_builds_inproc_tool() -> None:
    t = _make_tool("demo")
    assert isinstance(t, InprocTool)
    assert t.name == "demo"
    assert t.description == "echo: demo"
    assert t.input_schema == {"type": "object", "properties": {"msg": {"type": "string"}}}


def test_create_inproc_mcp_server_returns_sdk_wire_shape() -> None:
    t = _make_tool()
    cfg = create_inproc_mcp_server("my_server", tools=[t])

    assert set(cfg.keys()) == {"type", "name", "instance"}
    assert cfg["type"] == "sdk"
    assert cfg["name"] == "my_server"
    assert isinstance(cfg["instance"], Server)


def test_create_inproc_mcp_server_accepts_no_tools() -> None:
    cfg = create_inproc_mcp_server("empty")
    assert cfg["name"] == "empty"
    assert isinstance(cfg["instance"], Server)


# ── Real 2.x dispatch tests ────────────────────────────────────────────────
#
# These drive the shim through a real in-memory ``list_tools`` / ``call_tool``
# round-trip (``connected_session`` in ``tests/conftest.py``), the way the SDK
# client does in production, and assert on the client-side result. They replace
# the former private-handler tests that reached into the 1.x-only
# ``server.request_handlers`` dict and the ``res.root.*`` wrapper, which 2.x
# removed.


@pytest.mark.asyncio
async def test_list_tools_returns_registered_catalog() -> None:
    a = _make_tool("alpha")
    b = _make_tool("beta")
    server = create_inproc_mcp_server("srv", tools=[a, b])["instance"]

    async with connected_session(server) as client:
        result = await client.list_tools()

    assert [t.name for t in result.tools] == ["alpha", "beta"]
    assert [t.description for t in result.tools] == ["echo: alpha", "echo: beta"]


@pytest.mark.asyncio
async def test_call_tool_routes_by_name_and_wraps_text() -> None:
    server = create_inproc_mcp_server("srv", tools=[_make_tool("echo")])["instance"]

    async with connected_session(server) as client:
        result = await client.call_tool("echo", {"msg": "hi"})

    assert result.is_error is False
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert result.content[0].text == "got:hi"


@pytest.mark.asyncio
async def test_call_tool_propagates_is_error_flag() -> None:
    @tool("boom", "errors", {"type": "object", "properties": {}})
    async def boom(_: Any) -> dict[str, Any]:
        return {"type": "text", "text": "nope", "is_error": True}

    server = create_inproc_mcp_server("srv", tools=[boom])["instance"]

    async with connected_session(server) as client:
        result = await client.call_tool("boom", {})

    assert result.is_error is True
    assert result.content[0].text == "nope"


@pytest.mark.asyncio
async def test_call_tool_unknown_name_returns_error_result() -> None:
    # 2.x dropped the auto exception→error conversion, so the shim builds the
    # unknown-tool error result explicitly; it surfaces as is_error=True with the
    # message in content.
    server = create_inproc_mcp_server("srv", tools=[_make_tool("only")])["instance"]

    async with connected_session(server) as client:
        result = await client.call_tool("missing", {})

    assert result.is_error is True
    assert "'missing' not found" in result.content[0].text


@pytest.mark.asyncio
async def test_call_tool_handler_raise_returns_error_result() -> None:
    # A handler that raises is turned into an explicit is_error=True result — 2.x
    # no longer auto-converts a raised exception into an error result.
    @tool("kaboom", "raises", {"type": "object", "properties": {}})
    async def kaboom(_: Any) -> dict[str, Any]:
        raise ValueError("handler exploded")

    server = create_inproc_mcp_server("srv", tools=[kaboom])["instance"]

    async with connected_session(server) as client:
        result = await client.call_tool("kaboom", {})

    assert result.is_error is True
    assert "handler exploded" in result.content[0].text


@pytest.mark.asyncio
async def test_call_tool_non_text_return_yields_empty_content() -> None:
    @tool("silent", "no text", {"type": "object", "properties": {}})
    async def silent(_: Any) -> dict[str, Any]:
        return {"is_error": False}

    server = create_inproc_mcp_server("srv", tools=[silent])["instance"]

    async with connected_session(server) as client:
        result = await client.call_tool("silent", {})

    assert result.content == []
    assert result.is_error is False


@pytest.mark.asyncio
async def test_call_tool_unsupported_type_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @tool("imgish", "non-text", {"type": "object", "properties": {}})
    async def imgish(_: Any) -> dict[str, Any]:
        return {"type": "image", "data": "...", "mimeType": "image/png"}

    server = create_inproc_mcp_server("srv", tools=[imgish])["instance"]

    with caplog.at_level(logging.WARNING, logger="maestro.inproc_mcp"):
        async with connected_session(server) as client:
            result = await client.call_tool("imgish", {})

    assert result.content == []
    assert any(
        "Unsupported content type" in rec.message and "'image'" in rec.message
        for rec in caplog.records
    )
