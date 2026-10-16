"""Unit tests for the reflection retrieval surface owner (proposal 132).

The owner is a pure leaf — :func:`build_reflection_tool_set` filters an injected
:data:`~maestro.providers.base.ToolRegistry` and the module declares only
frozensets and two integer constants — so these build the production registry in
memory (the way ``tests/test_tool_adapter.py`` does, with ``MagicMock``
collaborators) plus synthetic registries, and assert the allowlist property, the
wire shape, purity, and the guard branches. No ``mem_fs``, no fixtures, no real
filesystem, network, or subprocess is touched (mirrors
``tests/test_plan_grounding.py``'s stance for a pure module).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from maestro import executor
from maestro.cli import _build_agent_tools
from maestro.inproc_mcp import InprocTool
from maestro.providers.anthropic_tool_adapter import AnthropicToolAdapter
from maestro.providers.base import ToolRegistry
from maestro.reflection_retrieval import (
    MAX_REFLECTION_TOOL_CALLS,
    MUTATING_INTROSPECTION_TOOLS,
    READ_ONLY_REFLECTION_TOOLS,
    REFLECTION_SESSION_MAX_TURNS,
    REFLECTION_TOOL_RESULT_CHAR_CAP,
    build_bounded_reflection_tool_set,
    build_reflection_tool_set,
)


async def _noop_handler(_input: Any) -> dict[str, Any]:
    return {"type": "text", "text": ""}


def _make_tool(name: str) -> InprocTool:
    return InprocTool(
        name=name,
        description=f"synthetic tool {name}",
        input_schema={"type": "object", "properties": {}},
        handler=_noop_handler,
    )


def _make_scripted_tool(
    name: str, calls: list[str], result: dict[str, Any],
) -> InprocTool:
    """A read-only tool whose handler records each invocation and returns *result*.

    *name* must be an allowlisted read-only name so the bounded constructor keeps
    it; *calls* accumulates one entry per underlying invocation, so a refused
    over-budget call (which never reaches the handler) leaves it unchanged.
    """
    async def _handler(_input: Any) -> dict[str, Any]:
        calls.append(name)
        return result

    return InprocTool(
        name=name,
        description=f"scripted tool {name}",
        input_schema={"type": "object", "properties": {}},
        handler=_handler,
    )


def _production_registry() -> ToolRegistry:
    """Build the live introspection registry with mock collaborators.

    Mirrors ``tests/test_tool_adapter.py``'s all-``MagicMock`` build so the
    allowlist is pinned against the registry the loop actually assembles.
    """
    agent_tools = _build_agent_tools(
        journal=MagicMock(),
        consciousness=MagicMock(),
        memory=MagicMock(),
        issue_tracker=MagicMock(),
    )
    return agent_tools["registry"]


# The read-only members in registry-insertion order — the wire shape a
# later session build consumes.
_EXPECTED_READ_ONLY_QUALIFIED = [
    "mcp__journal__read_journal",
    "mcp__journal__search_journal",
    "mcp__self_awareness__read_self_model",
    "mcp__memory__read_task_history",
    "mcp__memory__read_evolution_plans",
    "mcp__issues__list_open_issues",
    "mcp__diagnostics__get_diagnostic_snapshot",
    "mcp__diagnostics__get_recent_errors",
    "mcp__diagnostics__is_diagnostics_noteworthy",
]

# Read-only registry tools deliberately withheld from the reflection allowlist
# (proposal 149 Step 4): the conscious-workspace observer is off in base, so they
# could only present a stale or inert moment. Proposal 154 owns their deletion.
_WITHHELD_READ_ONLY_TOOLS = frozenset(
    {"read_conscious_moment", "read_conscious_access_profile"}
)


def test_classification_partitions_the_live_registry() -> None:
    """The allowlist and exclusion sets partition every live registry tool name.

    Disjoint, and their union equals the set of every tool name across the
    production registry — so a tool added to any factory in ``maestro/tools.py``
    without being classified reds this test (self-cleaning tripwire), and no
    allowlisted name is a phantom the registry cannot supply.
    """
    registry = _production_registry()
    all_names = {t.name for tools in registry.values() for t in tools}
    classified = (
        READ_ONLY_REFLECTION_TOOLS
        | MUTATING_INTROSPECTION_TOOLS
        | _WITHHELD_READ_ONLY_TOOLS
    )

    assert READ_ONLY_REFLECTION_TOOLS.isdisjoint(MUTATING_INTROSPECTION_TOOLS)
    assert READ_ONLY_REFLECTION_TOOLS.isdisjoint(_WITHHELD_READ_ONLY_TOOLS)
    assert MUTATING_INTROSPECTION_TOOLS.isdisjoint(_WITHHELD_READ_ONLY_TOOLS)
    assert classified == all_names


def test_conscious_inspection_tools_withheld_from_reflection() -> None:
    """Proposal 149 Step 4: the conscious-workspace inspection tools stay in the
    registry but never reach the reflection surface — the withdrawal is the
    allowlist's doing, not an absence."""
    registry = _production_registry()
    registry_names = {t.name for tools in registry.values() for t in tools}
    kept = {t.name for tools in build_reflection_tool_set(registry).values() for t in tools}
    bounded = {
        t.name
        for tools in build_bounded_reflection_tool_set(registry).values()
        for t in tools
    }

    for name in _WITHHELD_READ_ONLY_TOOLS:
        assert name in registry_names
        assert name not in READ_ONLY_REFLECTION_TOOLS
        assert name not in kept
        assert name not in bounded


def test_build_reflection_tool_set_keeps_only_read_only() -> None:
    """Filtering yields only allowlisted names, no mutating tool, no ``Bash``."""
    registry = _production_registry()

    filtered = build_reflection_tool_set(registry)

    kept = {t.name for tools in filtered.values() for t in tools}
    assert kept == set(READ_ONLY_REFLECTION_TOOLS)
    assert kept.isdisjoint(MUTATING_INTROSPECTION_TOOLS)
    assert "Bash" not in kept
    # Server order preserved; every server has a read-only member today, so all
    # five survive.
    assert list(filtered.keys()) == [
        "journal",
        "self_awareness",
        "memory",
        "issues",
        "diagnostics",
    ]


def test_materialized_subset_carries_no_mutating_member() -> None:
    """Materialising the subset yields the allowlisted read-only qualified names.

    Binds the filtered subset to the ``mcp__<server>__<tool>`` wire shape a later
    session build consumes, proving no mutating member reaches it.
    """
    filtered = build_reflection_tool_set(_production_registry())

    qualified, servers = AnthropicToolAdapter().materialize(filtered)

    assert qualified == _EXPECTED_READ_ONLY_QUALIFIED
    assert len(qualified) == 9
    for mutating in MUTATING_INTROSPECTION_TOOLS:
        assert not any(name.endswith(f"__{mutating}") for name in qualified)
    assert not any(name.endswith("__Bash") for name in qualified)
    assert set(servers.keys()) == {
        "journal",
        "self_awareness",
        "memory",
        "issues",
        "diagnostics",
    }


def test_build_is_pure_and_does_not_mutate_input() -> None:
    """Two calls return equal results; the input registry is untouched."""
    registry = _production_registry()
    pre_lengths = {name: len(tools) for name, tools in registry.items()}
    pre_ids = {name: [id(t) for t in tools] for name, tools in registry.items()}

    first = build_reflection_tool_set(registry)
    second = build_reflection_tool_set(registry)

    assert first == second
    # Input lists unchanged: same lengths, same tool identities, same list
    # objects (the constructor builds new lists, never mutating the inputs).
    assert {name: len(tools) for name, tools in registry.items()} == pre_lengths
    assert {
        name: [id(t) for t in tools] for name, tools in registry.items()
    } == pre_ids
    for name in registry:
        assert first[name] is not registry[name]


def test_empty_registry_returns_empty() -> None:
    """An empty registry filters to an empty registry."""
    assert build_reflection_tool_set({}) == {}


def test_server_with_only_mutating_tools_is_dropped() -> None:
    """A server left with no read-only member is dropped, not emitted empty."""
    registry: ToolRegistry = {
        "mutating_only": [
            _make_tool("write_journal_entry"),
            _make_tool("create_issue"),
        ],
    }

    assert build_reflection_tool_set(registry) == {}


def test_bash_named_tool_is_excluded() -> None:
    """A ``Bash``-named impostor is excluded by default (inclusion-by-allowlist)."""
    registry: ToolRegistry = {
        "journal": [_make_tool("Bash"), _make_tool("read_journal")],
    }

    filtered = build_reflection_tool_set(registry)

    kept = {t.name for tools in filtered.values() for t in tools}
    assert kept == {"read_journal"}
    assert "Bash" not in kept


def test_budget_and_cap_constants() -> None:
    """The budget is positive and the char cap equals Bash's hard cap."""
    assert MAX_REFLECTION_TOOL_CALLS > 0
    # Bound to the sibling literal rather than imported by the leaf, so the two
    # can never silently diverge.
    assert REFLECTION_TOOL_RESULT_CHAR_CAP == executor._BASH_HARD_CAP


def test_session_max_turns_is_a_backstop_above_the_call_budget() -> None:
    """``max_turns`` sits above the in-process call budget (fail-loud backstop)."""
    assert REFLECTION_SESSION_MAX_TURNS > MAX_REFLECTION_TOOL_CALLS
    assert REFLECTION_SESSION_MAX_TURNS == 2 * MAX_REFLECTION_TOOL_CALLS + 2


def test_bounded_preserves_read_only_structure() -> None:
    """The bounded constructor keeps exactly the read-only names and servers."""
    registry = _production_registry()

    bounded = build_bounded_reflection_tool_set(registry)

    kept = {t.name for tools in bounded.values() for t in tools}
    assert kept == set(READ_ONLY_REFLECTION_TOOLS)
    assert list(bounded.keys()) == [
        "journal",
        "self_awareness",
        "memory",
        "issues",
        "diagnostics",
    ]


@pytest.mark.asyncio
async def test_bounded_counter_is_shared_and_refuses_over_budget() -> None:
    """One shared counter across servers; the (N+1)th call is refused in-process.

    Calls alternate across two different servers' tools, so a per-tool counter
    would let each run its own budget — the shared counter caps the *total*, and
    the over-budget call returns an ``is_error`` text without reaching a handler.
    """
    calls: list[str] = []
    registry: ToolRegistry = {
        "journal": [
            _make_scripted_tool("read_journal", calls, {"type": "text", "text": "j"}),
        ],
        "self_awareness": [
            _make_scripted_tool(
                "read_self_model", calls, {"type": "text", "text": "s"},
            ),
        ],
    }
    bounded = build_bounded_reflection_tool_set(registry)
    handlers = [t.handler for tools in bounded.values() for t in tools]

    for i in range(MAX_REFLECTION_TOOL_CALLS):
        result = await handlers[i % len(handlers)]({})
        assert result.get("is_error") is not True
    assert len(calls) == MAX_REFLECTION_TOOL_CALLS

    refused = await handlers[0]({})
    assert refused == {
        "type": "text",
        "text": "<budget exhausted; finish the reflection now>",
        "is_error": True,
    }
    # The underlying handler was never reached by the refused call.
    assert len(calls) == MAX_REFLECTION_TOOL_CALLS


@pytest.mark.asyncio
async def test_bounded_truncates_over_cap_text_result() -> None:
    """An over-cap text result is truncated with the ``[truncated — N …]`` suffix."""
    calls: list[str] = []
    overflow = 50
    big = "x" * (REFLECTION_TOOL_RESULT_CHAR_CAP + overflow)
    registry: ToolRegistry = {
        "journal": [
            _make_scripted_tool("read_journal", calls, {"type": "text", "text": big}),
        ],
    }
    bounded = build_bounded_reflection_tool_set(registry)

    result = await bounded["journal"][0].handler({})

    expected = (
        "x" * REFLECTION_TOOL_RESULT_CHAR_CAP
        + f"\n\n[truncated — {overflow} more chars]"
    )
    assert result == {"type": "text", "text": expected}


@pytest.mark.asyncio
async def test_bounded_passes_under_cap_text_unchanged() -> None:
    """An under-cap text result is returned byte-identically (same object)."""
    calls: list[str] = []
    original = {"type": "text", "text": "short"}
    registry: ToolRegistry = {
        "journal": [_make_scripted_tool("read_journal", calls, original)],
    }
    bounded = build_bounded_reflection_tool_set(registry)

    result = await bounded["journal"][0].handler({})

    assert result is original


@pytest.mark.asyncio
async def test_bounded_passes_non_text_result_through() -> None:
    """A non-``text`` result passes through the cap unchanged."""
    calls: list[str] = []
    original = {"type": "image", "data": "..."}
    registry: ToolRegistry = {
        "journal": [_make_scripted_tool("read_journal", calls, original)],
    }
    bounded = build_bounded_reflection_tool_set(registry)

    result = await bounded["journal"][0].handler({})

    assert result is original


@pytest.mark.asyncio
async def test_bounded_two_invocations_get_independent_counters() -> None:
    """A fresh invocation gets a fresh counter; one exhausted set never mutes another."""
    calls: list[str] = []
    registry: ToolRegistry = {
        "journal": [
            _make_scripted_tool("read_journal", calls, {"type": "text", "text": "j"}),
        ],
    }
    first = build_bounded_reflection_tool_set(registry)
    second = build_bounded_reflection_tool_set(registry)

    for _ in range(MAX_REFLECTION_TOOL_CALLS):
        await first["journal"][0].handler({})
    refused = await first["journal"][0].handler({})
    assert refused["is_error"] is True

    # The second invocation's counter is independent — its first call executes.
    ok = await second["journal"][0].handler({})
    assert ok == {"type": "text", "text": "j"}
