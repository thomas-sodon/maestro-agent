"""Proposal 142 — the SDK seam enforces each caller's declared tool surface.

Under ``[sdk] enforced_tool_surface_enabled`` the public ``query`` /
``create_session`` / ``query_on_session`` delegators derive every call's
``tools`` from its caller's :class:`~maestro.tool_surface.ToolSurface`, refuse a
tool name or MCP server the surface does not admit, refuse a shell-capable
surface without action-gate hooks, tag each session with its effective surface,
and refuse a turn whose caller's surface differs from its session's. The
provider is a mock returned by a patched ``get_phase_provider``; no network or
subprocess.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import call_policy, sdk, tool_surface
from maestro.call_policy import CALL_POLICIES, CallPolicy
from maestro.config import Config
from maestro.sdk import QueryOptions, QueryResult
from maestro.tool_surface import (
    REFLECTION_READ_ONLY,
    REPO_MUTATING,
    TOOL_FREE,
    resolve_call_options,
)

_HOOKS: dict[str, Any] = {"PreToolUse": [object()]}

_INTROSPECTION = {
    name: {} for name in ("journal", "self_awareness", "memory", "issues", "diagnostics")
}


class _Session:
    """A plain session double whose instance ``__dict__`` holds real tags."""


def _config(serena: bool = False) -> Config:
    return Config(
        context7_mcp_enabled=False,
        serena_mcp_enabled=serena,
    )


def _agreeing(caller: str) -> QueryOptions:
    """Minimal options a caller's own surface admits."""
    surface = CALL_POLICIES[caller].surface
    hooks = _HOOKS if surface.builtins & tool_surface.SHELL_CAPABLE_BUILTINS else None
    return QueryOptions(caller=caller, hooks=hooks)


@pytest.fixture
def provider() -> Iterator[MagicMock]:
    """Patch the phase and active provider lookups with one mock provider."""
    mock = MagicMock()
    mock.query = AsyncMock(return_value=QueryResult(response="result"))
    mock.create_session = AsyncMock(side_effect=lambda *a, **k: _Session())
    mock.query_on_session = AsyncMock(return_value=QueryResult(response="turn"))
    with (
        patch("maestro.providers.registry.get_phase_provider", return_value=mock),
        patch("maestro.providers.registry.get_active_provider", return_value=mock),
    ):
        yield mock


@pytest.fixture
def enabled() -> None:
    """Turn seam enforcement on (the conftest owner restores it)."""
    sdk.configure_enforced_tool_surface(_config())


# ── Pure resolver ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", sorted(CALL_POLICIES))
def test_every_caller_resolves_to_its_surface_builtins(caller: str) -> None:
    options = _agreeing(caller)
    resolved = resolve_call_options(options, _config())
    expected = sorted(tool_surface.call_effective_surface(caller, options).builtins)
    assert resolved.tools == expected
    assert resolved.tools is not None
    assert options.tools is None


def test_execute_step_shape_resolves() -> None:
    options = QueryOptions(
        caller="execute_step",
        allowed_tools=[
            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
            "mcp__messaging__send_message", "mcp__journal__read_journal",
        ],
        mcp_servers={**_INTROSPECTION, "messaging": {}, "maestro_email": {}},
        hooks=_HOOKS,
    )
    resolved = resolve_call_options(options, _config())
    assert resolved.tools == sorted(REPO_MUTATING.builtins)


def test_grounded_contemplation_resolves() -> None:
    options = QueryOptions(
        caller="contemplation_session",
        allowed_tools=["mcp__journal__read_journal"],
        mcp_servers=_INTROSPECTION,
    )
    resolved = resolve_call_options(options, _config())
    assert resolved.tools == []


def test_reflection_names_on_ungrounded_evolution_raise() -> None:
    options = QueryOptions(
        caller="evolution_session",
        allowed_tools=["mcp__journal__read_journal"],
    )
    with pytest.raises(ValueError, match="does not admit tool"):
        resolve_call_options(options, _config())


def test_unadmitted_server_raises() -> None:
    options = QueryOptions(caller="evolution_session", mcp_servers=_INTROSPECTION)
    with pytest.raises(ValueError, match="does not admit MCP server"):
        resolve_call_options(options, _config())


def test_serena_name_gated_on_serena_flag() -> None:
    options = QueryOptions(
        caller="execute_step",
        allowed_tools=["mcp__serena__find_symbol"],
        hooks=_HOOKS,
    )
    with pytest.raises(ValueError, match="mcp__serena__find_symbol"):
        resolve_call_options(options, _config(serena=False))
    resolved = resolve_call_options(options, _config(serena=True))
    assert resolved.tools == sorted(REPO_MUTATING.builtins)


def test_serena_mutating_name_refused() -> None:
    cfg = _config(serena=True)
    denied = next(
        n for n in cfg.external_mcp_disallowed_tools() or []
        if n.startswith("mcp__serena__")
    )
    options = QueryOptions(caller="execute_step", allowed_tools=[denied], hooks=_HOOKS)
    with pytest.raises(ValueError, match=denied):
        resolve_call_options(options, cfg)


@pytest.mark.parametrize("hooks", [None, {}, {"PreToolUse": []}])
def test_shell_capable_without_hooks_raises(hooks: dict[str, Any] | None) -> None:
    with pytest.raises(ValueError, match="action-gate hooks"):
        resolve_call_options(
            QueryOptions(caller="execute_step", hooks=hooks), _config(),
        )


@pytest.mark.parametrize(
    ("options", "match"),
    [
        (None, "no QueryOptions"),
        (QueryOptions(), "no caller tag"),
        (QueryOptions(caller="not_a_caller"), "not registered"),
    ],
)
def test_unresolvable_call_raises(options: QueryOptions | None, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        resolve_call_options(options, _config())


def test_call_site_deny_list_preserved() -> None:
    options = QueryOptions(
        caller="execute_step", hooks=_HOOKS, disallowed_tools=["Agent", "Write"],
    )
    cfg = _config(serena=True)
    resolved = resolve_call_options(options, cfg)
    surface_denials = tool_surface.resolve_tool_options(
        REPO_MUTATING, cfg,
    ).disallowed_tools or []
    assert surface_denials
    assert resolved.disallowed_tools == [
        "Agent", "Write", *(n for n in surface_denials if n not in ("Agent", "Write")),
    ]


def test_none_deny_list_filled_from_surface() -> None:
    cfg = _config(serena=True)
    options = QueryOptions(caller="execute_step", hooks=_HOOKS)
    resolved = resolve_call_options(options, cfg)
    assert resolved.disallowed_tools == cfg.external_mcp_disallowed_tools()


# ── Seam wiring ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_flag_off_passes_options_through(provider: MagicMock) -> None:
    for caller in CALL_POLICIES:
        options = QueryOptions(caller=caller)
        assert sdk._resolve_tool_surface_options(options) is options
    options = QueryOptions(caller="execute_step")
    session = await sdk.create_session(options=options)
    assert provider.create_session.call_args.kwargs["options"] is options
    assert sdk._SESSION_SURFACE_ATTR not in session.__dict__
    await sdk.query_on_session(session, "p", caller=None)
    provider.query_on_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_query_resolves_tools(provider: MagicMock) -> None:
    options = QueryOptions(caller="propose_plan")
    await sdk.query("p", options=options)
    sent = provider.query.call_args.kwargs["options"]
    assert sent.tools == []
    assert options.tools is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_query_refusal_precedes_dispatch(provider: MagicMock) -> None:
    with pytest.raises(ValueError, match="action-gate hooks"):
        await sdk.query("p", options=QueryOptions(caller="execute_step"))
    provider.query.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_session_tag_round_trips(provider: MagicMock) -> None:
    session = await sdk.create_session(
        options=QueryOptions(caller="implementation_session", hooks=_HOOKS),
    )
    assert session.__dict__[sdk._SESSION_SURFACE_ATTR] == REPO_MUTATING
    await sdk.query_on_session(session, "p", caller="auto_fix")
    provider.query_on_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_mismatched_turn_raises(provider: MagicMock) -> None:
    session = await sdk.create_session(
        options=QueryOptions(caller="evolution_session"),
    )
    with pytest.raises(ValueError, match="differs from its session"):
        await sdk.query_on_session(session, "p", caller="auto_fix")
    provider.query_on_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_rejection_update_self_model_turn_passes(provider: MagicMock) -> None:
    session = await sdk.create_session(
        options=QueryOptions(caller="rejection_self_model_session"),
    )
    assert session.__dict__[sdk._SESSION_SURFACE_ATTR] == TOOL_FREE
    await sdk.query_on_session(session, "p", caller="update_self_model")
    provider.query_on_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_grounded_contemplation_update_self_model_turn_passes(
    provider: MagicMock,
) -> None:
    session = await sdk.create_session(
        options=QueryOptions(
            caller="contemplation_session", mcp_servers=_INTROSPECTION,
        ),
    )
    assert session.__dict__[sdk._SESSION_SURFACE_ATTR] == REFLECTION_READ_ONLY
    await sdk.query_on_session(session, "p", caller="update_self_model")
    provider.query_on_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize(
    ("caller", "match"),
    [(None, "no caller tag"), ("not_a_caller", "not registered")],
)
async def test_unresolvable_turn_raises(
    provider: MagicMock, caller: str | None, match: str,
) -> None:
    session = await sdk.create_session(options=QueryOptions(caller="evolution_session"))
    with pytest.raises(ValueError, match=match):
        await sdk.query_on_session(session, "p", caller=caller)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
@pytest.mark.parametrize("session", [_Session(), object(), MagicMock()])
async def test_untagged_session_raises(provider: MagicMock, session: Any) -> None:
    with pytest.raises(ValueError, match="no surface tag"):
        await sdk.query_on_session(session, "p", caller="evolution_session")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enabled")
async def test_seeded_registry_fault_refuses_turn(provider: MagicMock) -> None:
    session = await sdk.create_session(
        options=QueryOptions(caller="implementation_session", hooks=_HOOKS),
    )
    faulty = dict(CALL_POLICIES)
    faulty["auto_fix"] = CallPolicy(
        "high", TOOL_FREE, "implementation", call_policy.SessionScope.SHARED,
    )
    with (
        patch.object(call_policy, "CALL_POLICIES", MappingProxyType(faulty)),
        pytest.raises(ValueError, match="differs from its session"),
    ):
        await sdk.query_on_session(session, "p", caller="auto_fix")


def test_configure_toggles_global() -> None:
    cfg = _config()
    sdk.configure_enforced_tool_surface(cfg)
    assert sdk._enforced_tool_surface_config is cfg
    sdk.configure_enforced_tool_surface(None)
    assert sdk._enforced_tool_surface_config is None


def _production_mcp_config() -> Config:
    """Production's external MCP posture: only ``context7`` attached."""
    return Config(
        context7_mcp_enabled=True,
        serena_mcp_enabled=False,
    )


def test_real_executor_step_options_resolve() -> None:
    """``Executor._step_query_options`` — the real builder — resolves cleanly."""
    from maestro.email_tools import SERVER_KEY as EMAIL_KEY
    from maestro.executor import Executor
    from maestro.telegram_tools import SERVER_KEY as TELEGRAM_KEY

    servers = {**_INTROSPECTION, EMAIL_KEY: {}, TELEGRAM_KEY: {}, "context7": {}}
    with patch(
        "maestro.executor.create_messaging_server", return_value={},
    ):
        executor = Executor(
            notifier=MagicMock(), message_store=MagicMock(), mcp_servers=servers,
        )
        options = executor._step_query_options(
            "/ws", frozenset({"maestro/task/t1"}), "maestro/task/t1",
        )
    assert options.mcp_servers is not None
    assert "messaging" in options.mcp_servers
    resolved = resolve_call_options(options, _production_mcp_config())
    assert resolved.tools == sorted(REPO_MUTATING.builtins)


@pytest.mark.parametrize(
    "caller", ["implement_plan", "implementation_session", "self_review", "auto_fix"],
)
def test_real_action_gate_hooks_with_task_literal_resolve(caller: str) -> None:
    """``action_gate_hooks(plan)`` + the six-name literal resolves for each impl arm."""
    from types import SimpleNamespace

    from maestro.evolution import EvolutionEngine
    from maestro.tool_surface import TASK_TOOL_BUILTINS

    config = _production_mcp_config()
    plan = MagicMock(plan_id="p-142")
    owner = cast(EvolutionEngine, SimpleNamespace(_config=config))
    hooks = EvolutionEngine.action_gate_hooks(owner, plan)
    options = QueryOptions(
        caller=caller,
        allowed_tools=sorted(TASK_TOOL_BUILTINS),
        mcp_servers={**_INTROSPECTION, "context7": {}},
        disallowed_tools=config.external_mcp_disallowed_tools(),
        hooks=hooks,
    )
    resolved = resolve_call_options(options, config)
    assert resolved.tools == sorted(REPO_MUTATING.builtins)


@pytest.mark.asyncio
async def test_real_executor_start_session_resolves() -> None:
    """``Executor.start_session`` — the session builder — resolves cleanly."""
    from maestro.email_tools import SERVER_KEY as EMAIL_KEY
    from maestro.executor import Executor
    from maestro.telegram_tools import SERVER_KEY as TELEGRAM_KEY

    servers = {**_INTROSPECTION, EMAIL_KEY: {}, TELEGRAM_KEY: {}, "context7": {}}
    create = AsyncMock(return_value=MagicMock())
    with (
        patch("maestro.executor.create_messaging_server", return_value={}),
        patch("maestro.executor.create_session", create),
    ):
        executor = Executor(
            notifier=MagicMock(), message_store=MagicMock(), mcp_servers=servers,
        )
        await executor.start_session(
            "/ws", "identity", frozenset({"maestro/task/t1"}), "maestro/task/t1",
        )
    options = create.call_args.kwargs["options"]
    assert options.caller == "execute_step"
    resolved = resolve_call_options(options, _production_mcp_config())
    assert resolved.tools == sorted(REPO_MUTATING.builtins)
