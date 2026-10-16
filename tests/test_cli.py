"""Tests for CLI — argument parsing and non-async commands — fully mocked I/O."""

from __future__ import annotations

import argparse
import ast
import importlib.machinery
import io
import json
import logging
import re
import subprocess
import sys
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import gate_environment, model_profiles
from maestro.cli import (
    MCP_SERVE_SUBCOMMAND,
    _build_agent,
    _build_audit_history_backend,
    _build_conscious_episode_backend,
    _build_cost_sample_backend,
    _build_deed_ledger_backend,
    _build_escalation_ledger_backend,
    _build_gate_activation_backend,
    _build_journal_search_backend,
    _build_mcp_serve_registry,
    _build_pr_ledger_backend,
    _build_prediction_backend,
    _build_premise_backend,
    _build_question_resolution_backend,
    _build_rejection_history_backend,
    _build_settled_verdicts_backend,
    _build_stand_down_verdict_backend,
    _build_version_archive_backend,
    _format_duration,
    _measure_claim_grounding,
    _read_pid,
    _resolve_cli_auth_method,
    _resolve_mcp_serve_server,
    _serve_mcp_over_stdio,
    cmd_contemplate,
    cmd_evolve,
    cmd_issues,
    cmd_journal,
    cmd_kill,
    cmd_list_limitations,
    cmd_logs,
    cmd_messages,
    cmd_preflight,
    cmd_resume_task,
    cmd_run,
    cmd_self_model,
    cmd_show_plan,
    cmd_shutdown,
    cmd_status,
    run_mcp_serve,
)
from maestro.config import Config
from maestro.consciousness import Consciousness, SelfModel, claim_key
from maestro.journal import Journal
from maestro.memory import (
    EvolutionPlan,
    Memory,
    RejectionRecord,
    VersionRecord,
)
from tests.conftest import MockQueryResult, capture_stdout, connected_session

FAKE_DIR = Path("/fake/cli")


def _make_config(**overrides: object) -> Config:
    defaults: dict[str, object] = {
        "memory_dir": FAKE_DIR,
        "workspace": FAKE_DIR,
        # ``agent.run()`` calls ``create_session`` when reuse is on, which
        # spawns a real ClaudeSDKClient subprocess. CLI tests mock
        # ``query`` but not ``create_session``, so leaving reuse on causes
        # pytest to hang on a dead session. Match the convention in
        # conftest.make_agent.
        "enable_session_reuse": False,
        # ``check_secrets`` runs a ``gh auth status`` probe when this is
        # True; the conftest unit-test guard blocks unmocked ``gh`` calls,
        # so default off here and let any test that needs the probe path
        # opt back in (and mock ``subprocess.run``).
        "publication_enabled": False,
    }
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


# ── _build_agent ────────────────────────────────────────────────────


def test_build_agent(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    agent = _build_agent(config)
    assert agent is not None
    assert agent.is_running is False


def test_build_agent_without_telegram(mem_fs: dict[str, str]) -> None:
    """When telegram token is not set, NullNotifier is used."""
    config = _make_config()
    config.telegram_bot_token = None
    config.telegram_chat_id = "12345"
    agent = _build_agent(config)
    assert agent is not None


def test_build_agent_hands_agent_tools_to_agent(mem_fs: dict[str, str]) -> None:
    """``_build_agent`` threads the introspection registry into ``MaestroAgent``.

    The agent receives the same ``{"registry": ...}`` dict the executor got, so
    ``_build_contemplation_session_options`` can grant the bounded reflection
    surface behind the flag (proposal 132)."""
    agent = _build_agent(_make_config())
    assert agent._agent_tools is not None
    assert "registry" in agent._agent_tools
    # The very same object the executor received — one registry, not a copy.
    assert agent._agent_tools is agent.executor._agent_tools


def test_build_cost_sample_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) cost-sample backend is the JSONL store."""
    from maestro.cost_tracking import JsonlCostSampleStore

    backend = _build_cost_sample_backend(_make_config())
    assert isinstance(backend, JsonlCostSampleStore)


@patch("maestro.cli.StateStore")
def test_build_cost_sample_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite store.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteCostSampleStore

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_cost_sample_backend(config)
    assert isinstance(backend, SqliteCostSampleStore)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_deed_ledger_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) deed-ledger backend is the JSONL store."""
    from maestro.deed_ledger import JsonlDeedLedger

    backend = _build_deed_ledger_backend(_make_config())
    assert isinstance(backend, JsonlDeedLedger)


@patch("maestro.cli.StateStore")
def test_build_deed_ledger_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite deed ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteDeedLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_deed_ledger_backend(config)
    assert isinstance(backend, SqliteDeedLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_conscious_episode_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) conscious-episode backend is the JSONL store."""
    from maestro.conscious_workspace import JsonlConsciousEpisodeLedger

    backend = _build_conscious_episode_backend(_make_config())
    assert isinstance(backend, JsonlConsciousEpisodeLedger)


@patch("maestro.cli.StateStore")
def test_build_conscious_episode_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteConsciousEpisodeLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_conscious_episode_backend(config)
    assert isinstance(backend, SqliteConsciousEpisodeLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_gate_activation_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) gate-activation backend is the JSONL store."""
    from maestro.audit_checks import JsonlGateActivationLedger

    backend = _build_gate_activation_backend(_make_config())
    assert isinstance(backend, JsonlGateActivationLedger)


@patch("maestro.cli.StateStore")
def test_build_gate_activation_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite gate ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteGateActivationLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_gate_activation_backend(config)
    assert isinstance(backend, SqliteGateActivationLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_prediction_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) prediction backend is the JSONL store."""
    from maestro.audit_checks_calibration import (
        JsonlPredictionLedger,
    )

    backend = _build_prediction_backend(_make_config())
    assert isinstance(backend, JsonlPredictionLedger)


@patch("maestro.cli.StateStore")
def test_build_prediction_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite prediction ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqlitePredictionLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_prediction_backend(config)
    assert isinstance(backend, SqlitePredictionLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_premise_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) premise backend is the JSONL store."""
    from maestro.audit_checks_calibration import (
        JsonlPremiseLedger,
    )

    backend = _build_premise_backend(_make_config())
    assert isinstance(backend, JsonlPremiseLedger)


@patch("maestro.cli.StateStore")
def test_build_premise_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite premise ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqlitePremiseLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_premise_backend(config)
    assert isinstance(backend, SqlitePremiseLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_version_archive_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) version-archive backend is the JSONL store."""
    from maestro.memory import JsonlVersionArchiveLedger

    backend = _build_version_archive_backend(_make_config())
    assert isinstance(backend, JsonlVersionArchiveLedger)


@patch("maestro.cli.StateStore")
def test_build_version_archive_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite version-archive ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteVersionArchiveLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_version_archive_backend(config)
    assert isinstance(backend, SqliteVersionArchiveLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_journal_search_backend_ephemeral_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) journal-search backend is the ephemeral ranker."""
    from maestro.search import EphemeralJournalSearch

    backend = _build_journal_search_backend(_make_config())
    assert isinstance(backend, EphemeralJournalSearch)


@patch("maestro.cli.StateStore")
def test_build_journal_search_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the persistent index.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteJournalSearchIndex

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_journal_search_backend(config)
    assert isinstance(backend, SqliteJournalSearchIndex)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_pr_ledger_backend_json_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) per-PR-ledger backend is the JSON store."""
    from maestro.pr_ledger import JsonPRLedger

    backend = _build_pr_ledger_backend(_make_config())
    assert isinstance(backend, JsonPRLedger)


@patch("maestro.cli.StateStore")
def test_build_pr_ledger_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite per-PR ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqlitePRLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_pr_ledger_backend(config)
    assert isinstance(backend, SqlitePRLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_escalation_ledger_backend_json_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) escalation-standdown backend is the JSON store."""
    from maestro.escalation_bridge import JsonEscalationLedger

    backend = _build_escalation_ledger_backend(_make_config())
    assert isinstance(backend, JsonEscalationLedger)


@patch("maestro.cli.StateStore")
def test_build_escalation_ledger_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite escalation ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteEscalationLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_escalation_ledger_backend(config)
    assert isinstance(backend, SqliteEscalationLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_audit_history_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) audit-history backend is the JSONL store."""
    from maestro.memory import JsonlAuditHistoryLedger

    backend = _build_audit_history_backend(_make_config())
    assert isinstance(backend, JsonlAuditHistoryLedger)


@patch("maestro.cli.StateStore")
def test_build_audit_history_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite audit-history ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteAuditHistoryLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_audit_history_backend(config)
    assert isinstance(backend, SqliteAuditHistoryLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_rejection_history_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) rejection-history backend is the JSONL store."""
    from maestro.memory import JsonlRejectionHistoryLedger

    backend = _build_rejection_history_backend(_make_config())
    assert isinstance(backend, JsonlRejectionHistoryLedger)


@patch("maestro.cli.StateStore")
def test_build_rejection_history_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite rejection ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteRejectionHistoryLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_rejection_history_backend(config)
    assert isinstance(backend, SqliteRejectionHistoryLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_question_resolution_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) question-resolution backend is the JSONL store."""
    from maestro.memory import JsonlQuestionResolutionLedger

    backend = _build_question_resolution_backend(_make_config())
    assert isinstance(backend, JsonlQuestionResolutionLedger)


@patch("maestro.cli.StateStore")
def test_build_question_resolution_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite resolution ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteQuestionResolutionLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_question_resolution_backend(config)
    assert isinstance(backend, SqliteQuestionResolutionLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_stand_down_verdict_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) stand-down-verdict backend is the JSONL store."""
    from maestro.remediation_tracker import JsonlStandDownVerdictLedger

    backend = _build_stand_down_verdict_backend(_make_config())
    assert isinstance(backend, JsonlStandDownVerdictLedger)


@patch("maestro.cli.StateStore")
def test_build_stand_down_verdict_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite stand-down ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteStandDownVerdictLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_stand_down_verdict_backend(config)
    assert isinstance(backend, SqliteStandDownVerdictLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_build_settled_verdicts_backend_jsonl_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """The default (flag-off) settled-verdicts backend is the JSONL store."""
    from maestro.audit_checks_settlement import (
        JsonlSettledVerdictsLedger,
    )

    backend = _build_settled_verdicts_backend(_make_config())
    assert isinstance(backend, JsonlSettledVerdictsLedger)


@patch("maestro.cli.StateStore")
def test_build_settled_verdicts_backend_sqlite_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With ``sqlite_state_enabled`` on the backend is the SQLite settled-verdicts ledger.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the flag-driven class selection is asserted.
    """
    from maestro.state_store import SqliteSettledVerdictsLedger

    config = _make_config(sqlite_state_enabled=True)
    backend = _build_settled_verdicts_backend(config)
    assert isinstance(backend, SqliteSettledVerdictsLedger)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


# The dedicated notifier-construction warning (the one gated by warn_if_null).
# Distinct from check_secrets()'s "… not set — Telegram notifications disabled"
# secret-health lines, which fire for every command regardless of this flag.
_NOTIFIER_DISABLED_WARNING = (
    "Telegram notifications disabled: "
    "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not set"
)


def test_build_agent_without_telegram_suppresses_warning_by_default(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """Without warn_if_null, the notifier-disabled warning is suppressed (#204).

    Non-loop commands (run/contemplate/evolve/serve) build the agent with the
    default warn_if_null=False; the dedicated notifier warning is irrelevant
    noise for them and must not fire.
    """

    config = _make_config()
    config.telegram_bot_token = None
    config.telegram_chat_id = None
    with caplog.at_level(logging.WARNING, logger="maestro"):
        _build_agent(config)
    assert _NOTIFIER_DISABLED_WARNING not in caplog.text


def test_build_agent_warn_if_null_logs_warning(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """With warn_if_null=True (the loop path), the notifier-disabled warning
    fires when telegram credentials are missing."""

    config = _make_config()
    config.telegram_bot_token = None
    config.telegram_chat_id = None
    with caplog.at_level(logging.WARNING, logger="maestro"):
        _build_agent(config, warn_if_null=True)
    assert _NOTIFIER_DISABLED_WARNING in caplog.text


def test_build_agent_with_telegram(mem_fs: dict[str, str]) -> None:
    config = _make_config(
        telegram_bot_token="fake-token",
        telegram_chat_id="123",
    )
    with patch("maestro.notifications.telegram.TelegramNotifier") as mock_tg:
        _build_agent(config)
    mock_tg.assert_called_once_with(
        "fake-token", "123", max_message_length=config.telegram_max_length,
    )


def test_build_agent_calls_check_auth_only_once(
    mem_fs: dict[str, str],
) -> None:
    """_build_agent resolves check_auth() exactly once on the startup path.

    #280 removed the *explicit* second check_auth() call by logging via
    Config.auth_label(); #357 finished the job by threading the once-resolved
    method through both check_secrets() and auth_label() (via
    _resolve_cli_auth_method), so neither re-reads the auth env vars. Before
    #357 the anthropic path still resolved check_auth() twice — once inside
    check_secrets, once inside auth_label. This pins the single-read invariant.
    """
    from maestro.config import Config

    config = _make_config()
    call_count = 0

    original_check_auth = Config.check_auth

    def counting_check_auth() -> str:
        nonlocal call_count
        call_count += 1
        return original_check_auth()

    with patch.object(Config, "check_auth", staticmethod(counting_check_auth)):
        _build_agent(config)

    # #357: the CLI resolves check_auth() once (in _resolve_cli_auth_method) and
    # threads the method into both check_secrets() and auth_label(), so neither
    # re-resolves it. Exactly one auth-env read on the hot startup path.
    assert call_count == 1, "check_auth should be resolved exactly once on the startup path"


def test_resolve_cli_auth_method_returns_method_for_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The anthropic provider resolves check_auth() once so callers can reuse it (#357)."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _resolve_cli_auth_method("anthropic") == "oauth"


def test_resolve_cli_auth_method_returns_none_for_non_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-anthropic providers skip check_auth(), returning None (#357).

    check_auth() is anthropic-specific; resolving it eagerly for an openai or
    cli_paced provider would raise the wrong ('No Claude authentication found')
    SystemExit ahead of the real provider credential check. Clearing every
    anthropic credential proves the helper does not touch check_auth() here.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert _resolve_cli_auth_method("openai") is None
    assert _resolve_cli_auth_method("cli_paced") is None


def test_build_agent_non_anthropic_provider_surfaces_provider_message(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-anthropic provider keeps its own credential message first (#356).

    The eager top-level ``check_auth()`` is gated to the anthropic provider so a
    misconfigured ``openai`` provider with *both* the Anthropic creds and its
    own key missing surfaces the accurate ``OPENAI_API_KEY is not set`` message
    from ``check_secrets()`` — not the anthropic ``No Claude authentication
    found`` fallback that an unconditional top-level resolve would raise first.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = _make_config(sdk_provider="openai")
    with pytest.raises(SystemExit, match="OPENAI_API_KEY is not set"):
        _build_agent(config)


def test_build_agent_non_anthropic_provider_with_own_key_no_anthropic_crash(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid non-anthropic deployment builds without anthropic creds (#356 review).

    An ``openai`` provider whose OWN key is present but with the anthropic creds
    absent passes ``check_secrets()``; it must not then crash at the
    ``auth_label`` step with the misleading anthropic ``No Claude
    authentication found`` fallback, and must not read the anthropic auth env at
    all (``check_auth`` never fires for a non-anthropic provider).
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    config = _make_config(sdk_provider="openai")
    with patch.object(Config, "check_auth", wraps=Config.check_auth) as spy:
        agent = _build_agent(config)
    assert agent is not None
    spy.assert_not_called()


def test_build_agent_missing_mcp_launcher_raises_system_exit(
    mem_fs: dict[str, str],
) -> None:
    """An enabled external MCP server whose launcher is absent from PATH aborts
    startup with SystemExit, rather than crashing mid-loop on the first query."""
    config = _make_config(serena_mcp_enabled=True)

    def fake_which(cmd: str) -> str | None:
        return None if cmd == "uvx" else f"/usr/bin/{cmd}"

    with (
        patch("maestro.config.shutil.which", side_effect=fake_which),
        pytest.raises(SystemExit) as exc,
    ):
        _build_agent(config)
    assert "uvx" in str(exc.value)
    assert "serena" in str(exc.value)


def test_build_agent_resolved_launcher_does_not_raise(
    mem_fs: dict[str, str],
) -> None:
    """An enabled external MCP server whose launcher resolves on PATH proceeds
    past the launcher gate (no SystemExit)."""
    config = _make_config(serena_mcp_enabled=True)
    with patch("maestro.config.shutil.which", return_value="/usr/bin/uvx"):
        agent = _build_agent(config)
    assert agent is not None


def test_build_agent_refuses_own_login_in_d8(mem_fs: dict[str, str]) -> None:
    """Proposal 155 A4: a D8 list naming the bot's own login (any case) aborts."""
    config = _make_config(evolution_fuel_operator_github_logins=["thomas", "Maestro-Bot"])
    with (
        patch(
            "maestro.cli.SubprocessCodeHost.resolve_owner", return_value="maestro-bot",
        ),
        pytest.raises(SystemExit, match="maestro-bot"),
    ):
        _build_agent(config)


def test_build_agent_empty_d8_never_resolves_owner(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    with patch("maestro.cli.SubprocessCodeHost.resolve_owner") as resolve:
        assert _build_agent(config) is not None
    resolve.assert_not_called()


def test_build_agent_d8_unresolved_login_warns_and_proceeds(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    config = _make_config(evolution_fuel_operator_github_logins=["thomas"])
    with (
        patch("maestro.cli.SubprocessCodeHost.resolve_owner", return_value=""),
        caplog.at_level(logging.WARNING, logger="maestro"),
    ):
        assert _build_agent(config) is not None
    assert "could not resolve the loop's own GitHub login" in caplog.text


def test_build_agent_d8_without_own_login_proceeds(mem_fs: dict[str, str]) -> None:
    config = _make_config(evolution_fuel_operator_github_logins=["thomas"])
    with patch("maestro.cli.SubprocessCodeHost.resolve_owner", return_value="bot"):
        assert _build_agent(config) is not None


# ── _mcp-serve launcher (proposal 076 Step 1) ───────────────────────


@pytest.mark.parametrize(
    "server_name",
    ["journal", "self_awareness", "memory", "issues", "diagnostics"],
)
def test_resolve_mcp_serve_server_known(
    mem_fs: dict[str, str], server_name: str,
) -> None:
    """Each known logical server resolves to its in-process MCP server config."""
    cfg = _resolve_mcp_serve_server(server_name, _make_config())
    assert cfg is not None
    assert cfg["type"] == "sdk"
    assert cfg["name"] == server_name


def test_resolve_mcp_serve_server_unknown_returns_none(
    mem_fs: dict[str, str],
) -> None:
    """An unknown logical server resolves to None (no empty server hosted)."""
    assert _resolve_mcp_serve_server("not-a-server", _make_config()) is None


@pytest.mark.asyncio
async def test_mcp_serve_advertises_registry_tools(mem_fs: dict[str, str]) -> None:
    """Driven over an in-memory MCP session (the stdio seam, no real subprocess),
    a resolved server advertises exactly that server's registry tool names."""
    config = _make_config()
    expected = [t.name for t in _build_mcp_serve_registry(config)["journal"]]
    server_config = _resolve_mcp_serve_server("journal", config)
    assert server_config is not None
    async with connected_session(server_config["instance"]) as client:
        result = await client.list_tools()
    advertised = [t.name for t in result.tools]
    assert advertised == expected
    assert advertised, "the journal server must advertise at least one tool"


@pytest.mark.asyncio
async def test_mcp_serve_registry_threads_real_conscious_backend(
    mem_fs: dict[str, str],
) -> None:
    """The _mcp-serve registry wires a real conscious-episode backend, not None.

    Invoking ``read_conscious_moment`` over the (empty) JSONL ledger returns the
    not-recorded message — never the not-configured message a ``None`` backend
    would yield — so the production wiring is proven to thread a real backend.
    """
    registry = _build_mcp_serve_registry(_make_config())
    read_tool = next(
        t for t in registry["self_awareness"]
        if t.name == "read_conscious_moment"
    )
    result = await read_tool.handler({})
    assert result["text"] == "No conscious moment has been recorded yet."


def test_run_mcp_serve_unknown_server_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown server name exits non-zero with a one-line stderr error."""
    with patch("maestro.cli._resolve_mcp_serve_server", return_value=None):
        rc = run_mcp_serve("bogus", _make_config())
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown server 'bogus'" in err
    assert MCP_SERVE_SUBCOMMAND in err


def test_run_mcp_serve_known_server_serves() -> None:
    """A resolvable server is served over stdio and exits zero."""
    sentinel = {"type": "sdk", "name": "journal", "instance": object()}
    with patch(
        "maestro.cli._resolve_mcp_serve_server", return_value=sentinel,
    ), patch(
        "maestro.cli._serve_mcp_over_stdio", new=AsyncMock(),
    ) as mock_serve:
        rc = run_mcp_serve("journal", _make_config())
    assert rc == 0
    mock_serve.assert_awaited_once_with(sentinel)


@pytest.mark.asyncio
async def test_serve_mcp_over_stdio_runs_server_with_streams() -> None:
    """The serve coroutine binds the stdio streams into the server's run loop."""
    server = MagicMock()
    server.run = AsyncMock()
    server.create_initialization_options = MagicMock(return_value="INIT")

    @asynccontextmanager
    async def fake_stdio() -> Any:
        yield ("READ", "WRITE")

    with patch("mcp.server.stdio.stdio_server", fake_stdio):
        await _serve_mcp_over_stdio({"instance": server})

    server.run.assert_awaited_once_with("READ", "WRITE", "INIT")


def test_main_dispatches_mcp_serve_after_config_load(
    mem_fs: dict[str, str],
) -> None:
    """main() dispatches _mcp-serve after Config.from_env (it needs the resolved
    data dir) but before the provider/SDK machinery (it issues no queries)."""
    from maestro.cli import main

    config = _make_config()
    with patch("sys.argv", ["maestro", MCP_SERVE_SUBCOMMAND, "journal"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.run_mcp_serve", return_value=0) as mock_serve, \
         patch("maestro.cli.configure_provider") as mock_provider, \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    mock_serve.assert_called_once_with("journal", config)
    mock_provider.assert_not_called()


def test_quiet_http_client_loggers_raises_httpx_and_httpcore_to_warning() -> None:
    """httpx logs request URLs at INFO; a Telegram URL carries the bot token."""
    from maestro.cli import quiet_http_client_loggers

    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    saved = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
        quiet_http_client_loggers()
        assert loggers[0].level == logging.WARNING
        assert loggers[1].level == logging.WARNING
        assert not loggers[0].isEnabledFor(logging.INFO)
    finally:
        for logger, level in zip(loggers, saved, strict=True):
            logger.setLevel(level)


@pytest.mark.parametrize("verbose", [False, True])
def test_main_quiets_http_client_loggers(
    mem_fs: dict[str, str], verbose: bool,
) -> None:
    """main() quiets the HTTP client loggers under both root log levels."""
    from maestro.cli import main

    argv = ["maestro", MCP_SERVE_SUBCOMMAND, "journal"]
    if verbose:
        argv.insert(1, "--verbose")
    with patch("sys.argv", argv), \
         patch("maestro.cli.Config.from_env", return_value=_make_config()), \
         patch("maestro.cli.run_mcp_serve", return_value=0), \
         patch("maestro.cli.quiet_http_client_loggers") as mock_quiet, \
         pytest.raises(SystemExit):
        main()

    mock_quiet.assert_called_once_with()


# ── cmd_status ──────────────────────────────────────────────────────


def test_cmd_status(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    consciousness = Consciousness(config.memory_dir)
    model = consciousness.load_self_model()
    model.identity_statements = ["I am a test agent"]
    model.values = ["testing"]
    model.open_questions = ["Why test?"]
    consciousness.save_self_model(model)

    args = argparse.Namespace()
    output = capture_stdout(cmd_status, args, config)

    assert "Stage:" not in output
    assert "Evolutions: 0" in output
    assert "I am a test agent" in output
    assert "testing" in output
    assert "Why test?" in output
    assert "Purpose hypothesis" not in output
    assert "Leading purpose hypothesis" not in output


def test_cmd_status_auth_fallback(mem_fs: dict[str, str]) -> None:
    """auth_label() SystemExit is caught gracefully in cmd_status."""
    config = _make_config()
    config.ensure_dirs()

    args = argparse.Namespace()
    with patch.object(Config, "auth_label", side_effect=SystemExit("no auth")):
        output = capture_stdout(cmd_status, args, config)

    assert "Auth: not configured" in output


def test_cmd_status_does_not_swallow_runtime_error(mem_fs: dict[str, str]) -> None:
    """cmd_status must not swallow unexpected exceptions like RuntimeError from auth_label().

    Only SystemExit (the intentional 'not configured' signal) should be caught.
    """
    config = _make_config()
    config.ensure_dirs()

    args = argparse.Namespace()
    with patch.object(Config, "auth_label", side_effect=RuntimeError("unexpected env error")), \
         pytest.raises(RuntimeError, match="unexpected env error"):
        cmd_status(args, config)


def test_cmd_status_propagates_keyboard_interrupt(mem_fs: dict[str, str]) -> None:
    """BaseException subclasses like KeyboardInterrupt must not be swallowed."""
    config = _make_config()
    config.ensure_dirs()

    with patch.object(Config, "auth_label", side_effect=KeyboardInterrupt):
        args = argparse.Namespace()
        with pytest.raises(KeyboardInterrupt):
            cmd_status(args, config)


# ── cmd_show_plan ───────────────────────────────────────────────────


def _seed_show_plan(config: Config) -> None:
    """Seed a single inspectable evolution plan onto the mem-fs."""
    memory = Memory(config.memory_dir)
    memory.save_evolution_plan(EvolutionPlan(
        plan_id="plan-abc-123",
        title="Add show-plan CLI",
        category="capability",
        rationale="Operators need single-plan detail from the terminal.",
        proposed_changes=["Add cmd_show_plan", "Wire the subparser"],
        files_affected=["maestro/cli.py"],
        expected_impact="none",
        acceptance_criteria=["prints status", "unknown id exits nonzero"],
    ))


def test_cmd_show_plan_found_renders_fields(mem_fs: dict[str, str]) -> None:
    """show-plan <id> prints the plan's status, rationale, and criteria."""
    config = _make_config()
    config.ensure_dirs()
    _seed_show_plan(config)

    args = argparse.Namespace(plan_id="plan-abc-123", json=False)
    output = capture_stdout(cmd_show_plan, args, config)

    assert "plan-abc-123" in output
    assert "status: pending_review" in output
    assert "Operators need single-plan detail from the terminal." in output
    assert "unknown id exits nonzero" in output


def test_cmd_show_plan_json_is_parseable(mem_fs: dict[str, str]) -> None:
    """The --json variant emits json.loads-parseable output with the plan id."""
    config = _make_config()
    config.ensure_dirs()
    _seed_show_plan(config)

    args = argparse.Namespace(plan_id="plan-abc-123", json=True)
    output = capture_stdout(cmd_show_plan, args, config)

    data = json.loads(output)
    assert data["plan_id"] == "plan-abc-123"
    assert data["status"] == "pending_review"
    assert data["acceptance_criteria"] == [
        "prints status", "unknown id exits nonzero",
    ]


def test_cmd_show_plan_unknown_id_exits_nonzero(mem_fs: dict[str, str]) -> None:
    """An unknown plan id prints a not-found message and exits nonzero."""
    config = _make_config()
    config.ensure_dirs()
    _seed_show_plan(config)

    args = argparse.Namespace(plan_id="does-not-exist", json=False)
    with pytest.raises(SystemExit) as exc:
        cmd_show_plan(args, config)
    assert exc.value.code != 0


def test_cmd_show_plan_not_found_message(mem_fs: dict[str, str]) -> None:
    """The unknown-id branch surfaces a not-found message (no traceback)."""
    config = _make_config()
    config.ensure_dirs()
    _seed_show_plan(config)

    args = argparse.Namespace(plan_id="does-not-exist", json=False)
    output = capture_stdout(cmd_show_plan, args, config)
    assert "No evolution plan with id does-not-exist" in output


def test_cmd_show_plan_read_only(mem_fs: dict[str, str]) -> None:
    """cmd_show_plan performs zero state writes — the mem-fs is unchanged."""
    config = _make_config()
    config.ensure_dirs()
    _seed_show_plan(config)
    before = dict(mem_fs)

    args = argparse.Namespace(plan_id="plan-abc-123", json=False)
    capture_stdout(cmd_show_plan, args, config)

    assert mem_fs == before


def test_cmd_show_plan_empty_lists_render_none(mem_fs: dict[str, str]) -> None:
    """A plan with no changes/criteria renders explicit ``(none)`` markers."""
    config = _make_config()
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    memory.save_evolution_plan(EvolutionPlan(
        plan_id="plan-bare",
        title="Bare plan",
        category="capability",
        rationale="r",
        proposed_changes=[],
        files_affected=[],
        expected_impact="none",
        acceptance_criteria=[],
    ))

    args = argparse.Namespace(plan_id="plan-bare", json=False)
    output = capture_stdout(cmd_show_plan, args, config)

    assert "proposed_changes:\n  (none)" in output
    assert "acceptance_criteria:\n  (none)" in output


def test_show_plan_registered_in_handlers() -> None:
    """The show-plan subcommand is wired into the parser and handler map."""
    with patch.object(sys, "argv", ["maestro", "show-plan", "plan-abc-123"]), \
            patch("maestro.cli.cmd_show_plan") as mock_handler, \
            patch("maestro.cli.Config.from_env", return_value=_make_config()):
        from maestro.cli import main
        main()
    mock_handler.assert_called_once()


# ── cmd_list_limitations ────────────────────────────────────────────


def test_cmd_list_limitations_empty_state(mem_fs: dict[str, str]) -> None:
    """With no limitations recorded, an explicit message prints, exit 0."""
    config = _make_config()
    config.ensure_dirs()
    consciousness = Consciousness(config.memory_dir)
    model = consciousness.load_self_model()
    model.limitations_known = []
    consciousness.save_self_model(model)

    args = argparse.Namespace(json=False)
    output = capture_stdout(cmd_list_limitations, args, config)

    assert "No limitations recorded." in output


def test_cmd_list_limitations_mirror_flags(mem_fs: dict[str, str]) -> None:
    """Each limitation is flagged mirrored/unmirrored by open-question presence."""
    config = _make_config()
    config.ensure_dirs()
    consciousness = Consciousness(config.memory_dir)
    model = consciousness.load_self_model()
    model.limitations_known = ["Cannot fly", "Cannot swim"]
    # Only the first limitation has a mirrored [limitation] open question.
    model.open_questions = ["[limitation] Cannot fly"]
    consciousness.save_self_model(model)

    args = argparse.Namespace(json=False)
    output = capture_stdout(cmd_list_limitations, args, config)

    assert "[mirrored] Cannot fly" in output
    assert "-> [limitation] Cannot fly" in output
    assert "[unmirrored] Cannot swim" in output


def test_cmd_list_limitations_json_round_trip(mem_fs: dict[str, str]) -> None:
    """--json emits a parseable array with the mirror status per entry."""
    config = _make_config()
    config.ensure_dirs()
    consciousness = Consciousness(config.memory_dir)
    model = consciousness.load_self_model()
    model.limitations_known = ["Cannot fly", "Cannot swim"]
    model.open_questions = ["[limitation] Cannot fly"]
    consciousness.save_self_model(model)

    args = argparse.Namespace(json=True)
    output = capture_stdout(cmd_list_limitations, args, config)

    payload = json.loads(output)
    assert payload == [
        {
            "limitation": "Cannot fly",
            "mirrored": True,
            "open_question": "[limitation] Cannot fly",
        },
        {
            "limitation": "Cannot swim",
            "mirrored": False,
            "open_question": None,
        },
    ]


def test_cmd_list_limitations_skips_non_string_and_blank(
    mem_fs: dict[str, str],
) -> None:
    """Non-string and blank/whitespace limitation entries are skipped."""
    config = _make_config()
    config.ensure_dirs()
    consciousness = Consciousness(config.memory_dir)
    model = consciousness.load_self_model()
    # Bypass load-time coercion by injecting mixed entries directly; the
    # handler must defensively skip the non-string and whitespace-only rows.
    model.limitations_known = [123, "   ", "Cannot fly"]  # type: ignore[list-item]
    model.open_questions = []
    consciousness.save_self_model(model)
    # save_self_model filters non-strings on load; re-inject post-save so the
    # handler itself exercises the skip branches.
    reloaded = consciousness.load_self_model()
    reloaded.limitations_known = [123, "   ", "Cannot fly"]  # type: ignore[list-item]

    with patch.object(Consciousness, "load_self_model", return_value=reloaded):
        args = argparse.Namespace(json=False)
        output = capture_stdout(cmd_list_limitations, args, config)

    assert "[unmirrored] Cannot fly" in output
    assert "123" not in output


def test_list_limitations_registered_in_handlers() -> None:
    """The list-limitations subcommand is wired into parser and handler map."""
    with patch.object(sys, "argv", ["maestro", "list-limitations"]), \
            patch("maestro.cli.cmd_list_limitations") as mock_handler, \
            patch("maestro.cli.Config.from_env", return_value=_make_config()):
        from maestro.cli import main
        main()
    mock_handler.assert_called_once()


# ── cmd_journal ─────────────────────────────────────────────────────


def test_cmd_journal_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    args = argparse.Namespace(count=10, type=None, events=False)
    output = capture_stdout(cmd_journal, args, config)
    assert "empty" in output.lower()


def test_cmd_journal_with_entries(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    journal = Journal(config.memory_dir)
    journal.write_thought("My first thought", "reflection", triggered_by="agent_tool")
    journal.write_thought("A question", "question", triggered_by="agent_tool")

    args = argparse.Namespace(count=10, type=None, events=False)
    output = capture_stdout(cmd_journal, args, config)
    assert "My first thought" in output
    assert "A question" in output
    headers = [line for line in output.splitlines() if line.startswith("[")]
    assert headers[0].endswith(" | reflection]")
    assert headers[0].count("|") == 1


def test_cmd_journal_filter_type(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    journal = Journal(config.memory_dir)
    journal.write_thought("A reflection", "reflection", triggered_by="agent_tool")
    journal.write_thought("A question", "question", triggered_by="agent_tool")

    args = argparse.Namespace(count=10, type="question", events=False)
    output = capture_stdout(cmd_journal, args, config)
    assert "A question" in output
    assert "A reflection" not in output


def test_cmd_journal_events_reads_operational_records(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    journal = Journal(config.memory_dir)
    journal.write_thought("An inner thought", triggered_by="contemplation")
    journal.record_event("Shutting down.", entry_type="milestone", triggered_by="shutdown")
    journal.record_event("Audit finding", entry_type="audit_observation", triggered_by="audit_self_model")

    output = capture_stdout(
        cmd_journal, argparse.Namespace(count=10, type=None, events=True), config,
    )
    assert "Shutting down." in output
    assert "Audit finding" in output
    assert "An inner thought" not in output

    output = capture_stdout(
        cmd_journal, argparse.Namespace(count=10, type="milestone", events=True), config,
    )
    assert "Shutting down." in output
    assert "Audit finding" not in output


def test_cmd_journal_events_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    config.ensure_dirs()
    output = capture_stdout(
        cmd_journal, argparse.Namespace(count=10, type=None, events=True), config,
    )
    assert "No journal events." in output


def test_cmd_journal_count_zero_shows_nothing(mem_fs: dict[str, str]) -> None:
    """cmd_journal with count=0 should show no entries."""
    config = _make_config(memory_dir=Path("/fake/memory"), workspace=Path("/fake/ws"))
    j = Journal(Path("/fake/memory"))
    for i in range(3):
        j.write_thought(
            content=f"Entry {i}", entry_type="reflection",
            triggered_by="agent_tool",
        )

    args = argparse.Namespace(count=0, type="reflection", events=False)
    old = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        cmd_journal(args, config)
    finally:
        sys.stdout = old
    output = buf.getvalue()
    assert "Entry" not in output, (
        "count=0 should suppress all entries but they were printed"
    )


def test_cmd_journal_negative_count_does_not_say_empty(mem_fs: dict[str, str]) -> None:
    """--count -5 should not print 'Journal is empty' when journal has entries."""
    d = Path("/fake/journal_neg")
    config = Config(memory_dir=d, workspace=d)
    config.ensure_dirs()

    journal = Journal(d)
    journal.write_thought("test thought", triggered_by="agent_tool")

    args = argparse.Namespace(count=-5, type=None, events=False)
    old = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        cmd_journal(args, config)
    finally:
        sys.stdout = old

    output = buf.getvalue()
    assert "empty" not in output.lower()


def test_cmd_journal_invalid_type_shows_error(mem_fs: dict[str, str]) -> None:
    """cmd_journal with an invalid --type should show a helpful error,
    not silently return empty results."""
    config = _make_config()
    config.ensure_dirs()

    journal = Journal(FAKE_DIR)
    journal.write_thought("A thought", "reflection", triggered_by="agent_tool")

    args = argparse.Namespace(count=10, type="nonexistent_type", events=False)

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        cmd_journal(args, config)
    finally:
        sys.stdout = old_stdout

    output = buf.getvalue()
    assert "invalid" in output.lower() or "must be" in output.lower(), (
        f"cmd_journal with invalid --type silently returns empty instead of "
        f"showing a helpful error. Got: {output!r}"
    )


# ── cmd_run ─────────────────────────────────────────────────────────


def _mock_vcs() -> MagicMock:
    vcs = MagicMock()
    vcs.has_changes.return_value = False
    return vcs


def test_cmd_run(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    plan_response = json.dumps([{"description": "Do it"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock), \
         patch("maestro.cli.SubprocessVCS", return_value=_mock_vcs()):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Done")

        args = argparse.Namespace(goal="Test goal", context="")
        output = capture_stdout(cmd_run, args, config)

    assert "success" in output.lower()
    mock_plan.assert_called_once()
    mock_exec.assert_called_once()


def test_cmd_run_with_errors(mem_fs: dict[str, str]) -> None:
    config = _make_config(loop_max_replans=0)
    plan_response = json.dumps([{"description": "Fail step"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock), \
         patch("maestro.cli.SubprocessVCS", return_value=_mock_vcs()):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.side_effect = RuntimeError("boom")

        args = argparse.Namespace(goal="Failing", context="")
        output = capture_stdout(cmd_run, args, config)

    assert "failure" in output.lower()
    assert "Errors" in output
    mock_plan.assert_called_once()
    mock_exec.assert_called_once()


def test_cmd_run_none_result_summary() -> None:
    """cmd_run should handle result_summary being None without crashing."""
    mock_record = MagicMock()
    mock_record.outcome = "success"
    mock_record.duration_secs = 1.0
    mock_record.errors = []
    mock_record.result_summary = None

    mock_config = MagicMock()

    with patch("maestro.cli._build_agent") as mock_build, \
         patch("maestro.cli.asyncio") as mock_asyncio:
        mock_build.return_value = MagicMock()
        mock_asyncio.run.return_value = mock_record

        args = MagicMock()
        args.goal = "test"
        args.context = ""

        try:
            cmd_run(args, mock_config)
        except TypeError as exc:
            pytest.fail(f"cmd_run crashed on None result_summary: {exc}")


# ── cmd_resume_task ─────────────────────────────────────────────────


def test_main_dispatches_resume_task(mem_fs: dict[str, str]) -> None:
    """main() parses ``resume-task <id>`` and dispatches to cmd_resume_task."""
    from maestro.cli import main

    config = _make_config()
    mock_record = MagicMock(
        outcome="success", duration_secs=1.0, errors=[], result_summary="ok",
    )
    mock_agent = MagicMock()
    with patch("sys.argv", ["maestro", "resume-task", "task-abc"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.configure_provider"), \
         patch("maestro.cli._build_agent", return_value=mock_agent), \
         patch("maestro.cli.asyncio") as mock_asyncio:
        mock_asyncio.run.return_value = mock_record
        output = capture_stdout(main)

    assert "success" in output.lower()
    mock_agent.resume_task.assert_called_once_with("task-abc")
    mock_asyncio.run.assert_called_once()


@pytest.mark.parametrize("enabled", [True, False])
def test_main_wires_enforced_tool_surface(
    mem_fs: dict[str, str], enabled: bool,
) -> None:
    """main() hands the seam the config iff the proposal-142 flag is on."""
    from maestro.cli import main

    config = _make_config()
    config.sdk_enforced_tool_surface_enabled = enabled
    expected = config if enabled else None
    with patch("sys.argv", ["maestro", "resume-task", "task-abc"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.configure_provider"), \
         patch("maestro.cli._build_agent", return_value=MagicMock()), \
         patch("maestro.sdk.configure_enforced_tool_surface") as configure, \
         patch("maestro.cli.asyncio") as mock_asyncio:
        mock_asyncio.run.return_value = MagicMock(
            outcome="success", duration_secs=1.0, errors=[], result_summary="ok",
        )
        capture_stdout(main)

    configure.assert_called_once_with(expected)


def test_cmd_resume_task_surfaces_missing_checkpoint() -> None:
    """A missing/stale checkpoint prints a clean error and exits non-zero."""
    with patch("maestro.cli._build_agent", return_value=MagicMock()), \
         patch("maestro.cli.asyncio") as mock_asyncio:
        mock_asyncio.run.side_effect = ValueError(
            "No checkpoint found for task xyz",
        )
        args = argparse.Namespace(task_id="xyz")
        output = capture_stdout(cmd_resume_task, args, MagicMock())

    assert "Cannot resume task xyz" in output
    assert "No checkpoint found" in output


def test_cmd_resume_task_prints_errors() -> None:
    """A resumed task that finished with errors surfaces them."""
    mock_record = MagicMock(
        outcome="partial", duration_secs=2.0, errors=["boom"],
        result_summary="partial summary",
    )
    with patch("maestro.cli._build_agent", return_value=MagicMock()), \
         patch("maestro.cli.asyncio") as mock_asyncio:
        mock_asyncio.run.return_value = mock_record
        args = argparse.Namespace(task_id="t1")
        output = capture_stdout(cmd_resume_task, args, MagicMock())

    assert "partial" in output.lower()
    assert "Errors" in output
    assert "boom" in output


# ── cmd_contemplate ─────────────────────────────────────────────────


def test_cmd_contemplate(mem_fs: dict[str, str]) -> None:
    config = _make_config()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = [
            MockQueryResult(response="I think therefore I am"),
            MockQueryResult(response=json.dumps({"values": ["truth"]})),
        ]
        args = argparse.Namespace()
        output = capture_stdout(cmd_contemplate, args, config)

    assert "I think therefore I am" in output
    assert mock_q.call_count == 2  # introspect + update_self_model


# ── cmd_evolve ──────────────────────────────────────────────────────


def test_cmd_evolve(mem_fs: dict[str, str]) -> None:
    config = _make_config()

    plan_json = json.dumps({
        "title": "Evolve",
        "category": "awareness",
        "priority": "capability",
        "rationale": "Growth",
        "proposed_changes": ["X"],
        "files_affected": [],
        "expected_impact": "Y",
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_c, \
         patch("maestro.evolution.query", new_callable=AsyncMock) as mock_e, \
         patch("maestro.evolution.subprocess.run") as mock_sub:
        mock_e.side_effect = [MockQueryResult(response=plan_json)]
        # self_review subprocess calls (ruff, mypy, pytest)
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )

        args = argparse.Namespace()
        output = capture_stdout(cmd_evolve, args, config)

    assert "complete" in output.lower()
    assert mock_c.call_count == 0  # stage assessment deleted (proposal 149)
    # No GitHub repo means no gap issue to fuel evolve (proposal 155 step 4):
    # the run completes with no priced call and no gate subprocess.
    assert mock_e.call_count == 0
    assert mock_sub.call_count == 0


# ── cmd_issues ──────────────────────────────────────────────────────


def test_cmd_issues_no_repo(mem_fs: dict[str, str]) -> None:
    config = _make_config(github_repo=None)
    args = argparse.Namespace()
    output = capture_stdout(cmd_issues, args, config)
    assert "MAESTRO_GITHUB_REPO not set" in output


def test_cmd_issues_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config(github_repo="owner/repo")
    args = argparse.Namespace()

    with patch("maestro.cli.IssueTracker") as mock_cls:
        mock_cls.return_value.list_open.return_value = []
        output = capture_stdout(cmd_issues, args, config)

    assert "No open issues" in output


def test_cmd_issues_with_results(mem_fs: dict[str, str]) -> None:
    config = _make_config(github_repo="owner/repo")
    args = argparse.Namespace()

    from maestro.issues import Issue

    issues = [
        Issue(number=1, title="Bug", labels=["bug"], body="", created_at=""),
        Issue(number=2, title="Feature", labels=[], body="", created_at=""),
    ]

    with patch("maestro.cli.IssueTracker") as mock_cls:
        mock_cls.return_value.list_open.return_value = issues
        output = capture_stdout(cmd_issues, args, config)

    assert "#1: Bug [bug]" in output
    assert "#2: Feature" in output


# ── cmd_kill / cmd_shutdown ─────────────────────────────────────────


def test_cmd_kill_pid_file_preserved_on_permission_error(
    mem_fs: dict[str, str],
) -> None:
    """When os.kill raises PermissionError, the PID file must survive."""
    d = Path("/fake/kill")
    config = Config(memory_dir=d, workspace=d)
    config.ensure_dirs()
    pid_path = config.pid_path

    mem_fs[str(pid_path)] = "12345"

    args = argparse.Namespace(dashboard=False, all_processes=False)

    with patch("os.kill", side_effect=PermissionError("not permitted")):
        cmd_kill(args, config)

    assert str(pid_path) in mem_fs, (
        "PID file should be preserved when kill fails with PermissionError"
    )


def test_cmd_kill_pid_file_removed_on_success(mem_fs: dict[str, str]) -> None:
    """When os.kill succeeds, the PID file should be removed."""
    d = Path("/fake/kill2")
    config = Config(memory_dir=d, workspace=d)
    config.ensure_dirs()
    pid_path = config.pid_path

    mem_fs[str(pid_path)] = "12345"

    args = argparse.Namespace(dashboard=False, all_processes=False)

    with patch("os.kill"):
        cmd_kill(args, config)

    assert str(pid_path) not in mem_fs


def test_cmd_kill_pid_file_removed_on_process_not_found(
    mem_fs: dict[str, str],
) -> None:
    """When process doesn't exist, PID file should be removed."""
    d = Path("/fake/kill3")
    config = Config(memory_dir=d, workspace=d)
    config.ensure_dirs()
    pid_path = config.pid_path

    mem_fs[str(pid_path)] = "12345"

    args = argparse.Namespace(dashboard=False, all_processes=False)

    with patch("os.kill", side_effect=ProcessLookupError):
        cmd_kill(args, config)

    assert str(pid_path) not in mem_fs


def test_cmd_kill_catches_permission_error(
    mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    """cmd_kill should catch PermissionError from os.kill, not just ProcessLookupError."""
    config = Config(memory_dir=Path("/fake/memory"))
    mem_fs[str(config.pid_path)] = "12345"

    args = MagicMock()
    args.all_processes = False
    args.dashboard = False

    with patch("os.kill", side_effect=PermissionError("Operation not permitted")):
        cmd_kill(args, config)

    captured = capsys.readouterr()
    assert "Permission denied" in captured.out
    assert "12345" in captured.out


def test_cmd_shutdown_catches_permission_error(
    mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    """cmd_shutdown should catch PermissionError from os.kill, not just ProcessLookupError."""
    config = Config(memory_dir=Path("/fake/memory"))
    mem_fs[str(config.pid_path)] = "12345"

    args = MagicMock()
    args.all_processes = False
    args.dashboard = False

    with patch("os.kill", side_effect=PermissionError("Operation not permitted")):
        cmd_shutdown(args, config)

    captured = capsys.readouterr()
    assert "Permission denied" in captured.out
    assert "12345" in captured.out


def test_cmd_shutdown_still_catches_process_lookup_error(
    mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify ProcessLookupError is still caught after adding PermissionError handling."""
    config = Config(memory_dir=Path("/fake/memory"))
    mem_fs[str(config.pid_path)] = "99999"

    args = MagicMock()
    args.all_processes = False
    args.dashboard = False

    with patch("os.kill", side_effect=ProcessLookupError()):
        cmd_shutdown(args, config)

    captured = capsys.readouterr()
    assert "not found" in captured.out


# ── cmd_loop ────────────────────────────────────────────────────────


def test_cmd_loop_negative_task_every_causes_error() -> None:
    """Negative --task-every should result in ValueError from run_loop."""
    from maestro.cli import cmd_loop

    args = argparse.Namespace(
        interval=60, max_iters=1, evolve_every=5,
        task_every=-1, dream_every=10, smoke=False,
    )
    config = Config(memory_dir=Path("/fake"), workspace=Path("/fake"))

    with patch("maestro.cli._build_agent") as mock_build:
        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(
            side_effect=ValueError("task_every must be >= 0"),
        )
        mock_build.return_value = mock_agent

        with pytest.raises(ValueError, match="task_every"):
            cmd_loop(args, config)


def test_cmd_loop_negative_dream_every_causes_error() -> None:
    """Negative --dream-every should result in ValueError from run_loop."""
    from maestro.cli import cmd_loop

    args = argparse.Namespace(
        interval=60, max_iters=1, evolve_every=5,
        task_every=3, dream_every=-1, smoke=False,
    )
    config = Config(memory_dir=Path("/fake"), workspace=Path("/fake"))

    with patch("maestro.cli._build_agent") as mock_build:
        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(
            side_effect=ValueError("dream_every must be >= 0"),
        )
        mock_build.return_value = mock_agent

        with pytest.raises(ValueError, match="dream_every"):
            cmd_loop(args, config)


def test_cmd_loop_propagates_run_loop_exceptions() -> None:
    """cmd_loop must propagate exceptions raised by run_loop().

    Phase-level error suppression was removed from run_loop(), so any phase
    crash now raises out of the loop.  asyncio.run() re-raises inside cmd_loop,
    and the non-zero exit drives Docker's restart / safe-deploy.sh's rollback.
    The old "return False -> sys.exit(1)" path no longer exists because
    run_loop() now always returns True on clean exit.
    """
    from maestro.cli import cmd_loop

    args = argparse.Namespace(
        interval=60, max_iters=1, evolve_every=5,
        task_every=3, dream_every=10, smoke=False,
    )
    config = Config(memory_dir=Path("/fake"), workspace=Path("/fake"))

    with patch("maestro.cli._build_agent") as mock_build:
        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(side_effect=RuntimeError("phase crash"))
        mock_build.return_value = mock_agent

        with pytest.raises(RuntimeError, match="phase crash"):
            cmd_loop(args, config)


def test_cmd_loop_exits_zero_on_success(capsys) -> None:  # type: ignore[no-untyped-def]
    """cmd_loop should not sys.exit when run_loop returns True."""
    from maestro.cli import cmd_loop

    args = argparse.Namespace(
        interval=60, max_iters=1, evolve_every=5,
        task_every=3, dream_every=10, smoke=False,
    )
    config = Config(memory_dir=Path("/fake"), workspace=Path("/fake"))

    with patch("maestro.cli._build_agent") as mock_build:
        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(return_value=True)
        mock_build.return_value = mock_agent

        cmd_loop(args, config)

    captured = capsys.readouterr()
    assert "Maestro stopped." in captured.out


def test_cmd_loop_smoke_flag_forwarded_to_run_loop(
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    """cmd_loop forwards args.smoke=True as run_loop(smoke=True) and
    prints the smoke-mode banner instead of the normal startup line."""
    from maestro.cli import cmd_loop

    args = argparse.Namespace(
        interval=60, max_iters=1, evolve_every=5,
        task_every=3, dream_every=10, smoke=True,
    )
    config = Config(memory_dir=Path("/fake"), workspace=Path("/fake"))

    with patch("maestro.cli._build_agent") as mock_build:
        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(return_value=True)
        mock_build.return_value = mock_agent

        cmd_loop(args, config)

    mock_agent.run_loop.assert_awaited_once()
    _, kwargs = mock_agent.run_loop.call_args
    assert kwargs.get("smoke") is True
    output = capsys.readouterr().out
    assert "smoke iteration" in output.lower()


def test_cmd_loop_passes_warn_if_null_to_build_agent() -> None:
    """cmd_loop is the sole caller that opts into the notifier-disabled
    warning, so it must build the agent with warn_if_null=True (#204)."""
    from maestro.cli import cmd_loop

    args = argparse.Namespace(
        interval=60, max_iters=1, evolve_every=5,
        task_every=3, dream_every=10, smoke=False,
    )
    config = Config(memory_dir=Path("/fake"), workspace=Path("/fake"))

    # Mock write_heartbeat to stay hermetic: cmd_loop calls it with
    # config.memory_dir = Path("/fake") before _build_agent, which would
    # otherwise issue a real write_text() syscall (the OSError handler only
    # masks it when /fake is absent). The sibling cmd_loop tests patch the
    # same maestro.agent.write_heartbeat target -- cmd_loop imports the helper
    # locally, so maestro.cli has no module-level write_heartbeat to patch.
    with (
        patch("maestro.agent.write_heartbeat"),
        patch("maestro.cli._build_agent") as mock_build,
    ):
        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(return_value=True)
        mock_build.return_value = mock_agent

        cmd_loop(args, config)

    _, kwargs = mock_build.call_args
    assert kwargs.get("warn_if_null") is True


def test_cmd_loop_default_smoke_false_does_not_require_arg() -> None:
    """cmd_loop uses getattr(args, 'smoke', False) so older callers that
    built the Namespace without a smoke field still work."""
    from maestro.cli import cmd_loop

    # Deliberately omit the smoke attribute.
    args = argparse.Namespace(
        interval=60, max_iters=1, evolve_every=5,
        task_every=3, dream_every=10,
    )
    config = Config(memory_dir=Path("/fake"), workspace=Path("/fake"))

    with patch("maestro.cli._build_agent") as mock_build:
        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(return_value=True)
        mock_build.return_value = mock_agent

        cmd_loop(args, config)

    _, kwargs = mock_agent.run_loop.call_args
    assert kwargs.get("smoke") is False


def test_cmd_loop_forwards_curate_config_to_run_loop() -> None:
    """cmd_loop sources the curate cadence/switch from Config (proposal 060),
    giving the curate_pending_plans phase a production enable path."""
    from maestro.cli import cmd_loop

    args = argparse.Namespace(
        interval=60, max_iters=1, evolve_every=5,
        task_every=3, dream_every=10, smoke=False,
    )
    config = Config(
        memory_dir=Path("/fake"), workspace=Path("/fake"),
        loop_curate_every=7, loop_curate_enabled=True,
    )

    with patch("maestro.cli._build_agent") as mock_build:
        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(return_value=True)
        mock_build.return_value = mock_agent

        cmd_loop(args, config)

    _, kwargs = mock_agent.run_loop.call_args
    assert kwargs.get("curate_every") == 7
    assert kwargs.get("curate_enabled") is True


def test_cmd_loop_writes_heartbeat_before_build_agent() -> None:
    """cmd_loop must touch the Docker healthcheck heartbeat file *before*
    calling ``_build_agent``.

    This is the whole point of the early-write: the Docker healthcheck for
    the loop container probes ``/data/loop.heartbeat``'s mtime, and if agent
    construction (schema migration, MCP server setup, …) is slow, the
    healthcheck ran out of time during deploys and rolled back healthy
    containers. The fix is to write the file before any of that runs, so
    the first healthcheck always finds a fresh mtime.
    """
    from maestro.cli import cmd_loop

    args = argparse.Namespace(
        interval=60, max_iters=1, evolve_every=5,
        task_every=3, dream_every=10,
    )
    config = Config(memory_dir=Path("/fake"), workspace=Path("/fake"))

    call_order: list[str] = []

    def _heartbeat_side_effect(memory_dir: Path) -> None:
        call_order.append("write_heartbeat")

    def _build_side_effect(_config: Config, **_kwargs: Any) -> MagicMock:
        call_order.append("_build_agent")
        mock_agent = MagicMock()
        mock_agent.run_loop = AsyncMock(return_value=True)
        return mock_agent

    with patch(
        "maestro.agent.write_heartbeat",
        side_effect=_heartbeat_side_effect,
    ) as mock_write, patch(
        "maestro.cli._build_agent",
        side_effect=_build_side_effect,
    ) as mock_build:
        cmd_loop(args, config)

    mock_write.assert_called_once_with(config.memory_dir)
    mock_build.assert_called_once_with(
        config, smoke=False, check_publication_scope=True, warn_if_null=True,
    )
    assert call_order == ["write_heartbeat", "_build_agent"], (
        f"Expected write_heartbeat to run before _build_agent, got {call_order}"
    )


def test_cmd_loop_build_agent_failure_still_has_early_heartbeat() -> None:
    """If ``_build_agent`` blows up (e.g. schema migration error), the
    heartbeat write must still have happened first.

    Docker will see the file, but the container will also exit due to the
    uncaught exception — so an exited container with a fresh heartbeat is
    fine: Docker reports "exited" for dead containers regardless of
    healthcheck state. The key invariant is that a slow or failing build
    never prevents the heartbeat write from landing.
    """
    from maestro.cli import cmd_loop

    args = argparse.Namespace(
        interval=60, max_iters=1, evolve_every=5,
        task_every=3, dream_every=10,
    )
    config = Config(memory_dir=Path("/fake"), workspace=Path("/fake"))

    with (
        patch("maestro.agent.write_heartbeat") as mock_write,
        patch(
            "maestro.cli._build_agent",
            side_effect=RuntimeError("schema migration failed"),
        ),
        pytest.raises(RuntimeError, match="schema migration"),
    ):
        cmd_loop(args, config)

    mock_write.assert_called_once_with(config.memory_dir)


# ── cmd_logs ────────────────────────────────────────────────────────


def test_cmd_logs_date_without_sdk_warns(mem_fs: dict[str, str]) -> None:
    """--date without --sdk should warn that date has no effect."""
    d = Path("/fake/logs_date")
    config = Config(memory_dir=d, workspace=d)
    config.ensure_dirs()
    mem_fs[str(config.app_log_path)] = "line1\nline2\nline3\n"

    args = argparse.Namespace(
        sdk=False, follow=False, lines=50, date="2025-01-01",
        session=None, list_sessions=False,
    )

    old = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        cmd_logs(args, config)
    finally:
        sys.stdout = old

    output = buf.getvalue()
    assert "--sdk" in output or "ignored" in output.lower() or "line1" not in output


def test_cmd_logs_session_without_sdk_warns(mem_fs: dict[str, str]) -> None:
    """--session without --sdk should warn user."""
    d = Path("/fake/logs_session")
    config = Config(memory_dir=d, workspace=d)
    config.ensure_dirs()
    mem_fs[str(config.app_log_path)] = "log line\n"

    args = argparse.Namespace(
        sdk=False, follow=False, lines=50, date=None,
        session="some-session", list_sessions=False,
    )

    old_stderr = sys.stderr
    old_stdout = sys.stdout
    sys.stderr = err_buf = io.StringIO()
    sys.stdout = out_buf = io.StringIO()
    try:
        cmd_logs(args, config)
    finally:
        sys.stderr = old_stderr
        sys.stdout = old_stdout

    combined = out_buf.getvalue() + err_buf.getvalue()
    assert "--sdk" in combined or "ignored" in combined.lower()


# ── _read_pid ───────────────────────────────────────────────────────


def test_read_pid_fractional_returns_none(tmp_path: Path) -> None:
    """A PID file containing a fractional value like '3.7' should return None."""
    pid_file = tmp_path / "test.pid"
    pid_file.write_text("3.7")
    result = _read_pid(pid_file)
    assert result is None, f"Expected None for fractional PID '3.7', got {result}"


def test_read_pid_valid_integer(tmp_path: Path) -> None:
    """A PID file containing a valid integer should return that integer."""
    pid_file = tmp_path / "test.pid"
    pid_file.write_text("12345")
    result = _read_pid(pid_file)
    assert result == 12345


def test_read_pid_scientific_notation_returns_none(tmp_path: Path) -> None:
    """A PID file containing scientific notation like '1e400' should return None."""
    pid_file = tmp_path / "test.pid"
    pid_file.write_text("1e400")
    result = _read_pid(pid_file)
    assert result is None, f"Expected None for scientific notation PID '1e400', got {result}"


def test_read_pid_zero_returns_none(tmp_path: Path) -> None:
    """A PID file containing 0 should return None (line 335)."""
    pid_file = tmp_path / "test.pid"
    pid_file.write_text("0")
    result = _read_pid(pid_file)
    assert result is None


def test_read_pid_negative_returns_none(tmp_path: Path) -> None:
    """A PID file containing a negative integer should return None (line 335)."""
    pid_file = tmp_path / "test.pid"
    pid_file.write_text("-1")
    result = _read_pid(pid_file)
    assert result is None


# ── main ────────────────────────────────────────────────────────────


def test_main_no_args() -> None:
    """main() with no command should exit with code 1."""
    from maestro.cli import main
    with patch("sys.argv", ["maestro"]), pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


# ── preflight ──────────────────────────────────────────────────────


def test_cmd_preflight_success(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight passes with valid config and fresh state.

    Provisions the three optional secrets locally so ``check_secrets`` returns
    no warnings, letting the assertion pin the exact ``secrets: ok`` line. The
    autouse ``_fake_auth`` fixture only sets ``ANTHROPIC_API_KEY`` and
    ``GITHUB_TOKEN``, so without these overrides the success path reports
    ``secrets: 3 warning(s)`` and the old ``"secrets" in output`` check passed
    regardless of secrets health (#282).
    """
    # Clear any ambient CLAUDE_CODE_OAUTH_TOKEN so check_auth resolves the
    # fixture's ANTHROPIC_API_KEY (the api_key path); an ambient oauth token
    # without the ``sk-ant-oat`` prefix would otherwise add a warning and break
    # the pinned ``secrets: ok`` (mirrors the test_config.py convention).
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    # Kept local to this test (not added to the autouse ``_fake_auth`` fixture)
    # so tests that deliberately exercise the warnings path stay unaffected.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCdef_test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "dashboard-test-token")
    config = _make_config()
    args = argparse.Namespace()
    output = capture_stdout(cmd_preflight, args, config)
    assert "Preflight checks passed" in output
    assert "config: ok" in output
    assert "imports: ok" in output
    assert "self_model: evolutions=0" in output
    assert "secrets: ok" in output


def test_cmd_preflight_with_existing_state(mem_fs: dict[str, str]) -> None:
    """Preflight reports loaded state correctly."""
    config = _make_config()
    config.ensure_dirs()

    consciousness = Consciousness(config.memory_dir)
    model = consciousness.load_self_model()
    model.evolution_count = 3
    consciousness.save_self_model(model)

    journal = Journal(config.memory_dir)
    journal.write_thought(
        content="test entry",
        entry_type="reflection",
        triggered_by="agent_tool",
    )

    args = argparse.Namespace()
    output = capture_stdout(cmd_preflight, args, config)
    assert "self_model: evolutions=3" in output
    assert "1 entries" in output


def test_cmd_preflight_no_auth(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight exits 1 when no auth is configured."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    config = _make_config()
    args = argparse.Namespace()
    output = capture_stdout(cmd_preflight, args, config)
    assert "FAIL" in output


def test_cmd_preflight_uses_auth_label_not_check_auth(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cmd_preflight renders the human-readable auth label, not the raw method.

    Regression for #280: the secrets block used to call ``config.check_auth()``
    a second time (after ``check_secrets()`` had already validated the same
    credential) purely to print the ``auth:`` line — a redundant re-read of the
    auth env vars.  The fix delegates to ``Config.auth_label()`` (mirroring the
    sibling ``_build_agent`` fix), which as a side effect surfaces the readable
    label.  A pure call-count assertion cannot separate the bug from the fix
    (both paths reach ``check_auth()`` the same number of times), so assert
    behaviourally on the rendered ``auth:`` line: it must show the
    ``auth_label()`` text and NOT the raw ``oauth`` method string.
    """
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    config = _make_config()
    args = argparse.Namespace()
    output = capture_stdout(cmd_preflight, args, config)
    assert "auth: OAuth token (Max/Pro subscription)" in output
    assert "auth: oauth" not in output


def test_preflight_resolves_auth_once(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cmd_preflight resolves the auth method exactly once (#356).

    The secrets block used to call ``check_auth()`` twice — once inside
    ``check_secrets()`` for the anthropic provider-credential precondition and
    again inside ``auth_label()`` for the label. The fix resolves the method
    once at the CLI and threads it through both, so ``check_auth()`` fires a
    single time. Spying with ``wraps`` keeps the real resolution (the fixture's
    ``ANTHROPIC_API_KEY``) so the rendered ``auth:`` line still validates.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    config = _make_config()
    args = argparse.Namespace()
    with patch.object(Config, "check_auth", wraps=Config.check_auth) as spy:
        output = capture_stdout(cmd_preflight, args, config)
    assert spy.call_count == 1
    assert "auth: API key" in output


def test_preflight_non_anthropic_provider_surfaces_provider_message(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight keeps a non-anthropic provider's own message first (#356).

    With the eager top-level ``check_auth()`` gated to the anthropic provider,
    a misconfigured ``openai`` provider missing both the Anthropic creds and its
    own key fails with ``OPENAI_API_KEY is not set`` — not the anthropic
    ``No Claude authentication found`` fallback an unconditional resolve raised.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = _make_config(sdk_provider="openai")
    args = argparse.Namespace()
    output = capture_stdout(cmd_preflight, args, config)
    assert "OPENAI_API_KEY is not set" in output
    assert "No Claude authentication found" not in output


def test_preflight_non_anthropic_provider_with_own_key_no_anthropic_crash(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight passes a valid non-anthropic deployment without anthropic creds (#356 review).

    An ``openai`` provider whose OWN key is present (anthropic creds absent)
    passes ``check_secrets()``; the label step must not crash with the anthropic
    ``No Claude authentication found`` fallback — it renders a provider-neutral
    auth label instead and never reads the anthropic auth env.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    config = _make_config(sdk_provider="openai")
    args = argparse.Namespace()
    with patch.object(Config, "check_auth", wraps=Config.check_auth) as spy:
        output = capture_stdout(cmd_preflight, args, config)
    assert "No Claude authentication found" not in output
    assert "auth: openai provider credential" in output
    spy.assert_not_called()


def test_cmd_preflight_no_github_token(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight exits 1 when GITHUB_TOKEN is missing."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = _make_config()
    args = argparse.Namespace()
    output = capture_stdout(cmd_preflight, args, config)
    assert "FAIL" in output
    assert "GITHUB_TOKEN" in output


def test_cmd_preflight_self_model_error(mem_fs: dict[str, str]) -> None:
    """Preflight exits 1 when self model cannot be loaded."""
    config = _make_config()
    args = argparse.Namespace()
    with patch.object(Consciousness, "load_self_model", side_effect=OSError("disk")):
        output = capture_stdout(cmd_preflight, args, config)
    assert "FAIL: Cannot load self model" in output


def test_cmd_preflight_journal_error(mem_fs: dict[str, str]) -> None:
    """Preflight exits 1 when journal cannot be loaded."""
    config = _make_config()
    args = argparse.Namespace()
    with patch.object(Journal, "read_recent", side_effect=OSError("disk")):
        output = capture_stdout(cmd_preflight, args, config)
    assert "FAIL: Cannot load journal" in output


def test_cmd_preflight_memory_error(mem_fs: dict[str, str]) -> None:
    """Preflight exits 1 when memory cannot be loaded."""
    config = _make_config()
    args = argparse.Namespace()
    with patch.object(Memory, "task_count", side_effect=OSError("disk")):
        output = capture_stdout(cmd_preflight, args, config)
    assert "FAIL: Cannot load memory" in output


def test_cmd_preflight_import_error(mem_fs: dict[str, str]) -> None:
    """Preflight exits 1 when a core module fails to import."""
    config = _make_config()
    args = argparse.Namespace()
    with patch("importlib.import_module", side_effect=ImportError("broken")):
        output = capture_stdout(cmd_preflight, args, config)
    assert "FAIL: Module import error" in output


def test_cmd_preflight_mcp_launchers_ok(mem_fs: dict[str, str]) -> None:
    """Preflight reports mcp_launchers ok when no external server is enabled."""
    config = _make_config()
    args = argparse.Namespace()
    output = capture_stdout(cmd_preflight, args, config)
    assert "mcp_launchers: ok" in output


def test_cmd_preflight_missing_mcp_launcher_fails(mem_fs: dict[str, str]) -> None:
    """Preflight exits 1 with an actionable message when a launcher is missing."""
    config = _make_config(serena_mcp_enabled=True)
    args = argparse.Namespace()

    def fake_which(cmd: str) -> str | None:
        return None if cmd == "uvx" else f"/usr/bin/{cmd}"

    with patch("maestro.config.shutil.which", side_effect=fake_which):
        output = capture_stdout(cmd_preflight, args, config)
    assert "FAIL" in output
    assert "uvx" in output
    assert "serena" in output


def _sibling_finder(
    monkeypatch: pytest.MonkeyPatch, resolved: dict[str, str | None],
) -> None:
    """Bind the oracle's ``find_spec`` to a stand-in resolving only *resolved*.

    ``maestro.gate_environment`` is the single owner of the sibling probe and
    binds ``find_spec`` at import, so patching ``importlib.util`` no longer
    reaches it (proposal 133 step 9). A name mapped to a string resolves as a
    real distribution module, a name mapped to ``None`` resolves as the
    origin-less PEP 420 namespace portion, and anything else is absent.
    """
    def fake_find_spec(name: str) -> importlib.machinery.ModuleSpec | None:
        if name not in resolved:
            return None
        return importlib.machinery.ModuleSpec(
            name, loader=None, origin=resolved[name],
        )

    monkeypatch.setattr(gate_environment, "find_spec", fake_find_spec)


_SIBLINGS_RESOLVED: dict[str, str | None] = {
    "maestro_sdk": "/site-packages/maestro_sdk/__init__.py",
    "maestro_sdk.mcp_config": "/site-packages/maestro_sdk/mcp_config.py",
    "maestro_review": "/site-packages/maestro_review/__init__.py",
    "maestro_review.review": "/site-packages/maestro_review/review.py",
}


def test_preflight_fails_on_maestro_sdk_namespace_shadow(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight exits 1 when maestro_sdk resolves only as a PEP 420 namespace.

    Reproduces the loop container's shadow (#3872): under PYTHONPATH=/workspace the
    sibling src-layout dir binds an empty ``maestro_sdk`` namespace — ``import
    maestro_sdk`` succeeds (a spec with ``origin=None``) but the submodule
    ``maestro_sdk.mcp_config`` does not resolve. Preflight must fail loud here with
    an actionable ``pip install`` line rather than deferring the break to the next
    in-container gate run — now in the oracle's own wording.
    """
    resolved = dict(_SIBLINGS_RESOLVED)
    del resolved["maestro_sdk.mcp_config"]
    _sibling_finder(monkeypatch, resolved)

    config = _make_config()
    args = argparse.Namespace()
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as excinfo:
        cmd_preflight(args, config)
    assert excinfo.value.code != 0
    output = buf.getvalue()
    assert "FAIL" in output
    assert "namespace shadow" in output
    assert 'pip install -e "maestro_sdk/"' in output


def test_preflight_fails_when_maestro_sdk_entirely_absent(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight fails loud when maestro_sdk is not importable at all.

    When the sibling is missing from ``sys.path`` entirely (not even a namespace),
    ``find_spec`` raises ``ModuleNotFoundError`` on the parent import rather than
    returning ``None``. The owner treats that the same way — an actionable exit-1,
    not an uncaught traceback.
    """
    def raising_find_spec(name: str) -> importlib.machinery.ModuleSpec | None:
        raise ModuleNotFoundError("No module named 'maestro_sdk'")

    monkeypatch.setattr(gate_environment, "find_spec", raising_find_spec)

    config = _make_config()
    args = argparse.Namespace()
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as excinfo:
        cmd_preflight(args, config)
    assert excinfo.value.code != 0
    output = buf.getvalue()
    assert 'pip install -e "maestro_sdk/"' in output


def test_preflight_fails_when_maestro_review_is_absent(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check now covers *every* declared sibling, not just ``maestro_sdk``.

    Routing through ``probe_provisioning`` widened the startup gate from the one
    hard-coded submodule to the owner's whole ``_SIBLINGS`` registry, so a missing
    ``maestro_review`` — which the old inline probe never looked at — now fails
    startup too, naming its own repair.
    """
    resolved = {
        k: v for k, v in _SIBLINGS_RESOLVED.items()
        if not k.startswith("maestro_review")
    }
    _sibling_finder(monkeypatch, resolved)

    config = _make_config()
    args = argparse.Namespace()
    buf = io.StringIO()
    with redirect_stdout(buf), pytest.raises(SystemExit) as excinfo:
        cmd_preflight(args, config)
    assert excinfo.value.code != 0
    output = buf.getvalue()
    assert "maestro_review" in output
    assert 'pip install -e "maestro_review/"' in output


def test_preflight_succeeds_when_every_sibling_submodule_resolves(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preflight reports ``siblings: ok`` when every declared submodule resolves.

    The passing counterpart to the two guards above: with real distribution specs
    for both siblings the verdict is ``SOUND``, ``declines_gate_run`` is false,
    and preflight completes normally.
    """
    _sibling_finder(monkeypatch, dict(_SIBLINGS_RESOLVED))

    config = _make_config()
    args = argparse.Namespace()
    output = capture_stdout(cmd_preflight, args, config)
    assert "siblings: ok" in output
    assert "Preflight checks passed" in output


# ── cmd_messages ──────────────────────────────────────────────────────


def test_cmd_messages_empty(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    args = argparse.Namespace(count=20)
    output = capture_stdout(cmd_messages, args, config)
    assert "No messages" in output


def test_cmd_messages_empty_default_count(mem_fs: dict[str, str]) -> None:
    config = _make_config()
    args = argparse.Namespace(count=None)
    output = capture_stdout(cmd_messages, args, config)
    assert "No messages" in output


def test_cmd_messages_shows_messages(mem_fs: dict[str, str]) -> None:
    from maestro.messaging import Message, MessageStore

    config = _make_config()
    store = MessageStore(FAKE_DIR)
    store.save_message(Message(
        message_id="m1", timestamp="2026-03-31T12:00:00",
        sender="thomas", content="Focus on docs",
        message_type="instruction",
    ))
    args = argparse.Namespace(count=20)
    output = capture_stdout(cmd_messages, args, config)
    assert "Thomas" in output
    assert "Focus on docs" in output
    assert "instruction" in output


# ── _format_duration ───────────────────────────────────────────────


def test_format_duration_seconds() -> None:
    assert _format_duration(0) == "0s"
    assert _format_duration(45) == "45s"
    assert _format_duration(59.4) == "59s"


def test_format_duration_minutes() -> None:
    assert _format_duration(60) == "1.0m"
    assert _format_duration(150) == "2.5m"
    assert _format_duration(3540) == "59.0m"


def test_format_duration_hours() -> None:
    assert _format_duration(3600) == "1.0h"
    assert _format_duration(7200) == "2.0h"


def test_format_duration_boundary_values() -> None:
    """Values that round up to the next unit must display in that unit."""
    # Seconds rounding up to minutes
    assert _format_duration(59.5) == "1.0m"
    assert _format_duration(59.9) == "1.0m"

    # Minutes rounding up to hours
    assert _format_duration(3599) == "1.0h"
    assert _format_duration(3599.5) == "1.0h"


# ── cmd_self_model export / import ───────────────────────────────────


def test_self_model_subcommand_registered(mem_fs: dict[str, str]) -> None:
    """main() routes the 'self-model' subcommand to cmd_self_model."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "self-model", "export", "/tmp/sm.json"]), \
         patch("maestro.cli.cmd_self_model") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_cmd_self_model_export_dumps_untruncated(mem_fs: dict[str, str]) -> None:
    """export writes the raw self-model with over-length entries intact."""
    config = _make_config()
    long_entry = "x" * 250
    mem_fs[str(FAKE_DIR / "self_model.json")] = json.dumps({
        "identity_statements": [long_entry],
    })
    args = argparse.Namespace(self_model_command="export", file="/tmp/sm.json")
    output = capture_stdout(cmd_self_model, args, config)
    assert "Exported" in output
    assert long_entry in mem_fs["/tmp/sm.json"]


def test_cmd_self_model_export_missing_file_exits(mem_fs: dict[str, str]) -> None:
    """export on an uninitialised memory dir errors and exits non-zero."""
    config = _make_config()
    args = argparse.Namespace(self_model_command="export", file="/tmp/sm.json")
    with pytest.raises(SystemExit):
        cmd_self_model(args, config)
    assert "/tmp/sm.json" not in mem_fs


def test_cmd_self_model_export_malformed_json_exits(
    mem_fs: dict[str, str],
) -> None:
    """export on a corrupt (unparseable) live self_model.json errors cleanly
    and exits non-zero without writing the output file."""
    config = _make_config()
    mem_fs[str(FAKE_DIR / "self_model.json")] = "not json {"
    args = argparse.Namespace(self_model_command="export", file="/tmp/sm.json")
    with pytest.raises(SystemExit):
        cmd_self_model(args, config)
    assert "/tmp/sm.json" not in mem_fs


def test_cmd_self_model_export_non_dict_exits(mem_fs: dict[str, str]) -> None:
    """export on a live self_model.json holding a JSON array (non-object)
    errors cleanly and exits non-zero without writing the output file."""
    config = _make_config()
    mem_fs[str(FAKE_DIR / "self_model.json")] = json.dumps([1, 2, 3])
    args = argparse.Namespace(self_model_command="export", file="/tmp/sm.json")
    with pytest.raises(SystemExit):
        cmd_self_model(args, config)
    assert "/tmp/sm.json" not in mem_fs


def test_cmd_self_model_import_dry_run_reports_no_write(
    mem_fs: dict[str, str],
) -> None:
    """import --dry-run reports the truncation and leaves self_model.json
    unwritten."""
    config = _make_config()
    mem_fs["/tmp/edit.json"] = json.dumps({"identity_statements": ["x" * 250]})
    args = argparse.Namespace(
        self_model_command="import", file="/tmp/edit.json", dry_run=True,
    )
    output = capture_stdout(cmd_self_model, args, config)
    assert "truncated" in output
    assert "Dry run" in output
    assert str(FAKE_DIR / "self_model.json") not in mem_fs


def test_cmd_self_model_import_writes_capped(mem_fs: dict[str, str]) -> None:
    """import (real) writes the capped self-model with the entry truncated."""
    config = _make_config()
    mem_fs["/tmp/edit.json"] = json.dumps({"identity_statements": ["x" * 250]})
    args = argparse.Namespace(
        self_model_command="import", file="/tmp/edit.json", dry_run=False,
    )
    output = capture_stdout(cmd_self_model, args, config)
    assert "Imported" in output
    saved = json.loads(mem_fs[str(FAKE_DIR / "self_model.json")])
    assert len(saved["identity_statements"][0]) == 200
    assert saved["identity_statements"][0].endswith("…")


def test_cmd_self_model_import_clean_file(mem_fs: dict[str, str]) -> None:
    """A within-caps file reports a clean round-trip and still writes."""
    config = _make_config()
    mem_fs["/tmp/edit.json"] = json.dumps({"identity_statements": ["short"]})
    args = argparse.Namespace(
        self_model_command="import", file="/tmp/edit.json", dry_run=False,
    )
    output = capture_stdout(cmd_self_model, args, config)
    assert "round-trips cleanly" in output
    assert "Imported" in output
    assert str(FAKE_DIR / "self_model.json") in mem_fs


def test_cmd_self_model_import_file_not_found_exits(
    mem_fs: dict[str, str],
) -> None:
    """import on a missing input file errors and exits non-zero."""
    config = _make_config()
    args = argparse.Namespace(
        self_model_command="import",
        file="/fake/cli/nonexistent-import.json",
        dry_run=False,
    )
    with pytest.raises(SystemExit):
        cmd_self_model(args, config)


def test_cmd_self_model_import_malformed_json_exits(
    mem_fs: dict[str, str],
) -> None:
    """import on malformed JSON errors and exits non-zero."""
    config = _make_config()
    mem_fs["/tmp/edit.json"] = "not json {"
    args = argparse.Namespace(
        self_model_command="import", file="/tmp/edit.json", dry_run=False,
    )
    with pytest.raises(SystemExit):
        cmd_self_model(args, config)


def test_cmd_self_model_import_non_dict_exits(mem_fs: dict[str, str]) -> None:
    """import on a JSON array (non-object) errors and exits non-zero."""
    config = _make_config()
    mem_fs["/tmp/edit.json"] = json.dumps([1, 2, 3])
    args = argparse.Namespace(
        self_model_command="import", file="/tmp/edit.json", dry_run=False,
    )
    with pytest.raises(SystemExit):
        cmd_self_model(args, config)


def test_cmd_self_model_no_subcommand(mem_fs: dict[str, str]) -> None:
    """No sub-subcommand prints usage and exits non-zero."""
    config = _make_config()
    args = argparse.Namespace(self_model_command=None)
    with pytest.raises(SystemExit):
        cmd_self_model(args, config)


# ── cmd_logs SDK-only flags ──────────────────────────────────────────


def test_cmd_logs_sdk_only_flags_warning(mem_fs: dict[str, str]) -> None:
    """cmd_logs prints warning when SDK-only flags used without --sdk (line 405)."""
    config = _make_config()
    args = argparse.Namespace(
        sdk=False,
        date="2026-01-01",
        session=None,
        list_sessions=False,
        lines=None,
        follow=False,
    )
    with patch("maestro.logs.tail_file"):
        output = capture_stdout(cmd_logs, args, config)
    assert "--date" in output
    assert "ignored without --sdk" in output


# ── main file logging / command dispatch ─────────────────────────────


def test_main_dispatches_status_command(mem_fs: dict[str, str]) -> None:
    """main() dispatches to cmd_status for 'status' command (line 569)."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "status"]), \
         patch("maestro.cli.cmd_status") as mock_cmd:
        main()

    mock_cmd.assert_called_once()


def test_main_dispatches_gate_hook_before_config_load() -> None:
    """The hidden ``_gate-bash-hook`` subcommand (proposal 066) must answer
    from stdin/env alone: a ``Config.from_env`` failure inside the hook would
    exit non-zero, which the claude CLI reads as a non-blocking hook error —
    i.e. fail-open. main() therefore dispatches it before config load."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "_gate-bash-hook"]), \
         patch("maestro.cli.run_gate_bash_hook", return_value=0) as mock_hook, \
         patch("maestro.cli.Config.from_env") as mock_config, \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    mock_hook.assert_called_once()
    mock_config.assert_not_called()


def test_main_gate_hook_emits_deny_verdict(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through main(): a PR-mutating payload on stdin yields the
    deny verdict JSON on stdout and exit code 0 (the verdict rides the JSON)."""
    from maestro.cli import main

    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    monkeypatch.delenv("MAESTRO_GATE_ALLOWED_BRANCH", raising=False)
    with patch("sys.argv", ["maestro", "_gate-bash-hook"]), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    decision = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def test_main_configures_provider_from_resolved(mem_fs: dict[str, str]) -> None:
    """main() selects the provider from config.resolved_sdk_provider() (058 Step 6)."""
    from maestro.cli import main

    config = Config(sdk_provider="openai")
    # ``openai`` is not yet registered (Phase 3), so the real capability gate
    # would fail loud in ``get_provider`` — and it carries no mutation guard,
    # so the action-gate coverage check (proposal 066) would too; patch both
    # out to isolate the configure_provider wiring this test pins.
    with patch("sys.argv", ["maestro", "status"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.cmd_status"), \
         patch("maestro.cli.validate_provider_capabilities"), \
         patch("maestro.cli.validate_action_gate_coverage"), \
         patch("maestro.cli.configure_provider") as mock_configure:
        main()

    mock_configure.assert_called_once_with("openai")


def test_main_configures_phase_providers_from_config(mem_fs: dict[str, str]) -> None:
    """main() installs the per-phase override map so the seam routes a phase's
    session to its ``[sdk] <phase>_provider`` at runtime (058 Step 6 runtime half)."""
    from maestro.cli import main

    config = Config(
        sdk_phase_providers={"contemplation": "cli_paced"},
        enable_session_reuse=True,
    )
    with patch("sys.argv", ["maestro", "status"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.cmd_status"), \
         patch("maestro.cli.validate_provider_capabilities"), \
         patch("maestro.cli.validate_action_gate_coverage"), \
         patch("maestro.cli.configure_provider"), \
         patch("maestro.providers.cli_paced.configure_cli_paced"), \
         patch("maestro.cli.configure_phase_providers") as mock_phase:
        main()

    mock_phase.assert_called_once_with({"contemplation": "cli_paced"})


def test_main_validates_action_gate_coverage(mem_fs: dict[str, str]) -> None:
    """main() runs the proposal-066 trust check on the loaded config so an
    un-gated provider on a mutating phase fails at config-load, not mid-task."""
    from maestro.cli import main

    config = Config()
    with patch("sys.argv", ["maestro", "status"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.cmd_status"), \
         patch("maestro.cli.validate_action_gate_coverage") as mock_gate:
        main()

    mock_gate.assert_called_once_with(config)


def test_main_configures_cli_paced_when_selected(mem_fs: dict[str, str]) -> None:
    """main() applies [sdk.cli_paced] overrides when cli_paced is selected (056 Step 12)."""
    from maestro.cli import main

    config = Config(
        sdk_provider="cli_paced",
        sdk_cli_paced_cadence={"mean_key_ms": 50.0},
        sdk_cli_paced_stream_idle_timeout_seconds=120.0,
        sdk_cli_paced_probe_metrics_enabled=True,
        sdk_cli_paced_probe_metrics_every=3,
    )
    with patch("sys.argv", ["maestro", "status"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.cmd_status"), \
         patch("maestro.cli.validate_provider_capabilities"), \
         patch("maestro.cli.configure_provider"), \
         patch("maestro.providers.cli_paced.configure_cli_paced") as mock_cfg:
        main()

    mock_cfg.assert_called_once_with(
        cadence_overrides={"mean_key_ms": 50.0},
        stream_idle_timeout_seconds=120.0,
        probe_metrics_enabled=True,
        probe_metrics_every=3,
        marker_pack=None,
    )


def test_main_cli_paced_zero_idle_passes_none(mem_fs: dict[str, str]) -> None:
    """A 0 idle timeout means 'fall back to maestro_sdk default' → None is passed."""
    from maestro.cli import main

    config = Config(
        sdk_provider="cli_paced",
        sdk_cli_paced_stream_idle_timeout_seconds=0.0,
    )
    with patch("sys.argv", ["maestro", "status"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.cmd_status"), \
         patch("maestro.cli.validate_provider_capabilities"), \
         patch("maestro.cli.configure_provider"), \
         patch("maestro.providers.cli_paced.configure_cli_paced") as mock_cfg:
        main()

    mock_cfg.assert_called_once_with(
        cadence_overrides={},
        stream_idle_timeout_seconds=None,
        probe_metrics_enabled=False,
        probe_metrics_every=1,
        marker_pack=None,
    )


def test_main_cli_paced_resolves_marker_pack_setting(mem_fs: dict[str, str]) -> None:
    """main() routes [sdk.cli_paced] marker_pack through resolve_marker_pack
    and forwards the resolved pack to configure_cli_paced (075 Step 4)."""
    from maestro.cli import main

    config = Config(
        sdk_provider="cli_paced",
        sdk_cli_paced_marker_pack="auto",
    )
    sentinel = object()
    with patch("sys.argv", ["maestro", "status"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.cmd_status"), \
         patch("maestro.cli.validate_provider_capabilities"), \
         patch("maestro.cli.configure_provider"), \
         patch("maestro.providers.cli_paced.configure_cli_paced") as mock_cfg, \
         patch(
             "maestro.providers.cli_paced.resolve_marker_pack",
             return_value=sentinel,
         ) as mock_resolve:
        main()

    mock_resolve.assert_called_once_with("auto")
    assert mock_cfg.call_args.kwargs["marker_pack"] is sentinel


def test_main_does_not_configure_cli_paced_for_anthropic(mem_fs: dict[str, str]) -> None:
    """The default anthropic path never reaches configure_cli_paced (lazy posture)."""
    from maestro.cli import main

    config = Config(sdk_provider="anthropic")
    with patch("sys.argv", ["maestro", "status"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.cmd_status"), \
         patch("maestro.cli.validate_provider_capabilities"), \
         patch("maestro.cli.configure_provider"), \
         patch("maestro.providers.cli_paced.configure_cli_paced") as mock_cfg:
        main()

    mock_cfg.assert_not_called()


def test_main_configures_cli_paced_from_phase_override(mem_fs: dict[str, str]) -> None:
    """A per-phase cli_paced override (global still anthropic) also triggers config."""
    from maestro.cli import main

    config = Config(
        sdk_provider="anthropic",
        sdk_phase_providers={"artifact_review": "cli_paced"},
    )
    with patch("sys.argv", ["maestro", "status"]), \
         patch("maestro.cli.Config.from_env", return_value=config), \
         patch("maestro.cli.cmd_status"), \
         patch("maestro.cli.validate_provider_capabilities"), \
         patch("maestro.cli.configure_provider"), \
         patch("maestro.providers.cli_paced.configure_cli_paced") as mock_cfg:
        main()

    mock_cfg.assert_called_once_with(
        cadence_overrides={},
        stream_idle_timeout_seconds=None,
        probe_metrics_enabled=False,
        probe_metrics_every=1,
        marker_pack=None,
    )


def test_main_sets_up_file_logging_for_run(mem_fs: dict[str, str]) -> None:
    """main() sets up rotating file logging for 'run' command."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "run", "test goal"]), \
         patch("maestro.cli.cmd_run") as mock_cmd, \
         patch("maestro.sdk.configure_sdk_logging") as mock_sdk_log, \
         patch("maestro.sdk.configure_cost_tracking") as mock_cost, \
         patch("maestro.sdk.configure_sdk_model") as mock_sdk_model, \
         patch(
             "logging.handlers.RotatingFileHandler",
             return_value=MagicMock(),
         ) as mock_handler:
        main()

    mock_cmd.assert_called_once()
    mock_sdk_log.assert_called_once()
    mock_cost.assert_called_once()
    # configure_sdk_model is called with the configured model from TOML
    # (the registry's DEFAULT_MODEL) after the empty-string normalisation.
    mock_sdk_model.assert_called_once_with(model_profiles.DEFAULT_MODEL)
    # RotatingFileHandler must be wired with the configured rotation knobs
    # and ``encoding="utf-8"`` (proposal-aligned with the
    # all-open()-takes-utf-8 invariant in CLAUDE.md).
    mock_handler.assert_called_once()
    kwargs = mock_handler.call_args.kwargs
    from maestro.config import Config
    config = Config.from_env()
    assert kwargs["maxBytes"] == config.log_max_bytes
    assert kwargs["backupCount"] == config.log_backup_count
    assert kwargs["encoding"] == "utf-8"


def _doa_cli_plan(
    plan_id: str,
    status: str = "obsolete",
    obsolete_reason: str | None = None,
    source_task_id: str | None = None,
    parent_plan_id: str | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
) -> EvolutionPlan:
    """Build an EvolutionPlan carrying the fields the DOA reader reads."""
    return EvolutionPlan(
        plan_id=plan_id,
        title="Add a widget",
        category="capability",
        rationale="r",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status=status,  # type: ignore[arg-type]
        source_task_id=source_task_id,
        obsolete_reason=obsolete_reason,
        parent_plan_id=parent_plan_id,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# cmd_measure_rejection_classes (proposal 136)
# ---------------------------------------------------------------------------


def _rejection(plan_id: str, reason: str, category: str = "other") -> RejectionRecord:
    """Build a RejectionRecord carrying the fields the class reader reads."""
    return RejectionRecord(
        plan_id=plan_id,
        title="t",
        category="capability",
        priority="medium",
        rejection_reason=reason,
        timestamp="2026-01-01T00:00:00Z",
        rejection_category=category,  # type: ignore[arg-type]
    )


def _make_cli_version(**overrides: object) -> VersionRecord:
    defaults: dict[str, object] = dict(
        version_id="v1",
        plan_id="ep_1",
        plan_title="Add list-versions command",
        commit_sha="abcdef0123456789",
        merged_at="2026-06-24T12:00:00+00:00",
        window_tasks_total=0,
        window_tasks_succeeded=0,
        window_tasks_failed=0,
        window_sdk_cost_usd=0.0,
        window_sdk_calls=0,
        window_cache_hit_rate=0.0,
        window_rate_limit_hits=0,
        window_error_count=0,
        quality_gate_pass=True,
        notes=None,
    )
    defaults.update(overrides)
    return VersionRecord(**defaults)  # type: ignore[arg-type]


def test_main_configures_sdk_for_resume_task(mem_fs: dict[str, str]) -> None:
    """resume-task re-enters agent.run(), so it gets the same SDK setup as run."""
    from maestro.cli import main

    with patch("sys.argv", ["maestro", "resume-task", "t1"]), \
         patch("maestro.cli.cmd_resume_task") as mock_cmd, \
         patch("maestro.sdk.configure_sdk_logging") as mock_sdk_log, \
         patch("maestro.sdk.configure_cost_tracking") as mock_cost, \
         patch("maestro.sdk.configure_sdk_concurrency") as mock_conc, \
         patch("maestro.sdk.configure_sdk_model") as mock_sdk_model, \
         patch(
             "logging.handlers.RotatingFileHandler",
             return_value=MagicMock(),
         ):
        main()

    mock_cmd.assert_called_once()
    mock_sdk_log.assert_called_once()
    mock_cost.assert_called_once()
    mock_conc.assert_called_once()
    mock_sdk_model.assert_called_once_with(model_profiles.DEFAULT_MODEL)


def test_every_build_agent_handler_gets_sdk_configuration() -> None:
    """Every subcommand whose handler calls _build_agent is SDK-configured."""
    import ast
    import inspect

    import maestro.cli as cli_mod
    from maestro.cli import _SDK_RUNNING_COMMANDS

    tree = ast.parse(inspect.getsource(cli_mod))
    agent_handlers = {
        node.name for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("cmd_")
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_build_agent"
            for call in ast.walk(node)
        )
    }
    assert "cmd_run" in agent_handlers
    assert "cmd_resume_task" in agent_handlers

    main_def = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    handler_dict = next(
        node.value for node in ast.walk(main_def)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "handlers"
        and isinstance(node.value, ast.Dict)
    )
    command_of = {
        value.id: key.value
        for key, value in zip(handler_dict.keys, handler_dict.values, strict=True)
        if isinstance(key, ast.Constant) and isinstance(value, ast.Name)
    }
    for handler in agent_handlers:
        assert handler in command_of
        assert command_of[handler] in _SDK_RUNNING_COMMANDS


def test_measure_claim_grounding_buckets_each_status() -> None:
    """Every live claim lands in exactly one bucket; a non-string claim is skipped.

    resolved / wrong_polarity / unresolvable count by their persisted status; a
    claim whose citation is 'absent', carries an unrecognised status, or was never
    cited folds into 'absent' (no resolving evidence), so the four buckets always
    partition the string-claim total.
    """
    model = SelfModel()
    model.capabilities_known = [
        "resolved claim",
        "wrong polarity claim",
        "unparseable claim",
        "absent anchor claim",
        "never cited claim",
        "unknown status claim",
        123,  # non-string: skipped, not counted  # type: ignore[list-item]
    ]
    model.claim_evidence = {
        claim_key("resolved claim"): {"status": "resolved"},
        claim_key("wrong polarity claim"): {"status": "wrong_polarity"},
        claim_key("unparseable claim"): {"status": "unresolvable"},
        claim_key("absent anchor claim"): {"status": "absent"},
        claim_key("unknown status claim"): {"status": "banana"},
        # "never cited claim" has no entry at all.
    }

    counts = _measure_claim_grounding(model)

    assert counts == {
        "total": 6,
        "resolved": 1,
        "wrong_polarity": 1,
        "unresolvable": 1,
        # "absent anchor" + "never cited" + "unknown status" all read as absent.
        "absent": 3,
    }


_FuncNode = ast.FunctionDef | ast.AsyncFunctionDef


def _cli_functions(tree: ast.AST) -> dict[str, _FuncNode]:
    """Map every top-level-or-nested function name in ``cli.py`` to its node."""
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _handler_dispatch_map(tree: ast.AST) -> dict[str, str]:
    """Map subcommand name -> handler function name from ``main()``'s dispatch.

    Narrowed to the ``handlers = {...}`` assignment inside ``main()`` so an
    unrelated ``str -> cmd_*`` dict literal elsewhere in ``cli.py`` cannot inject
    phantom entries into the guard's scope.
    """
    main = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    handlers: dict[str, str] = {}
    for node in ast.walk(main):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "handlers"
            for target in node.targets
        ):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and isinstance(value, ast.Name)
            ):
                handlers[key.value] = value.id
    return handlers


def _seam_routed_subcommands(tree: ast.AST) -> dict[str, str]:
    """Map each subcommand name to its handler, keeping only seam-routed ones.

    A subcommand is *seam-routed* when its dispatch handler calls a
    ``_build_*_backend(...)`` seam — i.e. the data it prints comes from the
    flag-selected backend (``state.db`` under ``[paths] sqlite_state_enabled``,
    else the JSONL sidecar), not from one fixed file.  A command that reads a
    raw path instead (``cost-report`` / ``cost-per-task`` read
    ``config.usage_log_path``) is deliberately excluded: naming its sidecar is
    accurate, not drift.

    Detection is *direct-call* only: the seam call must appear inside the
    handler's own body (sync or ``async``).  A handler that reaches a seam
    solely through a helper it delegates to is out of scope — widening that
    would need a call graph, and every handler in the tree today calls its seam
    directly.
    """
    handlers = _handler_dispatch_map(tree)
    functions = _cli_functions(tree)

    def _calls_seam(name: str) -> bool:
        node = functions.get(name)
        if node is None:
            return False
        return any(
            isinstance(call.func, ast.Name)
            and call.func.id.startswith("_build_")
            and call.func.id.endswith("_backend")
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )

    return {name: fn for name, fn in handlers.items() if _calls_seam(fn)}


def _subparser_help_by_name(tree: ast.AST) -> dict[str, str]:
    """Collect every ``add_parser("<name>", help="...")`` literal pair."""
    helps: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "add_parser"
            or not node.args
        ):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "help"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                helps[first.value] = kw.value.value
    return helps


def _names_bare_sidecar(text: str) -> bool:
    """True when *text* cites a ``*.jsonl`` sidecar without naming the flag."""
    return bool(re.search(r"\b[\w.]+\.jsonl\b", text)) and (
        "sqlite_state_enabled" not in text
    )


def test_backend_seam_subcommand_help_names_the_seam() -> None:
    """A seam-routed subcommand's help/docstring must not name a bare sidecar.

    ``list-versions`` and ``measure-gate-triggers`` read through
    ``_build_version_archive_backend`` / ``_build_gate_activation_backend``, so
    under ``[paths] sqlite_state_enabled`` they surface rows from ``state.db``
    and the JSONL sidecar stops being written.  Prose that names only the
    sidecar points an operator at a file the command never opens — so both the
    argparse ``help=`` and the handler's own docstring are covered.  Registry-
    shaped on purpose: any future subcommand routed through a ``_build_*_backend``
    seam is covered the moment it is registered.
    """
    source = (Path(__file__).resolve().parent.parent / "maestro" / "cli.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source, filename="cli.py")

    seam_routed = _seam_routed_subcommands(tree)
    helps = _subparser_help_by_name(tree)
    functions = _cli_functions(tree)

    # Non-vacuity floor: the two known seam-routed readers must be in scope,
    # so a walker that silently collects nothing cannot pass for free.
    assert "list-versions" in seam_routed
    assert "measure-gate-triggers" in seam_routed
    # And the raw-path readers must stay out of scope.
    assert "cost-report" not in seam_routed
    assert "cost-per-task" not in seam_routed

    offenders = {
        name: helps[name]
        for name in seam_routed
        if name in helps and _names_bare_sidecar(helps[name])
    }
    assert offenders == {}, (
        "seam-routed subcommand help names a bypassed sidecar as its sole "
        f"source: {offenders}"
    )

    doc_offenders = {
        name: handler
        for name, handler in seam_routed.items()
        if (doc := ast.get_docstring(functions[handler])) and _names_bare_sidecar(doc)
    }
    assert doc_offenders == {}, (
        "seam-routed handler docstring names a bypassed sidecar without naming "
        f"the flag that selects the backend: {doc_offenders}"
    )
