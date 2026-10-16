"""Unit tests for the vendor-neutral provider contract (proposal 058).

Phase 1 Step 1 lands :mod:`maestro.providers.base` only: the ``Provider``
protocol, the frozen ``Capabilities`` descriptor, the opaque ``Session``
handle, and re-exports of the contract types. These tests pin that surface so
later phases (registry, concrete providers) build on a stable contract.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from maestro import sdk
from maestro.providers import base


def test_contract_types_reexported_are_sdk_objects() -> None:
    """The re-exported contract types are the *same* objects as in ``sdk``."""
    assert base.QueryOptions is sdk.QueryOptions
    assert base.QueryResult is sdk.QueryResult
    assert base.QueryUsage is sdk.QueryUsage
    assert base.ToolCall is sdk.ToolCall
    assert base.ErrorCategory is sdk.ErrorCategory
    assert base.SDKError is sdk.SDKError


def test_all_lists_the_public_surface() -> None:
    """``__all__`` advertises the contract, capabilities, protocol, session, registry."""
    assert set(base.__all__) == {
        "Capabilities",
        "ErrorCategory",
        "Provider",
        "QueryOptions",
        "QueryResult",
        "QueryUsage",
        "SDKError",
        "Session",
        "ToolAdapter",
        "ToolRegistry",
        "ToolCall",
    }


def test_tool_registry_is_dict_alias() -> None:
    """``ToolRegistry`` is the ``dict[str, list[InprocTool]]`` alias."""
    from maestro.inproc_mcp import InprocTool

    # The alias is the parameterised dict generic; ``__origin__`` reaches
    # the builtin ``dict``, and the args carry the key/value types.
    assert base.ToolRegistry.__origin__ is dict
    key_type, value_type = base.ToolRegistry.__args__
    assert key_type is str
    assert value_type.__origin__ is list
    assert value_type.__args__ == (InprocTool,)


def test_tool_adapter_is_runtime_checkable_protocol() -> None:
    """``ToolAdapter`` is a ``Protocol`` with a single ``materialize`` method."""

    class _StubAdapter:
        def materialize(self, registry):  # type: ignore[no-untyped-def]
            return [], {}

    class _MissingAdapter:
        pass

    assert isinstance(_StubAdapter(), base.ToolAdapter)
    assert not isinstance(_MissingAdapter(), base.ToolAdapter)


def test_session_is_an_opaque_alias() -> None:
    """``Session`` is an opaque handle, not a named vendor type."""
    # Typed as ``Any`` so a vendor-specific session flows through the protocol
    # on both sides. The alias exists so call sites read ``Session`` rather
    # than a Claude type while staying assignment-compatible.
    from typing import Any

    assert base.Session is Any


def _capabilities_fields() -> set[str]:
    return {f.name for f in dataclasses.fields(base.Capabilities)}


def test_capabilities_has_the_eight_declared_flags() -> None:
    """The descriptor splits MCP into in-process vs. remote, eight flags total."""
    assert _capabilities_fields() == {
        "sessions",
        "in_process_tools",
        "remote_mcp",
        "extended_thinking",
        "long_context",
        "structured_output",
        "usage_metering",
        "streaming",
    }


def test_capabilities_all_fields_are_bool() -> None:
    """Every capability flag is declared as ``bool``."""
    for f in dataclasses.fields(base.Capabilities):
        assert f.type in ("bool", bool)


def test_capabilities_is_frozen() -> None:
    """A provider's advertised capabilities cannot drift at runtime."""
    caps = base.Capabilities(
        sessions=True,
        in_process_tools=True,
        remote_mcp=True,
        extended_thinking=True,
        long_context=True,
        structured_output=True,
        usage_metering=True,
        streaming=True,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.remote_mcp = False  # type: ignore[misc]


class _StubProvider:
    """Minimal structural implementation of the protocol surface."""

    capabilities = base.Capabilities(
        sessions=True,
        in_process_tools=True,
        remote_mcp=False,
        extended_thinking=False,
        long_context=False,
        structured_output=True,
        usage_metering=True,
        streaming=True,
    )

    async def query(self, prompt, *, options=None, system=None,
                    complete_on=None, stream_idle_timeout_seconds=None):
        return base.QueryResult(response="")

    async def create_session(self, initial_prompt=None, *, options=None,
                             system=None, resume_session_id=None):
        return object()

    async def query_on_session(self, session, prompt, *, caller=None,
                               complete_on=None, stream_idle_timeout_seconds=None):
        return base.QueryResult(response="")

    async def disconnect_session(self, session):
        return None


def test_runtime_checkable_accepts_full_implementation() -> None:
    """A class providing all four operations satisfies the protocol."""
    assert isinstance(_StubProvider(), base.Provider)


def test_runtime_checkable_rejects_missing_operation() -> None:
    """Missing one of the four operations fails the structural check."""

    class _Partial:
        capabilities = _StubProvider.capabilities

        async def query(self, prompt, *, options=None, system=None,
                        complete_on=None, stream_idle_timeout_seconds=None):
            return base.QueryResult(response="")

        # create_session / query_on_session / disconnect_session missing.

    assert not isinstance(_Partial(), base.Provider)


@pytest.mark.parametrize(
    ("method", "sdk_func"),
    [
        ("query", "query"),
        ("create_session", "create_session"),
        ("query_on_session", "query_on_session"),
        ("disconnect_session", "disconnect_session"),
    ],
)
def test_protocol_methods_are_async(method: str, sdk_func: str) -> None:
    """All four protocol operations are declared ``async``."""
    fn = getattr(base.Provider, method)
    assert inspect.iscoroutinefunction(fn)


def test_protocol_signatures_match_sdk_functions() -> None:
    """The protocol reproduces today's ``sdk.py`` keyword surface exactly.

    ``query`` mirrors ``sdk.query``; ``create_session`` mirrors
    ``sdk.create_session`` — modulo the delegator-only ``phase`` parameter. The
    session-attached operations rename the first positional from the Claude
    session type to the opaque ``session`` handle but keep the remaining keyword
    surface identical.

    ``phase`` is deliberately *not* on the provider contract: ``sdk.query`` /
    ``sdk.create_session`` accept it only to *select* the provider via
    ``get_phase_provider`` (058 Step 6 runtime routing), then call the chosen
    provider without it. Strip it before comparing, and pin that it stays a
    seam-only parameter.
    """
    # One-shot query: identical parameters once the seam-only ``phase`` is
    # stripped (protocol adds only ``self``).
    proto_query = list(inspect.signature(base.Provider.query).parameters)
    sdk_query = [p for p in inspect.signature(sdk.query).parameters if p != "phase"]
    assert proto_query == ["self", *sdk_query]
    assert "phase" in inspect.signature(sdk.query).parameters

    proto_create = list(inspect.signature(base.Provider.create_session).parameters)
    sdk_create = [
        p for p in inspect.signature(sdk.create_session).parameters if p != "phase"
    ]
    assert proto_create == ["self", *sdk_create]
    assert "phase" in inspect.signature(sdk.create_session).parameters

    # query_on_session: ``client`` -> ``session`` rename, rest identical.
    proto_qos = list(inspect.signature(base.Provider.query_on_session).parameters)
    sdk_qos = list(inspect.signature(sdk.query_on_session).parameters)
    assert proto_qos[0] == "self"
    assert proto_qos[1] == "session"
    assert sdk_qos[0] == "client"
    assert proto_qos[2:] == sdk_qos[1:]

    # disconnect_session: ``client`` -> ``session`` rename, no other params.
    proto_disc = list(inspect.signature(base.Provider.disconnect_session).parameters)
    sdk_disc = list(inspect.signature(sdk.disconnect_session).parameters)
    assert proto_disc == ["self", "session"]
    assert sdk_disc == ["client"]
