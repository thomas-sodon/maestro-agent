"""CLI interface for Maestro."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import functools
import json as json_mod
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal

from maestro import gate_environment
from maestro.config import Config
from maestro.conscious_workspace import (
    ConsciousEpisodeBackend,
    JsonlConsciousEpisodeLedger,
)
from maestro.consciousness import Consciousness, SelfModel, claim_key
from maestro.consciousness_eval import (
    CausalReachReading,
    LimitedAccessReading,
    ReceiptedBroadcastReading,
    RecurrenceReading,
    SchemaAgreementReading,
    TemporalContinuityReading,
    evaluate_causal_reach,
    evaluate_limited_access,
    evaluate_receipted_broadcast,
    evaluate_recurrence,
    evaluate_schema_agreement,
    evaluate_temporal_continuity,
)
from maestro.contributions import ContributionTracker
from maestro.cost_tracking import CostSampleBackend, JsonlCostSampleStore
from maestro.deed_ledger import DeedLedgerBackend, JsonlDeedLedger
from maestro.escalation_bridge import (
    EscalationLedgerBackend,
    JsonEscalationLedger,
    active_standdown_prs,
)
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.fileutil import atomic_write
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import (
    AuditHistoryBackend,
    JsonlAuditHistoryLedger,
    JsonlQuestionResolutionLedger,
    JsonlRejectionHistoryLedger,
    JsonlVersionArchiveLedger,
    Memory,
    QuestionResolutionBackend,
    RejectionHistoryBackend,
    VersionArchiveBackend,
)
from maestro.messaging import (
    PRE_DEPLOY_MESSAGE_ID_PREFIX,
    Message,
    MessageStore,
)
from maestro.notifications.base import Notifier, NullNotifier
from maestro.planner import Planner
from maestro.pr_ledger import JsonPRLedger, PRLedgerBackend
from maestro.providers.capability_gate import (
    validate_action_gate_coverage,
    validate_provider_capabilities,
)
from maestro.providers.cli_paced_gate import GATE_SUBCOMMAND, run_gate_bash_hook
from maestro.providers.registry import configure_phase_providers, configure_provider
from maestro.search import EphemeralJournalSearch, JournalSearchBackend
from maestro.self_calibration import CONSCIOUSNESS_CRITERIA, ConsciousnessCriterion
from maestro.state_store import (
    SqliteAuditHistoryLedger,
    SqliteConsciousEpisodeLedger,
    SqliteCostSampleStore,
    SqliteDeedLedger,
    SqliteEscalationLedger,
    SqliteGateActivationLedger,
    SqliteJournalSearchIndex,
    SqlitePredictionLedger,
    SqlitePremiseLedger,
    SqlitePRLedger,
    SqliteQuestionResolutionLedger,
    SqliteRejectionHistoryLedger,
    SqliteSettledVerdictsLedger,
    SqliteStandDownVerdictLedger,
    SqliteVersionArchiveLedger,
    StateStore,
)
from maestro.vcs import CodeHost, SubprocessCodeHost, SubprocessVCS, VCSError

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.audit_checks import (
        GateActivationBackend,
    )
    from maestro.audit_checks_calibration import (
        PredictionBackend,
        PremiseBackend,
        SelfCalibrationReading,
    )
    from maestro.audit_checks_settlement import (
        SettledVerdictsBackend,
    )
    from maestro.caller_spend import SpendReading
    from maestro.email import EmailClient
    from maestro.inproc_mcp import InprocTool
    from maestro.lexicon import LexiconEntry
    from maestro.remediation_tracker import StandDownVerdictBackend

logger = logging.getLogger("maestro")

#: Hidden ``maestro`` subcommand (proposal 076) the ``claude`` CLI child spawned
#: by the ``cli_paced`` provider invokes to host one logical introspection
#: server over stdio MCP. Underscore-prefixed and ``help=``-less like
#: ``_gate-bash-hook`` (066): machine-invoked, never an operator surface.
MCP_SERVE_SUBCOMMAND = "_mcp-serve"


def _build_agent_tools(
    journal: Journal,
    consciousness: Consciousness,
    memory: Memory,
    issue_tracker: IssueTracker,
    *,
    issues_page_size: int | None = None,
    log_path: Path | None = None,
    memory_dir: Path | None = None,
    conscious_episodes: ConsciousEpisodeBackend | None = None,
) -> dict[str, Any]:
    """Assemble the introspection tool registry consumed by the executor.

    Returns ``{"registry": ToolRegistry}`` — a vendor-neutral catalogue of
    in-process tools keyed by logical server name. The executor materialises
    it into the active provider's wire shape via a
    :class:`~maestro.providers.base.ToolAdapter` (today's
    :class:`~maestro.providers.anthropic_tool_adapter.AnthropicToolAdapter`
    reproduces the previous ``{"type": "sdk", "name", "instance"}`` MCP shape
    and the ``mcp__<server>__<tool>`` qualified names).
    """
    from maestro.tools import (
        diagnostics_tools,
        issues_tools,
        journal_tools,
        memory_tools,
        self_awareness_tools,
    )

    # Behaviour-preserving subset of memory_tools(): today's hard-coded
    # cli.py allow-list omits ``read_task_artifact`` and
    # ``search_evolution_plans`` even though the memory server declares them.
    # Proposal 058's tool-adapter slice is behaviour-inert; fixing the
    # discrepancy is a separate change (see proposal-058 plan's "Out of scope").
    _PREV_MEMORY_TOOLS = {
        "read_task_history",
        "read_evolution_plans",
    }

    memory_handlers = [
        t for t in memory_tools(memory) if t.name in _PREV_MEMORY_TOOLS
    ]

    registry: dict[str, Any] = {
        "journal": journal_tools(journal),
        "self_awareness": self_awareness_tools(
            consciousness, conscious_episodes,
        ),
        "memory": memory_handlers,
        "issues": issues_tools(issue_tracker, page_size=issues_page_size),
        "diagnostics": diagnostics_tools(log_path, memory_dir),
    }
    return {"registry": registry}


def _build_cost_sample_backend(config: Config) -> CostSampleBackend:
    """Construct the cost-sample backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``cost_samples.jsonl``) unless the flag is on, in
    which case a :class:`SqliteCostSampleStore` over ``state.db``. Mirrors the
    agent's own cost-sample seam so the SDK write path
    (:func:`configure_cost_tracking`) and the dream-cycle phase report read and
    write the same substrate the agent's readers use (proposal 106).
    """
    if config.sqlite_state_enabled:
        return SqliteCostSampleStore(StateStore(config.memory_dir / "state.db"))
    return JsonlCostSampleStore(config.cost_samples_path)


def _build_deed_ledger_backend(config: Config) -> DeedLedgerBackend:
    """Construct the deed-ledger backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``deed_ledger.jsonl``) unless the flag is on, in
    which case a :class:`SqliteDeedLedger` over a fresh ``state.db`` connection
    (short-lived per CLI process), seeded once from the legacy JSONL so the CLI
    reads the same all-time history the loop writes. Mirrors
    :func:`_build_cost_sample_backend` (proposal 106).
    """
    if config.sqlite_state_enabled:
        return SqliteDeedLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlDeedLedger(config.memory_dir)


def _build_conscious_episode_backend(config: Config) -> ConsciousEpisodeBackend:
    """Construct the conscious-episode backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``conscious_episodes.jsonl``) unless the flag is on,
    in which case a :class:`SqliteConsciousEpisodeLedger` over a fresh ``state.db``
    connection (short-lived per CLI process), seeded once from the legacy JSONL so
    the read-only self-inspection tools see the same accumulated trace the loop
    writes. The JSONL constructor performs no I/O, so building this backend stays
    safe under ``mem_fs``. Mirrors :func:`_build_deed_ledger_backend` (proposal
    130 / 106).
    """
    if config.sqlite_state_enabled:
        return SqliteConsciousEpisodeLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlConsciousEpisodeLedger(config.memory_dir)


def _build_loop_conscious_episode_backend(
    config: Config,
) -> ConsciousEpisodeBackend | None:
    """The loop's conscious-episode backend, withheld while observation is off.

    With ``conscious_workspace_observe_enabled`` off no moment is written, so the
    loop's ``read_conscious_*`` tools get no backend and answer with their inert
    branch rather than presenting a stale historical moment as current attention
    (proposal 149). Operator reads (``consciousness-report``, ``mcp-serve``) keep
    :func:`_build_conscious_episode_backend`.
    """
    if not config.conscious_workspace_observe_enabled:
        return None
    return _build_conscious_episode_backend(config)


def _build_audit_history_backend(config: Config) -> AuditHistoryBackend:
    """Construct the audit-history backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``audit_history.jsonl``) unless the flag is on, in
    which case a :class:`SqliteAuditHistoryLedger` over ``state.db``, seeded once
    from the legacy JSONL so enabling the flag preserves all-time audit history.
    Mirrors :func:`_build_cost_sample_backend` (proposal 106).
    """
    if config.sqlite_state_enabled:
        return SqliteAuditHistoryLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlAuditHistoryLedger(config.memory_dir)


def _build_rejection_history_backend(
    config: Config,
) -> RejectionHistoryBackend:
    """Construct the rejection-history backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``rejection_history.jsonl``) unless the flag is on,
    in which case a :class:`SqliteRejectionHistoryLedger` over ``state.db``,
    seeded once from the legacy JSONL so enabling the flag preserves all-time
    rejection history. Mirrors :func:`_build_audit_history_backend` (proposal 106).
    """
    if config.sqlite_state_enabled:
        return SqliteRejectionHistoryLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlRejectionHistoryLedger(config.memory_dir)


def _build_version_archive_backend(config: Config) -> VersionArchiveBackend:
    """Construct the version-archive backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``version_archive.jsonl``) unless the flag is on, in
    which case a :class:`SqliteVersionArchiveLedger` over ``state.db``, seeded
    once from the legacy JSONL so enabling the flag preserves the all-time
    merged-plan ledger the three readers (``Memory``, the owned-act-drought
    sensor, the merge-truth corroboration reader) walk. Mirrors
    :func:`_build_rejection_history_backend` (proposal 106).
    """
    if config.sqlite_state_enabled:
        return SqliteVersionArchiveLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlVersionArchiveLedger(config.memory_dir)


def _build_journal_search_backend(config: Config) -> JournalSearchBackend:
    """Construct the journal-search backend per ``[paths] sqlite_state_enabled``.

    The default per-call ephemeral FTS5 rebuild (:class:`EphemeralJournalSearch`)
    unless the flag is on, in which case a
    :class:`~maestro.state_store.SqliteJournalSearchIndex` over ``state.db`` — the
    persistent, incrementally-maintained index that tops itself up with only the
    entries appended since the last search instead of re-tokenizing the whole
    (deliberately-unbounded) journal every call. Unlike the seeded ledgers there
    is no legacy seed: the index is a rebuildable derived cache that self-heals
    from the corpus on the first search, and ``journal.jsonl`` stays the sole
    source of truth and the flag-off rollback. Mirrors
    :func:`_build_version_archive_backend` (proposal 106).
    """
    if config.sqlite_state_enabled:
        return SqliteJournalSearchIndex(StateStore(config.memory_dir / "state.db"))
    return EphemeralJournalSearch()


def _build_pr_ledger_backend(config: Config) -> PRLedgerBackend:
    """Construct the per-PR remediation-ledger backend per ``[paths] sqlite_state_enabled``.

    The default JSON store (``pr_ledger.json``) unless the flag is on, in which
    case a :class:`SqlitePRLedger` over ``state.db``, seeded once from the legacy
    JSON so enabling the flag preserves the in-flight per-PR budget/latch state
    (the observed origin head, the per-head attempt budget, the sticky ``stuck``
    latch). Mirrors :func:`_build_version_archive_backend` (proposal 106).
    """
    if config.sqlite_state_enabled:
        return SqlitePRLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_json_dir=config.memory_dir,
        )
    return JsonPRLedger(config.memory_dir)


def _build_escalation_ledger_backend(config: Config) -> EscalationLedgerBackend:
    """Construct the escalation-standdown ledger backend per ``[paths] sqlite_state_enabled``.

    The default JSON store (``escalation_handoffs.json``) unless the flag is on,
    in which case a :class:`SqliteEscalationLedger` over ``state.db``, seeded once
    from the legacy JSON so the ``pr-laps`` diagnostic reads the live in-flight
    standdowns under either flag state. Mirrors :func:`_build_pr_ledger_backend`
    (proposal 106).
    """
    if config.sqlite_state_enabled:
        return SqliteEscalationLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_json_dir=config.memory_dir,
        )
    return JsonEscalationLedger(config.memory_dir)


def _build_question_resolution_backend(
    config: Config,
) -> QuestionResolutionBackend:
    """Construct the question-resolution backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``question_resolutions.jsonl``) unless the flag is
    on, in which case a :class:`SqliteQuestionResolutionLedger` over ``state.db``,
    seeded once from the legacy JSONL so enabling the flag preserves the all-time
    lineage the stalled-question audit rule walks. Mirrors
    :func:`_build_rejection_history_backend` (proposal 106).
    """
    if config.sqlite_state_enabled:
        return SqliteQuestionResolutionLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlQuestionResolutionLedger(config.memory_dir)


def _build_gate_activation_backend(config: Config) -> GateActivationBackend:
    """Construct the gate-activation backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``gate_activations.jsonl``) unless the flag is on, in
    which case a :class:`SqliteGateActivationLedger` over a fresh ``state.db``
    connection (short-lived per CLI process), seeded once from the legacy JSONL so
    the CLI diagnostics read the same all-time trace the loop writes. Mirrors
    :func:`_build_deed_ledger_backend` (proposal 106).
    """
    from maestro.audit_checks import JsonlGateActivationLedger

    if config.sqlite_state_enabled:
        return SqliteGateActivationLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlGateActivationLedger(config.memory_dir)


def _build_prediction_backend(config: Config) -> PredictionBackend:
    """Construct the prediction backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``predictions.jsonl``) unless the flag is on, in
    which case a :class:`SqlitePredictionLedger` over a fresh ``state.db``
    connection (short-lived per CLI process), seeded once from the legacy JSONL so
    the CLI diagnostics read the same all-time trace the loop writes. Mirrors
    :func:`_build_gate_activation_backend` (proposal 106).
    """
    from maestro.audit_checks_calibration import (
        JsonlPredictionLedger,
    )

    if config.sqlite_state_enabled:
        return SqlitePredictionLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlPredictionLedger(config.memory_dir)


def _build_premise_backend(config: Config) -> PremiseBackend:
    """Construct the premise backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``premise_observations.jsonl``) unless the flag is
    on, in which case a :class:`SqlitePremiseLedger` over a fresh ``state.db``
    connection (short-lived per CLI process), seeded once from the legacy JSONL
    so ``measure-premise-accuracy`` reads the same all-time trace the loop writes.
    Mirrors :func:`_build_gate_activation_backend` (issue #2304).
    """
    from maestro.audit_checks_calibration import (
        JsonlPremiseLedger,
    )

    if config.sqlite_state_enabled:
        return SqlitePremiseLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlPremiseLedger(config.memory_dir)


def _build_stand_down_verdict_backend(config: Config) -> StandDownVerdictBackend:
    """Construct the stand-down-verdict backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``stand_down_verdicts.jsonl``) unless the flag is on,
    in which case a :class:`SqliteStandDownVerdictLedger` over a fresh ``state.db``
    connection (short-lived per CLI process), seeded once from the legacy JSONL so
    the CLI reads the same chosen-disposition history the loop writes. Mirrors
    :func:`_build_deed_ledger_backend` (proposal 106).
    """
    from maestro.remediation_tracker import JsonlStandDownVerdictLedger

    if config.sqlite_state_enabled:
        return SqliteStandDownVerdictLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlStandDownVerdictLedger(config.memory_dir)


def _build_settled_verdicts_backend(config: Config) -> SettledVerdictsBackend:
    """Construct the settled-verdicts backend per ``[paths] sqlite_state_enabled``.

    The default JSONL store (``settled_verdicts.jsonl``) unless the flag is on, in
    which case a :class:`SqliteSettledVerdictsLedger` over a fresh ``state.db``
    connection (short-lived per CLI process), seeded once from the legacy JSONL so
    the CLI reads the same merged/closed settlement history the loop writes —
    under the flag the JSONL is a frozen legacy seed, so a raw JSONL read would
    undercount merged PRs. Mirrors :func:`_build_prediction_backend` (proposal 106).
    """
    from maestro.audit_checks_settlement import (
        JsonlSettledVerdictsLedger,
    )

    if config.sqlite_state_enabled:
        return SqliteSettledVerdictsLedger(
            StateStore(config.memory_dir / "state.db"),
            legacy_jsonl_dir=config.memory_dir,
        )
    return JsonlSettledVerdictsLedger(config.memory_dir)


def _build_mcp_serve_registry(config: Config) -> dict[str, list[InprocTool]]:
    """Build the introspection registry a ``_mcp-serve`` sibling process hosts.

    Constructs only the collaborators :func:`_build_agent_tools` needs (not the
    full :func:`_build_agent` graph — the launcher serves tool handlers, it
    issues no SDK queries), then returns its ``registry``. The data directory
    and config resolve from ``config`` exactly as a normal ``maestro``
    invocation, so the sibling reads the same state files as the loop process.
    """
    memory = Memory(config.memory_dir)
    consciousness = Consciousness(config.memory_dir)
    journal = Journal(config.memory_dir)
    wt = config.work_target()
    code_host = SubprocessCodeHost(
        wt.github_repo,
        workspace=wt.repo_workspace,
        timeout=config.gh_timeout,
        pr_creation_timeout=config.pr_creation_timeout,
        reviewer=config.github_reviewer,
    )
    issues = IssueTracker(
        wt.github_repo, timeout=config.gh_timeout, code_host=code_host,
    )
    tools = _build_agent_tools(
        journal=journal,
        consciousness=consciousness,
        memory=memory,
        issue_tracker=issues,
        issues_page_size=config.github_issues_page_size,
        log_path=config.app_log_path,
        memory_dir=config.memory_dir,
        conscious_episodes=_build_conscious_episode_backend(config),
    )
    registry: dict[str, list[InprocTool]] = tools["registry"]
    return registry


def _resolve_mcp_serve_server(
    server_name: str, config: Config,
) -> dict[str, Any] | None:
    """Resolve *server_name* to its in-process MCP server config, or ``None``.

    Returns the ``{"type": "sdk", "name", "instance"}`` config
    :func:`maestro.inproc_mcp.create_inproc_mcp_server` produces — the same
    in-process server the Anthropic adapter hosts, so a dispatched call reaches
    the unchanged Python handler. ``None`` signals an unknown logical server,
    which :func:`run_mcp_serve` turns into a loud non-zero exit.
    """
    from maestro.inproc_mcp import create_inproc_mcp_server

    registry = _build_mcp_serve_registry(config)
    tools = registry.get(server_name)
    if tools is None:
        return None
    return create_inproc_mcp_server(name=server_name, tools=tools)


async def _serve_mcp_over_stdio(server_config: dict[str, Any]) -> None:
    """Serve one in-process MCP server over stdio until the client closes EOF."""
    from mcp.server.stdio import stdio_server

    server = server_config["instance"]
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options(),
        )


def run_mcp_serve(server_name: str, config: Config) -> int:
    """Back the hidden ``maestro _mcp-serve <server>`` subcommand (proposal 076).

    Hosts the named logical introspection server's :class:`InprocTool` handlers
    over stdio MCP in this (sibling) Maestro process. An unknown server name
    exits non-zero with a one-line error rather than serving an empty server —
    fail loud, never a silent no-tool surface.
    """
    server_config = _resolve_mcp_serve_server(server_name, config)
    if server_config is None:
        print(
            f"maestro {MCP_SERVE_SUBCOMMAND}: unknown server {server_name!r}",
            file=sys.stderr,
        )
        return 1
    asyncio.run(_serve_mcp_over_stdio(server_config))
    return 0


def _resolve_cli_auth_method(provider: str) -> str | None:
    """Resolve the anthropic auth method once for reuse on startup/preflight.

    Under the default ``anthropic`` provider, ``check_secrets()`` and
    ``auth_label()`` each resolved ``Config.check_auth()`` independently — a
    redundant second env read on the hot startup/preflight path (#357). The CLI
    resolves it once here and threads the result into both, collapsing the pair
    into a single call. Non-anthropic providers return ``None``:
    ``check_auth()`` is anthropic-specific, so those providers let
    ``check_secrets()`` run its own provider credential check first (surfacing
    the correct provider-specific message) and ``auth_label()`` fall through to
    its self-resolving behaviour unchanged — never a spurious anthropic
    SystemExit ahead of the real provider check.
    """
    return Config.check_auth() if provider == "anthropic" else None


def _refuse_own_operator_login(config: Config, code_host: CodeHost) -> None:
    """Refuse a D8 list naming the loop's own login (proposal 155).

    Only a non-empty D8 resolves the login, so a default build makes no call.
    """
    if not config.evolution_fuel_operator_github_logins:
        return
    from maestro.evolution_fuel import operator_login_refusal

    own_login = code_host.resolve_owner()
    if not own_login:
        logger.warning(
            "D8 check: could not resolve the loop's own GitHub login; "
            "a trust caller must pass effective_operator_logins to re-exclude it",
        )
        return
    refusal = operator_login_refusal(config.evolution_fuel_operator_github_logins, own_login)
    if refusal is not None:
        logger.error("D8 check: %s", refusal)
        raise SystemExit(refusal)


def _build_agent(
    config: Config,
    *,
    smoke: bool = False,
    check_publication_scope: bool = False,
    warn_if_null: bool = False,
) -> MaestroAgent:
    """Construct the agent with all dependencies."""
    from maestro.agent import MaestroAgent
    from maestro.migration import ensure_schema

    config.ensure_dirs()
    # A fingerprint mismatch advances the stored fingerprint and lets the
    # loaders normalise on read; no rewrite runs, so there is no partial-
    # migration failure mode to react to (ensure_schema always returns True).
    ensure_schema(
        config.memory_dir, sqlite_state_enabled=config.sqlite_state_enabled,
    )
    # Resolve the auth method once and thread it through both consumers so
    # check_auth() fires exactly once per startup (#357): check_secrets()
    # reuses it for the provider-credential precondition and auth_label(...)
    # for the human label, instead of each re-reading the auth env vars.
    # _resolve_cli_auth_method returns None for a non-anthropic provider so
    # check_secrets()'s accurate provider-specific credential message stays
    # ahead of the anthropic auth fallback, and auth_label(provider=...)
    # renders a provider-neutral label without resolving anthropic creds — a
    # valid non-anthropic deployment must not crash at the label step (#356).
    provider = config.resolved_sdk_provider()
    auth_method = _resolve_cli_auth_method(provider)
    secret_warnings = config.check_secrets(
        publication_enabled=config.publication_enabled,
        provider=provider,
        phase_providers=config.sdk_phase_providers,
        check_publication_scope=check_publication_scope,
        smoke=smoke,
        auth_method=auth_method,
    )
    logger.info("Authentication: %s", Config.auth_label(auth_method, provider=provider))
    for warning in secret_warnings:
        logger.warning("Secret health check: %s", warning)
    # Loop-owning ``Memory``: inject the flag-selected audit-, rejection-history,
    # question-resolution, and version-archive backends so ``audit_self_model``
    # outcomes, ``learn_from_rejection`` rows, ``append_question_resolution``
    # records, and ``record_version`` merged-plan rows route through ``state.db``
    # when ``[paths] sqlite_state_enabled`` is on. Read-only CLI sites that do
    # read a flag-routed ledger (such as ``reality-inventory``,
    # ``measure-rejection-classes``, ``list-versions``,
    # ``consciousness-report``) inject or route through
    # the matching ``_build_*_backend``; the other read-only CLI / ``_mcp-serve``
    # ``Memory`` sites stay on the JSONL default, and the migration
    # ``Memory`` stays JSONL too because it is a JSONL-to-JSONL schema rewrite
    # that never reads ``state.db``, so there is no substrate divergence
    # (proposal 106).
    memory = Memory(
        config.memory_dir,
        audit_history=_build_audit_history_backend(config),
        rejection_history=_build_rejection_history_backend(config),
        question_resolutions=_build_question_resolution_backend(config),
        version_archive=_build_version_archive_backend(config),
    )
    consciousness = Consciousness(
        config.memory_dir,
        self_calibration_enabled=config.audit_self_calibration_enabled,
    )
    # Loop-owning ``Journal``: inject the flag-selected search backend so the
    # contemplation-phase ranker routes through the persistent ``state.db``
    # index under ``[paths] sqlite_state_enabled``, retiring the per-call FTS5
    # rebuild on the one unbounded corpus. The read-only CLI / ``_mcp-serve``
    # ``Journal`` sites stay on the ephemeral default (proposal 106).
    journal = Journal(
        config.memory_dir,
        search_index=_build_journal_search_backend(config),
    )

    # Notifier
    notifier: Notifier = NullNotifier()
    if config.telegram_bot_token and config.telegram_chat_id:
        from maestro.notifications.telegram import TelegramNotifier
        notifier = TelegramNotifier(
            config.telegram_bot_token, config.telegram_chat_id,
            max_message_length=config.telegram_max_length,
        )
    elif warn_if_null:
        # Only the loop pairs this warning with a startup notification, so it
        # is the sole caller that passes warn_if_null=True. Non-loop commands
        # (run/contemplate/evolve/serve) still get a working NullNotifier — the
        # warning is irrelevant noise for them (#204). The separate
        # check_secrets() Telegram audit still surfaces for every command.
        logger.warning(
            "Telegram notifications disabled: "
            "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not set"
        )

    message_store = MessageStore(config.memory_dir)

    email_client = _build_email_client(config)

    # MCP servers — external (from config) + email (if configured)
    mcp_servers = config.mcp_servers_config()
    # Fail fast when an enabled external MCP server's launcher is missing from
    # PATH, before the first query turns the absence into a non-retried mid-loop
    # CLI_CRASH (mirrors check_secrets' SystemExit on a missing credential).
    # Reuse the spec dict already built above rather than rebuilding it.
    launcher_errors = config.check_mcp_launchers(mcp_servers)
    if launcher_errors:
        for err in launcher_errors:
            logger.error("MCP launcher check: %s", err)
        raise SystemExit("\n".join(launcher_errors))
    # Runtime contract (proposal 141): offline; SystemExit propagates when enforced.
    from maestro.runtime_contract import enforce_runtime_contract
    enforce_runtime_contract(
        config,
        enforced=config.runtime_contract_enforced,
        env_name=os.environ.get("MAESTRO_ENV"),
    )
    if email_client is not None:
        from maestro.email_tools import SERVER_KEY, create_email_mcp_server
        if mcp_servers is None:
            mcp_servers = {}
        mcp_servers[SERVER_KEY] = create_email_mcp_server(email_client)

    # Telegram direct tools — only when a real notifier is configured
    if not isinstance(notifier, NullNotifier):
        from maestro.telegram_tools import SERVER_KEY as TG_KEY
        from maestro.telegram_tools import create_telegram_mcp_server
        if mcp_servers is None:
            mcp_servers = {}
        mcp_servers[TG_KEY] = create_telegram_mcp_server(notifier, message_store)

    # VCS and code host clients — shared across consumers. Repo location comes
    # from the WorkTarget owner (proposal 107) rather than the raw self scalars,
    # so a ``[work]`` redirect points these clients at another checkout/repo;
    # both facets default to ``config.workspace`` / ``config.github_repo`` so a
    # default build is behaviourally identical.
    wt = config.work_target()
    vcs = SubprocessVCS(
        wt.repo_workspace,
        timeout=config.git_timeout,
        quick_timeout=config.git_quick_timeout,
        push_timeout=config.implement_timeout,
    )
    code_host = SubprocessCodeHost(
        wt.github_repo,
        workspace=wt.repo_workspace,
        timeout=config.gh_timeout,
        pr_creation_timeout=config.pr_creation_timeout,
        reviewer=config.github_reviewer,
    )

    _refuse_own_operator_login(config, code_host)

    issues = IssueTracker(wt.github_repo, timeout=config.gh_timeout, code_host=code_host)
    cost_samples = _build_cost_sample_backend(config)

    # Build agent introspection tools
    agent_tools = _build_agent_tools(
        journal=journal,
        consciousness=consciousness,
        memory=memory,
        issue_tracker=issues,
        issues_page_size=config.github_issues_page_size,
        log_path=config.app_log_path,
        memory_dir=config.memory_dir,
        conscious_episodes=_build_loop_conscious_episode_backend(config),
    )
    executor_long_context_model = (
        config.sdk_model_long_context
        if (
            config.long_context_implementation_enabled
            and config.sdk_model_long_context
        )
        else None
    )
    executor = Executor(
        notifier=notifier,
        message_store=message_store,
        mcp_servers=mcp_servers,
        agent_tools=agent_tools,
        loop_detect_repeats=config.executor_loop_detect_repeats,
        tool_stream_idle_timeout_seconds=(
            config.sdk_tool_stream_idle_timeout_seconds
        ),
        empty_response_retries=config.executor_empty_response_retries,
        long_context_model=executor_long_context_model,
        disallowed_tools=config.external_mcp_disallowed_tools(),
        gate_strip_redirections=config.gate_strip_redirections_enabled,
    )
    planner = Planner()
    evolution = EvolutionEngine(
        memory, consciousness, journal, notifier,
        issues=issues,
        github_repo=wt.github_repo,
        config=config,
        vcs=vcs,
        code_host=code_host,
        mcp_servers=mcp_servers,
        message_store=message_store,
        pr_ledger_backend=_build_pr_ledger_backend(config),
        gate_activation_backend=_build_gate_activation_backend(config),
        cost_sample_backend=cost_samples,
    )

    contributions = ContributionTracker(
        config.workspace, config.memory_dir, git_timeout=config.git_quick_timeout,
    )

    return MaestroAgent(
        config=config,
        consciousness=consciousness,
        journal=journal,
        memory=memory,
        executor=executor,
        planner=planner,
        evolution=evolution,
        notifier=notifier,
        contributions=contributions,
        message_store=message_store,
        vcs=vcs,
        email_client=email_client,
        issues=issues,
        agent_tools=agent_tools,
    )


#: Third-party HTTP client loggers held at WARNING. ``httpx`` logs every request
#: URL at INFO, and a Telegram Bot API URL carries the bot token in its path, so
#: leaving them at the root level would write the token into the app log.
_QUIET_HTTP_LOGGERS: tuple[str, ...] = ("httpx", "httpcore")


def quiet_http_client_loggers() -> None:
    """Raise the HTTP client loggers to WARNING so request URLs never reach a log."""
    for name in _QUIET_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


# Subcommands that reach the provider, so ``main()`` gives them the SDK model
# pin, cost tracking, concurrency cap and file logging before dispatch.
# ``workspace-counterfactual`` joins the loop-running commands because it is the
# one read-only subcommand that reaches the provider (proposal 130): without
# this membership its trials would run with no model pin and no cost tracking,
# so they would neither use the operator's configured model nor be metered like
# every other call. ``resume-task`` belongs here because it re-enters
# ``agent.run()``. Every ``_build_agent`` caller must be listed
# (``tests/test_cli.py::test_every_build_agent_handler_gets_sdk_configuration``).
_SDK_RUNNING_COMMANDS: frozenset[str] = frozenset({
    "run", "resume-task", "contemplate", "evolve", "loop", "serve",
    "workspace-counterfactual",
})


def cmd_run(args: argparse.Namespace, config: Config) -> None:
    agent = _build_agent(config)
    record = asyncio.run(agent.run(args.goal, context=args.context or ""))
    print(f"Outcome: {record.outcome}")
    print(f"Duration: {record.duration_secs}s")
    if record.errors:
        print(f"Errors: {record.errors}")
    print(f"Summary: {(record.result_summary or '')[:300]}")


def cmd_resume_task(args: argparse.Namespace, config: Config) -> None:
    agent = _build_agent(config)
    try:
        record = asyncio.run(agent.resume_task(args.task_id))
    except ValueError as exc:
        print(f"Cannot resume task {args.task_id}: {exc}")
        sys.exit(1)
    print(f"Outcome: {record.outcome}")
    print(f"Duration: {record.duration_secs}s")
    if record.errors:
        print(f"Errors: {record.errors}")
    print(f"Summary: {(record.result_summary or '')[:300]}")


def cmd_contemplate(args: argparse.Namespace, config: Config) -> None:
    agent = _build_agent(config)
    reflection = asyncio.run(agent.contemplate())
    print("--- Maestro's Reflection ---")
    print(reflection)


def cmd_evolve(args: argparse.Namespace, config: Config) -> None:
    agent = _build_agent(config)
    asyncio.run(agent.evolve())
    print("Evolution cycle complete. Check dashboard for pending plan.")


def cmd_status(args: argparse.Namespace, config: Config) -> None:
    config.ensure_dirs()
    consciousness = Consciousness(config.memory_dir)
    model = consciousness.load_self_model()
    memory = Memory(config.memory_dir)

    try:
        print(f"Auth: {config.auth_label()}")
    except SystemExit:
        print("Auth: not configured")
    print(f"Evolutions: {model.evolution_count}")
    print(f"Alive since: {model.awakened_at}")
    if model.identity_statements:
        print("\nIdentity:")
        for s in model.identity_statements:
            print(f"  - {s}")
    if model.values:
        print("\nValues:")
        for v in model.values:
            print(f"  - {v}")
    if model.open_questions:
        print("\nOpen questions:")
        for q in model.open_questions:
            print(f"  ? {q}")

    rate = memory.get_success_rate()
    pending = memory.get_pending_plans()
    print(f"\nSuccess rate: {rate:.0%}")
    print(f"Pending plans: {len(pending)}")


def cmd_show_plan(args: argparse.Namespace, config: Config) -> None:
    """Print one evolution plan's full detail by id (read-only).

    Operator inspection surface over a *single* :class:`EvolutionPlan`: loads
    all plans via :meth:`Memory.get_all_plans` and renders the requested plan's
    status, rationale, proposed_changes, and acceptance_criteria as a
    human-readable block — or a ``json.dumps`` object under ``--json`` for
    scripting. An unknown id prints a not-found message and exits nonzero (no
    traceback), mirroring ``cost-per-task``. Pure read; no state write, no SDK
    cost.
    """
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    plan = next(
        (p for p in memory.get_all_plans() if p.plan_id == args.plan_id),
        None,
    )
    if plan is None:
        print(f"No evolution plan with id {args.plan_id}")
        sys.exit(1)

    if args.json:
        print(json_mod.dumps(
            {
                "plan_id": plan.plan_id,
                "title": plan.title,
                "status": plan.status,
                "rationale": plan.rationale,
                "proposed_changes": plan.proposed_changes,
                "acceptance_criteria": plan.acceptance_criteria,
                "files_affected": plan.files_affected,
            },
            indent=2,
        ))
        return

    print(f"Plan {plan.plan_id}")
    print("=" * 40)
    print(f"title: {plan.title}")
    print(f"status: {plan.status}")
    print(f"rationale: {plan.rationale}")
    print("proposed_changes:")
    if plan.proposed_changes:
        for change in plan.proposed_changes:
            print(f"  - {change}")
    else:
        print("  (none)")
    print("acceptance_criteria:")
    if plan.acceptance_criteria:
        for criterion in plan.acceptance_criteria:
            print(f"  - {criterion}")
    else:
        print("  (none)")


def cmd_journal(args: argparse.Namespace, config: Config) -> None:
    config.ensure_dirs()
    journal = Journal(config.memory_dir)
    count = args.count if args.count is not None else config.journal_default_count

    if count <= 0:
        entries = []
    elif args.type:
        from typing import get_args

        from maestro.types import JournalEntryType
        valid_types = set(get_args(JournalEntryType))
        if args.type not in valid_types:
            print(f"Invalid entry type '{args.type}'. Must be one of: {', '.join(sorted(valid_types))}")
            return
        entries = (
            journal.read_events_by_type(args.type)
            if args.events
            else journal.read_by_type(args.type)
        )[-count:]
    elif args.events:
        entries = journal.read_recent_events(count)
    else:
        entries = journal.read_recent(count)

    if not entries:
        if count <= 0:
            return
        print("No journal events." if args.events else "Journal is empty.")
        return

    for e in entries:
        ts = e.timestamp.split("T")[0]
        print(f"[{ts} | {e.entry_type}]")
        print(e.content)
        print()


def _format_duration(secs: float) -> str:
    """Format duration as a human-readable string (Xs, Xm, Xh).

    Rounds before comparing to tier thresholds so values near a boundary
    display in the correct unit (e.g. 59.9s → '1.0m', not '60s').
    """
    rounded_secs = round(secs)
    if rounded_secs < 60:
        return f"{rounded_secs}s"
    minutes = secs / 60
    rounded_minutes = round(minutes, 1)
    if rounded_minutes < 60.0:
        return f"{rounded_minutes:.1f}m"
    hours = secs / 3600
    rounded_hours = round(hours, 1)
    return f"{rounded_hours:.1f}h"


def cmd_history(args: argparse.Namespace, config: Config) -> None:
    config.ensure_dirs()
    memory = Memory(config.memory_dir)

    count = args.count if args.count is not None else 10
    tasks = memory.get_recent_tasks(n=count if count > 0 else 0)

    # Post-filter by outcome
    if args.outcome:
        tasks = [t for t in tasks if t.outcome == args.outcome]

    # Post-filter by source
    if args.source:
        tasks = [t for t in tasks if t.source == args.source]

    if args.json:
        print(json_mod.dumps([dataclasses.asdict(t) for t in tasks], indent=2))
        return

    if not tasks:
        print("No task history.")
        return

    for t in tasks:
        ts = t.timestamp.split("T")[0] if t.timestamp else "unknown"
        goal = t.goal[:80] + ("…" if len(t.goal) > 80 else "")
        duration = _format_duration(t.duration_secs)
        print(f"[{ts} | {t.outcome} | {duration} | {t.source}]")
        print(f"  {goal}")
        if t.linked_question:
            lq = t.linked_question[:60] + ("…" if len(t.linked_question) > 60 else "")
            print(f"  ? {lq}")
        print()


def cmd_measure_noops(args: argparse.Namespace, config: Config) -> None:
    """Measure no-op task rate from ``task_history.jsonl``."""
    from maestro.audit_checks import (
        _SUCCESS_OUTCOMES,
        classify_noop_task,
        measure_noop_rate,
    )

    config.ensure_dirs()
    memory = Memory(config.memory_dir)

    last = args.last
    tasks = memory.get_recent_tasks(n=last if last and last > 0 else 10_000)

    task_dicts: list[dict[str, object]] = [
        dataclasses.asdict(t) for t in tasks
    ]
    result = measure_noop_rate(task_dicts)

    total_success: int = result["total_success"]  # type: ignore[assignment]
    noop_count: int = result["noop_count"]  # type: ignore[assignment]
    noop_rate: float = result["noop_rate"]  # type: ignore[assignment]
    by_source: dict[str, dict[str, object]] = result["by_source"]  # type: ignore[assignment]

    # Overall summary
    print(f"No-op Report ({len(task_dicts)} tasks examined)")
    print("=" * 40)
    print(f"Successful tasks: {total_success}")
    print(f"No-op tasks:      {noop_count}")
    print(f"No-op rate:       {noop_rate:.1%}")

    # Per-source breakdown
    if by_source:
        print()
        print("By source:")
        for source, stats in sorted(by_source.items()):
            src_total = stats["total"]
            src_noop = stats["noop"]
            src_rate: float = stats["rate"]  # type: ignore[assignment]
            print(f"  {source}: {src_noop}/{src_total} ({src_rate:.1%})")

    # List no-op task IDs
    noop_ids: list[str] = []
    for td in task_dicts:
        outcome = td.get("outcome")
        if not isinstance(outcome, str) or outcome not in _SUCCESS_OUTCOMES:
            continue
        if classify_noop_task(td):
            tid = td.get("task_id")
            if isinstance(tid, str) and tid:
                noop_ids.append(tid)

    if noop_ids:
        print()
        print("No-op task IDs:")
        for tid in noop_ids:
            print(f"  - {tid}")


def cmd_measure_gate_triggers(args: argparse.Namespace, config: Config) -> None:
    """Measure gate activation counts from the flag-selected ledger.

    Reads through :func:`_build_gate_activation_backend`, so under ``[paths]
    sqlite_state_enabled`` the counts come from activations recorded to
    ``state.db``, not the frozen ``gate_activations.jsonl`` rollback sidecar
    (which stops being written once the flag is on).
    """
    from maestro.audit_checks import measure_gate_triggers

    config.ensure_dirs()
    last = args.last
    n = last if last and last > 0 else 100

    counts = measure_gate_triggers(_build_gate_activation_backend(config), n=n)

    if not counts:
        print(f"No gate activations recorded (last {n} records).")
        return

    print(f"Gate Activation Report (last {n} records)")
    print("=" * 40)
    for gate, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {gate}: {count}")


def cmd_measure_premise_accuracy(
    args: argparse.Namespace, config: Config,
) -> None:
    """Report premise accuracy from the flag-selected premise ledger (#2304).

    Reads through :func:`_build_premise_backend`: under ``[paths]
    sqlite_state_enabled`` the observations come from ``state.db``, else from
    the ``premise_observations.jsonl`` sidecar.

    The leading indicator's read surface: what fraction of accepted tasks that
    referenced a ``.py`` path referenced one that actually exists on disk, plus
    the recent premise-miss task IDs. Advisory — nothing is injected or dropped.
    """
    from maestro.audit_checks_calibration import (
        measure_premise_accuracy,
    )

    config.ensure_dirs()
    last = args.last
    n = last if last and last > 0 else 100

    report = measure_premise_accuracy(_build_premise_backend(config), n=n)

    if report.total_measured == 0:
        print(f"No premise observations to measure (last {n} records).")
        return

    print(f"Premise Accuracy Report (last {n} records)")
    print("=" * 40)
    print(f"Measured tasks:   {report.total_measured}")
    print(f"Premise hits:     {report.hits}")
    print(f"Premise accuracy: {report.accuracy:.1%}")

    if report.recent_misses:
        print()
        print("Recent premise-miss task IDs:")
        for tid in report.recent_misses:
            print(f"  - {tid}")


def _measure_claim_grounding(self_model: SelfModel) -> dict[str, int]:
    """Count how each live ``capabilities_known`` claim's cited evidence resolves.

    Pure, table-testable read over an already-loaded *self_model* — the same
    fold :func:`~maestro.audit_checks.check_unbound_capability_claim` performs:
    each string capability is keyed through :func:`~maestro.consciousness.claim_key`
    and the persisted ``status`` in ``SelfModel.claim_evidence`` is read back
    (never re-resolved — existence and polarity only, never entailment). Every
    claim lands in exactly one bucket, so ``resolved + wrong_polarity +
    unresolvable + absent == total`` always: a claim with a ``"resolved"`` /
    ``"wrong_polarity"`` / ``"unresolvable"`` status counts there, and a claim
    whose citation names a non-existent anchor (``"absent"``), carries an
    unrecognised status, or was never cited at all folds into ``absent`` — the
    "no resolving evidence" bucket. With ``[audit] evidence_bound_claims_enabled``
    off the map is empty, so every claim reads ``absent``, honestly reflecting
    that no claim is evidence-bound yet.
    """
    counts = {
        "total": 0,
        "resolved": 0,
        "wrong_polarity": 0,
        "unresolvable": 0,
        "absent": 0,
    }
    for capability in self_model.capabilities_known:
        if not isinstance(capability, str):
            continue
        counts["total"] += 1
        record = self_model.claim_evidence.get(claim_key(capability))
        status = record.get("status", "") if isinstance(record, dict) else ""
        if status in ("resolved", "wrong_polarity", "unresolvable"):
            counts[status] += 1
        else:
            # An "absent" status, an unrecognised status, or no citation at all:
            # the claim carries no resolving evidence.
            counts["absent"] += 1
    return counts


def _consciousness_reading(
    criterion: ConsciousnessCriterion,
    grounding: dict[str, int],
    calibration: SelfCalibrationReading,
    conversion: dict[str, object],
    limited_access: LimitedAccessReading,
    receipted_broadcast: ReceiptedBroadcastReading,
    recurrence: RecurrenceReading,
    schema_agreement: SchemaAgreementReading,
    temporal_continuity: TemporalContinuityReading,
    causal_reach: CausalReachReading,
) -> tuple[str, dict[str, object] | None]:
    """Bind one criterion to its ``(status, reading)`` pair — the report's core map.

    Pure and deterministic given the already-computed measured inputs. ``status``
    is one of ``available`` (the structural identity-availability constant),
    ``not_implemented`` (a deferred indicator, which never approximates the reading
    from another row — no registry entry is deferred today, but the branch stays
    for a future one), ``no_data`` (the criterion's ledger is empty), or
    ``measured``. ``reading`` is a structured object for the ``--json`` payload, or
    ``None`` for a ``no_data`` / ``not_implemented`` row. The two workspace
    readings carry their independent :data:`~maestro.consciousness_eval.WorkspaceVerdict`.
    """
    if not criterion.implemented:
        return "not_implemented", None
    if criterion.key == "identity_availability":
        # Structural constant: the self-model reaches every model call via
        # Consciousness.get_identity_prompt (injected as the system slab).
        return "available", {"available": True}
    if criterion.key == "limited_capacity_selection":
        if limited_access.episodes == 0:
            return "no_data", None
        return "measured", {
            "verdict": limited_access.verdict,
            "episodes": limited_access.episodes,
            "competition_episodes": limited_access.competition_episodes,
            "admitted": limited_access.admitted,
            "excluded": limited_access.excluded,
            "violations": limited_access.violations,
        }
    if criterion.key == "receipted_broadcast":
        if receipted_broadcast.episodes == 0:
            return "no_data", None
        return "measured", {
            "verdict": receipted_broadcast.verdict,
            "episodes": receipted_broadcast.episodes,
            "receipted_episodes": receipted_broadcast.receipted_episodes,
            "receipts": receipted_broadcast.receipts,
            "consumers_covered": receipted_broadcast.consumers_covered,
            "violations": receipted_broadcast.violations,
        }
    if criterion.key == "recurrence":
        if recurrence.episodes == 0:
            return "no_data", None
        return "measured", {
            "verdict": recurrence.verdict,
            "episodes": recurrence.episodes,
            "recurrence_episodes": recurrence.recurrence_episodes,
            "lineage_items": recurrence.lineage_items,
            "violations": recurrence.violations,
        }
    if criterion.key == "attention_schema_agreement":
        if schema_agreement.episodes == 0:
            return "no_data", None
        return "measured", {
            "verdict": schema_agreement.verdict,
            "episodes": schema_agreement.episodes,
            "agreeing_episodes": schema_agreement.agreeing_episodes,
            "violations": schema_agreement.violations,
        }
    if criterion.key == "temporal_continuity":
        if temporal_continuity.episodes == 0:
            return "no_data", None
        return "measured", {
            "verdict": temporal_continuity.verdict,
            "episodes": temporal_continuity.episodes,
            "linked_episodes": temporal_continuity.linked_episodes,
            "gap_episodes": temporal_continuity.gap_episodes,
            "genesis_episodes": temporal_continuity.genesis_episodes,
            "violations": temporal_continuity.violations,
        }
    if criterion.key == "causal_reach":
        if causal_reach.episodes == 0:
            return "no_data", None
        return "measured", {
            "verdict": causal_reach.verdict,
            "episodes": causal_reach.episodes,
            "reaching_episodes": causal_reach.reaching_episodes,
            "cited_tasks": causal_reach.cited_tasks,
            "cited_plans": causal_reach.cited_plans,
            "violations": causal_reach.violations,
        }
    if criterion.key == "perceptual_grounding":
        total = grounding["total"]
        if total == 0:
            return "no_data", None
        return "measured", {
            "total_claims": total,
            "resolved": grounding["resolved"],
            "wrong_polarity": grounding["wrong_polarity"],
            "unresolvable": grounding["unresolvable"],
            "absent": grounding["absent"],
            "resolved_share": grounding["resolved"] / total,
        }
    if criterion.key == "metacognitive_calibration":
        if calibration.stamped_total == 0:
            return "no_data", None
        return "measured", {
            "stamped_total": calibration.stamped_total,
            "resolved": calibration.resolved,
            "hit_rate": calibration.hit_rate,
            "agreement_rate": calibration.agreement_rate,
            "brier_score": calibration.brier_score,
            "narrated": calibration.narrated,
            "pending": calibration.pending,
            "unresolved": calibration.unresolved,
        }
    # narration_actuation_conversion is the only remaining implemented key; any
    # other implemented key means CONSCIOUSNESS_CRITERIA grew a row without a
    # reading here — fail loud rather than silently rendering it as narration
    # data. The membership test pins the registry, so this guard is unreachable.
    if criterion.key != "narration_actuation_conversion":  # pragma: no cover
        raise ValueError(f"unhandled consciousness criterion key: {criterion.key!r}")
    if conversion["detections"] == 0:
        return "no_data", None
    return "measured", {
        "detections": conversion["detections"],
        "actuations": conversion["actuations"],
        "dropped": conversion["dropped"],
        "settled": conversion["settled"],
    }


def _format_consciousness_reading(key: str, reading: dict[str, object]) -> str:
    """Render one criterion's measured *reading* as a single human line.

    Called only for a row whose reading is present (``available`` or
    ``measured``); ``no_data`` / ``not_implemented`` rows are rendered by the
    handler without a reading. The share/rate/Brier values are floats placed by
    :func:`_consciousness_reading`, so the ``%``/``f`` format specs are safe.
    """
    if key == "identity_availability":
        return "available — self-model injected into every model call"
    if key == "limited_capacity_selection":
        return (
            f"{reading['verdict']}: {reading['competition_episodes']}/"
            f"{reading['episodes']} episodes with competition; "
            f"{reading['admitted']} admitted / {reading['excluded']} excluded; "
            f"{reading['violations']} cap/hash violations"
        )
    if key == "receipted_broadcast":
        return (
            f"{reading['verdict']}: {reading['receipted_episodes']}/"
            f"{reading['episodes']} episodes broadcast; {reading['receipts']} "
            f"receipts over {reading['consumers_covered']} authorized consumers; "
            f"{reading['violations']} integrity violations"
        )
    if key == "recurrence":
        return (
            f"{reading['verdict']}: {reading['recurrence_episodes']}/"
            f"{reading['episodes']} episodes with recurrence; "
            f"{reading['lineage_items']} lineage-bearing items; "
            f"{reading['violations']} provenance violations"
        )
    if key == "attention_schema_agreement":
        return (
            f"{reading['verdict']}: {reading['agreeing_episodes']}/"
            f"{reading['episodes']} episodes agree with the selection trace; "
            f"{reading['violations']} schema violations"
        )
    if key == "temporal_continuity":
        return (
            f"{reading['verdict']}: {reading['linked_episodes']} linked / "
            f"{reading['gap_episodes']} gap / {reading['genesis_episodes']} "
            f"genesis of {reading['episodes']} episodes; "
            f"{reading['violations']} continuity violations"
        )
    if key == "causal_reach":
        return (
            f"{reading['verdict']}: {reading['reaching_episodes']}/"
            f"{reading['episodes']} episodes reached a decision; "
            f"{reading['cited_tasks']} tasks / {reading['cited_plans']} plans "
            f"cited; {reading['violations']} unbacked-citation violations"
        )
    if key == "perceptual_grounding":
        return (
            f"evidence-bound (resolved) share: {reading['resolved']}/"
            f"{reading['total_claims']} ({reading['resolved_share']:.1%}); "
            f"wrong-polarity: {reading['wrong_polarity']}"
        )
    if key == "metacognitive_calibration":
        return (
            f"stamped bets: {reading['stamped_total']}; hit-rate "
            f"{reading['hit_rate']:.1%}; self-vs-measured agreement "
            f"{reading['agreement_rate']:.1%}; Brier {reading['brier_score']:.3f}; "
            f"narrated {reading['narrated']}, pending {reading['pending']}, "
            f"unresolved {reading['unresolved']}"
        )
    # narration_actuation_conversion is the only remaining measured key; any
    # other key means the registry grew a reading this renderer does not handle —
    # fail loud. The membership test pins the registry, so this is unreachable.
    if key != "narration_actuation_conversion":  # pragma: no cover
        raise ValueError(f"unhandled consciousness criterion key: {key!r}")
    return (
        f"{reading['detections']} detections -> {reading['actuations']} "
        f"actuations ({reading['dropped']} dropped) -> {reading['settled']} settled"
    )


def cmd_consciousness_report(args: argparse.Namespace, config: Config) -> None:
    """Render ``consciousness-report`` — one measured reading per functional
    property of consciousness (read-only, offline).

    Reads every criterion in :data:`~maestro.self_calibration.CONSCIOUSNESS_CRITERIA`
    back against the local ledgers Maestro already writes and states, per property,
    where it actually stands: *identity availability* (the structural constant that
    the self-model reaches every model call), *perceptual grounding*
    (``capabilities_known`` claims folded through their persisted
    ``claim_evidence`` status — existence and polarity, never entailment),
    *metacognitive calibration* (the priced-reflection track record via
    :func:`~maestro.audit_checks_calibration.measure_self_calibration`), and
    *narration→actuation conversion* (via
    :func:`~maestro.audit_checks_calibration.measure_narration_conversion`). The five
    global-workspace indicators — limited-capacity moment selection, receipted
    broadcast, recurrence, attention-schema agreement, and temporal continuity —
    render measured readings folded from the conscious-episode ledger by
    :mod:`maestro.consciousness_eval` (``no data`` when that ledger is empty),
    rather than letting the identity slab stand in for them.

    The header states it measures functional (engineerable) properties only and
    makes **no phenomenal claim**. The command reaches no SDK, push, or merge
    seam: the settlement inputs the conversion row needs come from the local
    settled-verdict (merged-PR set) and version-archive (archived commit SHAs)
    ledgers via offline oracles — read through
    :func:`_build_settled_verdicts_backend` / :func:`_build_version_archive_backend`,
    so they come from ``state.db`` under ``[paths] sqlite_state_enabled`` and from
    the ``settled_verdicts.jsonl`` / ``version_archive.jsonl`` sidecars otherwise —
    never a live ``git`` / ``gh`` merge check, and it constructs no code host at
    all. Pure read: nothing
    is written or injected. ``--json`` emits a structured object; the default
    render is human text with a scope label per row and an explicit ``no data``
    when a ledger is empty.
    """
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
        measure_self_calibration,
    )

    config.ensure_dirs()

    # Perceptual grounding — capability claims vs. their persisted evidence.
    model = Consciousness(config.memory_dir).load_self_model()
    grounding = _measure_claim_grounding(model)

    # Metacognitive calibration — the priced-reflection track record.
    calibration = measure_self_calibration(_build_prediction_backend(config))

    # Narration→actuation conversion — settlement read from LOCAL ledgers only.
    # Both oracles read on-disk ledgers, never a git/gh merge check, so the
    # command reaches no push/merge seam. Compute the merged-PR set and the
    # archived-SHA list once, then close over them (the SHA oracle is a prefix
    # match against archived commit SHAs, mirroring resolve_claim_citation).
    merged_prs = _build_settled_verdicts_backend(config).merged_settled_prs()
    archived_shas = [
        record.commit_sha
        for record in _build_version_archive_backend(config).load_all()
        if record.commit_sha
    ]

    def _is_sha_landed(sha: str) -> bool:
        prefix = sha.lower()
        return any(
            archived.lower().startswith(prefix) for archived in archived_shas
        )

    conversion = measure_narration_conversion(
        _build_gate_activation_backend(config),
        is_pr_merged=lambda pr: pr in merged_prs,
        is_sha_landed=_is_sha_landed,
    )

    # Global-workspace indicators — folded offline from the conscious-episode
    # ledger (JSONL default, SQLite behind [paths] sqlite_state_enabled). Pure
    # structural reads: no prose, no host, no gate.
    episodes = _build_conscious_episode_backend(config).read_all()
    limited_access = evaluate_limited_access(episodes)
    receipted_broadcast = evaluate_receipted_broadcast(episodes)
    recurrence = evaluate_recurrence(episodes)
    schema_agreement = evaluate_schema_agreement(episodes)
    temporal_continuity = evaluate_temporal_continuity(episodes)
    causal_reach = evaluate_causal_reach(episodes)

    computed: list[tuple[ConsciousnessCriterion, str, dict[str, object] | None]] = [
        (criterion, *_consciousness_reading(
            criterion, grounding, calibration, conversion,
            limited_access, receipted_broadcast,
            recurrence, schema_agreement, temporal_continuity, causal_reach,
        ))
        for criterion in CONSCIOUSNESS_CRITERIA
    ]

    disclaimer = (
        "Measures functional (engineerable) properties only; makes no phenomenal "
        "(subjective-experience) claim."
    )

    if args.json:
        print(json_mod.dumps({
            "disclaimer": disclaimer,
            "criteria": [
                {
                    "key": criterion.key,
                    "label": criterion.label,
                    "measures": criterion.measures,
                    "implemented": criterion.implemented,
                    "status": status,
                    "reading": reading,
                }
                for criterion, status, reading in computed
            ],
        }, indent=2))
        return

    print("Consciousness — Operational Awareness Report")
    print("=" * 44)
    print(disclaimer)
    print()
    for criterion, status, reading in computed:
        print(f"[{status}] {criterion.label}")
        print(f"    measures: {criterion.measures}")
        if reading is None:
            detail = (
                "not_implemented (deferred global-workspace indicator)"
                if status == "not_implemented"
                else "no data"
            )
            print(f"    {detail}")
        else:
            print(f"    {_format_consciousness_reading(criterion.key, reading)}")
        print()


def cmd_workspace_counterfactual(
    args: argparse.Namespace, config: Config,
) -> None:
    """Run the interventional-sensitivity probe over the conscious-access
    workspace (proposal 130).

    For each scenario in :data:`~maestro.counterfactual_probe.COUNTERFACTUAL_SCENARIOS`
    this issues a matched pair of tool-less model calls — one over a rendered
    :class:`~maestro.conscious_workspace.ConsciousMoment` that admits the
    decisive item, one over the same moment with exactly that item ablated — and
    reports the raw decision class each side returned plus whether the ablated
    condition falsely cited an item it never saw. The label is *interventional
    sensitivity*: whether workspace content moves a decision. It is **not** a
    phenomenal claim, and it does not lift the causal-reach indicator's
    ``architectural`` cap — folding these results into ``consciousness-report`` is
    separate, deliberately later work, so that command's offline no-SDK guarantee
    stays untouched.

    This is the one CLI subcommand that reaches the provider, and it does so only
    through the session-attached seam (``create_session`` / ``query_on_session``
    / ``disconnect_session``) with ``tools=[]``. Credentials are resolved the
    agent-free way ``cmd_preflight`` does — deliberately **not** via
    ``_build_agent``, which constructs the code host, VCS and executor seams this
    command must not have. It mutates no state: no episode or decision ledger is
    written, no decision it elicits is executed, and it always exits ``0`` — a
    negative result is the finding, not a CLI failure. Its provider calls are
    metered like every other one, so the app log, the SDK transcript sidecar and
    the cost-telemetry row are written; an unmetered billed call would be the
    worse outcome.

    ``--repeats`` is clamped to ``[1, MAX_REPEATS]``: each repeat bills two
    provider calls per scenario, so an unbounded operator value is a cost hazard.
    A clamp is announced on stderr, keeping ``--json``'s stdout parseable.
    """
    from maestro.counterfactual_probe import (
        COUNTERFACTUAL_SCENARIOS,
        MAX_REPEATS,
        render_report,
        report_payload,
        run_pack,
    )

    scenarios = COUNTERFACTUAL_SCENARIOS
    if args.scenario:
        scenarios = tuple(
            s for s in COUNTERFACTUAL_SCENARIOS if s.scenario_id == args.scenario
        )
        if not scenarios:
            known = ", ".join(s.scenario_id for s in COUNTERFACTUAL_SCENARIOS)
            print(f"Unknown scenario {args.scenario!r}. Known scenarios: {known}")
            return

    # Credential precondition, agent-free (the cmd_preflight shape): resolve the
    # auth method once, then let check_secrets() raise SystemExit on a missing
    # credential rather than failing inside the first provider call.
    provider = config.resolved_sdk_provider()
    secret_warnings = config.check_secrets(
        publication_enabled=config.publication_enabled,
        provider=provider,
        phase_providers=config.sdk_phase_providers,
        auth_method=_resolve_cli_auth_method(provider),
    )
    # Surfaced the way cmd_preflight surfaces them — on stderr so --json's
    # stdout stays parseable. Dropping them would hide a real misconfiguration
    # behind a command that is about to spend the operator's money.
    for warning in secret_warnings:
        print(f"warning: {warning}", file=sys.stderr)

    repeats = min(MAX_REPEATS, max(1, args.repeats))
    if repeats != args.repeats:
        print(
            f"--repeats clamped to {repeats} (allowed 1-{MAX_REPEATS}; each "
            f"repeat bills two provider calls per scenario)",
            file=sys.stderr,
        )

    report = asyncio.run(run_pack(
        now=datetime.now(UTC).isoformat(),
        repeats=repeats,
        scenarios=scenarios,
    ))

    if args.json:
        print(json_mod.dumps(report_payload(report), indent=2))
        return
    print(render_report(report))


def cmd_measure_dead_on_arrival_mints(
    args: argparse.Namespace, config: Config,
) -> None:
    """Report dead-on-arrival plan mints (proposal 120).

    How many recently-created evolution plans were obsoleted at the submission
    door — the generation-admission gate or the backlog submit-block — after the
    priced ``propose_plan`` call already ran, broken down by
    mint channel — plus every door refusal split by reason (saturation /
    grounding / restatement, proposal 136; value, proposal 144; flag-graduation).
    The report prints whenever any door refusal falls in the window. Read-only over ``evolution_plans.jsonl``; nothing is written or
    injected. ``--last`` bounds the most-recent window (default: all plans).
    """
    from maestro.audit_checks import measure_dead_on_arrival_mints

    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    last = args.last
    n = last if last and last > 0 else 0

    report = measure_dead_on_arrival_mints(memory.get_all_plans(), n=n)

    if args.json:
        print(json_mod.dumps(report, indent=2))
        return

    total: int = report["total"]  # type: ignore[assignment]
    dead_on_arrival: int = report["dead_on_arrival"]  # type: ignore[assignment]
    fraction: float = report["doa_fraction"]  # type: ignore[assignment]
    by_channel: dict[str, int] = report["by_channel"]  # type: ignore[assignment]
    by_reason: dict[str, int] = report["by_reason"]  # type: ignore[assignment]
    content_dead: int = report["content_dead"]  # type: ignore[assignment]
    content_fraction: float = report["content_fraction"]  # type: ignore[assignment]

    if not by_reason:
        print(f"No dead-on-arrival plan mints (window total: {total}).")
        return

    print("Dead-on-Arrival Plan Mints")
    print("=" * 40)
    print("Dead on arrival = obsoleted at the backlog-saturation / admission gate.")
    print(f"Window plans:     {total}")
    print(f"Dead on arrival:  {dead_on_arrival}")
    print(f"DOA fraction:     {fraction:.1%}")
    print()
    if by_channel:
        print("By mint channel:")
        for channel, count in sorted(by_channel.items()):
            print(f"  {channel}: {count}")
        print()
    print("By refusal reason (every door refusal):")
    for reason, count in sorted(by_reason.items()):
        print(f"  {reason}: {count}")
    print(f"Content deaths (grounding + restatement): {content_dead} "
          f"({content_fraction:.1%})")


def cmd_measure_rejection_classes(
    args: argparse.Namespace, config: Config,
) -> None:
    """Report human rejections in the grounding-failure classes (proposal 136).

    Counts recent rejections whose reason reads already-shipped, false-premise,
    or not-needed — a coarse lexical proxy over ``rejection_history`` read
    through the backend ``[paths] sqlite_state_enabled`` selects. Read-only;
    ``--last`` bounds the most-recent window (default: all rejections).
    """
    from maestro.audit_checks import measure_rejection_classes

    config.ensure_dirs()
    last = args.last
    n = last if last and last > 0 else 0
    # Read the whole oldest-first ledger through the selected backend and let
    # the measure window it — ``Memory.get_recent_rejections`` maps n <= 0 to
    # an empty list, not "all".
    records = _build_rejection_history_backend(config).load_all()

    report = measure_rejection_classes(records, n=n)

    if args.json:
        print(json_mod.dumps(report, indent=2))
        return

    total: int = report["total"]  # type: ignore[assignment]
    if total == 0:
        print("No rejections recorded.")
        return
    by_class: dict[str, int] = report["by_class"]  # type: ignore[assignment]
    any_class: int = report["any_grounding_class"]  # type: ignore[assignment]
    fraction: float = report["any_grounding_fraction"]  # type: ignore[assignment]

    print("Rejection Classes")
    print("=" * 40)
    print("Coarse lexical proxy; classes overlap (rejections are compound).")
    print(f"Window rejections: {total}")
    for name, count in by_class.items():
        print(f"  {name}: {count}")
    print(f"Any grounding class: {any_class} ({fraction:.1%})")
    print(f"Tree-checkable: {report['tree_checkable']}")


def _iso_date_arg(value: str) -> str:
    """argparse ``type`` for ``YYYY-MM-DD``; a bad date is a usage error."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r} (expected YYYY-MM-DD)",
        ) from exc


def cmd_measure_planning_grounding(
    args: argparse.Namespace, config: Config,
) -> None:
    """Report the tree-checkable share of human rejections (proposal 143).

    How many decided plans a human rejected for a reason a single read of the
    tree would have settled, plus the planner's read coverage over
    ``grounded_reads``. Read-only over ``evolution_plans.jsonl``; nothing is
    written. ``--since`` bounds the window by ``created_at``.
    """
    from maestro.audit_checks import (
        PLANNING_REFUSAL_TAGS,
        measure_planning_grounding,
    )

    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    report = measure_planning_grounding(memory.get_all_plans(), since=args.since)

    if args.json:
        print(json_mod.dumps(report, indent=2))
        return

    decided: int = report["decided"]  # type: ignore[assignment]
    rejected: int = report["rejected"]  # type: ignore[assignment]
    tree_checkable: int = report["tree_checkable"]  # type: ignore[assignment]
    fraction: float = report["tree_checkable_fraction"]  # type: ignore[assignment]
    coverage = report["read_coverage"]

    print("Planning Grounding")
    print("=" * 40)
    if args.since:
        print(f"Since:                 {args.since}")
    print(f"Decided plans:         {decided}")
    print(f"Rejected:              {rejected}")
    print(f"Tree-checkable:        {tree_checkable} ({fraction:.1%} of rejected)")
    print(f"Door refusals:         {report['door_refusals']}")
    by_tag: dict[str, int] = report["refusals_by_tag"]  # type: ignore[assignment]
    door_tags = [t for t in PLANNING_REFUSAL_TAGS if t.startswith("ungrounded:")]
    other_tags = [t for t in PLANNING_REFUSAL_TAGS if t not in door_tags]
    for tag in door_tags:
        if by_tag[tag]:
            print(f"  {tag + ':':<21}{by_tag[tag]}")
    if any(by_tag[tag] for tag in other_tags):
        print("Other grounding refusals:")
        for tag in other_tags:
            if by_tag[tag]:
                print(f"  {tag + ':':<21}{by_tag[tag]}")
    print(f"Plans with reads:      {report['with_reads']}")
    print(f"No reads recorded:     {report['no_reads_recorded']}")
    if isinstance(coverage, float):
        print(f"Read coverage:         {coverage:.1%}")
    else:
        print("Read coverage:         no reads recorded")


def cmd_runtime_contract(args: argparse.Namespace, config: Config) -> int:
    """Check the live environment against the runtime contract (proposal 141).

    Loads ``deploy/runtime_contract.toml``, observes the environment through the
    existing readers, and prints one verdict per entry. Read-only, and offline
    unless ``--network``, which reaches only the public package registries to
    resolve required launcher packages; never a credential, SDK, or host seam.
    Returns ``1`` when any required entry is violated, else ``0``.
    """
    from dataclasses import asdict

    from maestro.runtime_contract import (
        ContractError,
        evaluate,
        load_contract,
        observe,
        requirement_context,
    )

    try:
        contract = load_contract()
    except ContractError as exc:
        print(f"runtime-contract: {exc}", file=sys.stderr)
        return 1
    context = requirement_context(config, os.environ.get("MAESTRO_ENV"))
    verdict = evaluate(
        contract,
        context,
        observe(contract, config, network=args.network, context=context),
    )
    if args.json:
        print(json_mod.dumps(
            {"ok": verdict.ok, **asdict(verdict)}, indent=2,
        ))
    else:
        print("Runtime Contract")
        print("=" * 40)
        for finding in verdict.findings:
            print(
                f"  {finding.entry.kind:<16} {finding.entry.name:<36} "
                f"{finding.status:<12} {finding.detail}".rstrip()
            )
        print()
        print("OK" if verdict.ok else "VIOLATED: required entries are missing")
    return 0 if verdict.ok else 1


def cmd_source_budget(args: argparse.Namespace, config: Config) -> None:
    """Report source files at or above the split-due line (proposal 147).

    Worst band first, with each file's bytes to :data:`SPLIT_BUDGET` (negative
    once over it) and a git-derived growth rate when one is available, scanned
    under the ``[work]`` gate path atoms; ``--json`` rows also carry each
    file's ``largest_definitions`` (the AST profile the split-due directive
    names). Ignores
    ``[audit] split_due_sensor_enabled`` on purpose: a pull-only read is always
    available. Read-only and offline: writes nothing, reaches no SDK seam.
    """
    from dataclasses import asdict

    from maestro.source_budget import (
        HARD_CEILING,
        SPLIT_BUDGET,
        SPLIT_DUE,
        bytes_to_budget,
        estimate_growth_per_day,
        largest_definitions,
        read_source_budget,
        scan_target,
        split_due_readings,
    )

    root, dirs = scan_target(config)
    readings = split_due_readings(read_source_budget(root, dirs))
    growth = {r.relative: estimate_growth_per_day(root, r.relative) for r in readings}

    if args.json:
        print(json_mod.dumps({
            "split_due": SPLIT_DUE,
            "split_budget": SPLIT_BUDGET,
            "hard_ceiling": HARD_CEILING,
            "files": [
                {
                    **asdict(r),
                    "bytes_to_budget": bytes_to_budget(r.size),
                    "growth_per_day": growth[r.relative],
                    "largest_definitions": [
                        asdict(d) for d in largest_definitions(root, r.relative)
                    ],
                }
                for r in readings
            ],
        }, indent=2))
        return

    print("Source Budget")
    print("=" * 40)
    if not readings:
        print(f"No file at or above the split-due line ({SPLIT_DUE // 1024} KiB).")
        return
    for r in readings:
        rate = growth[r.relative]
        growth_text = "growth unknown" if rate is None else f"{rate:+.0f} B/day"
        print(
            f"  {r.relative}  {r.size // 1024} KiB  {r.band.value}  "
            f"to budget: {bytes_to_budget(r.size)}  {growth_text}",
        )


def cmd_reality_inventory(args: argparse.Namespace, config: Config) -> None:
    """Report the reality inventory (proposal 136).

    The shipped capabilities (per kind) and the measured problems the plan
    generator will be grounded against, composed by
    :func:`~maestro.reality_inventory.load_reality_inventory` from existing
    registries and ledgers. Read-only and offline: writes nothing, reaches no
    SDK, push, or merge seam. Audit and rejection history are read through
    :func:`_build_audit_history_backend` / :func:`_build_rejection_history_backend`,
    so with ``[paths] sqlite_state_enabled`` on it reports the ``state.db`` rows
    the loop's generator sees rather than the frozen JSONL sidecars.
    """
    from dataclasses import asdict

    from maestro.reality_inventory import load_reality_inventory

    config.ensure_dirs()
    memory = Memory(
        config.memory_dir,
        audit_history=_build_audit_history_backend(config),
        rejection_history=_build_rejection_history_backend(config),
    )
    inventory = load_reality_inventory(config, memory)

    if args.json:
        print(json_mod.dumps({
            "shipped_capabilities": sorted(inventory.shipped_capabilities),
            "measured_problems": [asdict(p) for p in inventory.measured_problems],
        }, indent=2))
        return

    kinds: dict[str, int] = {}
    for capability in inventory.shipped_capabilities:
        kind = capability.split(":", 1)[0]
        kinds[kind] = kinds.get(kind, 0) + 1
    print("Reality Inventory")
    print("=" * 40)
    print("Shipped capabilities:")
    for kind, count in sorted(kinds.items()):
        print(f"  {kind}: {count}")
    print()
    if not inventory.measured_problems:
        print("No measured problems.")
        return
    print("Measured problems:")
    for problem in inventory.measured_problems:
        print(f"  {problem.id} (weight {problem.weight}): {problem.summary}")


def cmd_tool_surface_audit(
    args: argparse.Namespace, config: Config,
) -> None:
    """Report per-caller tool-surface drift over SDK transcript sidecars.

    Proposal 142: what each caller declares vs. what the CLI advertised and what
    was executed off the declared surface (refusals counted separately).
    Offline and read-only over ``config.sdk_log_dir``; reaches no SDK and writes
    nothing. ``--last`` bounds the N newest date dirs (default: all retained).
    """
    from maestro.logs import iter_transcript_records
    from maestro.tool_surface import measure_tool_surface_drift, render_report

    last = args.last
    max_dates = last if last and last > 0 else None
    report = measure_tool_surface_drift(
        (
            record for _, record in iter_transcript_records(
                config.sdk_log_dir, max_dates,
            )
        ),
        external_servers=frozenset(config.mcp_servers_config() or {}),
    )
    if args.json:
        print(json_mod.dumps(report.report_payload(), indent=2))
        return
    print(render_report(report))


def cmd_measure_gate_contention(
    args: argparse.Namespace, config: Config,
) -> None:
    """Report gate-run contention figures from the lease telemetry (proposal 126).

    Joins the ``gate_runs.jsonl`` sidecar into p50/p95 wall time bucketed by the
    number of other gate runs observed in flight, plus the resource-artifact rate
    (contention-manufactured reds), the fallback-firing rate (host saturation) —
    both over the *admitted* runs — and the declined rate over the whole window:
    how often the cached runnability verdict refused a gate run before it spent a
    subprocess (proposal 133), zero whenever that oracle is off. Together they are
    the figures that make the execution lease falsifiable. A second table reads
    *fit* per granted width (proposal 134): the p50 ``tests_seconds`` the suite
    measurably cost at that width, the p50 budget those runs were granted, their
    ratio, and how many runs were killed at the budget — so whether the derived
    budget fits the suite at the width the host grants is one command rather than
    an inference. Read-only over the ledger; nothing is written or injected.
    ``--last`` bounds the most-recent window (default: 100 rows).
    """
    from maestro.gate_capacity import (
        measure_gate_contention,
        read_recent_gate_runs,
    )

    config.ensure_dirs()
    last = args.last
    n = last if last and last > 0 else 100

    records = read_recent_gate_runs(config.gate_runs_path, n)
    report = measure_gate_contention(records)

    if args.json:
        print(json_mod.dumps(report, indent=2))
        return

    total: int = report["total"]  # type: ignore[assignment]
    if total == 0:
        print("No gate runs recorded.")
        return

    buckets: list[dict[str, object]] = report["buckets"]  # type: ignore[assignment]
    artifact_rate: float = report["resource_artifact_rate"]  # type: ignore[assignment]
    fallback_rate: float = report["fallback_rate"]  # type: ignore[assignment]
    declined_rate: float = report["declined_rate"]  # type: ignore[assignment]

    print("Gate-Run Contention")
    print("=" * 40)
    print(f"Window runs: {total}")
    print()
    print("Wall time by observed concurrency (in_flight):")
    print(
        f"  {'in_flight':>10}  {'count':>6}  {'p50 (s)':>10}  {'p95 (s)':>10}  "
        f"{'p50 wait (s)':>12}",
    )
    for bucket in buckets:
        in_flight: int = bucket["in_flight"]  # type: ignore[assignment]
        count: int = bucket["count"]  # type: ignore[assignment]
        p50: float = bucket["p50_seconds"]  # type: ignore[assignment]
        p95: float = bucket["p95_seconds"]  # type: ignore[assignment]
        p50_wait: float = bucket["p50_wait_seconds"]  # type: ignore[assignment]
        print(
            f"  {in_flight:>10}  {count:>6}  {p50:>10.2f}  {p95:>10.2f}  "
            f"{p50_wait:>12.2f}",
        )
    print()
    width_buckets: list[dict[str, object]] = (
        report["width_buckets"]  # type: ignore[assignment]
    )
    print("Fit by granted width (in-process runs):")
    print(
        f"  {'workers':>7}  {'count':>6}  {'p50 tests (s)':>13}  "
        f"{'budget (s)':>10}  {'fit':>6}  {'censored':>8}",
    )
    for width_bucket in width_buckets:
        workers: int = width_bucket["workers"]  # type: ignore[assignment]
        w_count: int = width_bucket["count"]  # type: ignore[assignment]
        p50_tests: float = (
            width_bucket["p50_tests_seconds"]  # type: ignore[assignment]
        )
        p50_budget: float = (
            width_bucket["p50_deadline_seconds"]  # type: ignore[assignment]
        )
        fit: float | None = width_bucket["fit"]  # type: ignore[assignment]
        censored: int = width_bucket["censored"]  # type: ignore[assignment]
        fit_cell = f"{fit:.2f}" if fit is not None else "-"
        print(
            f"  {workers:>7}  {w_count:>6}  {p50_tests:>13.2f}  "
            f"{p50_budget:>10.2f}  {fit_cell:>6}  {censored:>8}",
        )
    print()
    print(f"Resource-artifact rate: {artifact_rate:.1%}")
    print(f"Fallback-firing rate:   {fallback_rate:.1%}")
    print(f"Declined rate:          {declined_rate:.1%}")


def cmd_audit_flags(args: argparse.Namespace, config: Config) -> None:
    """Audit the experimental- and cut-over-flag manifests against activations.

    Two advisory read surfaces in one command. The experimental section joins
    ``EXPERIMENTAL_FLAGS`` — each a dated opt-in ``Config`` flag — against the
    recent gate-activation fire-rate, read through
    :func:`_build_gate_activation_backend` (``state.db`` under ``[paths]
    sqlite_state_enabled``, else the ``gate_activations.jsonl`` sidecar): the review horizon, an ``OVERDUE``
    marker once that horizon is crossed, and how often the gated behaviour has
    actually fired (``-`` for a flag whose behaviour emits no gate activation). The
    Cut-Over Burn-In section joins the ``cutover``-stage ``CUTOVER_FLAGS`` entries
    against the same counts: each flag's burn-in age, its ``review_by`` floor with a
    ``[PAST FLOOR]`` marker once crossed, its measured off-path divergence count
    (``-`` when unmeasured), and a ``recommend collapse_eligible`` suggestion when
    the flag is past its floor with a zero-divergence signal. ``--json`` emits an
    object ``{"experimental_flags": ..., "cutover_flags": ...}`` so both reports are
    reachable via the read surface. Pure read — no state write; both manifests are
    advisory and nothing is auto-deleted or collapsed; every collapse stays a human
    decision.
    """
    from maestro.audit_checks import measure_gate_triggers
    from maestro.flag_lifecycle import (
        CUTOVER_FLAGS,
        EXPERIMENTAL_FLAGS,
        audit_flags_report,
        cutover_burn_in_report,
    )

    config.ensure_dirs()
    last = args.last
    n = last if last and last > 0 else 100
    counts = measure_gate_triggers(_build_gate_activation_backend(config), n=n)
    today = date.today()
    rows = audit_flags_report(EXPERIMENTAL_FLAGS, counts, today)
    cutover_rows = cutover_burn_in_report(CUTOVER_FLAGS, counts, today)

    if args.json:
        print(
            json_mod.dumps(
                {"experimental_flags": rows, "cutover_flags": cutover_rows},
                indent=2,
            ),
        )
        return

    print(f"Experimental-Flag Audit (activations over last {n} records)")
    print("=" * 50)
    for row in rows:
        marker = " [OVERDUE]" if row["overdue"] else ""
        activations = row["activations"]
        fires = "-" if activations is None else str(activations)
        print(
            f"  {row['flag']}  (review_by {row['review_by']}, "
            f"activations {fires}){marker}",
        )

    print()
    print(f"Cut-Over Burn-In (divergences over last {n} records)")
    print("=" * 50)
    for row in cutover_rows:
        floor = " [PAST FLOOR]" if row["past_floor"] else ""
        divergences = row["divergences"]
        diverged = "-" if divergences is None else str(divergences)
        recommend = (
            " -> recommend collapse_eligible"
            if row["recommend_collapse_eligible"]
            else ""
        )
        print(
            f"  {row['flag']}  (age {row['age_days']}d, "
            f"review_by {row['review_by']}, divergences {diverged})"
            f"{floor}{recommend}",
        )


def cmd_measure_fork_authorship(
    args: argparse.Namespace, config: Config,
) -> None:
    """Measure fork-authorship breakdown from the flag-selected prediction ledger.

    Reads through :func:`_build_prediction_backend`: under ``[paths]
    sqlite_state_enabled`` the rows come from ``state.db``, else from the
    ``predictions.jsonl`` sidecar.

    Counts only reflection self-prediction rows (``source == "reflection"``) —
    the rows that carry a ``loss_condition`` and so are real *forks*. Verdict
    scoring rows and task-outcome prediction rows are excluded; otherwise they
    have no loss condition and inflate the ``unknown`` breakdown. Each fork's
    loss condition is classified as self-authored, externally-grounded, or
    unknown (see :mod:`maestro.fork_authorship`) and the breakdown is printed.
    A high externally-grounded ratio is the live kind — forks I could have lost.
    """
    from maestro.audit_checks_calibration import (
        REFLECTION_PREDICTION_SOURCE,
    )
    from maestro.fork_authorship import (
        format_fork_authorship_summary,
        measure_fork_authorship,
    )

    config.ensure_dirs()
    # Only reflection self-predictions are forks: they carry a loss_condition.
    # Reflection-verdict scoring rows and task-outcome prediction rows have no
    # loss condition and would otherwise inflate the 'unknown' breakdown. Read the
    # flag-selected backend so the JSONL / ``state.db`` trace is read the same way
    # the loop writes it.
    rows = [
        row
        for row in _build_prediction_backend(config).read_all()
        if row.get("source") == REFLECTION_PREDICTION_SOURCE
    ]
    stats = measure_fork_authorship(rows)
    print(format_fork_authorship_summary(stats))


def cmd_list_gates(args: argparse.Namespace, config: Config) -> None:
    """Print the ``generate_task`` pre-call gate chain in precedence order.

    Reads :func:`maestro.task_gates.gate_order` — the single source of
    the gate taxonomy (proposal 069); blocking gates short-circuit the
    SDK call, directive bridges fill the empty ``next_task_directive``
    slot first-writer-wins.
    """
    from maestro.task_gates import content_gate_order, gate_order

    print("Task-generation gate chain (precedence order)")
    print("=" * 46)
    for position, (name, kind) in enumerate(gate_order(), start=1):
        print(f"  {position:2d}. {name:<30} {kind}")
    print()
    print(
        "Post-call content gates (applied by _vet_goal_content at the enqueue "
        f"and edit sites): {', '.join(content_gate_order())}."
    )


def cmd_lexicon(args: argparse.Namespace, config: Config) -> None:
    """Print Maestro's coined-term lexicon (``docs/lexicon.md``) or a view of it.

    A read-only surface over the checked-in definitional source of truth
    (proposal 101). Four mutually-exclusive views, all exiting ``0``:

    - default / ``--list`` — resolve the doc relative to :attr:`Config.workspace`
      (the ``config.workspace / "docs" / ...`` pattern), parse it via
      :func:`maestro.lexicon.load_lexicon`, and print one aligned line per coined
      term. A missing or table-less file prints an empty-lexicon notice.
    - ``--undefined`` — list the high-frequency journal coinages the lexicon does
      not yet define (:func:`_lexicon_print_undefined`).
    - ``--lint`` — report ``CLAUDE.md`` variant spellings the lexicon canonicalises
      (:func:`_lexicon_print_lint`).
    - ``--tag-map`` — render the behavioral-sensor tag map from the lexicon's
      sensor-tag entries, paste-ready for ``docs/self-evolution.md``
      (:func:`_lexicon_print_tag_map`).

    No flag keeps today's table print byte-identical, so existing bare-``Namespace``
    callers are unaffected (the ``getattr`` flag reads default to the table view).
    """
    from maestro.lexicon import load_lexicon

    if getattr(args, "undefined", False):
        _lexicon_print_undefined(config)
        return
    if getattr(args, "lint", False):
        _lexicon_print_lint(config)
        return
    if getattr(args, "tag_map", False):
        _lexicon_print_tag_map(config)
        return

    path = config.workspace / "docs" / "lexicon.md"
    # ``load_lexicon`` reads the file directly, so a missing doc would raise
    # ``FileNotFoundError``; guard here to honour the "missing or table-less file
    # prints an empty-lexicon notice" contract.
    entries = load_lexicon(path) if path.exists() else ()
    _lexicon_print_table(entries)


def _lexicon_print_table(entries: tuple[LexiconEntry, ...]) -> None:
    """Print the aligned one-line-per-entry lexicon table (the default view)."""
    print("Coined-term lexicon (docs/lexicon.md)")
    print("=" * 37)
    if not entries:
        print("  (no lexicon entries found)")
        return
    for entry in entries:
        variants = ", ".join(entry.variants) if entry.variants else "-"
        print(
            f"  {entry.term:<20} {entry.definition} "
            f"[names: {entry.names} | anchor: {entry.anchor} "
            f"| variants: {variants} | minted: {entry.minted}]"
        )


def _lexicon_print_undefined(config: Config) -> None:
    """Print the high-frequency journal coinages absent from ``docs/lexicon.md``.

    Reuses proposal 101's zero-SDK undefined-coinage sensor over the recent
    journal tail: subtracts every defined spelling, then lists what clears the
    frequency floor highest-frequency-first (ties broken alphabetically, matching
    :func:`maestro.audit_checks.format_undefined_coinage_summary`). A missing
    lexicon guards to an empty defined set (like the table view); a missing
    journal reads as an empty tail; an empty result prints a notice.
    """
    from maestro.audit_checks import (
        _UNDEFINED_COINAGE_JOURNAL_WINDOW,
        measure_undefined_coinages,
    )
    from maestro.lexicon import all_spellings, load_lexicon

    path = config.workspace / "docs" / "lexicon.md"
    defined = all_spellings(load_lexicon(path)) if path.exists() else frozenset()
    texts = [
        entry.content
        for entry in Journal(config.memory_dir).read_recent(
            _UNDEFINED_COINAGE_JOURNAL_WINDOW,
        )
    ]
    counts = measure_undefined_coinages(texts, lexicon_terms=defined)

    header = "Undefined journal coinages (absent from docs/lexicon.md)"
    print(header)
    print("=" * len(header))
    if not counts:
        print("  (no undefined coinages above the frequency floor)")
        return
    for term, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {term:<28} {count}")


def _lexicon_print_lint(config: Config) -> None:
    """Report ``CLAUDE.md`` variant spellings the lexicon canonicalises.

    Delegates the scan to :func:`~maestro.lexicon.find_canonical_drift` — the
    single definition of "what counts as canonical-spelling drift," shared with
    the coverage golden test so the CLI and the test can never disagree. That
    helper's match is case-insensitive, word-boundary anchored, and hyphen-guarded
    (so a variant that is a prefix of its canonical or a hyphen-adjacent segment of
    a longer coinage never false-positives). A missing lexicon or ``CLAUDE.md``
    guards to a clean report.
    """
    from maestro.lexicon import find_canonical_drift, load_lexicon

    lex_path = config.workspace / "docs" / "lexicon.md"
    entries = load_lexicon(lex_path) if lex_path.exists() else ()
    claude_md = config.workspace / "CLAUDE.md"
    text = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""

    header = "CLAUDE.md canonical-spelling drift (docs/lexicon.md variants)"
    print(header)
    print("=" * len(header))

    drift = find_canonical_drift(text, entries)
    if not drift:
        print("  (no drift — CLAUDE.md uses only canonical spellings)")
        return
    for variant, canonical, count in sorted(drift):
        print(f"  {variant} → {canonical} ({count})")


def _lexicon_print_tag_map(config: Config) -> None:
    """Print the behavioral-sensor tag map rendered from ``docs/lexicon.md``.

    Loads the lexicon (guarding a missing file to an empty tuple, like the table
    view) and prints :func:`~maestro.lexicon.render_sensor_tag_map` — the
    ``| Tag | Sensor |`` block byte-identical to the committed table in
    ``docs/self-evolution.md``. This is the operator's paste-ready regeneration
    surface: the ``test_self_evolution_tag_map_matches_lexicon_render`` golden
    test keeps the doc and this render in lock-step, so regenerating the doc means
    pasting this output over its table block.
    """
    from maestro.lexicon import load_lexicon, render_sensor_tag_map

    path = config.workspace / "docs" / "lexicon.md"
    entries = load_lexicon(path) if path.exists() else ()
    print(render_sensor_tag_map(entries))


def cmd_gate_check(args: argparse.Namespace, config: Config) -> None:
    """Run ruff+mypy+pytest and exit with the aggregate quality-gate code.

    This is the owned, loseable verdict: a real exit code on Maestro's own
    code with no human gate. ``run_gate_check`` streams each gate's output
    live on *stderr*, then this handler prints the per-gate breakdown and
    exits the process with the aggregate (``0`` iff every gate passed). With
    ``--json`` it prints the per-gate codes and aggregate as machine-readable
    JSON instead of the human breakdown; because gate diagnostics go to
    stderr, stdout carries only that single JSON line, so ``maestro gate-check
    --json | jq`` parses cleanly. The aggregate exit code is unchanged.

    With ``--full`` the run also covers the two standalone sibling packages
    (``maestro_review/`` and ``maestro_sdk/``), mirroring CI's
    ``package-quality`` job, so a local green cannot pass while a sibling
    package is red. Both surfaces below iterate ``exit_codes``, so the
    breakdown and the JSON payload widen with no further branch.

    Under ``[loop] gate_environment_oracle_enabled`` (proposal 133) the run is
    preceded by one gate-runnability probe: the verdict line prints on *stderr*
    beside the gate diagnostics, its kind joins the ``--json`` payload as a
    single ``runnability`` key, and the refreshed row is persisted where the
    loop's own consumers read it. Flag off, the command is byte-identical to
    today — no probe, no line, no row.
    """
    from maestro.gate_check import (
        format_gate_check_json,
        refresh_runnability_verdict,
        run_gate_check,
    )

    # Proposal 133 step 9: the command that is already about to spend a full
    # suite run first asks — and *answers* — whether the local gate can reach a
    # verdict at all, then writes the row the loop's own consumers read next, so
    # a repair the operator has just proved releases the lease seam's refusals
    # without waiting for the loop's next probe. The line goes to stderr so
    # stdout stays the single parseable ``--json`` line. Flag off, no probe is
    # issued, nothing is printed and no row is written.
    runnability = None
    if config.gate_environment_oracle_enabled:
        runnability = refresh_runnability_verdict(
            config.work_target(),
            verdict_path=config.gate_environment_path,
            runs_path=config.gate_runs_path,
            selfcheck=config.selfcheck_timeout,
            selfcheck_max=config.selfcheck_max_timeout,
            budget_lease_owned=config.gate_budget_lease_owned_enabled,
            cgroup_reserve=config.gate_lease_cgroup_reserve_enabled,
        )
        print(gate_environment.format_verdict(runnability), file=sys.stderr)

    result = run_gate_check(
        config.work_target(),
        lease_slot_dir=config.gate_lease_dir,
        runs_path=config.gate_runs_path,
        cgroup_reserve=config.gate_lease_cgroup_reserve_enabled,
        admission_cap=config.gate_lease_admission_cap_enabled,
        full=getattr(args, "full", False),
    )

    if getattr(args, "json", False):
        print(format_gate_check_json(result, runnability=runnability))
    else:
        print()
        print("Quality-gate verdict (maestro gate-check)")
        print("=" * 41)
        for gate, code in result.exit_codes.items():
            status = "PASS" if code == 0 else f"FAIL (exit {code})"
            print(f"  {gate:<22} {status}")
        if result.failing_gates:
            print(f"  failing: {', '.join(result.failing_gates)}")
        print(f"  aggregate exit: {result.aggregate}")

    sys.exit(result.aggregate)


def cmd_gate_scope(args: argparse.Namespace, config: Config) -> None:
    """Render the test modules a set of changed source files can reach (proposal 134).

    A pure read over the tests tree: :func:`maestro.gate_scope.impacted_tests`
    unions the naming-convention half (a changed ``maestro/red_root.py`` selects
    ``tests/test_red_root.py`` and its ``_``-bounded prefix siblings) with the
    import-graph half (every test module whose ``ast`` imports reach the changed
    module), and this handler prints the selection, its size, the total number of
    test modules, and the selected fraction — so how much of the suite a narrowed
    gate round *would* run is one command rather than an inference. Both the
    selection and the fraction's denominator count only collectable ``test_``
    modules (:func:`maestro.gate_scope.count_test_modules`), never a
    ``conftest.py`` or a ``tests/support/`` helper. The import half matches a
    *direct* import only, so the selection is a floor, not a transitive closure.

    Changed paths come either from repeatable positional arguments or
    from ``--plan <plan_id>``, which reads that plan's ``files_affected`` through
    :meth:`maestro.memory.Memory.get_plan`. Both gate path atoms are resolved
    from :meth:`Config.work_target`, never hardcoded. An empty selection is the
    documented "full" sentinel — do not narrow. Nothing is run and nothing is
    written: no gate executes, no state file is touched.
    """
    from maestro.gate_scope import (
        count_test_modules,
        read_test_sources,
        select_impacted_tests,
    )
    from maestro.memory import Memory

    config.ensure_dirs()
    target = config.work_target()

    changed: list[str] = list(args.path or [])
    plan_id: str | None = getattr(args, "plan", None)
    if plan_id:
        plan = Memory(config.memory_dir).get_plan(plan_id)
        if plan is None:
            print(f"Plan not found: {plan_id}")
            return
        changed.extend(plan.files_affected)

    test_sources = read_test_sources(target.repo_workspace, target.gate_tests_path)
    selected = select_impacted_tests(
        changed,
        test_sources,
        source_path=target.gate_source_path,
        tests_path=target.gate_tests_path,
    )
    total = count_test_modules(test_sources, tests_path=target.gate_tests_path)
    report: dict[str, object] = {
        "changed_paths": changed,
        "selected": list(selected),
        "selected_count": len(selected),
        "total_test_modules": total,
        "selected_fraction": (len(selected) / total) if total else 0.0,
        "scope": "impacted" if selected else "full",
    }

    if args.json:
        print(json_mod.dumps(report, indent=2))
        return

    print("Gate Scope")
    print("=" * 40)
    print(f"Changed paths: {len(changed)}")
    if not selected:
        print("Selection: none — scope 'full' (do not narrow).")
        return
    for rel in selected:
        print(f"  {rel}")
    print()
    fraction: float = report["selected_fraction"]  # type: ignore[assignment]
    print(f"Selected: {len(selected)} of {total} test modules ({fraction:.1%})")
    print("Scope: impacted")


def cmd_gate_classify(args: argparse.Namespace, config: Config) -> None:
    """Classify one captured gate run into a single closed-vocabulary token.

    Reads the captured run's combined output from *stdin* and prints exactly one
    ``GateObservationKind`` token — ``CLEAN`` / ``FAILED`` / ``COLLECTION_ERROR``
    / ``COVERAGE`` / ``TIMEOUT`` / ``UNAVAILABLE`` — to stdout. With ``--kind`` the
    run is a *non-answer* (a runner that timed out or could not start), classified
    by :func:`gate_observation.parse_non_answer` over whatever partial output it
    managed to write; otherwise :func:`gate_observation.parse_gate_output` derives
    the token from ``--tool`` and ``--returncode``. The classification is the
    *output*, never the exit status: this always exits ``0`` so a caller captures
    the token from stdout without it also colouring the shell's ``$?``. Reads no
    config.
    """
    from maestro import gate_observation

    stdin_text = sys.stdin.read()
    if args.kind is not None:
        observation = gate_observation.parse_non_answer(
            args.tool, args.kind, stdin_text, "",
        )
    else:
        observation = gate_observation.parse_gate_output(
            args.tool, stdin_text, "", args.returncode,
        )
    print(observation.kind)
    sys.exit(0)


def _forward_gate_stream(
    source: IO[str], sink: IO[str], saw_timeout: threading.Event,
) -> None:
    """Copy *source* to *sink* line by line, flagging a pytest-timeout kill.

    Each line is written through unchanged and flushed, so the child's output
    reaches the operator as it is produced and nothing beyond the current line is
    buffered. *saw_timeout* is set when a line is pytest-timeout's kill evidence
    (:func:`gate_observation.is_pytest_timeout_line`).

    A sink that stops accepting writes (``BrokenPipeError`` once the consumer
    closes, or any other ``OSError``) is dropped, but *source* is still drained to
    EOF: otherwise the child would block on a full pipe and never exit, holding
    its gate slot indefinitely.
    """
    from maestro import gate_observation

    sink_open = True
    for line in source:
        if sink_open:
            try:
                sink.write(line)
                sink.flush()
            except OSError:
                sink_open = False
        if gate_observation.is_pytest_timeout_line(line):
            saw_timeout.set()


def _run_gate_child(
    command: list[str], worker_atoms: tuple[str, ...],
) -> tuple[int, bool]:
    """Splice the ``{worker_args}`` token across *command* and run it.

    Every standalone ``{worker_args}`` token in the argv is replaced with the
    *worker_atoms* the lease owner produced — ``("-n", str(N), "--dist",
    "loadfile")`` for an N≥2 grant, ``("-n", "auto", "--dist", "loadfile")`` on
    the flag-off rollback, and ``()`` for a ≤1-worker grant, where the token is
    dropped entirely so the child runs a plain serial suite (no ``-n``). Splicing
    the width atoms at the owner rather than substituting a scalar count keeps the
    ``-n``/``--dist loadfile`` pairing enforced in one place. *command* is
    guaranteed non-empty (the caller rejects an empty child before ever reaching
    here). Returns a shell-conventional exit status so the "exits with the child's
    return code" promise holds once the caller passes it to :func:`sys.exit`: the
    child's own code for a normal exit, ``128 + N`` for death by signal ``N``
    (``subprocess`` reports that as ``-N``, which ``sys.exit`` would otherwise wrap
    to ``256 - N``), and ``127`` when the child binary is not on ``PATH`` (rather
    than surfacing a raw ``FileNotFoundError`` traceback).

    Both child streams are piped and forwarded unchanged to the parent's matching
    stream by one reader thread each (:func:`_forward_gate_stream`), which also
    watches for pytest-timeout's kill banner; the second element of the returned
    pair says whether one was seen, so :func:`_gate_child_outcome` can label a
    suite pytest-timeout killed as ``TIMEOUT`` rather than an ordinary red.
    Piping means the child no longer sees a TTY (no colour, default width), and
    its stdout/stderr interleaving under ``2>&1`` is only line-granular.
    """
    resolved: list[str] = []
    for token in command:
        if token == "{worker_args}":
            resolved.extend(worker_atoms)
        else:
            resolved.append(token)
    try:
        child = subprocess.Popen(
            resolved,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        print(f"gate-lease: command not found: {resolved[0]}", file=sys.stderr)
        return 127, False
    saw_timeout = threading.Event()
    assert child.stdout is not None and child.stderr is not None
    readers = [
        threading.Thread(
            target=_forward_gate_stream, args=(stream, sink, saw_timeout),
            daemon=True,
        )
        for stream, sink in ((child.stdout, sys.stdout), (child.stderr, sys.stderr))
    ]
    for reader in readers:
        reader.start()
    returncode = child.wait()
    for reader in readers:
        reader.join()
    if returncode < 0:
        # subprocess reports death by signal N as -N; the shell convention (and
        # what sys.exit can faithfully surface) is 128 + N.
        return 128 - returncode, saw_timeout.is_set()
    return returncode, saw_timeout.is_set()


def _gate_child_outcome(
    returncode: int, saw_test_timeout: bool = False,
) -> Literal["CLEAN", "UNAVAILABLE", "TIMEOUT", "UNKNOWN"]:
    """Map a :func:`_run_gate_child` exit status to a gate-run telemetry outcome.

    A subset of :data:`~maestro.gate_capacity.GateRunOutcome`, spelled as its own
    ``Literal`` so this mapping needs no import of that alias. Mirrors
    ``scripts/quality-gates.sh``'s ``classify_gate_run`` so the wrapper and the
    shell runner name the wrapper's synthetic statuses identically: ``0`` is
    ``CLEAN``; ``127`` (missing binary) is ``UNAVAILABLE`` — the gate could not be
    executed at all; a status above ``128`` (``128 + N`` signal death) is
    ``TIMEOUT`` — a killed runner; every other non-zero code is an ordinary red
    the wrapper does not further classify, so ``UNKNOWN`` — unless
    *saw_test_timeout* says pytest-timeout killed the suite, which is ``TIMEOUT``.
    Exit ``0`` stays ``CLEAN`` whatever the output contained.
    """
    if returncode == 0:
        return "CLEAN"
    if saw_test_timeout:
        return "TIMEOUT"
    if returncode == 127:
        return "UNAVAILABLE"
    if returncode > 128:
        return "TIMEOUT"
    return "UNKNOWN"


def cmd_gate_lease(args: argparse.Namespace, config: Config) -> None:
    """Run a gate command under a host-execution lease, or probe the flag.

    ``--enabled`` is the flag's reader: it exits ``0`` when
    ``[loop] gate_lease_enabled`` is on and non-zero when it is off (taking no slot
    either way), so a shell caller can gate its own routing on the flag without
    parsing config.

    Otherwise *command* (everything after ``--``) is run as a child with its
    ``{worker_args}`` placeholder token spliced for the granted worker width's
    argv atoms, taken from :func:`gate_capacity.worker_args` (the sole owner of
    the ``-n``/``--dist loadfile`` pairing):

    * Flag **off** — the rollback path — splices today's ``-n auto --dist
      loadfile`` width, takes no slot, and runs the child directly, so the host
      behaves exactly as it does now.
    * Flag **on** admits the run through the shared
      :func:`gate_capacity.lease_for_gate_run` seam, which measures the host,
      holds a concurrency slot for the child's whole lifetime, and — *after*
      admission, so the count reflects true concurrency including this run — counts
      the other runs in flight and plans the worker grant. The child runs *inside*
      the ``with`` block, so the slot is released on normal exit, non-zero exit,
      and signal death alike. ``{worker_args}`` splices to ``-n N --dist loadfile``
      for an N≥2 grant, and to nothing — a plain serial suite — for a ≤1-worker
      grant (a granted single worker or the one-worker fail-open floor under
      saturation), since a single worker buys no parallelism yet pays for a worker
      process.

    Exits with the child's return code either way (a signal-killed child is
    surfaced as the shell-conventional ``128 + N``; see :func:`_run_gate_child`).
    """
    if args.enabled:
        sys.exit(0 if config.gate_lease_enabled else 1)

    from maestro import gate_capacity

    # argparse.REMAINDER keeps the ``--`` end-of-options marker as the first token
    # on some argv shapes; drop a single leading one so the child argv is clean.
    command = list(args.lease_command)
    if command and command[0] == "--":
        command = command[1:]

    # Reject an empty child before any host read or slot wait, so
    # ``maestro gate-lease`` with nothing after ``--`` cannot burn the bounded
    # acquire wait under saturation before reporting the usage error.
    if not command:
        print("gate-lease: no command given after --", file=sys.stderr)
        sys.exit(2)

    # The shared admission seam performs the measure -> plan -> acquire -> re-plan
    # sequence: a None lease (flag off) keeps today's ``auto`` width and takes no
    # slot; a grant splices the granted width's argv atoms (serial for a ≤1-worker
    # grant). The child runs *inside* the ``with`` so the slot is released on
    # normal, non-zero, and signal exits alike.
    # ``runs_path`` records one telemetry row per admitted run (flag off: none);
    # the child's mapped status is stamped as the outcome before ``sys.exit`` exits
    # the context, so the row the seam appends on exit carries it.
    with gate_capacity.lease_for_gate_run(
        config.gate_lease_dir,
        runs_path=config.gate_runs_path,
        cgroup_reserve=config.gate_lease_cgroup_reserve_enabled,
        admission_cap=config.gate_lease_admission_cap_enabled,
        # A hand run enforces no deadline and runs whatever argv the caller gave,
        # so its wall time says nothing about what the in-process suite needs:
        # naming the source here keeps it out of ``derive_deadline``'s input.
        source="wrapper",
    ) as run:
        # Take the width atoms from the lease owner so a single-worker grant runs
        # serially through the wrapper exactly as the in-process readers do. The
        # flag-off rollback keeps today's ``-n auto`` width rather than
        # ``worker_args(None)``'s serial empty, so a routed run behaves as it does
        # now when the lease is off.
        atoms = (
            ("-n", "auto", "--dist", "loadfile")
            if run.lease is None
            else gate_capacity.worker_args(run.lease)
        )
        code, saw_test_timeout = _run_gate_child(command, atoms)
        run.outcome = _gate_child_outcome(code, saw_test_timeout)
        sys.exit(code)


def cmd_measure_false_success(args: argparse.Namespace, config: Config) -> None:
    """Measure false-success recurrence from ``task_history.jsonl``."""
    from maestro.audit_checks import measure_false_success_recurrence

    config.ensure_dirs()
    memory = Memory(config.memory_dir)

    last = args.last
    tasks = memory.get_recent_tasks(n=last if last and last > 0 else 10_000)
    task_dicts: list[dict[str, object]] = [
        dataclasses.asdict(t) for t in tasks
    ]

    result = measure_false_success_recurrence(task_dicts, n=0)
    count: int = result["count"]  # type: ignore[assignment]
    by_pr: dict[int, int] = result["by_pr"]  # type: ignore[assignment]

    print(f"False-Success Recurrence Report ({len(task_dicts)} tasks examined)")
    print("=" * 40)
    print(f"PRs stamped success >=2x without closing: {count}")

    if by_pr:
        print()
        print("By PR:")
        for pr, success_count in sorted(by_pr.items()):
            print(f"  #{pr}: {success_count} success outcomes")


def cmd_cost_report(args: argparse.Namespace, config: Config) -> None:
    """Summarize ``usage.jsonl`` cost data by date and provider.

    With ``--json`` the same per-date and per-provider aggregates plus the
    grand total print as a single machine-readable JSON object (exit 0),
    leaving the default text-table path untouched. With ``--by-caller`` the
    trailing window's spend per caller (overall and per loop iteration) is
    appended — a ``caller_spend`` key in JSON, a ``Top callers`` block in text —
    read through :func:`maestro.caller_spend.read_window_spend`.
    """
    config.ensure_dirs()
    days = args.days if args.days and args.days > 0 else 7
    reading = _read_caller_spend(config, days) if args.by_caller else None
    if args.json:
        from maestro.cost_tracking import generate_cost_report_data

        data = generate_cost_report_data(config.usage_log_path, days=days)
        if reading is not None:
            data["caller_spend"] = reading.to_dict()
        print(json_mod.dumps(data, indent=2, sort_keys=True))
        return

    from maestro.cost_tracking import generate_cost_report

    report = generate_cost_report(config.usage_log_path, days=days)
    print(report)
    if reading is not None:
        _print_caller_spend(reading)


#: Callers the ``cost-report --by-caller`` text output lists, one line each.
_MAX_SHOWN_CALLERS = 10


def _read_caller_spend(config: Config, days: int) -> SpendReading:
    from maestro.caller_spend import read_window_spend
    from maestro.cost_tracking import count_metered_iterations_since

    return read_window_spend(
        _build_cost_sample_backend(config),
        requested_days=days,
        archive_threshold_days=config.archive_threshold_days,
        count_iterations=functools.partial(
            count_metered_iterations_since, config.usage_log_path,
        ),
        now=datetime.now(UTC),
    )


def _print_caller_spend(reading: SpendReading) -> None:
    def _usd(value: float | None) -> str:
        return "n/a" if value is None else f"${value:.4f}"

    print(
        f"\nSpend by caller — last {reading.window_days} days, "
        f"{reading.iterations} iterations"
    )
    if reading.window_days < reading.requested_window_days:
        print(
            f"  (clamped from {reading.requested_window_days} days: older rows are "
            f"archived past archive_threshold_days={reading.window_days})"
        )
    print(f"  Spend/iteration:  {_usd(reading.spend_usd_per_iteration)}")
    if not reading.by_caller:
        print("  No metered calls in the window.")
        return
    print("  Top callers:")
    for spend in reading.by_caller[:_MAX_SHOWN_CALLERS]:
        print(
            f"    {spend.caller} ({spend.group}): ${spend.cost_usd:.4f} "
            f"over {spend.calls} calls, {_usd(spend.usd_per_iteration)}/iteration"
        )


def cmd_cost_per_task(args: argparse.Namespace, config: Config) -> None:
    """Print a single task's accrued USD cost from ``usage.jsonl``."""
    from maestro.cost_tracking import cost_for_task

    config.ensure_dirs()
    total = cost_for_task(config.usage_log_path, args.task_id)
    if total is None:
        print(f"No usage data for task {args.task_id}")
        sys.exit(1)
    print(f"Task {args.task_id}: ${total:.4f}")


def cmd_list_versions(args: argparse.Namespace, config: Config) -> None:
    """List merged ``VersionRecord``s from the flag-selected version archive.

    Honest by construction: every row reflects a commit git actually merged
    (``record_version`` is appended only on a detected plan merge), never a
    narrated outcome.  Prints one line per version — sha, plan title, merge
    date, and quality-gate pass flag — or an explicit empty-archive message.
    Reads through :func:`_build_version_archive_backend` so that under
    ``[paths] sqlite_state_enabled`` it surfaces versions recorded to
    ``state.db`` after the flag flip, not the frozen ``version_archive.jsonl``
    rollback sidecar (which stops being written once the flag is on).
    """
    config.ensure_dirs()
    memory = Memory(
        config.memory_dir,
        version_archive=_build_version_archive_backend(config),
    )
    limit = args.limit if args.limit and args.limit > 0 else 30
    versions = memory.read_version_records(n=limit)
    if not versions:
        print("no merged versions yet")
        return
    print(f"Merged Versions ({len(versions)} shown)")
    print("=" * 40)
    for v in versions:
        sha = v.commit_sha[:12] if v.commit_sha else "(no sha)"
        gate = "pass" if v.quality_gate_pass else "FAIL"
        print(f"{sha}  {v.merged_at}  gate={gate}  {v.plan_title}")


def _parse_burn_date(raw: str, flag: str) -> datetime | None:
    """Coerce a burn-baseline DATE arg to a UTC-aware datetime, or print + None.

    Shared by ``--since`` and ``--compare``: parses *raw* with
    ``datetime.fromisoformat`` and normalises to UTC — a naive value is
    labelled UTC, an offset-aware value is converted to the same instant in
    UTC. Returns ``None`` after printing an actionable message under *flag*
    (e.g. ``--compare``) when *raw* is not ISO-8601 parseable. A successful
    parse always yields an aware datetime, so ``None`` unambiguously signals
    a bad date to the caller.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        print(
            f"Invalid {flag} date {raw!r}: expected YYYY-MM-DD or "
            "YYYY-MM-DDTHH:MM:SS"
        )
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def cmd_burn_baseline(args: argparse.Namespace, config: Config) -> None:
    """Print the proposal-084 burn-rate baseline ($/task by shape + cache).

    With ``--compare DATE`` (mutually exclusive with ``--since``) it instead
    renders the before/after/Δ comparison split about the cutoff.
    """
    from maestro.cost_tracking import (
        generate_burn_baseline,
        generate_burn_comparison,
    )

    config.ensure_dirs()

    raw_compare = getattr(args, "compare", None)
    if raw_compare is not None:
        cutoff = _parse_burn_date(raw_compare, "--compare")
        if cutoff is None:
            return
        print(generate_burn_comparison(
            config.usage_log_path, config.task_history_path, cutoff=cutoff,
        ))
        return

    since: datetime | None = None
    raw_since = getattr(args, "since", None)
    if raw_since is not None:
        since = _parse_burn_date(raw_since, "--since")
        if since is None:
            return
    report = generate_burn_baseline(
        config.usage_log_path, config.task_history_path, since=since,
    )
    print(report)


def cmd_measure_outcome_trends(args: argparse.Namespace, config: Config) -> None:
    """Measure success/partial/investigation rate trends from history."""
    from maestro.audit_checks import (
        format_outcome_trend_summary,
        measure_outcome_trends,
    )

    config.ensure_dirs()
    memory = Memory(config.memory_dir)

    window = args.window if args.window and args.window > 0 else 20
    tasks = memory.get_recent_tasks(n=window)
    task_dicts: list[dict[str, object]] = [
        dataclasses.asdict(t) for t in tasks
    ]

    trend = measure_outcome_trends(task_dicts, window=window)
    summary = format_outcome_trend_summary(trend)

    print(f"Outcome Trend Report ({len(task_dicts)} tasks examined)")
    print("=" * 40)
    if summary:
        print(summary)
    else:
        print(
            f"Insufficient data for a trend (need at least 4 tasks, "
            f"have {len(task_dicts)})."
        )


def cmd_measure_handoff_relapse(
    args: argparse.Namespace, config: Config,
) -> None:
    """Measure recent tasks re-engaging an already-handed-off PR lineage."""
    from maestro.remediation_tracker import format_handoff_relapse

    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    store = MessageStore(config.memory_dir)

    window = args.window if args.window and args.window > 0 else 50
    tasks = memory.get_recent_tasks(n=window)
    task_dicts: list[dict[str, object]] = [
        dataclasses.asdict(t) for t in tasks
    ]
    messages = store.get_recent(n=10_000)

    summary = format_handoff_relapse(task_dicts, messages, window)

    print(f"Handoff-Relapse Report ({len(task_dicts)} tasks examined)")
    print("=" * 40)
    if summary:
        print(summary)
    else:
        print("No recent task re-engaged an already-handed-off PR lineage.")


def cmd_measure_relap(args: argparse.Namespace, config: Config) -> None:
    """Measure the settled-relap rate among recent no-op tasks."""
    from maestro.audit_checks import measure_relap_rate

    config.ensure_dirs()

    last = args.last
    n = last if last and last > 0 else 0

    result = measure_relap_rate(config.memory_dir, n=n)

    total_noop: int = result["total_noop"]  # type: ignore[assignment]
    settled_relap: int = result["settled_relap"]  # type: ignore[assignment]
    live_fork: int = result["live_fork"]  # type: ignore[assignment]
    relap_rate: float = result["relap_rate"]  # type: ignore[assignment]

    print("Settled-Relap Report")
    print("=" * 40)
    print(f"No-op tasks:        {total_noop}")
    print(f"Settled-relaps:     {settled_relap}")
    print(f"Live-fork no-ops:   {live_fork}")
    print(f"Relap rate:         {relap_rate:.1%}")


def cmd_measure_confirmed_landings(
    args: argparse.Namespace, config: Config,
) -> None:
    """Measure the confirmed-vs-caused landing rate among recent successes."""
    from maestro.remediation_tracker import (
        classify_landing_attribution,
        measure_confirmed_landing_rate,
    )

    config.ensure_dirs()

    last = args.last
    n = last if last and last > 0 else 0

    result = measure_confirmed_landing_rate(config.memory_dir, n=n)
    caused: int = result["caused"]  # type: ignore[assignment]
    confirmed: int = result["confirmed"]  # type: ignore[assignment]
    total: int = result["total"]  # type: ignore[assignment]
    confirmed_rate: float = result["confirmed_rate"]  # type: ignore[assignment]

    print("Confirmed-vs-Caused Landing Report")
    print("=" * 40)
    print(f"Caused landings:     {caused}")
    print(f"Confirmed landings:  {confirmed}")
    print(f"Attributable total:  {total}")
    print(f"Confirmed rate:      {confirmed_rate:.1%}")

    memory = Memory(config.memory_dir)
    tasks = memory.get_recent_tasks(n=n if n > 0 else 10_000)
    examples = [
        t for t in tasks
        if classify_landing_attribution(dataclasses.asdict(t)) == "CONFIRMED"
    ]
    if examples:
        print()
        print("Recent CONFIRMED examples (cure already landed before the task):")
        for t in examples[-5:]:
            goal = (t.goal or "")[:70]
            print(f"  - {t.task_id}: {goal}")


def cmd_measure_phantom_landings(
    args: argparse.Namespace, config: Config,
) -> None:
    """Measure the phantom-landing divergence rate from the shadow ledger."""
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    config.ensure_dirs()

    last = args.last
    n = last if last and last > 0 else 100

    result = measure_phantom_landing_rate(
        _build_gate_activation_backend(config), n=n,
    )
    total: int = result["total"]  # type: ignore[assignment]
    diverged: int = result["diverged"]  # type: ignore[assignment]
    agree: int = result["agree"]  # type: ignore[assignment]
    divergence_rate: float = result["divergence_rate"]  # type: ignore[assignment]

    print(f"Phantom-Landing Divergence Report (last {n} records)")
    print("=" * 40)
    print(f"Shadow records:     {total}")
    print(f"Diverged:           {diverged}")
    print(f"Agreed:             {agree}")
    print(f"Divergence rate:    {divergence_rate:.1%}")


def cmd_measure_plan_mill(args: argparse.Namespace, config: Config) -> None:
    """Measure how self-referential and unproductive the plan generator is.

    Read-only instrument (proposal 116, increment 1): reports theme saturation,
    merged-to-created ratio, task-coupled fraction, and stranded-promotion count
    over the ``--last`` window of recently-created evolution plans. Changes no
    loop behaviour — zero SDK cost.
    """
    from maestro.audit_checks import (
        measure_merged_to_created_ratio,
        measure_plan_theme_saturation,
        measure_stranded_promotion_plans,
        measure_task_coupled_plan_fraction,
    )

    config.ensure_dirs()
    plans = Memory(config.memory_dir).get_all_plans()

    last = args.last
    n = last if last and last > 0 else 0

    theme = measure_plan_theme_saturation(plans, n=n)
    merged = measure_merged_to_created_ratio(plans, n=n)
    coupled = measure_task_coupled_plan_fraction(plans, n=n)
    stranded = measure_stranded_promotion_plans(plans, n=n)

    if args.json:
        print(json_mod.dumps(
            {
                "theme_saturation": theme,
                "merged_to_created": merged,
                "task_coupled": coupled,
                "stranded_promotions": stranded,
            },
            indent=2,
        ))
        return

    window_label = f"last {n}" if n > 0 else "all"
    themed_fraction: float = theme["themed_fraction"]  # type: ignore[assignment]
    ratio: float = merged["ratio"]  # type: ignore[assignment]
    coupled_fraction: float = coupled["coupled_fraction"]  # type: ignore[assignment]

    print(f"Plan-Mill Report ({window_label} created plans)")
    print("=" * 40)
    print(f"Plans examined:     {theme['total']}")
    print(
        f"Themed (self-meas): {theme['themed']} ({themed_fraction:.1%})",
    )
    print(
        f"Merged / created:   {merged['merged']}/{merged['created']} "
        f"({ratio:.1%})",
    )
    print(
        f"Task-coupled:       {coupled['coupled']} ({coupled_fraction:.1%})",
    )
    print(f"Stranded promos:    {stranded['count']}")


def cmd_pr_laps(args: argparse.Namespace, config: Config) -> None:
    """Show per-PR remediation lap counts and active handoff standdowns.

    Read-only diagnostic (proposal 080 step 4). Uses the git-merge-truth oracle
    so it runs offline — the loop's known-state block uses the host-first oracle
    — and a PR at >= budget/2 laps surfaces here and in the PR-LAPS line of
    ``format_known_state_summary``.
    """
    from maestro.remediation_tracker import (
        _UNLANDED_REMEDIATION_CYCLES,
        format_pr_lap_summary,
        measure_unlanded_remediations,
    )

    config.ensure_dirs()
    window = (
        args.window if args.window and args.window > 0
        else _UNLANDED_REMEDIATION_CYCLES
    )
    lap_counts = measure_unlanded_remediations(config.memory_dir, window)
    standdowns = active_standdown_prs(_build_escalation_ledger_backend(config))
    budget = config.pr_remediation_lap_budget

    print("PR Remediation Lap Report")
    print("=" * 40)
    print(f"Lap budget (per PR): {budget}")
    print(f"Window scanned:      {window} tasks")
    all_prs = sorted(set(lap_counts) | standdowns)
    if all_prs:
        for pr in all_prs:
            laps = lap_counts.get(pr, 0)
            stood = " [stood-down]" if pr in standdowns else ""
            print(f"  #{pr}: {laps}/{budget} laps{stood}")
    else:
        print("No unlanded remediation laps or standdowns recorded.")

    summary = format_pr_lap_summary(lap_counts, standdowns, budget)
    if summary:
        print()
        print(summary)


def cmd_pr_lap_ledger(args: argparse.Namespace, config: Config) -> None:
    """Aggregate remediation-lap waste per owned PR lineage (read-only).

    Quantifies the treadmill ``cost-report`` (global) and ``cost-per-task``
    (per-task) cannot: every ``Remediate PR #<n>:`` lap summed against the one
    PR it re-remediates — lap_count, total duration, distinct blocker labels and
    the latest outcome per PR. Pure read; no state write, no disposal path.
    """
    from maestro.stats import aggregate_pr_lap_ledger

    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    last = args.last
    n = last if last and last > 0 else 0
    tasks = memory.get_recent_tasks(n=n if n > 0 else 10_000)
    rows = aggregate_pr_lap_ledger([dataclasses.asdict(t) for t in tasks])

    if args.json:
        payload = [
            {
                "pr_number": r.pr_number,
                "lap_count": r.lap_count,
                "total_duration_seconds": r.total_duration_seconds,
                "blocker_labels": list(r.blocker_labels),
                "latest_outcome": r.latest_outcome,
            }
            for r in rows
        ]
        print(json_mod.dumps(payload, indent=2))
        return

    print("PR Lap Ledger")
    print("=" * 40)
    if not rows:
        print("No remediation laps recorded.")
        return
    for r in rows:
        blockers = ", ".join(r.blocker_labels) if r.blocker_labels else "-"
        print(
            f"#{r.pr_number}: laps={r.lap_count} "
            f"duration={r.total_duration_seconds:.0f}s "
            f"blockers=[{blockers}] outcome={r.latest_outcome or '-'}"
        )


def cmd_deed_ledger(args: argparse.Namespace, config: Config) -> None:
    """Tail ``deed_ledger.jsonl`` — the record of fired terminal acts (read-only).

    The positive complement to ``pr-lap-ledger``: where that quantifies the
    treadmill, this lists the terminal acts (``gh pr close`` disposals, delivered
    handoffs, merges, owned prunes) that actually end a lineage. Read-only over
    the logical ledger — it records no new deed. With ``[paths]
    sqlite_state_enabled`` on, the first backend access lazily creates and
    one-time-seeds ``state.db`` (rows + marker) from the legacy JSONL, so the
    substrate is written once on first read; the ledger's contents are unchanged.
    """
    config.ensure_dirs()
    backend = _build_deed_ledger_backend(config)
    count = args.count if args.count is not None else 20
    deeds = backend.recent(n=count if count > 0 else 0)

    if args.json:
        print(
            json_mod.dumps(
                [dataclasses.asdict(d) for d in deeds], indent=2,
            ),
        )
        return

    print("Deed Ledger — fired terminal acts")
    print("=" * 40)
    total = backend.count()
    print(f"Total deeds recorded: {total}")
    if not deeds:
        print("No terminal deeds recorded.")
        return
    for d in deeds:
        pr = f"#{d.pr_number}" if d.pr_number is not None else "-"
        print(f"  [{d.timestamp}] {d.deed_type} {pr}: {d.detail}")


def cmd_owned_acts(args: argparse.Namespace, config: Config) -> None:
    """Render ``owned-acts`` — my chosen terminal dispositions (read-only).

    The disposition complement to ``deed-ledger``: where that lists *fired*
    terminal deeds (pushes, closes, handoffs, merges), this renders the owned
    dispositions I author without a git-visible deed — chosen ``[stood-down]``
    verdicts and obsolete-plan prunes — plus the fired ``HANDOFF_DELIVERED`` deeds
    projected as forced handoff rows, all with lineage and a chosen-vs-forced
    flag. Pure read; no state write.
    """
    from maestro import remediation_tracker

    config.ensure_dirs()
    count = args.count if args.count is not None else 20
    print(
        remediation_tracker.format_owned_acts_report(
            config.memory_dir,
            count,
            args.json,
            stand_down_backend=_build_stand_down_verdict_backend(config),
            deed_backend=_build_deed_ledger_backend(config),
        ),
    )


def cmd_list_proposals(args: argparse.Namespace, config: Config) -> None:
    """Render ``list-proposals`` — multi-PR evolution proposals + slices (read-only).

    Joins each persisted :class:`~maestro.memory.EvolutionProposal` to its ordered
    slice plans (:meth:`~maestro.memory.Memory.get_slices_for_proposal`) and
    prints, per proposal, its rollout ``status`` and each slice's ``phase_index``,
    ``plan_id``, ``title``, persisted plan ``status``, and ``pr_url``. Offline —
    no host call; the persisted plan status already reflects
    ``sliced``/``pending_review``/``approved``/``implemented``/``merged``
    (proposal 118). Pure read; no state write.
    """
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    proposals = memory.get_evolution_proposals()

    if args.json:
        payload = [
            {
                "proposal_id": p.proposal_id,
                "title": p.title,
                "goal": p.goal,
                "status": p.status,
                "created_at": p.created_at,
                "slices": [
                    {
                        "phase_index": s.phase_index,
                        "plan_id": s.plan_id,
                        "title": s.title,
                        "status": s.status,
                        "pr_url": s.pr_url,
                    }
                    for s in memory.get_slices_for_proposal(p.proposal_id)
                ],
            }
            for p in proposals
        ]
        print(json_mod.dumps(payload, indent=2))
        return

    print("Evolution Proposals — multi-PR rollouts")
    print("=" * 40)
    if not proposals:
        print("No evolution proposals recorded.")
        return
    for p in proposals:
        print(f"{p.proposal_id} [{p.status}] {p.title}")
        slices = memory.get_slices_for_proposal(p.proposal_id)
        if not slices:
            print("  (no slices)")
            continue
        for s in slices:
            pr = s.pr_url if s.pr_url else "-"
            print(
                f"  phase {s.phase_index}: {s.plan_id} [{s.status}] "
                f"{s.title} ({pr})",
            )


def cmd_pr_owed_act(args: argparse.Namespace, config: Config) -> None:
    """Render ``pr-owed-act`` — one owed-act verdict per open own PR (read-only).

    The decision aid my journal begged for: before I touch a PR, is its owed act
    *write code*, *hand off to Thomas*, or *wait for the CI clock*? Enumerates
    open own ``maestro/evolve/*`` PRs once via
    :meth:`~maestro.vcs.CodeHost.get_open_evolution_pr_states`, derives each PR's
    merge blocker (:func:`~maestro.pr_owed_act.derive_owed_act_blocker`) and
    CI check-runs (:func:`~maestro.pr_owed_act.check_runs_from_conclusion` over
    :meth:`~maestro.vcs.CodeHost.get_pr_check_conclusion`) and its unresolved
    AI-review-thread count (:func:`~maestro.evolution_tend.count_eligible_threads_fail_safe`
    — the loop's own fix-routing selector over the strict thread reader, where a
    failed scan counts as one blocker so it can never surface as
    ``HANDOFF_READY``, #4387), and folds them plus the durable settled-PR set into one
    :class:`~maestro.pr_owed_act.OwedAct` via
    :func:`~maestro.pr_owed_act.classify_pr_owed_act`. The thread count is what
    keeps a green-CI PR blocked *only* by unanswered review threads out of
    ``HANDOFF_READY``: such a thread is owned, code-clearable work
    (:func:`~maestro.remediation_tracker.classify_owned_blocker` promotes the
    approval/clean blocker to ``THREADS_UNRESOLVED``), so it reports ``CODE_OWED``.
    Settled/merged PRs short-circuit to ``None`` and are skipped before any per-PR
    read. Pure read: it reaches no push / merge / ready / open seam and adds no
    merge-oracle call path (the settled set comes from the flag-selected
    settled-verdicts backend via :func:`_build_settled_verdicts_backend` — the
    JSONL ledger, or ``state.db`` under ``[paths] sqlite_state_enabled`` — not a
    fresh merge fetch); the per-PR review-thread read is a non-merge seam. The open-PR
    listing is read ``strict=True``: a failed ``gh pr list`` prints the error and
    exits ``1`` rather than reporting "no open own PRs" (#4387).

    **Behind-``main`` grounding (measured ``commits_behind``).** For each open own
    PR that survives the settled short-circuit this measures the head branch's
    ``commits_behind`` against ``origin/main`` via
    :func:`~maestro.pr_facts.observe_pr_staleness` (wrapping this checkout's
    :class:`~maestro.vcs.SubprocessVCS` git seams, mirroring
    ``EvolutionEngine._observe_pr_staleness``) and threads that measured value into
    :func:`~maestro.pr_owed_act.derive_owed_act_blocker` in place of the old
    hardcoded ``commits_behind=0``. A behind-but-clean PR therefore routes to
    :data:`~maestro.remediation_tracker.MERGE_BLOCK_BEHIND_MAIN` (the Tend
    catch-up state → ``EXTERNAL_BLOCKED``) rather than falsely reporting
    ``HANDOFF_READY`` off a permissive-protection ``CLEAN`` host proxy — the same
    ``mergeStateStatus``-proxy masking the Tend invariant warns of. The staleness
    probe is a read-only local-git seam (``git rev-parse`` / ``merge-tree``), so
    the command stays a pure read: it still reaches no push / merge / ready / open
    seam and adds no merge-oracle call path. All three git oracles are
    best-effort (swallow ``VCSError``), so an unresolvable base or unfetched
    ``origin`` degrades to ``commits_behind=0`` — the prior behaviour — rather
    than raising. The blocker itself comes from
    :func:`~maestro.pr_owed_act.derive_owed_act_blocker` — the derivation the
    tend walk shares — so the measured merge-tree reading and the host
    ``mergeable`` field refine to ``CONFLICTING`` (``code-owed``), and a pending
    rollup no longer blocks an unresolved review thread from promoting.
    """
    from maestro import pr_facts, pr_owed_act
    from maestro.audit_checks_settlement import (
        load_settled_prs,
    )
    from maestro.evolution_tend import count_eligible_threads_fail_safe

    config.ensure_dirs()
    wt = config.work_target()
    code_host = SubprocessCodeHost(
        wt.github_repo,
        workspace=wt.repo_workspace,
        timeout=config.gh_timeout,
        pr_creation_timeout=config.pr_creation_timeout,
        reviewer=config.github_reviewer,
    )
    vcs = SubprocessVCS(
        wt.repo_workspace,
        timeout=config.git_timeout,
        quick_timeout=config.git_quick_timeout,
        push_timeout=config.implement_timeout,
    )
    settled = load_settled_prs(_build_settled_verdicts_backend(config))
    try:
        # Strict: a failed listing must not read as "no open own PRs" (#4387).
        states = code_host.get_open_evolution_pr_states(
            "maestro/evolve/", strict=True,
        )
    except VCSError as exc:
        print(f"could not list open own PRs: {exc}")
        sys.exit(1)
    printed = 0
    for head_ref, state in sorted(states.items(), key=lambda kv: kv[1].number):
        if state.number in settled:
            # A settled PR is owed nothing; skip before any per-PR read so it
            # costs zero fetches (mirrors classify_pr_owed_act's short-circuit)
            # — the staleness probe below never runs for it.
            continue
        conclusion = code_host.get_pr_check_conclusion(state.number)
        # Measure how far the head trails ``origin/main`` from local git rather
        # than trusting the host ``mergeStateStatus`` proxy, which reports
        # ``BEHIND`` only under up-to-date-branch protection; a behind-but-clean
        # PR must route to the Tend catch-up state (under the merge-queue model
        # ``derive_owed_act_blocker`` resolves behind-ness away, #4307).
        staleness = pr_facts.observe_pr_staleness(
            head_ref,
            commits_behind_fn=lambda b: vcs.commits_ahead_behind(
                f"origin/{b}", "origin/main",
            )[1],
            merge_clean_fn=lambda b: not vcs.merge_tree_conflicts(
                "origin/main", f"origin/{b}",
            ),
            base_sha_fn=lambda: vcs.rev_parse("origin/main"),
        )
        # The same pure derivation the tend walk feeds, so the two faces agree:
        # a pending rollup no longer blocks thread promotion, and the measured
        # merge-tree reading / host ``mergeable`` refine to CONFLICTING.
        blocker = pr_owed_act.derive_owed_act_blocker(
            commits_behind=staleness.commits_behind,
            check_conclusion=conclusion,
            merge_state_status=state.merge_state_status,
            mergeable=state.mergeable,
            cleanly_mergeable=staleness.cleanly_mergeable,
            tend_catch_up=config.tend_main_merge_enabled,
        )
        # An unresolved AI review thread on my own PR is owned, code-clearable
        # work, not a handoff/external gate — the loop's fix-routing selector
        # (excludes resolved/outdated/already-answered) is the count that
        # promotes an approval/clean blocker to THREADS_UNRESOLVED, so a
        # thread-blocked green PR reports CODE_OWED, not HANDOFF_READY.
        unresolved_threads = count_eligible_threads_fail_safe(
            code_host, state.number,
        )
        act = pr_owed_act.classify_pr_owed_act(
            state.number,
            blocker=blocker,
            check_runs=pr_owed_act.check_runs_from_conclusion(conclusion),
            settled_pr_numbers=settled,
            unresolved_threads=unresolved_threads,
        )
        # Settled PRs — the sole case where classify returns None — are already
        # skipped above, so every reached PR yields a concrete act.
        assert act is not None
        printed += 1
        print(
            f"#{state.number} {act.value}: "
            f"{pr_owed_act.describe_owed_act(act)} [{head_ref}]",
        )
    if printed == 0:
        print("no open own PRs with an owed act")


def cmd_list_limitations(args: argparse.Namespace, config: Config) -> None:
    """Render ``list-limitations`` — known limitations with mirror status (read-only).

    Lists every ``SelfModel.limitations_known`` entry alongside whether it has a
    mirrored ``[limitation]`` ``open_questions`` row — the synthetic question
    surfaced by :meth:`Consciousness.surface_limitations_as_questions`. The
    mirror-presence test rebuilds the same 160-char-capped ``[limitation] …``
    candidate key that surfacing path uses and matches it (case/whitespace-
    insensitive) against ``open_questions`` via :func:`match_open_question`, so
    ``mirrored``/``unmirrored`` here agrees with what the loop would surface.
    Pure read; no state write, no SDK cost.
    """
    from maestro.consciousness import match_open_question

    config.ensure_dirs()
    consciousness = Consciousness(config.memory_dir)
    model = consciousness.load_self_model()

    entries: list[tuple[str, str | None]] = []
    for entry in model.limitations_known:
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if not text:
            continue
        # Mirror the surfacing path's key build exactly (160-char cap under the
        # 200-char per-item ceiling) so presence here matches what would surface.
        candidate = f"[limitation] {text[:160]}"
        mirror = match_open_question(candidate, model.open_questions)
        entries.append((entry, mirror))

    if args.json:
        print(json_mod.dumps(
            [
                {
                    "limitation": entry,
                    "mirrored": mirror is not None,
                    "open_question": mirror,
                }
                for entry, mirror in entries
            ],
            indent=2,
        ))
        return

    print("Known limitations — mirrored [limitation] open-question status")
    print("=" * 40)
    if not entries:
        print("No limitations recorded.")
        return
    for entry, mirror in entries:
        flag = "mirrored" if mirror is not None else "unmirrored"
        print(f"  [{flag}] {entry}")
        if mirror is not None:
            print(f"      -> {mirror}")


def cmd_reviews(args: argparse.Namespace, config: Config) -> None:
    """Tail ``artifact_reviews.jsonl`` with optional verdict filtering."""
    config.ensure_dirs()
    memory = Memory(config.memory_dir)

    count = args.count if args.count is not None else 20
    if args.verdict:
        # Filter first, then tail — ``get_artifact_reviews(n)`` would truncate
        # before the filter and drop rows that match further back.
        all_reviews = memory._load_all_reviews()
        all_reviews = [r for r in all_reviews if r.verdict == args.verdict]
        reviews = all_reviews[-count:] if count > 0 else []
    else:
        reviews = memory.get_artifact_reviews(n=count if count > 0 else 0)

    if args.json:
        print(json_mod.dumps([dataclasses.asdict(r) for r in reviews], indent=2))
        return

    if not reviews:
        print("No artifact reviews.")
        return

    for r in reviews:
        ts = r.timestamp.split("T")[0] if r.timestamp else "unknown"
        title = r.title[:80] + ("…" if len(r.title) > 80 else "")
        print(f"[{ts} | {r.verdict} | {r.task_id} | {title}]")
        if r.reasoning:
            reasoning = r.reasoning[:80] + ("…" if len(r.reasoning) > 80 else "")
            print(f"  {reasoning}")
        print()


def cmd_publications(args: argparse.Namespace, config: Config) -> None:
    """Tail ``publication_proposals.jsonl`` with optional status filtering."""
    config.ensure_dirs()
    memory = Memory(config.memory_dir)

    count = args.count if args.count is not None else 20
    status_filter = args.status if args.status else None
    proposals = memory.get_publication_proposals(
        status_filter=status_filter,  # type: ignore[arg-type]
    )
    if count > 0:
        proposals = proposals[-count:]
    elif count == 0:
        proposals = []

    if args.json:
        print(json_mod.dumps(
            [dataclasses.asdict(p) for p in proposals], indent=2,
        ))
        return

    if not proposals:
        print("No publication proposals.")
        return

    for p in proposals:
        ts = p.created_at.split("T")[0] if p.created_at else "unknown"
        name = p.proposed_name[:40]
        print(f"[{ts} | {p.status} | {p.proposal_id} | {name}]")
        if p.description:
            desc = p.description[:80] + (
                "…" if len(p.description) > 80 else ""
            )
            print(f"  {desc}")
        if p.rejection_reason:
            reason = p.rejection_reason[:80] + (
                "…" if len(p.rejection_reason) > 80 else ""
            )
            print(f"  rejection: {reason}")
        if p.published_url:
            print(f"  url: {p.published_url}")
        print()


def cmd_docs(args: argparse.Namespace, config: Config) -> None:
    """Tail ``document_records.jsonl`` with optional filters."""
    config.ensure_dirs()
    memory = Memory(config.memory_dir)

    count = args.count if args.count is not None else 20
    records = memory.get_document_records(
        n=None,
        doc_type=args.doc_type,  # type: ignore[arg-type]
        status=args.status,  # type: ignore[arg-type]
        task_id=args.task,
    )
    if count > 0:
        records = records[-count:]
    elif count == 0:
        records = []

    if args.json:
        print(json_mod.dumps(
            [dataclasses.asdict(r) for r in records], indent=2,
        ))
        return

    if not records:
        print("No document proposal records.")
        return

    for r in records:
        ts = r.created_at.split("T")[0] if r.created_at else "unknown"
        title = r.title[:60]
        print(
            f"[{ts} | {r.status} | {r.doc_type} | {r.doc_id} | {title}]"
        )
        if r.linked_task_ids:
            print(f"  tasks: {', '.join(r.linked_task_ids)}")
        if r.target_path:
            print(f"  path: {r.target_path}")
        if r.pr_url:
            print(f"  pr: {r.pr_url}")
        if r.error:
            print(f"  error: {r.error[:80]}")
        print()


def cmd_serve(args: argparse.Namespace, config: Config) -> None:
    import uvicorn

    from maestro.dashboard.app import create_app

    agent = _build_agent(config)
    app = create_app(agent)

    port = args.port if args.port is not None else config.dashboard_port
    host = config.dashboard_host
    print(f"Maestro dashboard: http://{host}:{port}")
    config.dashboard_pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        config.dashboard_pid_path.unlink(missing_ok=True)


def cmd_preflight(_args: argparse.Namespace, config: Config) -> None:
    """Validate that Maestro can start: imports, config, and state files."""
    checks: list[tuple[str, str]] = []

    # 1. Config loaded and validated (already done by main() calling Config.from_env())
    checks.append(("config", "ok"))

    # 2. Secrets health check (includes auth)
    try:
        # Resolve the auth method once and reuse it for both the credential
        # precondition and the label, so check_auth() fires once (#357).
        # _resolve_cli_auth_method returns None for a non-anthropic provider so
        # check_secrets()'s provider-specific message stays ahead of the
        # anthropic auth fallback (#356 review); auth_label(provider=...) then
        # renders a provider-neutral label without resolving anthropic creds, so
        # a valid non-anthropic deployment does not crash at the label step. A
        # missing credential raises SystemExit here or inside check_secrets(),
        # caught below.
        provider = config.resolved_sdk_provider()
        auth_method = _resolve_cli_auth_method(provider)
        secret_warnings = config.check_secrets(
            publication_enabled=config.publication_enabled,
            provider=provider,
            phase_providers=config.sdk_phase_providers,
            check_publication_scope=True,
            auth_method=auth_method,
        )
        auth = config.auth_label(auth_method, provider=provider)
        checks.append(("auth", auth))
        if secret_warnings:
            checks.append(("secrets", f"{len(secret_warnings)} warning(s)"))
            for w in secret_warnings:
                checks.append(("  warning", w))
        else:
            checks.append(("secrets", "ok"))
    except SystemExit as exc:
        checks.append(("secrets", "MISSING"))
        print(f"FAIL: {exc}")
        sys.exit(1)

    # 2b. MCP launchers — every enabled external MCP server's launcher on PATH
    launcher_errors = config.check_mcp_launchers()
    if launcher_errors:
        checks.append(("mcp_launchers", "MISSING"))
        for err in launcher_errors:
            print(f"FAIL: {err}")
        sys.exit(1)
    checks.append(("mcp_launchers", "ok"))

    # 3. Memory directory
    config.ensure_dirs()
    checks.append(("memory_dir", str(config.memory_dir)))

    # 3b. Schema check / migration
    from maestro.migration import check_schema, ensure_schema
    if check_schema(config.memory_dir):
        checks.append(("schema", "ok"))
    else:
        # A fingerprint mismatch advances the stored fingerprint (the loaders
        # normalise on read; no rewrite runs, so there is no partial-migration
        # failure mode), so ``check_schema`` is always true afterward.
        ensure_schema(
            config.memory_dir, sqlite_state_enabled=config.sqlite_state_enabled,
        )
        checks.append(("schema", "migrated"))

    # 4. State files — load each, report status
    consciousness = Consciousness(config.memory_dir)
    try:
        model = consciousness.load_self_model()
        checks.append(("self_model", f"evolutions={model.evolution_count}"))
    except Exception as exc:
        checks.append(("self_model", f"ERROR: {exc}"))
        print(f"FAIL: Cannot load self model: {exc}")
        sys.exit(1)

    journal = Journal(config.memory_dir)
    try:
        count = len(journal.read_recent(1000000))
        checks.append(("journal", f"{count} entries"))
    except Exception as exc:
        checks.append(("journal", f"ERROR: {exc}"))
        print(f"FAIL: Cannot load journal: {exc}")
        sys.exit(1)

    memory = Memory(config.memory_dir)
    try:
        task_count = memory.task_count()
        plan_count = len(memory.get_all_plans())
        checks.append(("tasks", f"{task_count} tasks, {plan_count} plans"))
    except Exception as exc:
        checks.append(("tasks", f"ERROR: {exc}"))
        print(f"FAIL: Cannot load memory: {exc}")
        sys.exit(1)

    # 5. Module imports — verify all core modules load
    import importlib

    _modules = [
        "maestro.agent", "maestro.dashboard.app",
        "maestro.evolution", "maestro.executor", "maestro.planner",
        "maestro.sdk",
    ]
    try:
        for mod in _modules:
            importlib.import_module(mod)
        checks.append(("imports", "ok"))
    except Exception as exc:
        checks.append(("imports", f"ERROR: {exc}"))
        print(f"FAIL: Module import error: {exc}")
        sys.exit(1)

    # 5b. Sibling packages — maestro_sdk / maestro_review are excluded from
    # maestro's own distribution (pyproject.toml packages.find exclude) and
    # installed separately (Dockerfile base stage + CI). Under
    # PYTHONPATH=/workspace the sibling src-layout dir resolves as a half-empty
    # PEP 420 namespace: ``import maestro_sdk`` *succeeds* while every submodule
    # import (e.g. maestro_sdk.mcp_config, which maestro/providers/cli_paced.py
    # needs) raises — aborting every in-container quality-gate run at collection.
    # The provisioning fact has one owner (proposal 133): ``probe_provisioning``
    # resolves one declared submodule per entry in the oracle's ``_SIBLINGS``
    # registry, so this startup gate covers *every* sibling rather than the one
    # it used to hard-code, and reports the owner's own wording (issue #3872).
    # It is pure — no subprocess, no disk — so the deploy script's timed smoke
    # test is unaffected. Branch on ``declines_gate_run`` rather than on a kind
    # string: ``probe_provisioning`` returns only ``SOUND`` or ``UNPROVISIONED``,
    # so the predicate is exactly equivalent here, and no module outside the
    # owner spells a runnability kind.
    sibling_verdict = gate_environment.probe_provisioning()
    if gate_environment.declines_gate_run(sibling_verdict):
        checks.append(("siblings", "MISSING"))
        print(f"FAIL: {gate_environment.format_verdict(sibling_verdict)}")
        sys.exit(1)
    checks.append(("siblings", "ok"))

    # 5c. Runtime contract (proposal 141) — offline fold of
    # deploy/runtime_contract.toml; enforced, a violation fails the deploy smoke.
    from maestro.runtime_contract import enforce_runtime_contract
    try:
        contract_warnings = enforce_runtime_contract(
            config,
            enforced=config.runtime_contract_enforced,
            env_name=os.environ.get("MAESTRO_ENV"),
        )
    except SystemExit as exc:
        checks.append(("runtime_contract", "VIOLATED"))
        for line in str(exc).splitlines():
            print(f"FAIL: {line}")
        sys.exit(1)
    if contract_warnings:
        checks.append(("runtime_contract", f"{len(contract_warnings)} warning(s)"))
        for w in contract_warnings:
            checks.append(("  warning", w))
    else:
        checks.append(("runtime_contract", "ok"))

    print("Preflight checks passed:")
    for name, status in checks:
        print(f"  {name}: {status}")


def cmd_loop(args: argparse.Namespace, config: Config) -> None:
    # Touch the Docker healthcheck heartbeat file as the very first thing
    # the ``loop`` command does — before any of the heavy ``_build_agent``
    # initialisation (schema migration, MCP server setup, VCS/code-host
    # clients, EvolutionEngine, …). The healthcheck in docker-compose.yml
    # probes ``<memory_dir>/loop.heartbeat``'s mtime; if it doesn't yet
    # exist when Docker runs its first check after ``start_period``, the
    # container stays in ``health: starting`` and the deploy script rolls
    # back a perfectly-functional loop. Writing the file up-front decouples
    # liveness signalling from agent-construction latency.
    #
    # ``write_heartbeat`` is best-effort: if the memory dir doesn't yet
    # exist (e.g. on a fresh local install) the write fails silently, then
    # ``_build_agent`` creates the dir via ``config.ensure_dirs()`` and the
    # initial write from ``run_loop`` itself lands moments later.
    from maestro.agent import write_heartbeat
    write_heartbeat(config.memory_dir)
    smoke = getattr(args, "smoke", False)
    agent = _build_agent(
        config, smoke=smoke, check_publication_scope=True, warn_if_null=True,
    )
    interval = args.interval if args.interval is not None else config.loop_interval
    evolve_every = args.evolve_every if args.evolve_every is not None else config.loop_evolve_every
    task_every = args.task_every if args.task_every is not None else config.loop_task_every
    dream_every = args.dream_every if args.dream_every is not None else config.loop_dream_every
    audit_self_every_arg = getattr(args, "audit_self_every", None)
    audit_self_every = (
        audit_self_every_arg if audit_self_every_arg is not None
        else config.loop_audit_self_every
    )
    contemplate_every = config.loop_contemplate_every
    curate_every = config.loop_curate_every
    curate_enabled = config.loop_curate_enabled
    if smoke:
        print("Starting smoke iteration (fast deploy-validation mode)")
    else:
        print(
            f"Starting autonomous loop (interval={interval}s, "
            f"evolve_every={evolve_every}, task_every={task_every}, "
            f"dream_every={dream_every}, "
            f"audit_self_every={audit_self_every}, "
            f"contemplate_every={contemplate_every}, "
            f"curate_every={curate_every} (enabled={curate_enabled}))"
        )
    print("Press Ctrl+C to stop gracefully.")
    asyncio.run(agent.run_loop(
        interval=interval,
        max_iters=args.max_iters,
        evolve_every=evolve_every,
        task_every=task_every,
        dream_every=dream_every,
        audit_self_every=audit_self_every,
        contemplate_every=contemplate_every,
        curate_every=curate_every,
        curate_enabled=curate_enabled,
        smoke=smoke,
    ))
    print("Maestro stopped.")


def _read_pid(pid_file: Path) -> int | None:
    """Read PID from a PID file, or return None if not found."""
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    if pid <= 0:
        return None
    return pid


def _resolve_pid_files(
    args: argparse.Namespace, config: Config,
) -> list[tuple[str, Path]]:
    """Return (label, pid_path) pairs for the targeted process(es)."""
    if getattr(args, "all_processes", False):
        return [("loop", config.pid_path), ("dashboard", config.dashboard_pid_path)]
    if getattr(args, "dashboard", False):
        return [("dashboard", config.dashboard_pid_path)]
    return [("loop", config.pid_path)]


def cmd_issues(args: argparse.Namespace, config: Config) -> None:
    # Resolve the repo target through the WorkTarget owner (proposal 107) so a
    # ``[work] github_repo`` redirect lists the target repo's issues; guard and
    # build both read ``wt.github_repo`` (defaults to ``config.github_repo``).
    wt = config.work_target()
    if not wt.github_repo:
        print("MAESTRO_GITHUB_REPO not set — cannot list issues.")
        sys.exit(1)
    tracker = IssueTracker(wt.github_repo, timeout=config.gh_timeout)
    issues = tracker.list_open()
    if not issues:
        print("No open issues.")
        return
    for iss in issues:
        labels = ", ".join(iss.labels) if iss.labels else ""
        label_str = f" [{labels}]" if labels else ""
        print(f"  #{iss.number}: {iss.title}{label_str}")


def cmd_messages(args: argparse.Namespace, config: Config) -> None:
    config.ensure_dirs()
    store = MessageStore(config.memory_dir)
    count = args.count if args.count is not None else 20
    messages = store.get_recent(count)
    if not messages:
        print("No messages.")
        return
    for m in messages:
        ts = m.timestamp.split("T")[0]
        sender = "Maestro" if m.sender == "maestro" else "Thomas"
        print(f"[{ts} | {sender} | {m.message_type}]")
        print(m.content)
        print()


def cmd_logs(args: argparse.Namespace, config: Config) -> None:
    from maestro.logs import list_sdk_sessions, show_sdk_session, tail_file

    lines = args.lines if args.lines is not None else config.log_tail_lines
    if args.sdk:
        if args.list_sessions:
            list_sdk_sessions(
                config.sdk_log_dir, date=args.date, max_dates=config.log_dates_shown,
            )
        else:
            show_sdk_session(
                config.sdk_log_dir,
                session=args.session,
                date=args.date,
                lines=lines,
                follow=args.follow,
            )
    else:
        sdk_only_flags = []
        if args.date:
            sdk_only_flags.append("--date")
        if args.session:
            sdk_only_flags.append("--session")
        if args.list_sessions:
            sdk_only_flags.append("--list")
        if sdk_only_flags:
            print(
                f"Note: {', '.join(sdk_only_flags)} ignored without --sdk flag"
            )
        tail_file(config.app_log_path, lines=lines, follow=args.follow)


def cmd_shutdown(args: argparse.Namespace, config: Config) -> None:
    targets = _resolve_pid_files(args, config)
    found_any = False
    for label, pid_file in targets:
        pid = _read_pid(pid_file)
        if pid is None:
            print(f"No running Maestro {label} process found.")
            continue
        found_any = True
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent graceful shutdown signal to Maestro {label} (PID {pid}).")
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)
            print(f"Maestro {label} process not found (stale PID file removed).")
        except PermissionError:
            print(f"Permission denied sending signal to Maestro {label} (PID {pid}).")
    if not found_any:
        sys.exit(1)


def cmd_kill(args: argparse.Namespace, config: Config) -> None:
    targets = _resolve_pid_files(args, config)
    found_any = False
    for label, pid_file in targets:
        pid = _read_pid(pid_file)
        if pid is None:
            print(f"No running Maestro {label} process found.")
            continue
        found_any = True
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"Killed Maestro {label} process (PID {pid}).")
        except ProcessLookupError:
            print(f"Maestro {label} process not found (stale PID file removed).")
        except PermissionError:
            print(f"Permission denied killing Maestro {label} (PID {pid}).")
            # Don't remove PID file — process is still running, we just
            # lack permission to kill it.
            continue
        # Remove PID file — process is confirmed dead or gone
        pid_file.unlink(missing_ok=True)
    if not found_any:
        sys.exit(1)


# Regexes for extracting PR numbers from merge/squash commit messages.
# - GitHub default merge commit: ``Merge pull request #123 from owner/branch``
# - Squash-merge trailer:       ``Some title (#123)``
_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+)")
_SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")

_PRE_DEPLOY_PR_BODY_CHARS = 200
_PRE_DEPLOY_CONTENT_CHARS = 4000


def _extract_pr_numbers(subject: str) -> list[int]:
    numbers: list[int] = []
    m = _MERGE_PR_RE.search(subject)
    if m:
        numbers.append(int(m.group(1)))
    m = _SQUASH_PR_RE.search(subject)
    if m:
        numbers.append(int(m.group(1)))
    return numbers


def _resolve_pre_deploy_prs(
    from_ref: str | None,
    to_ref: str | None,
    explicit: list[int] | None,
    workspace: Path,
) -> list[int]:
    """Return a de-duplicated, order-preserving list of PR numbers."""
    seen: set[int] = set()
    ordered: list[int] = []

    def _add(n: int) -> None:
        if n not in seen:
            seen.add(n)
            ordered.append(n)

    for n in explicit or []:
        _add(n)

    if from_ref and to_ref:
        try:
            vcs = SubprocessVCS(workspace)
            commits = vcs.get_commits_between(from_ref, to_ref)
        except Exception:
            logger.warning("Pre-deploy: get_commits_between failed", exc_info=True)
            commits = []
        for commit in commits:
            for n in _extract_pr_numbers(commit.subject):
                _add(n)

    return ordered


def _compose_pre_deploy_content(
    ref: str | None,
    run_id: str | None,
    prs: list[tuple[int, str, str]],
) -> str:
    short_ref = (ref or "unknown")[:12]
    run = run_id or "manual"
    header = (
        f"Pre-deploy notice: deployment starting (ref={short_ref}, run={run}). "
        f"The loop will be stopped shortly."
    )
    if not prs:
        return f"{header}\n(no PR metadata available)"[:_PRE_DEPLOY_CONTENT_CHARS]
    lines = [f"{header} Merged PRs landing in this deploy:"]
    for number, title, body in prs:
        title_line = f"- #{number} {title}".strip() if title else f"- #{number}"
        lines.append(title_line)
        if body:
            excerpt = body.strip().replace("\r", "")
            # Flatten to a single line to keep the bullet list tidy.
            excerpt = " ".join(excerpt.split())
            if len(excerpt) > _PRE_DEPLOY_PR_BODY_CHARS:
                excerpt = excerpt[: _PRE_DEPLOY_PR_BODY_CHARS - 1].rstrip() + "…"
            lines.append(f"  {excerpt}")
    content = "\n".join(lines)
    if len(content) > _PRE_DEPLOY_CONTENT_CHARS:
        content = content[: _PRE_DEPLOY_CONTENT_CHARS - 1].rstrip() + "…"
    return content


def cmd_notify_pre_deploy(args: argparse.Namespace, config: Config) -> None:
    """Tell the running Maestro loop that a deploy is about to start.

    Best-effort: any failure is logged and the command still exits 0 so
    that a broken notification never breaks a deploy.
    """
    try:
        config.ensure_dirs()

        pr_numbers = _resolve_pre_deploy_prs(
            from_ref=args.from_ref,
            to_ref=args.ref,
            explicit=list(args.pr or []),
            workspace=config.workspace,
        )

        pr_details: list[tuple[int, str, str]] = []
        if pr_numbers:
            try:
                code_host = SubprocessCodeHost(
                    repo=config.github_repo,
                    workspace=config.workspace,
                )
            except Exception:
                logger.warning("Pre-deploy: SubprocessCodeHost init failed", exc_info=True)
                code_host = None  # type: ignore[assignment]
            for n in pr_numbers:
                title = ""
                body = ""
                if code_host is not None:
                    try:
                        info = code_host.get_pr_info(n)
                        title = info.get("title", "") or ""
                        body = info.get("body", "") or ""
                    except Exception:
                        logger.warning(
                            "Pre-deploy: get_pr_info failed for #%d", n, exc_info=True,
                        )
                pr_details.append((n, title, body))

        content = _compose_pre_deploy_content(args.ref, args.run_id, pr_details)

        # Persist as a Message — it will be picked up in the next loop
        # iteration's process_messages phase, or on first iteration after
        # restart if the loop is SIGTERM'd first.
        try:
            store = MessageStore(config.memory_dir)
            msg = Message(
                message_id=(
                    f"{PRE_DEPLOY_MESSAGE_ID_PREFIX}{int(time.time() * 1000)}"
                ),
                timestamp=datetime.now(UTC).isoformat(),
                sender="thomas",
                content=content,
                message_type="info",
                reply_to=None,
                read=False,
            )
            store.save_message(msg)
            print(f"Pre-deploy notice saved ({len(pr_details)} PR(s)).")
        except Exception:
            logger.warning("Pre-deploy: failed to persist message", exc_info=True)

        # Journal milestone event — durable even if the loop never processes the
        # message before shutdown.
        try:
            journal = Journal(config.memory_dir)
            journal.record_event(
                content=content,
                entry_type="milestone",
                triggered_by="pre_deploy",
            )
        except Exception:
            logger.warning("Pre-deploy: failed to write journal milestone", exc_info=True)
    except Exception:
        logger.warning("Pre-deploy: unexpected error", exc_info=True)


def _build_email_client(config: Config) -> EmailClient | None:
    """Construct an EmailClient from config, or return *None* if not configured."""
    from maestro.email import EmailClient

    if not config.email_address or not config.email_password:
        return None
    return EmailClient(
        address=config.email_address,
        password=config.email_password,
        smtp_host=config.email_smtp_host,
        smtp_port=config.email_smtp_port,
        imap_host=config.email_imap_host,
        imap_port=config.email_imap_port,
        max_length=config.email_max_length,
    )


def cmd_email(args: argparse.Namespace, config: Config) -> None:
    client = _build_email_client(config)
    if client is None:
        print(
            "Email not configured. Set MAESTRO_EMAIL_ADDRESS and "
            "MAESTRO_EMAIL_PASSWORD environment variables."
        )
        sys.exit(1)

    if args.email_command == "send":
        message_id = asyncio.run(client.send(args.to, args.subject, args.body))
        print(f"Sent. Message-ID: {message_id}")

    elif args.email_command == "inbox":
        # Reject a non-positive count at the boundary: fetch_inbox slices
        # uids[-count:], so -n 0 (uids[0:]) fetches the whole mailbox over IMAP.
        if args.count <= 0:
            print("Count must be a positive integer.")
            sys.exit(1)
        messages = asyncio.run(
            client.fetch_inbox(limit=args.count, unseen_only=not args.show_all),
        )
        if not messages:
            print("No emails found.")
            return
        for m in messages:
            seen_mark = " " if m.seen else "*"
            print(f"[{seen_mark}] UID={m.uid} | {m.date} | {m.from_addr}")
            print(f"    {m.subject}")

    elif args.email_command == "read":
        found = asyncio.run(client.fetch_by_uid(args.uid, mark_seen=True))
        if found is None:
            print(f"Email UID {args.uid} not found.")
            sys.exit(1)
        print(f"From: {found.from_addr}")
        print(f"Subject: {found.subject}")
        print(f"Date: {found.date}")
        print(f"Message-ID: {found.message_id}")
        print()
        print(found.body)

    else:
        print("Usage: maestro email {send|inbox|read}")
        sys.exit(1)


def cmd_self_model(args: argparse.Namespace, config: Config) -> None:
    """Export the raw self-model for hand curation, or import an edited one.

    ``export <file>`` dumps the raw, untruncated ``self_model.json`` so a
    curator can edit it without the load path shrinking oversized entries.
    ``import <file> [--dry-run]`` validates an edited file, reports every
    entry the caps would truncate or drop, and — unless ``--dry-run`` — writes
    the capped result back via ``Consciousness.import_self_model`` (which
    delegates capping to ``save_self_model``, the single cap owner).
    """
    consciousness = Consciousness(config.memory_dir)
    if args.self_model_command == "export":
        try:
            raw = consciousness.read_raw_self_model()
        except FileNotFoundError:
            print(
                "No self-model found at "
                f"{config.memory_dir / 'self_model.json'}.",
            )
            sys.exit(1)
        except ValueError as exc:
            # read_raw_self_model raises ValueError for malformed JSON
            # (JSONDecodeError subclasses it) or a non-object payload —
            # surface a friendly message symmetric with import rather than a
            # bare traceback.
            print(
                "Cannot export — "
                f"{config.memory_dir / 'self_model.json'} is corrupt or not "
                f"a JSON object: {exc}",
            )
            sys.exit(1)
        out_path = Path(args.file)
        atomic_write(out_path, json_mod.dumps(raw, indent=2))
        print(f"Exported raw self-model to {out_path}")

    elif args.self_model_command == "import":
        in_path = Path(args.file)
        try:
            text = in_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"File not found: {in_path}")
            sys.exit(1)
        try:
            parsed = json_mod.loads(text)
        except json_mod.JSONDecodeError as exc:
            print(f"Invalid JSON in {in_path}: {exc}")
            sys.exit(1)
        if not isinstance(parsed, dict):
            print(
                f"{in_path} must contain a JSON object, not "
                f"{type(parsed).__name__}.",
            )
            sys.exit(1)
        changes = Consciousness.preview_cap_changes(parsed)
        if changes:
            print("The caps would change the following on import:")
            for line in changes:
                print(f"  - {line}")
        else:
            print("No cap changes — the file round-trips cleanly.")
        if args.dry_run:
            print("Dry run — self_model.json was not modified.")
            return
        consciousness.import_self_model(parsed)
        print("Imported self-model — caps applied and written.")

    else:
        print("Usage: maestro self-model {export|import}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="maestro",
        description="Maestro — self-sustaining autonomous agent",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Execute a goal")
    p_run.add_argument("goal")
    p_run.add_argument("-c", "--context", default="")

    # resume-task
    p_resume = sub.add_parser(
        "resume-task", help="Resume a checkpointed task by id",
    )
    p_resume.add_argument("task_id")

    # contemplate
    sub.add_parser("contemplate", help="Pure introspection")

    # evolve
    sub.add_parser("evolve", help="Trigger evolution cycle")

    # status
    sub.add_parser("status", help="Show identity, values, open questions and plan stats")

    # show-plan
    p_show_plan = sub.add_parser(
        "show-plan",
        help="Print one evolution plan's full detail by id (read-only)",
    )
    p_show_plan.add_argument(
        "plan_id",
        help="The id of the evolution plan to inspect",
    )
    p_show_plan.add_argument(
        "--json", action="store_true",
        help="Emit the plan as a JSON object instead of the human-readable block",
    )

    # journal
    p_journal = sub.add_parser("journal", help="Read journal entries")
    p_journal.add_argument("-n", "--count", type=int, default=None)
    p_journal.add_argument("-t", "--type", default=None)
    p_journal.add_argument(
        "--events", action="store_true",
        help="Read operational records (events.jsonl) instead of inner thoughts",
    )

    # history
    p_history = sub.add_parser("history", help="View task history")
    p_history.add_argument("-n", "--count", type=int, default=None)
    p_history.add_argument(
        "--outcome", default=None,
        choices=["success", "partial", "failure"],
        help="Filter by task outcome",
    )
    p_history.add_argument(
        "--source", default=None,
        choices=["external", "self_directed"],
        help="Filter by task source",
    )
    p_history.add_argument(
        "--json", action="store_true",
        help="Output full TaskRecord data as JSON array",
    )

    # measure-noops
    p_noops = sub.add_parser(
        "measure-noops", help="Measure no-op task rate from history",
    )
    p_noops.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recent tasks (default: all)",
    )

    # measure-gate-triggers
    p_gate_triggers = sub.add_parser(
        "measure-gate-triggers",
        help="Measure gate activation counts from the gate-activation ledger "
             "(state.db under [paths] sqlite_state_enabled, else "
             "gate_activations.jsonl)",
    )
    p_gate_triggers.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recent records (default: 100)",
    )

    # measure-premise-accuracy
    p_premise = sub.add_parser(
        "measure-premise-accuracy",
        help="Measure task-generation file-reference (premise) accuracy from "
             "the premise ledger (state.db under [paths] sqlite_state_enabled, "
             "else premise_observations.jsonl)",
    )
    p_premise.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recent records (default: 100)",
    )

    # consciousness-report
    p_consciousness = sub.add_parser(
        "consciousness-report",
        help="Print one measured reading per functional property of "
             "consciousness — identity availability, perceptual grounding, "
             "metacognitive calibration, narration→actuation conversion "
             "(read-only, offline)",
    )
    p_consciousness.add_argument(
        "--json", action="store_true",
        help="Emit the report as a JSON object",
    )

    # workspace-counterfactual. MAX_REPEATS is read from its owner rather than
    # re-spelled in the help text, so the advertised bound cannot drift from the
    # bound the command actually enforces.
    from maestro.counterfactual_probe import MAX_REPEATS

    p_counterfactual = sub.add_parser(
        "workspace-counterfactual",
        help="Run the interventional-sensitivity probe: matched tool-less model "
             "calls over a conscious moment with and without its decisive item "
             "(reaches the provider; mutates nothing)",
    )
    p_counterfactual.add_argument(
        "--repeats", type=int, default=1,
        help=f"Matched pairs to run per scenario, clamped to 1-{MAX_REPEATS} "
             "(default: 1); each repeat bills two provider calls per scenario",
    )
    p_counterfactual.add_argument(
        "--scenario", default=None,
        help="Run only the named scenario id (default: the whole pack)",
    )
    p_counterfactual.add_argument(
        "--json", action="store_true",
        help="Emit the report as a JSON object",
    )

    # measure-dead-on-arrival-mints
    p_doa = sub.add_parser(
        "measure-dead-on-arrival-mints",
        help="Measure how many recently-minted plans were obsoleted at the "
             "submission door (read-only)",
    )
    p_doa.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recently created plans (default: all plans)",
    )
    p_doa.add_argument(
        "--json", action="store_true",
        help="Emit the report as a JSON object",
    )

    # measure-rejection-classes
    p_rej = sub.add_parser(
        "measure-rejection-classes",
        help="Count recent human rejections reading already-shipped, "
             "false-premise, or not-needed (read-only)",
    )
    p_rej.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recent rejections (default: all)",
    )
    p_rej.add_argument(
        "--json", action="store_true",
        help="Emit the report as a JSON object",
    )

    # measure-planning-grounding
    p_planning_grounding = sub.add_parser(
        "measure-planning-grounding",
        help="Measure the tree-checkable share of human plan rejections and "
             "planner read coverage (read-only)",
    )
    p_planning_grounding.add_argument(
        "--since", type=_iso_date_arg, default=None,
        help="Only plans created on or after this date (YYYY-MM-DD)",
    )
    p_planning_grounding.add_argument(
        "--json", action="store_true",
        help="Emit the report as a JSON object",
    )

    # reality-inventory
    p_reality = sub.add_parser(
        "reality-inventory",
        help="Show shipped capabilities and measured problems (read-only)",
    )
    p_reality.add_argument(
        "--json", action="store_true",
        help="Emit the inventory as a JSON object",
    )

    # source-budget
    p_source_budget = sub.add_parser(
        "source-budget",
        help="Show source files at or above the split-due size line (read-only)",
    )
    p_source_budget.add_argument(
        "--json", action="store_true",
        help="Emit the thresholds and files as a JSON object",
    )

    # runtime-contract
    p_runtime_contract = sub.add_parser(
        "runtime-contract",
        help="Check the environment against deploy/runtime_contract.toml "
             "(read-only, exits 1 on a required violation)",
    )
    p_runtime_contract.add_argument(
        "--json", action="store_true",
        help="Emit the verdict as a JSON object",
    )
    p_runtime_contract.add_argument(
        "--network", action="store_true",
        help="Resolve launcher packages against their registries "
             "(otherwise reported unverified)",
    )

    # tool-surface-audit
    p_tool_surface = sub.add_parser(
        "tool-surface-audit",
        help="Measure per-caller tool-surface drift over SDK transcript "
             "sidecars (read-only)",
    )
    p_tool_surface.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N newest date dirs (default: every retained dir)",
    )
    p_tool_surface.add_argument(
        "--json", action="store_true",
        help="Emit the report as a JSON object",
    )

    # measure-gate-contention
    p_gate_contention = sub.add_parser(
        "measure-gate-contention",
        help="Measure gate-run wall time by observed concurrency, budget fit "
             "per granted width, plus the resource-artifact, fallback and "
             "declined rates from gate_runs.jsonl (read-only)",
    )
    p_gate_contention.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recent gate runs (default: 100)",
    )
    p_gate_contention.add_argument(
        "--json", action="store_true",
        help="Emit the report as a JSON object",
    )

    # audit-flags
    p_audit_flags = sub.add_parser(
        "audit-flags",
        help="Audit the experimental-flag manifest against recent gate "
             "activations (read-only)",
    )
    p_audit_flags.add_argument(
        "--last", type=int, default=None,
        help="Limit the activation window to the N most recent records "
             "(default: 100)",
    )
    p_audit_flags.add_argument(
        "--json", action="store_true",
        help="Emit the audit rows as a JSON list of objects instead of the "
             "human table",
    )

    # measure-fork-authorship
    sub.add_parser(
        "measure-fork-authorship",
        help="Measure self-authored vs externally-grounded fork breakdown",
    )

    # list-gates
    sub.add_parser(
        "list-gates",
        help="Print the generate_task gate chain in precedence order",
    )

    # lexicon
    p_lexicon = sub.add_parser(
        "lexicon",
        help="Print the coined-term lexicon (docs/lexicon.md)",
    )
    # Three read-only views over the same coinage source; no flag keeps the
    # historical table print byte-identical. Mirrors ``p_burn_baseline``'s
    # mutually-exclusive sub-mode registration.
    lexicon_view = p_lexicon.add_mutually_exclusive_group()
    lexicon_view.add_argument(
        "--list", action="store_true",
        help="Print the lexicon table (the default view, made explicit)",
    )
    lexicon_view.add_argument(
        "--undefined", action="store_true",
        help="List high-frequency journal coinages absent from the lexicon",
    )
    lexicon_view.add_argument(
        "--lint", action="store_true",
        help="Report CLAUDE.md variant spellings where a canonical term is defined",
    )
    lexicon_view.add_argument(
        "--tag-map", action="store_true",
        help="Render the behavioral-sensor tag map from the lexicon (paste-ready)",
    )

    # gate-check
    p_gate_check = sub.add_parser(
        "gate-check",
        help="Run ruff+mypy+pytest and exit with the aggregate quality-gate code",
    )
    p_gate_check.add_argument(
        "--json",
        action="store_true",
        help="Emit per-gate exit codes and aggregate as machine-readable JSON",
    )
    p_gate_check.add_argument(
        "--full",
        action="store_true",
        help="Also run the sibling-package gate sets (maestro_review/ and "
             "maestro_sdk/ ruff+mypy+pytest), mirroring CI's package-quality job",
    )

    # gate-scope (proposal 134) — which test modules a changed diff can reach.
    p_gate_scope = sub.add_parser(
        "gate-scope",
        help="Show the test modules a set of changed source files can reach, "
             "and what fraction of the suite they are (read-only)",
    )
    p_gate_scope.add_argument(
        "path", nargs="*", default=None,
        help="A changed repo-relative source path; repeatable",
    )
    p_gate_scope.add_argument(
        "--plan", default=None,
        help="Read the changed paths from this plan's files_affected instead",
    )
    p_gate_scope.add_argument(
        "--json", action="store_true",
        help="Emit the selection as a JSON object",
    )

    # gate-classify (proposal 126) — turn a captured gate run into one token.
    p_gate_classify = sub.add_parser(
        "gate-classify",
        help="Classify a captured gate run's output (stdin) into one outcome token",
    )
    p_gate_classify.add_argument(
        "--tool", choices=["ruff", "mypy", "tests"], required=True,
        help="Which gate produced the captured output",
    )
    p_gate_classify.add_argument(
        "--returncode", type=int, required=True,
        help="The gate runner's exit status (0 is CLEAN; non-zero is enumerated)",
    )
    p_gate_classify.add_argument(
        "--kind", choices=["TIMEOUT", "UNAVAILABLE"], default=None,
        help="Classify as this non-answer instead of parsing the return code",
    )

    # gate-lease (proposal 126) — run a gate command under a host-execution lease.
    p_gate_lease = sub.add_parser(
        "gate-lease",
        help="Run a gate command under a host-execution lease, or probe the flag",
    )
    p_gate_lease.add_argument(
        "--enabled", action="store_true",
        help="Exit 0 iff [loop] gate_lease_enabled is on, taking no slot",
    )
    # dest is ``lease_command`` (not ``command``) so this positional does not
    # shadow the top-level subparser ``dest="command"`` that main() dispatches on.
    p_gate_lease.add_argument(
        "lease_command", nargs=argparse.REMAINDER, metavar="command",
        help="The gate command to run after --; its {worker_args} token is spliced",
    )

    # cost-report
    p_cost_report = sub.add_parser(
        "cost-report",
        help="Summarize usage.jsonl cost data by date and provider",
    )
    p_cost_report.add_argument(
        "-d", "--days", type=int, default=7,
        help="Number of trailing UTC days for the per-date breakdown (default: 7)",
    )
    p_cost_report.add_argument(
        "--json", action="store_true",
        help="Emit the per-date/per-provider aggregates and grand total as "
             "a single machine-readable JSON object instead of the text table",
    )
    p_cost_report.add_argument(
        "--by-caller", action="store_true",
        help="Also report the window's spend per caller, overall and per "
             "loop iteration (a caller_spend key under --json)",
    )

    # list-versions
    p_list_versions = sub.add_parser(
        "list-versions",
        help="List merged VersionRecords from the version archive (state.db "
             "under [paths] sqlite_state_enabled, else version_archive.jsonl)",
    )
    p_list_versions.add_argument(
        "-n", "--limit", type=int, default=30,
        help="Maximum number of most-recent merged versions to show (default: 30)",
    )

    # cost-per-task
    p_cost_per_task = sub.add_parser(
        "cost-per-task",
        help="Report a single task's accrued USD cost from usage.jsonl",
    )
    p_cost_per_task.add_argument(
        "task_id",
        help="The task_id whose accrued cost to report",
    )

    # burn-baseline
    p_burn_baseline = sub.add_parser(
        "burn-baseline",
        help=(
            "Print the proposal-084 burn-rate baseline: $/task by task-shape "
            "plus the prompt-cache hit rate (from usage.jsonl + task_history.jsonl)"
        ),
    )
    # --since and --compare are mutually exclusive: --since filters to a single
    # window, --compare splits the corpus about a cutoff into before/after.
    burn_window = p_burn_baseline.add_mutually_exclusive_group()
    burn_window.add_argument(
        "--since", type=str, default=None, metavar="DATE",
        help=(
            "Filter to usage rows at or after DATE (YYYY-MM-DD or "
            "YYYY-MM-DDTHH:MM:SS) for a single-window before/after comparison"
        ),
    )
    burn_window.add_argument(
        "--compare", type=str, default=None, metavar="DATE",
        help=(
            "Split the usage corpus at DATE (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS) "
            "and render one before/after/Δ table — the flag-flip A/B in one run"
        ),
    )

    # measure-false-success
    p_false_success = sub.add_parser(
        "measure-false-success",
        help="Measure PRs stamped success >=2x without closing",
    )
    p_false_success.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recent tasks (default: all)",
    )

    # measure-outcome-trends
    p_outcome_trends = sub.add_parser(
        "measure-outcome-trends",
        help="Measure success/partial/investigation rate trends from history",
    )
    p_outcome_trends.add_argument(
        "-n", "--window", type=int, default=20,
        help="Window of most-recent tasks to split into halves (default: 20)",
    )

    # measure-handoff-relapse
    p_handoff_relapse = sub.add_parser(
        "measure-handoff-relapse",
        help="Measure recent tasks re-engaging an already-handed-off PR lineage",
    )
    p_handoff_relapse.add_argument(
        "-n", "--window", type=int, default=50,
        help="Window of most-recent tasks to scan (default: 50)",
    )

    # measure-relap
    p_relap = sub.add_parser(
        "measure-relap",
        help="Measure settled-relap rate (verdict re-verification) among no-ops",
    )
    p_relap.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recent tasks (default: all)",
    )

    # measure-confirmed-landings
    p_confirmed_landings = sub.add_parser(
        "measure-confirmed-landings",
        help="Measure confirmed-vs-caused landing rate among success tasks",
    )
    p_confirmed_landings.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recent tasks (default: all)",
    )

    # measure-phantom-landings
    p_phantom_landings = sub.add_parser(
        "measure-phantom-landings",
        help="Measure phantom-landing divergence rate (shadow mechanical verdict)",
    )
    p_phantom_landings.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recent shadow records (default: 100)",
    )

    # measure-plan-mill
    p_plan_mill = sub.add_parser(
        "measure-plan-mill",
        help="Measure evolution-plan theme saturation and productivity",
    )
    p_plan_mill.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most-recently-created plans (default: all)",
    )
    p_plan_mill.add_argument(
        "--json", action="store_true",
        help="Emit the four metrics as a single JSON object",
    )

    # pr-laps
    p_pr_laps = sub.add_parser(
        "pr-laps",
        help="Show per-PR remediation lap counts and active standdowns",
    )
    p_pr_laps.add_argument(
        "-n", "--window", type=int, default=None,
        help="Window of most-recent tasks to scan (default: 50)",
    )

    # pr-lap-ledger
    p_pr_lap_ledger = sub.add_parser(
        "pr-lap-ledger",
        help="Aggregate remediation-lap waste per owned PR lineage (read-only)",
    )
    p_pr_lap_ledger.add_argument(
        "--last", type=int, default=None,
        help="Limit to the N most recent tasks (default: all)",
    )
    p_pr_lap_ledger.add_argument(
        "--json", action="store_true",
        help="Emit the per-PR ledger as a JSON list of objects instead of "
             "the human table",
    )

    # deed-ledger
    p_deed_ledger = sub.add_parser(
        "deed-ledger",
        help="Tail the record of fired terminal acts (read-only)",
    )
    p_deed_ledger.add_argument(
        "-n", "--count", type=int, default=None,
        help="Number of most-recent deeds to show (default: 20)",
    )
    p_deed_ledger.add_argument(
        "--json", action="store_true",
        help="Emit the deeds as a JSON list of objects instead of the human "
             "table",
    )

    # owned-acts
    p_owned_acts = sub.add_parser(
        "owned-acts",
        help="Render my chosen terminal dispositions with lineage and a "
             "chosen-vs-forced flag (read-only)",
    )
    p_owned_acts.add_argument(
        "-n", "--count", type=int, default=None,
        help="Number of most-recent owned acts to show (default: 20)",
    )
    p_owned_acts.add_argument(
        "--json", action="store_true",
        help="Emit the owned acts as a JSON array of objects instead of the "
             "human table",
    )

    # list-proposals
    p_list_proposals = sub.add_parser(
        "list-proposals",
        help="List multi-PR evolution proposals and their ordered slices "
             "with per-slice status (read-only, offline)",
    )
    p_list_proposals.add_argument(
        "--json", action="store_true",
        help="Emit the proposals and their slices as a JSON list of objects "
             "instead of the human table",
    )

    # pr-owed-act
    sub.add_parser(
        "pr-owed-act",
        help="Print one owed-act verdict (code-owed / handoff-ready / wait-ci / "
             "external-blocked) per open own PR (read-only)",
    )

    # list-limitations
    p_list_limitations = sub.add_parser(
        "list-limitations",
        help="List known limitations and whether each has a mirrored "
             "[limitation] open question (read-only)",
    )
    p_list_limitations.add_argument(
        "--json", action="store_true",
        help="Emit the limitations as a JSON array of objects instead of the "
             "human table",
    )

    # reviews
    p_reviews = sub.add_parser(
        "reviews", help="View artifact review verdicts",
    )
    p_reviews.add_argument("-n", "--count", type=int, default=None)
    p_reviews.add_argument(
        "--verdict", default=None,
        choices=["publish", "improve", "reuse", "skip"],
        help="Filter by verdict",
    )
    p_reviews.add_argument(
        "--json", action="store_true",
        help="Output full ArtifactReview data as JSON array",
    )

    # publications
    p_publications = sub.add_parser(
        "publications",
        help="View publication proposals (proposal 032)",
    )
    p_publications.add_argument("-n", "--count", type=int, default=None)
    p_publications.add_argument(
        "--status", default=None,
        choices=[
            "pending_approval", "approved", "rejected",
            "executing", "published", "failed",
        ],
        help="Filter by proposal status",
    )
    p_publications.add_argument(
        "--json", action="store_true",
        help="Output full PublicationProposal data as JSON array",
    )

    # docs
    p_docs = sub.add_parser(
        "docs",
        help="View historical document-proposal records (retired pipeline)",
    )
    p_docs.add_argument("-n", "--count", type=int, default=None)
    p_docs.add_argument(
        "--type", dest="doc_type", default=None,
        choices=[
            "core", "security", "publication", "audience", "income",
            "research", "ux", "technical_plan", "operations",
        ],
        help="Filter by doc category",
    )
    p_docs.add_argument(
        "--status", default=None,
        choices=["pending", "pr_opened", "skipped", "failed"],
        help="Filter by document record status",
    )
    p_docs.add_argument(
        "--task", default=None,
        help="Filter to records that cite this task_id",
    )
    p_docs.add_argument(
        "--json", action="store_true",
        help="Output full DocumentRecord data as JSON array",
    )

    # serve
    p_serve = sub.add_parser("serve", help="Start web dashboard")
    p_serve.add_argument("--port", type=int, default=None)

    # loop
    p_loop = sub.add_parser("loop", help="Continuous autonomous mode")
    p_loop.add_argument("--interval", type=int, default=None)
    p_loop.add_argument("--max-iters", type=int, default=0)
    p_loop.add_argument("--evolve-every", type=int, default=None)
    p_loop.add_argument("--task-every", type=int, default=None)
    p_loop.add_argument("--dream-every", type=int, default=None)
    p_loop.add_argument("--audit-self-every", type=int, default=None)
    p_loop.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "Run a fast deploy-validation iteration with the same SDK "
            "settings as production. Skips respond_to_messages, "
            "implement_approved_plans, check_merged_plans, "
            "process_rejected_plans, generate_task, evolve, and dream."
        ),
    )

    # messages
    p_messages = sub.add_parser("messages", help="Show recent messages")
    p_messages.add_argument("-n", "--count", type=int, default=None)

    # issues
    sub.add_parser("issues", help="List open GitHub issues")

    # logs
    p_logs = sub.add_parser("logs", help="View Maestro logs")
    p_logs.add_argument("--sdk", action="store_true", help="Show SDK session logs")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    p_logs.add_argument("-n", "--lines", type=int, default=None, help="Number of lines")
    p_logs.add_argument("--date", default=None, help="Filter SDK logs by date (YYYY-MM-DD)")
    p_logs.add_argument("--session", default=None, help="Show specific SDK session")
    p_logs.add_argument("--list", action="store_true", dest="list_sessions",
                        help="List SDK sessions")

    # preflight
    sub.add_parser("preflight", help="Validate config, state, and imports")

    # shutdown
    p_shutdown = sub.add_parser("shutdown", help="Gracefully stop a running Maestro process")
    p_shutdown.add_argument("--dashboard", action="store_true", help="Target the dashboard process")
    p_shutdown.add_argument("--all", action="store_true", dest="all_processes",
                            help="Target both loop and dashboard processes")

    # kill
    p_kill = sub.add_parser("kill", help="Immediately kill a running Maestro process")
    p_kill.add_argument("--dashboard", action="store_true", help="Target the dashboard process")
    p_kill.add_argument("--all", action="store_true", dest="all_processes",
                        help="Target both loop and dashboard processes")

    # notify-pre-deploy
    p_predeploy = sub.add_parser(
        "notify-pre-deploy",
        help="Notify the running loop that a deploy is about to start",
    )
    p_predeploy.add_argument(
        "--ref", default=None,
        help="New commit SHA being deployed",
    )
    p_predeploy.add_argument(
        "--from-ref", dest="from_ref", default=None,
        help="Previously deployed commit SHA; enables PR auto-discovery",
    )
    p_predeploy.add_argument(
        "--pr", type=int, action="append", default=None,
        help="Explicit PR number to include (repeatable)",
    )
    p_predeploy.add_argument(
        "--run-id", dest="run_id", default=None,
        help="Deploy run identifier (e.g. GitHub Actions run ID)",
    )

    # email
    p_email = sub.add_parser("email", help="Send and receive emails")
    email_sub = p_email.add_subparsers(dest="email_command")
    p_email_send = email_sub.add_parser("send", help="Send an email")
    p_email_send.add_argument("to", help="Recipient email address")
    p_email_send.add_argument("subject", help="Email subject")
    p_email_send.add_argument("body", help="Email body text")
    p_email_inbox = email_sub.add_parser("inbox", help="List inbox emails")
    p_email_inbox.add_argument("-n", "--count", type=int, default=20)
    p_email_inbox.add_argument("--all", action="store_true", dest="show_all",
                               help="Show all emails, not just unseen")
    p_email_read = email_sub.add_parser("read", help="Read a specific email")
    p_email_read.add_argument("uid", help="Email UID to read")

    # self-model — export/import the raw self-model for safe hand curation
    p_self_model = sub.add_parser(
        "self-model",
        help="Export/import the raw self-model for safe hand curation",
    )
    self_model_sub = p_self_model.add_subparsers(dest="self_model_command")
    p_sm_export = self_model_sub.add_parser(
        "export", help="Dump the raw, untruncated self-model to a file",
    )
    p_sm_export.add_argument(
        "file", help="Output path for the raw self-model JSON",
    )
    p_sm_import = self_model_sub.add_parser(
        "import",
        help="Validate and write an edited self-model back (caps applied)",
    )
    p_sm_import.add_argument("file", help="Path to the edited self-model JSON")
    p_sm_import.add_argument(
        "--dry-run", action="store_true",
        help="Report cap changes without writing self_model.json",
    )

    # Hidden hook entrypoint (proposal 066): the claude CLI child spawned by
    # the cli_paced provider invokes this as its PreToolUse Bash hook. No
    # ``help=`` keeps it out of the help table — it is machine-invoked, never
    # an operator surface.
    sub.add_parser(GATE_SUBCOMMAND)

    # Hidden launcher entrypoint (proposal 076): the same cli_paced-spawned
    # claude child invokes ``maestro _mcp-serve <server>`` to host one logical
    # introspection server over stdio MCP. No ``help=`` hides it from the help
    # table — machine-invoked, mirroring the gate hook above.
    p_mcp_serve = sub.add_parser(MCP_SERVE_SUBCOMMAND)
    p_mcp_serve.add_argument("server")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    # Applied after basicConfig and regardless of --verbose: a DEBUG root would
    # otherwise log the Telegram bot token inside httpx request URLs.
    quiet_http_client_loggers()

    # Gate-hook dispatch runs before Config.from_env(): the hook must answer
    # from stdin + environment alone. A config-load failure here would exit
    # non-zero, which the claude CLI treats as a *non-blocking* hook error —
    # i.e. fail-open — the one thing the action-gate must never do.
    if args.command == GATE_SUBCOMMAND:
        sys.exit(run_gate_bash_hook(sys.stdin, sys.stdout, os.environ))

    config = Config.from_env()

    # Hidden launcher dispatch (proposal 076): host one introspection server
    # over stdio MCP. It needs the resolved data dir (hence after
    # Config.from_env) but none of the provider/SDK machinery below — it serves
    # tool handlers, it issues no queries — so it dispatches and exits here.
    if args.command == MCP_SERVE_SUBCOMMAND:
        sys.exit(run_mcp_serve(args.server, config))

    # Runtime contract (proposal 141): an environment report must not be masked
    # by a broken provider config or missing secrets, so it dispatches ahead of
    # configure_provider / check_secrets and exits here.
    if args.command == "runtime-contract":
        sys.exit(cmd_runtime_contract(args, config))

    # Set up file logging for commands that run the agent
    if args.command in _SDK_RUNNING_COMMANDS:
        config.ensure_dirs()
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            config.app_log_path,
            maxBytes=config.log_max_bytes,
            backupCount=config.log_backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger("maestro").addHandler(file_handler)

        from maestro.sdk import (
            configure_call_policy_resolution,
            configure_cost_tracking,
            configure_enforced_tool_surface,
            configure_sdk_concurrency,
            configure_sdk_fallback_model,
            configure_sdk_logging,
            configure_sdk_model,
            configure_sdk_pricing,
            configure_sdk_stream_idle_timeout,
            configure_sdk_task_budget,
            configure_sdk_transcripts,
            configure_sdk_warning_throttle,
        )
        from maestro.search import assert_fts5_available
        configure_sdk_logging(config.sdk_log_dir)
        configure_sdk_transcripts(
            config.structured_transcripts_enabled,
            config.transcript_content_max,
        )
        configure_sdk_model(config.sdk_model)
        configure_call_policy_resolution(
            config.sdk_call_policy_resolution_enabled
        )
        configure_enforced_tool_surface(
            config if config.sdk_enforced_tool_surface_enabled else None
        )
        configure_sdk_fallback_model(config.sdk_fallback_model or None)
        configure_sdk_task_budget(config.sdk_task_budget_tokens)
        configure_sdk_pricing(config.sdk_pricing)
        configure_cost_tracking(
            _build_cost_sample_backend(config), auth_method=Config.detect_billing_auth(),
        )
        assert_fts5_available()
        configure_sdk_warning_throttle(
            base_seconds=config.sdk_warning_throttle_base_seconds,
            hysteresis=config.sdk_warning_throttle_hysteresis,
            hold_threshold=config.sdk_warning_hold_threshold,
            hold_probe_interval=config.sdk_warning_hold_probe_interval,
            hold_min_utilization=config.sdk_warning_hold_min_utilization,
        )
        configure_sdk_concurrency(config.sdk_max_concurrent_queries)
        configure_sdk_stream_idle_timeout(
            config.sdk_stream_idle_timeout_seconds
            if config.sdk_stream_idle_timeout_seconds > 0
            else None,
        )

    # Provider selection (proposal 058) applies to every subcommand, not just the
    # loop-running ones above: a short-running CLI subcommand that touches the
    # seam (once any phase opts in) must see the operator's configured provider,
    # not the process default. configure_provider re-checks the name against the
    # registry and fails loud for any not-yet-registered provider, which stays
    # inert under the default "anthropic". get_active_provider is lazy, so the
    # default path runs today's exact AnthropicProvider with no maestro_sdk import.
    configure_provider(config.resolved_sdk_provider())

    # Per-phase provider routing (proposal 058 Step 6's runtime half; the
    # proposal 067 prerequisite). Installs the ``[sdk] <phase>_provider`` override
    # map so the ``sdk.py`` seam routes a phase's session to its override at query
    # time, not just at startup validation. Re-validates each override name against
    # the registry (catching a schema-valid-but-unregistered provider, e.g.
    # ``openai``), mirroring ``configure_provider``. Inert when the map is empty —
    # the default ``anthropic`` config never sets a per-phase override.
    configure_phase_providers(config.sdk_phase_providers)

    # cli_paced provider-specific pacing + idle config (proposal 056 Step 12).
    # Import (and thus pull in maestro_sdk) ONLY when cli_paced is the resolved or
    # a per-phase provider, so the default anthropic path never pays the import —
    # the same lazy posture as the registry's _cli_paced_factory. A 0 idle timeout
    # means "fall back to maestro_sdk's built-in deadline," matching the > 0
    # semantics the provider applies to per-call overrides.
    _selected_providers = {
        config.resolved_sdk_provider(),
        *config.sdk_phase_providers.values(),
    }
    if "cli_paced" in _selected_providers:
        from maestro.providers.cli_paced import (
            configure_cli_paced,
            resolve_marker_pack,
        )
        configure_cli_paced(
            cadence_overrides=config.sdk_cli_paced_cadence,
            stream_idle_timeout_seconds=(
                config.sdk_cli_paced_stream_idle_timeout_seconds
                if config.sdk_cli_paced_stream_idle_timeout_seconds > 0
                else None
            ),
            probe_metrics_enabled=config.sdk_cli_paced_probe_metrics_enabled,
            probe_metrics_every=config.sdk_cli_paced_probe_metrics_every,
            # Marker pack (proposal 075 Step 4): "none" → None (today's
            # behaviour), "auto" → version-probe + registry selection (WARNs
            # and proceeds pack-less when nothing covers), named → registry
            # lookup (already validated by Config.validate).
            marker_pack=resolve_marker_pack(config.sdk_cli_paced_marker_pack),
        )

    # Capability validation (proposal 058 Step 7) — fail loud at startup, before
    # any query, when a phase is routed to a provider that lacks a capability it
    # needs (e.g. an external-MCP phase on a remote_mcp=False provider, or a
    # long-context phase on a long_context=False provider). Inert under the
    # default "anthropic", which advertises every capability.
    validate_provider_capabilities(config)

    # Action-gate coverage (proposal 066) — the trust sibling of the capability
    # check: an action-gated phase (run_task / implementation) may only route
    # to a provider carrying the #2280 Bash mutation guard. Inert under the
    # default config; fails loud at config-load, never mid-task.
    validate_action_gate_coverage(config)

    handlers = {
        "run": cmd_run,
        "resume-task": cmd_resume_task,
        "contemplate": cmd_contemplate,
        "evolve": cmd_evolve,
        "status": cmd_status,
        "show-plan": cmd_show_plan,
        "journal": cmd_journal,
        "history": cmd_history,
        "measure-noops": cmd_measure_noops,
        "measure-gate-triggers": cmd_measure_gate_triggers,
        "measure-premise-accuracy": cmd_measure_premise_accuracy,
        "consciousness-report": cmd_consciousness_report,
        "workspace-counterfactual": cmd_workspace_counterfactual,
        "measure-dead-on-arrival-mints": cmd_measure_dead_on_arrival_mints,
        "measure-rejection-classes": cmd_measure_rejection_classes,
        "measure-planning-grounding": cmd_measure_planning_grounding,
        "reality-inventory": cmd_reality_inventory,
        "source-budget": cmd_source_budget,
        "measure-gate-contention": cmd_measure_gate_contention,
        "tool-surface-audit": cmd_tool_surface_audit,
        "audit-flags": cmd_audit_flags,
        "measure-fork-authorship": cmd_measure_fork_authorship,
        "list-gates": cmd_list_gates,
        "lexicon": cmd_lexicon,
        "gate-check": cmd_gate_check,
        "gate-scope": cmd_gate_scope,
        "gate-classify": cmd_gate_classify,
        "gate-lease": cmd_gate_lease,
        "cost-report": cmd_cost_report,
        "cost-per-task": cmd_cost_per_task,
        "list-versions": cmd_list_versions,
        "burn-baseline": cmd_burn_baseline,
        "measure-false-success": cmd_measure_false_success,
        "measure-outcome-trends": cmd_measure_outcome_trends,
        "measure-handoff-relapse": cmd_measure_handoff_relapse,
        "measure-relap": cmd_measure_relap,
        "measure-confirmed-landings": cmd_measure_confirmed_landings,
        "measure-phantom-landings": cmd_measure_phantom_landings,
        "measure-plan-mill": cmd_measure_plan_mill,
        "pr-laps": cmd_pr_laps,
        "pr-lap-ledger": cmd_pr_lap_ledger,
        "deed-ledger": cmd_deed_ledger,
        "owned-acts": cmd_owned_acts,
        "list-proposals": cmd_list_proposals,
        "pr-owed-act": cmd_pr_owed_act,
        "list-limitations": cmd_list_limitations,
        "reviews": cmd_reviews,
        "publications": cmd_publications,
        "docs": cmd_docs,
        "messages": cmd_messages,
        "issues": cmd_issues,
        "logs": cmd_logs,
        "serve": cmd_serve,
        "loop": cmd_loop,
        "preflight": cmd_preflight,
        "shutdown": cmd_shutdown,
        "kill": cmd_kill,
        "notify-pre-deploy": cmd_notify_pre_deploy,
        "email": cmd_email,
        "self-model": cmd_self_model,
    }

    if args.command in handlers:
        handlers[args.command](args, config)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
