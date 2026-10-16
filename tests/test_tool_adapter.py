"""Unit tests for the vendor-neutral tool registry and the per-provider adapters.

The registry is the canonical surface for Maestro's in-process introspection
tools (proposal 058, tool-adapter slice). These tests pin
:class:`~maestro.providers.anthropic_tool_adapter.AnthropicToolAdapter`'s
transform from a :data:`~maestro.providers.base.ToolRegistry` into the
``{"type": "sdk", "name", "instance"}`` MCP wire shape and the
``mcp__<server>__<tool>`` qualified-name list the Claude Agent SDK expects, and
:class:`~maestro.providers.openai_tool_adapter.OpenAIToolAdapter`'s transform of
the same registry into flat Responses API function-tool specs plus the
function-call dispatch loop (Step 10).

The production round-trip test is a regression pin: materialising the registry
that ``maestro.cli._build_agent_tools`` produces must yield the same
qualified-name list the previous hard-coded ``cli.py`` allow-list emitted, so
the move is behaviour-preserving.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp.server import Server

from maestro.inproc_mcp import InprocTool
from maestro.providers import base
from maestro.providers.anthropic_tool_adapter import AnthropicToolAdapter
from maestro.providers.cli_paced_tool_adapter import (
    CliPacedToolAdapter,
    launcher_config_env,
)
from maestro.providers.openai_tool_adapter import OpenAIToolAdapter
from maestro_sdk import MCP_SERVERS_KEY
from tests.conftest import connected_session


async def _noop_handler(_input: Any) -> dict[str, Any]:
    return {"type": "text", "text": ""}


def _make_tool(name: str) -> InprocTool:
    return InprocTool(
        name=name,
        description=f"synthetic tool {name}",
        input_schema={"type": "object", "properties": {}},
        handler=_noop_handler,
    )


def test_anthropic_adapter_satisfies_tool_adapter_protocol() -> None:
    """The production adapter structurally satisfies the ``ToolAdapter`` protocol."""
    assert isinstance(AnthropicToolAdapter(), base.ToolAdapter)


def test_materialize_two_servers_round_trip() -> None:
    """Two-server registry round-trips into qualified names + wire-shape dict."""
    registry = {
        "alpha": [_make_tool("a1"), _make_tool("a2")],
        "beta": [_make_tool("b1")],
    }

    qualified, servers = AnthropicToolAdapter().materialize(registry)

    # Second-tuple-element keys equal the registry keys.
    assert set(servers.keys()) == {"alpha", "beta"}
    # Each value is the SDK wire shape: type=sdk, name matches the server name,
    # instance is an MCP Server.
    for server_name, cfg in servers.items():
        assert cfg["type"] == "sdk"
        assert cfg["name"] == server_name
        assert isinstance(cfg["instance"], Server)
    # First-tuple-element list equals mcp__<srv>__<tool> in registry-insertion
    # order: servers in insertion order, tools in their list order.
    assert qualified == [
        "mcp__alpha__a1",
        "mcp__alpha__a2",
        "mcp__beta__b1",
    ]


def test_materialize_empty_registry() -> None:
    """Empty registry materialises to empty list + empty dict."""
    qualified, servers = AnthropicToolAdapter().materialize({})
    assert qualified == []
    assert servers == {}


def test_materialize_production_registry_matches_prior_allowlist() -> None:
    """Regression pin: production registry materialises the expected tools.

    The previous ``maestro/cli.py`` hard-coded the first 12 entries below.
    Materialising the registry that ``_build_agent_tools`` now produces must
    yield the same list — proving the move from "hard-coded names + manually
    wrapped servers" to "neutral registry materialised by the adapter" is
    byte-identical at the wire — plus the three ``diagnostics`` tools added by
    the fifth in-process introspection server (issue #445).

    Note: today's memory server declares ``read_task_artifact`` and
    ``search_evolution_plans`` in addition to the three listed below, but the
    prior hard-coded list omitted both. The proposal-058 tool-adapter slice
    is behaviour-inert and preserves that discrepancy; fixing it is a
    separate change (see proposal-058's "Out of scope" note).
    """
    expected = [
        "mcp__journal__write_journal_entry",
        "mcp__journal__read_journal",
        "mcp__journal__search_journal",
        "mcp__self_awareness__read_self_model",
            "mcp__self_awareness__read_conscious_moment",
        "mcp__self_awareness__read_conscious_access_profile",
        "mcp__memory__read_task_history",
        "mcp__memory__read_evolution_plans",
        "mcp__issues__list_open_issues",
        "mcp__issues__create_issue",
        "mcp__issues__close_issue",
        "mcp__diagnostics__get_diagnostic_snapshot",
        "mcp__diagnostics__get_recent_errors",
        "mcp__diagnostics__is_diagnostics_noteworthy",
    ]

    from maestro.cli import _build_agent_tools

    agent_tools = _build_agent_tools(
        journal=MagicMock(),
        consciousness=MagicMock(),
        memory=MagicMock(),
        issue_tracker=MagicMock(),
    )
    qualified, servers = AnthropicToolAdapter().materialize(
        agent_tools["registry"],
    )

    assert qualified == expected
    assert set(servers.keys()) == {
        "journal",
        "self_awareness",
        "memory",
        "issues",
        "diagnostics",
    }


# --- OpenAI adapter (proposal 058 Step 10) ---------------------------------


async def _echo_handler(arguments: Any) -> dict[str, Any]:
    """Echo the decoded arguments so dispatch round-trips are observable."""
    return {"type": "text", "text": f"args={arguments!r}"}


def _echo_tool(name: str) -> InprocTool:
    return InprocTool(
        name=name,
        description=f"echo tool {name}",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        handler=_echo_handler,
    )


class _FakeFunctionCall:
    """Stand-in for a Responses API ``function_call`` output item."""

    type = "function_call"

    def __init__(self, name: str, arguments: str, call_id: str) -> None:
        self.name = name
        self.arguments = arguments
        self.call_id = call_id


class _FakeTextItem:
    """Stand-in for a non-tool Responses API output item (text / reasoning)."""

    type = "message"


class _FakeResponse:
    """Stand-in for a Responses API response exposing an ``output`` list."""

    def __init__(self, output: list[Any]) -> None:
        self.output = output


def _openai_registry() -> dict[str, list[InprocTool]]:
    return {
        "journal": [_echo_tool("read"), _echo_tool("write")],
        "memory": [_echo_tool("search")],
    }


def test_openai_adapter_satisfies_tool_adapter_protocol() -> None:
    assert isinstance(OpenAIToolAdapter(), base.ToolAdapter)


def test_openai_adapter_qualified_names_are_registry_order() -> None:
    names, _specs = OpenAIToolAdapter().materialize(_openai_registry())
    assert names == ["journal__read", "journal__write", "memory__search"]


def test_openai_adapter_spec_parameters_equal_input_schema() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    tool = InprocTool(
        name="search",
        description="memory search tool",
        input_schema=schema,
        handler=_echo_handler,
    )
    _names, specs = OpenAIToolAdapter().materialize({"memory": [tool]})
    spec = specs["memory__search"]
    assert spec["type"] == "function"
    assert spec["name"] == "memory__search"
    assert spec["description"] == "memory search tool"
    # The JSON Schema maps 1:1 onto the function ``parameters`` (identity).
    assert spec["parameters"] == schema
    assert spec["parameters"] is schema


def test_openai_adapter_empty_registry() -> None:
    names, specs = OpenAIToolAdapter().materialize({})
    assert names == []
    assert specs == {}


def test_openai_adapter_handler_map_keys_match_materialize() -> None:
    adapter = OpenAIToolAdapter()
    names, _specs = adapter.materialize(_openai_registry())
    handlers = adapter.handler_map(_openai_registry())
    assert sorted(handlers) == sorted(names)


@pytest.mark.asyncio
async def test_openai_adapter_dispatch_runs_handler_and_packs_output() -> None:
    adapter = OpenAIToolAdapter()
    handlers = adapter.handler_map(_openai_registry())
    call = _FakeFunctionCall(
        name="memory__search", arguments='{"q": "purpose"}', call_id="call_1",
    )
    message = await adapter.dispatch(call, handlers)
    assert message == {
        "type": "function_call_output",
        "call_id": "call_1",
        # The unchanged handler is called with the decoded arguments and its
        # ``{"type": "text", "text": ...}`` return is fed back as the output.
        "output": "args={'q': 'purpose'}",
    }


@pytest.mark.asyncio
async def test_openai_adapter_dispatch_decodes_empty_arguments() -> None:
    adapter = OpenAIToolAdapter()
    handlers = adapter.handler_map(_openai_registry())
    call = _FakeFunctionCall(name="journal__read", arguments="", call_id="call_2")
    message = await adapter.dispatch(call, handlers)
    assert message["output"] == "args={}"


@pytest.mark.asyncio
async def test_openai_adapter_dispatch_unknown_tool_fails_loud() -> None:
    adapter = OpenAIToolAdapter()
    handlers = adapter.handler_map(_openai_registry())
    call = _FakeFunctionCall(name="memory__missing", arguments="{}", call_id="call_3")
    with pytest.raises(ValueError, match="not found"):
        await adapter.dispatch(call, handlers)


@pytest.mark.asyncio
async def test_openai_adapter_dispatch_handler_without_text_yields_empty_output() -> None:
    adapter = OpenAIToolAdapter()
    call = _FakeFunctionCall(name="memory__search", arguments="{}", call_id="call_4")

    async def _no_text_handler(_arguments: Any) -> dict[str, Any]:
        return {"type": "text"}

    handlers = {"memory__search": _no_text_handler}
    message = await adapter.dispatch(call, handlers)
    assert message["output"] == ""


@pytest.mark.asyncio
async def test_openai_adapter_run_tool_calls_skips_non_tool_items() -> None:
    adapter = OpenAIToolAdapter()
    handlers = adapter.handler_map(_openai_registry())
    response = _FakeResponse(
        output=[
            _FakeTextItem(),
            _FakeFunctionCall("journal__read", "{}", "call_a"),
            _FakeFunctionCall("memory__search", '{"q": "x"}', "call_b"),
        ],
    )
    outputs = await adapter.run_tool_calls(response, handlers)
    assert [o["call_id"] for o in outputs] == ["call_a", "call_b"]
    assert outputs[0]["output"] == "args={}"
    assert outputs[1]["output"] == "args={'q': 'x'}"


@pytest.mark.asyncio
async def test_openai_adapter_run_tool_calls_empty_output() -> None:
    adapter = OpenAIToolAdapter()
    response = _FakeResponse(output=[])
    outputs = await adapter.run_tool_calls(
        response, adapter.handler_map(_openai_registry()),
    )
    assert outputs == []


# --- CLI-paced adapter (proposal 061 Step 4, 076 Step 2) -------------------


def _cli_registry() -> dict[str, list[InprocTool]]:
    return {
        "journal": [_echo_tool("read"), _echo_tool("write")],
        "memory": [_echo_tool("search")],
    }


def test_cli_paced_adapter_satisfies_tool_adapter_protocol() -> None:
    assert isinstance(CliPacedToolAdapter(), base.ToolAdapter)


def test_cli_paced_qualified_names_equal_anthropic_form() -> None:
    """Acceptance: qualified names equal the Anthropic adapter's mcp__server__tool form."""
    registry = _cli_registry()
    cli_names, _config = CliPacedToolAdapter(env={}).materialize(registry)
    anthropic_names, _servers = AnthropicToolAdapter().materialize(registry)
    assert cli_names == anthropic_names
    assert cli_names == ["mcp__journal__read", "mcp__journal__write", "mcp__memory__search"]


def test_cli_paced_materialize_emits_launcher_shaped_config() -> None:
    """Acceptance: each --mcp-config entry is a `maestro _mcp-serve <server>` launcher.

    Proposal 076 Step 2 retargets the per-server entries from proposal 061's
    deferred placeholder to the shipped hidden subcommand. With no config env to
    forward (``env={}``) the ``env`` block is omitted entirely.
    """
    _names, config = CliPacedToolAdapter(env={}).materialize(_cli_registry())
    servers = config[MCP_SERVERS_KEY]
    # One stdio launcher per registry server, in registry order.
    assert list(servers) == ["journal", "memory"]
    for server_name, spec in servers.items():
        assert spec == {
            "command": sys.executable,
            "args": ["-P", "-m", "maestro.cli", "_mcp-serve", server_name],
        }


def test_cli_paced_launcher_args_match_cli_subcommand() -> None:
    """The emitted ``args[0]`` is exactly the subcommand cli.py wires its parser to.

    Pins the lazy-imported ``MCP_SERVE_SUBCOMMAND`` so a rename in cli.py cannot
    silently leave the adapter spawning an unknown subcommand (a non-zero exit).
    """
    from maestro.cli import MCP_SERVE_SUBCOMMAND

    _names, config = CliPacedToolAdapter(env={}).materialize({"journal": []})
    spec = config[MCP_SERVERS_KEY]["journal"]
    assert spec["args"] == ["-P", "-m", "maestro.cli", MCP_SERVE_SUBCOMMAND, "journal"]


def test_cli_paced_materialize_threads_config_env_into_each_entry() -> None:
    """Acceptance: a threaded config env rides every launcher entry's ``env`` block.

    The sibling ``maestro _mcp-serve`` reads ``MAESTRO_ENV`` to select the same
    overlay (hence the same state files) as the loop that spawned it.
    """
    adapter = CliPacedToolAdapter(env={"MAESTRO_ENV": "prod"})
    _names, config = adapter.materialize(_cli_registry())
    for spec in config[MCP_SERVERS_KEY].values():
        assert spec["env"] == {"MAESTRO_ENV": "prod"}


def test_cli_paced_materialize_empty_registry() -> None:
    names, config = CliPacedToolAdapter(env={}).materialize({})
    assert names == []
    assert config == {MCP_SERVERS_KEY: {}}


def test_launcher_config_env_forwards_maestro_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default env capture forwards ``MAESTRO_ENV`` from this process."""
    monkeypatch.setenv("MAESTRO_ENV", "staging")
    assert launcher_config_env() == {"MAESTRO_ENV": "staging"}


def test_launcher_config_env_omits_unset_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset var is omitted (not blanked) so the child keeps the loop's default."""
    monkeypatch.delenv("MAESTRO_ENV", raising=False)
    assert launcher_config_env() == {}


def test_cli_paced_default_env_captures_process_config_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default-constructed adapter threads the process's config env (no plumbing)."""
    monkeypatch.setenv("MAESTRO_ENV", "prod")
    _names, config = CliPacedToolAdapter().materialize({"journal": []})
    assert config[MCP_SERVERS_KEY]["journal"]["env"] == {"MAESTRO_ENV": "prod"}


def test_cli_paced_launcher_entries_are_selected_by_external_mcp_servers() -> None:
    """Acceptance: command-shaped launcher entries are forwarded by the provider selector.

    The provider's ``_external_mcp_servers`` keeps only ``command``-shaped specs;
    proposal 061's deferred entries already had a ``command`` key, but this pins
    that the retargeted launcher entries (076 Step 2) still select in, so the
    provider forwards them onto the spawn argv with no provider change.
    """
    from maestro.providers.cli_paced import _external_mcp_servers

    _names, config = CliPacedToolAdapter(env={"MAESTRO_ENV": "prod"}).materialize(
        _cli_registry(),
    )
    servers = config[MCP_SERVERS_KEY]
    selected = _external_mcp_servers(base.QueryOptions(mcp_servers=servers))
    assert selected == servers


def test_cli_paced_build_servers_emits_in_process_sdk_hosts() -> None:
    servers = CliPacedToolAdapter().build_servers(_cli_registry())
    assert set(servers) == {"journal", "memory"}
    for server_name, cfg in servers.items():
        assert cfg["type"] == "sdk"
        assert cfg["name"] == server_name
        assert isinstance(cfg["instance"], Server)


def test_cli_paced_build_servers_empty_registry() -> None:
    assert CliPacedToolAdapter().build_servers({}) == {}


@pytest.mark.asyncio
async def test_cli_paced_host_advertises_registry_tools() -> None:
    """Acceptance: the hosted server's advertised tools match the registry."""
    servers = CliPacedToolAdapter().build_servers(_cli_registry())
    instance = servers["journal"]["instance"]
    async with connected_session(instance) as client:
        result = await client.list_tools()
    assert [t.name for t in result.tools] == ["read", "write"]


@pytest.mark.asyncio
async def test_cli_paced_dispatch_reaches_unchanged_handler() -> None:
    """Acceptance: a dispatched tool call reaches the unchanged Python handler."""
    servers = CliPacedToolAdapter().build_servers(_cli_registry())
    instance = servers["memory"]["instance"]
    async with connected_session(instance) as client:
        result = await client.call_tool("search", {"q": "purpose"})
    # The unchanged _echo_handler ran in-process and its text came back.
    assert result.is_error is False
    assert result.content[0].text == "args={'q': 'purpose'}"
