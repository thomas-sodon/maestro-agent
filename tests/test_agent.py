"""Tests for MaestroAgent construction — fully mocked I/O."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import agent as agent_module
from maestro import conscious_workspace as cw
from maestro.agent import (
    MaestroAgent,
    write_heartbeat,
)
from maestro.audit_checks import (
    JsonImpactReviewSurfacedStore,
)
from maestro.audit_checks_phantom import (
    JsonOrphanedCureStore,
    JsonUnconfirmedOwnLandingStore,
)
from maestro.audit_checks_settlement import (
    JsonlSettledVerdictsLedger,
)
from maestro.config import Config
from maestro.conscious_workspace import JsonlConsciousEpisodeLedger
from maestro.consciousness import Consciousness, PendingPRRequest
from maestro.cost_tracking import JsonlCostSampleStore
from maestro.deed_ledger import JsonlDeedLedger
from maestro.directive_ledger import JsonlDroppedDirectiveLedger
from maestro.escalation_bridge import JsonEscalationLedger
from maestro.evolution import EvolutionEngine, QualityGateResult
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.lever_capture import JsonlLeverAttemptsLedger
from maestro.loop import assembly as assembly_module
from maestro.loop.pr_remediation import (
    STALLED_REMEDIATION_LAP_CAP,
)
from maestro.loop.task_generation import _extract_file_paths
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from maestro.outcome_grounding import JsonMergeRequestServicedStore
from maestro.planner import Planner
from maestro.pr_ledger import JsonPRLedger
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    JsonlFetchTruthLedger,
    JsonlObligationForkLedger,
    JsonlPrProvenanceLedger,
    JsonlStandDownVerdictLedger,
    JsonPreflightPassLedger,
    JsonRemediationTrackerStore,
    TrackerState,
)
from maestro.sdk import SDKError
from maestro.settlement_record_reconciler import (
    EscalationStanddownReconciler,
    JsonlSettlementReconciliationLedger,
)
from maestro.state_store import (
    SqliteConsciousEpisodeLedger,
    SqliteCostSampleStore,
    SqliteDeedLedger,
    SqliteDroppedDirectiveLedger,
    SqliteEscalationLedger,
    SqliteFetchTruthLedger,
    SqliteImpactReviewSurfacedStore,
    SqliteLeverAttemptsLedger,
    SqliteMergeRequestServicedStore,
    SqliteObligationForkLedger,
    SqliteOrphanedCureStore,
    SqliteOwnedBacklogLedger,
    SqlitePreflightPassStore,
    SqlitePRLedger,
    SqlitePrProvenanceLedger,
    SqliteRemediationTrackerStore,
    SqliteSettledVerdictsLedger,
    SqliteSettlementReconciliationLedger,
    SqliteStandDownVerdictLedger,
    SqliteTaskQueue,
    SqliteUnconfirmedOwnLandingStore,
)
from maestro.stats import JsonlOwnedBacklogLedger
from maestro.task_queue import TaskQueue
from tests.conftest import MockQueryResult
from tests.support.gap_fuel import make_fuel, stub_evolve_phase

FAKE_DIR = Path("/fake/agent_test")

_OK_REVIEW = QualityGateResult(
    passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
    raw_output="Ruff: clean\n\nMypy: clean\n\nTests: clean",
)


def _build_agent(
    mem_fs: dict[str, str], config: Config | None = None,
) -> MaestroAgent:
    if config is None:
        config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR)
    config.ensure_dirs()
    if isinstance(assembly_module.StateStore, MagicMock):
        # ``@patch("maestro.loop.assembly.StateStore")`` hands every SQLite adapter a
        # MagicMock. All but one are lazy, but the remediation tracker loads its
        # state eagerly at construction and unpacks it (proposal 106), so the
        # mocked store must hand back a real, empty ``TrackerState`` rather than
        # a MagicMock that iterates empty.
        mocked = assembly_module.StateStore.return_value
        mocked.load_remediation_tracker.return_value = TrackerState(
            {}, {}, {}, {}, 0,
        )
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    executor = Executor()
    planner = Planner()
    evolution = EvolutionEngine(memory, consciousness, journal, notifier,
                                config=config)
    vcs = MagicMock()
    vcs.has_changes.return_value = False

    return MaestroAgent(
        config=config, consciousness=consciousness, journal=journal,
        memory=memory, executor=executor, planner=planner,
        evolution=evolution, notifier=notifier, vcs=vcs,
    )


def test_agent_construction(mem_fs: dict[str, str]) -> None:
    """MaestroAgent should construct with all dependencies."""
    agent = _build_agent(mem_fs)
    assert agent.is_running is False
    assert agent.current_task is None
    assert agent.current_phase is None
    assert agent.config.memory_dir == FAKE_DIR


def test_agent_seam_uses_json_queue_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSON backend."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.task_queue, TaskQueue)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_queue_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite backend.

    ``StateStore`` is patched so no real ``sqlite3.connect`` against the fake
    ``memory_dir`` occurs — only the seam's class selection is asserted.
    """
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.task_queue, SqliteTaskQueue)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")


def test_agent_seam_uses_json_cost_samples_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL backend."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.cost_samples, JsonlCostSampleStore)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_cost_samples_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite cost-sample backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.cost_samples, SqliteCostSampleStore)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_shares_one_state_store_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """A single ``StateStore`` backs both the queue and the cost samples.

    The seam constructs ``StateStore`` exactly once (not one per store) and
    hands the same instance to both adapters, so the flag-on path opens one
    connection to ``state.db``.
    """
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    mock_store.assert_called_once_with(FAKE_DIR / "state.db")
    assert isinstance(agent.task_queue, SqliteTaskQueue)
    assert isinstance(agent.cost_samples, SqliteCostSampleStore)
    assert isinstance(agent.deed_ledger, SqliteDeedLedger)
    assert isinstance(agent.conscious_episodes, SqliteConsciousEpisodeLedger)
    assert isinstance(agent.owned_backlog, SqliteOwnedBacklogLedger)
    assert isinstance(agent.pr_provenance, SqlitePrProvenanceLedger)
    assert isinstance(
        agent.settlement_reconciliations, SqliteSettlementReconciliationLedger,
    )
    assert isinstance(agent.obligation_fork, SqliteObligationForkLedger)
    assert isinstance(agent.fetch_truth, SqliteFetchTruthLedger)
    assert isinstance(agent.preflight_passes, SqlitePreflightPassStore)
    assert isinstance(agent.dropped_directives, SqliteDroppedDirectiveLedger)
    assert isinstance(agent.settled_verdicts, SqliteSettledVerdictsLedger)
    assert isinstance(agent.stand_down_verdicts, SqliteStandDownVerdictLedger)
    assert isinstance(agent.pr_ledger_backend, SqlitePRLedger)
    assert isinstance(agent.escalation_standdowns, SqliteEscalationLedger)
    assert isinstance(
        agent.unconfirmed_own_landing, SqliteUnconfirmedOwnLandingStore,
    )
    assert isinstance(agent.orphaned_cure, SqliteOrphanedCureStore)
    assert isinstance(
        agent.impact_review_surfaced, SqliteImpactReviewSurfacedStore,
    )
    assert isinstance(
        agent.merge_request_serviced, SqliteMergeRequestServicedStore,
    )
    assert isinstance(agent.lever_attempts, SqliteLeverAttemptsLedger)
    assert agent.task_queue._store is mock_store.return_value
    assert agent.cost_samples._store is mock_store.return_value
    assert agent.deed_ledger._store is mock_store.return_value
    assert agent.conscious_episodes._store is mock_store.return_value
    assert agent.owned_backlog._store is mock_store.return_value
    assert agent.pr_provenance._store is mock_store.return_value
    assert agent.settlement_reconciliations._store is mock_store.return_value
    assert agent.obligation_fork._store is mock_store.return_value
    assert agent.fetch_truth._store is mock_store.return_value
    assert agent.preflight_passes._store is mock_store.return_value
    assert agent.dropped_directives._store is mock_store.return_value
    assert agent.settled_verdicts._store is mock_store.return_value
    assert agent.stand_down_verdicts._store is mock_store.return_value
    assert agent.pr_ledger_backend._store is mock_store.return_value
    assert agent.escalation_standdowns._store is mock_store.return_value
    assert agent.unconfirmed_own_landing._store is mock_store.return_value
    assert agent.orphaned_cure._store is mock_store.return_value
    assert agent.impact_review_surfaced._store is mock_store.return_value
    assert agent.merge_request_serviced._store is mock_store.return_value
    assert agent.lever_attempts._store is mock_store.return_value


@patch("maestro.loop.assembly.logger")
@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_runs_eager_migration_when_flag_on(
    mock_store: MagicMock, mock_logger: MagicMock, mem_fs: dict[str, str],
) -> None:
    """The flag-on seam eagerly migrates the whole fleet once and logs it.

    Patching ``StateStore`` alone runs the wiring for coverage but asserts
    nothing behavioural, so a regression that dropped or mis-wired
    ``migrate_from_legacy`` — or stopped logging its provenance summary — would
    still pass. This pins both: the eager pass fires exactly once with
    ``config.memory_dir`` and its rendered summary reaches the one log line.
    """
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    _build_agent(mem_fs, config)
    migrate = mock_store.return_value.migrate_from_legacy
    migrate.assert_called_once_with(FAKE_DIR)
    migrate.return_value.render.assert_called_once_with()
    mock_logger.info.assert_any_call(
        "state-store migration (state.db): %s",
        migrate.return_value.render.return_value,
    )


def test_agent_seam_uses_json_deed_ledger_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL deed ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.deed_ledger, JsonlDeedLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_deed_ledger_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite deed-ledger backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.deed_ledger, SqliteDeedLedger)


def test_agent_seam_uses_json_conscious_episodes_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL conscious-episode
    ledger (proposal 130), the observe-only ``attend`` phase's append seam."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.conscious_episodes, JsonlConsciousEpisodeLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_conscious_episodes_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite conscious-episode backend
    (proposal 130) — the wiring ``test_attend_records_over_sqlite_backend`` swaps
    in by hand, asserted here at the ``__init__`` seam itself."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.conscious_episodes, SqliteConsciousEpisodeLedger)


def test_agent_seam_uses_json_lever_attempts_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL lever ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.lever_attempts, JsonlLeverAttemptsLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_lever_attempts_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite lever-attempts backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.lever_attempts, SqliteLeverAttemptsLedger)


def test_agent_seam_uses_json_owned_backlog_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL backlog ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.owned_backlog, JsonlOwnedBacklogLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_owned_backlog_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite owned-backlog backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.owned_backlog, SqliteOwnedBacklogLedger)


def test_agent_seam_uses_json_pr_provenance_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL provenance ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.pr_provenance, JsonlPrProvenanceLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_pr_provenance_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite provenance backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.pr_provenance, SqlitePrProvenanceLedger)


def test_agent_seam_uses_json_settlement_reconciliations_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL reconciliation ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(
        agent.settlement_reconciliations, JsonlSettlementReconciliationLedger,
    )


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_settlement_reconciliations_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite reconciliation backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(
        agent.settlement_reconciliations, SqliteSettlementReconciliationLedger,
    )


def test_agent_seam_uses_json_obligation_fork_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL fork ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.obligation_fork, JsonlObligationForkLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_obligation_fork_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite obligation-fork backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.obligation_fork, SqliteObligationForkLedger)


def test_agent_seam_uses_json_fetch_truth_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL fetch ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.fetch_truth, JsonlFetchTruthLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_fetch_truth_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite fetch-truth backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.fetch_truth, SqliteFetchTruthLedger)


def test_agent_seam_uses_json_preflight_passes_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSON preflight ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.preflight_passes, JsonPreflightPassLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_preflight_passes_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite preflight-pass backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.preflight_passes, SqlitePreflightPassStore)


def test_agent_seam_uses_json_dropped_directives_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.dropped_directives, JsonlDroppedDirectiveLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_dropped_directives_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite dropped-directive backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.dropped_directives, SqliteDroppedDirectiveLedger)


def test_agent_seam_uses_json_settled_verdicts_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.settled_verdicts, JsonlSettledVerdictsLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_settled_verdicts_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite settled-verdicts backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.settled_verdicts, SqliteSettledVerdictsLedger)


def test_agent_seam_uses_json_stand_down_verdicts_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSONL ledger."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.stand_down_verdicts, JsonlStandDownVerdictLedger)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_stand_down_verdicts_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite stand-down-verdict backend."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.stand_down_verdicts, SqliteStandDownVerdictLedger)


def test_agent_seam_uses_json_pr_ledger_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSON pr-ledger backend,
    and ``PRLedger`` wraps that selected backend."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.pr_ledger_backend, JsonPRLedger)
    assert agent.pr_ledger._backend is agent.pr_ledger_backend


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_pr_ledger_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite pr-ledger backend sharing
    the agent's one ``StateStore``, and ``PRLedger`` wraps it."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.pr_ledger_backend, SqlitePRLedger)
    assert agent.pr_ledger_backend._store is mock_store.return_value
    assert agent.pr_ledger._backend is agent.pr_ledger_backend


def test_agent_seam_uses_json_escalation_ledger_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSON escalation
    backend, and the agent-composed reconciler registry holds it first."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.escalation_standdowns, JsonEscalationLedger)
    # The escalation-standdown reconciler is composed first (historical order)
    # and holds the flag-selected backend.
    first = agent._settlement_reconciler_registry[0]
    assert isinstance(first, EscalationStanddownReconciler)
    assert first._backend is agent.escalation_standdowns


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_escalation_ledger_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite escalation backend sharing
    the agent's one ``StateStore``, and the escalation reconciler holds it."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.escalation_standdowns, SqliteEscalationLedger)
    assert agent.escalation_standdowns._store is mock_store.return_value
    first = agent._settlement_reconciler_registry[0]
    assert isinstance(first, EscalationStanddownReconciler)
    assert first._backend is agent.escalation_standdowns


def test_agent_seam_uses_json_remediation_tracker_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSON tracker backend,
    and ``RemediationTracker`` persists through that selected backend."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(
        agent.remediation_tracker_backend, JsonRemediationTrackerStore,
    )
    assert agent.remediation_tracker._backend is agent.remediation_tracker_backend


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_remediation_tracker_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite tracker backend sharing the
    agent's one ``StateStore``, and the tracker persists through it."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(
        agent.remediation_tracker_backend, SqliteRemediationTrackerStore,
    )
    assert agent.remediation_tracker_backend._store is mock_store.return_value
    assert agent.remediation_tracker._backend is agent.remediation_tracker_backend
    # The settlement reconciler keeps holding the same live tracker instance,
    # so eviction reaches the selected store.
    assert any(
        getattr(r, "_tracker", None) is agent.remediation_tracker
        for r in agent._settlement_reconciler_registry
    )


def test_agent_seam_uses_json_unconfirmed_landing_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSON sidecar store."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(
        agent.unconfirmed_own_landing, JsonUnconfirmedOwnLandingStore,
    )


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_unconfirmed_landing_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite unconfirmed-landing store."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(
        agent.unconfirmed_own_landing, SqliteUnconfirmedOwnLandingStore,
    )


def test_agent_seam_uses_json_orphaned_cure_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSON sidecar store."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.orphaned_cure, JsonOrphanedCureStore)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_orphaned_cure_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite orphaned-cure store."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(agent.orphaned_cure, SqliteOrphanedCureStore)


def test_agent_seam_uses_json_impact_review_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSON sidecar store."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(agent.impact_review_surfaced, JsonImpactReviewSurfacedStore)


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_impact_review_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite impact-review store."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(
        agent.impact_review_surfaced, SqliteImpactReviewSurfacedStore,
    )


def test_agent_seam_uses_json_merge_request_serviced_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Default (``sqlite_state_enabled`` off) constructs the JSON sidecar store."""
    agent = _build_agent(mem_fs)
    assert agent.config.sqlite_state_enabled is False
    assert isinstance(
        agent.merge_request_serviced, JsonMergeRequestServicedStore,
    )


@patch("maestro.loop.assembly.StateStore")
def test_agent_seam_uses_sqlite_merge_request_serviced_when_flag_on(
    mock_store: MagicMock, mem_fs: dict[str, str],
) -> None:
    """With the flag on the seam constructs the SQLite serviced-marker store."""
    config = Config(
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, sqlite_state_enabled=True,
    )
    agent = _build_agent(mem_fs, config)
    assert isinstance(
        agent.merge_request_serviced, SqliteMergeRequestServicedStore,
    )


def test_build_identity_context(mem_fs: dict[str, str]) -> None:
    """_build_identity_context should combine the framing and identity prompts."""
    agent = _build_agent(mem_fs)
    ctx = agent._build_identity_context()
    assert "Maestro" in ctx
    assert "awakening" not in ctx


# --- run_loop() task_every tests ---


@pytest.mark.asyncio
async def test_run_loop_calls_generate_task_on_task_every(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() calls generate_task() every task_every iterations."""
    agent = make_agent()

    from maestro.task_queue import QueuedTask
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]

    async def _fake_generate(session=None):  # type: ignore[no-untyped-def]
        # Mirror generate_task: append to the queue.
        agent.task_queue.save([
            QueuedTask(
                task_id="qg",
                goal="Evaluate which open question is most testable.",
                linked_question="Q1",
            ),
        ])

    agent.generate_task = AsyncMock(side_effect=_fake_generate)  # type: ignore[method-assign]
    agent.run = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # 3 iterations, task_every=3 -> generate_task() fires once (on iteration 3).
    # run_task pops the queued goal in the same iteration.
    await agent.run_loop(interval=0, max_iters=3, task_every=3, evolve_every=999)

    agent.generate_task.assert_called_once()
    agent.run.assert_called_once_with(
        "Evaluate which open question is most testable.",
        context="self_directed",
        source="self_directed",
        linked_question="Q1",
        pr_branch=None,
        acceptance_criteria=None,
        workspace_moment_id=None,
        workspace_item_ids=None,
        defer_persist=False,
    )


@pytest.mark.asyncio
async def test_run_loop_skips_run_when_generate_task_leaves_queue_empty(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() does not call run() when generate_task() enqueues nothing."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent.run = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=3, task_every=3, evolve_every=999)

    agent.generate_task.assert_called_once()
    agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_task_every_zero_disables_generation(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() does not call generate_task() when task_every=0 (disabled)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent.run = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=6, task_every=0, evolve_every=999)

    agent.generate_task.assert_not_called()
    # run_task only fires when there's pending work — queue is empty here.
    agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_evolve_every_zero_disables_evolution(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() does not call evolve() when evolve_every=0 (disabled)."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(return_value=(None, None))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=3, task_every=999, evolve_every=0)

    agent.evolve.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_writes_heartbeat(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """run_loop() writes a heartbeat file during each iteration."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    heartbeat_path = str(agent.config.memory_dir / "loop.heartbeat")
    assert heartbeat_path in mem_fs
    # Heartbeat should contain an ISO timestamp
    assert "T" in mem_fs[heartbeat_path]


@pytest.mark.asyncio
async def test_run_loop_writes_heartbeat_at_startup(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """run_loop() writes the heartbeat before any phase has a chance to run.

    Mocks ``_persist_progress`` so it does *not* refresh the heartbeat. The
    only remaining write site is the explicit one at the top of ``run_loop``,
    so the file's presence after the loop exits proves it was written before
    any phase ran. This guarantees the Docker healthcheck can pass even when
    the first iteration's contemplate phase makes a slow LLM call or crash
    recovery delays the loop body.
    """
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent._persist_progress = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    heartbeat_path = str(agent.config.memory_dir / "loop.heartbeat")
    assert heartbeat_path in mem_fs
    assert "T" in mem_fs[heartbeat_path]


def test_persist_progress_refreshes_heartbeat(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """_persist_progress() refreshes the heartbeat after every phase."""
    agent = make_agent()
    heartbeat_path = str(agent.config.memory_dir / "loop.heartbeat")

    # No heartbeat yet — only created by _write_heartbeat / _persist_progress.
    assert heartbeat_path not in mem_fs

    agent._persist_progress(1, "contemplate")
    assert heartbeat_path in mem_fs
    first = mem_fs[heartbeat_path]
    assert "T" in first

    # A second call rewrites the file (mem_fs records the latest content).
    with patch("maestro.agent.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2030, 1, 1, tzinfo=UTC)
        mock_dt.UTC = UTC
        agent._persist_progress(1, "evolve")

    assert mem_fs[heartbeat_path] == "2030-01-01T00:00:00+00:00"
    assert mem_fs[heartbeat_path] != first


def test_persist_progress_preserves_last_shutdown_when_shutting_down(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A phase-boundary write after a shutdown signal must keep last_shutdown.

    ``_on_shutdown_signal`` eagerly writes ``last_shutdown`` to lifecycle.json
    so a SIGKILL after ``stop_grace_period`` is classified as a requested
    shutdown rather than a crash. If a phase completes *after* the signal, the
    subsequent ``_persist_progress`` full-overwrite would erase that marker,
    reopening the false-crash window. With the shutdown event set, the payload
    must re-include a fresh ``last_shutdown``.
    """
    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)

    # Signal has fired: the event is set and the eager marker is on disk.
    agent._ensure_shutdown_event().set()
    agent._write_lifecycle_shutdown()
    assert "last_shutdown" in json.loads(mem_fs[lifecycle_path])

    # A phase completes after the signal and persists at the phase boundary.
    agent._persist_progress(3, "evolve")

    data = json.loads(mem_fs[lifecycle_path])
    assert data.get("last_shutdown") is not None
    # The resume cursor from this write is present too — the marker is added,
    # not substituted for the progress payload.
    assert data["iteration"] == 3
    assert data["next_phase"] == "evolve"


def test_persist_progress_omits_last_shutdown_when_not_shutting_down(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """Without a set shutdown event, ``_persist_progress`` adds no marker.

    Control for the guard above: the marker is added only while shutting down.
    Covers both false branches — the event absent (never created) and the
    event created but unset — so the guard is provably conditional and never
    creates the event as a side effect of a normal phase write.
    """
    agent = make_agent()
    lifecycle_path = str(agent.config.lifecycle_path)

    # Event never created: a normal phase write must not create it, and must
    # not stamp last_shutdown.
    assert agent._shutdown_event is None
    agent._persist_progress(1, "contemplate")
    assert agent._shutdown_event is None
    assert "last_shutdown" not in json.loads(mem_fs[lifecycle_path])

    # Event created but not set (e.g. shutdown not requested): still no marker.
    agent._ensure_shutdown_event()
    assert not agent._shutdown_event.is_set()
    agent._persist_progress(2, "dream")
    assert "last_shutdown" not in json.loads(mem_fs[lifecycle_path])


def test_write_heartbeat_free_function_writes_file(mem_fs) -> None:  # type: ignore[no-untyped-def]
    """The module-level ``write_heartbeat`` writes an ISO timestamp to
    ``<memory_dir>/loop.heartbeat`` without needing a ``MaestroAgent``.

    This is what ``cmd_loop`` calls *before* ``_build_agent`` so the Docker
    healthcheck sees a fresh heartbeat within the first moment of the CLI
    starting, regardless of how long agent construction takes.
    """
    memory_dir = Path("/fake/heartbeat_standalone")
    heartbeat_path = str(memory_dir / "loop.heartbeat")

    assert heartbeat_path not in mem_fs

    write_heartbeat(memory_dir)

    assert heartbeat_path in mem_fs
    assert "T" in mem_fs[heartbeat_path]


def test_write_heartbeat_free_function_swallows_oserror() -> None:
    """A filesystem error during ``write_heartbeat`` is logged, not raised.

    The caller in ``cmd_loop`` runs before any directory setup, so a missing
    memory_dir must never crash the loop command startup.
    """
    memory_dir = Path("/fake/heartbeat_oserror")
    with patch.object(
        type(memory_dir / "loop.heartbeat"),
        "write_text",
        side_effect=OSError("read-only fs"),
    ):
        # Must not raise.
        write_heartbeat(memory_dir)


def test_agent_write_heartbeat_method_delegates_to_free_function(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """``MaestroAgent._write_heartbeat`` must go through the free function.

    Keeps every heartbeat write — pre-build, run_loop entry, and each
    ``_persist_progress`` phase boundary — on a single code path so
    behavioural changes only need to be made in one place.
    """
    agent = make_agent()
    with patch("maestro.agent.write_heartbeat") as mock_write:
        agent._write_heartbeat()
        mock_write.assert_called_once_with(agent.config.memory_dir)


@pytest.mark.asyncio
async def test_run_loop_calls_ensure_main_branch(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() calls ensure_main_branch at the start of each iteration."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with patch.object(agent.evolution, "ensure_main_branch") as mock_ensure:
        await agent.run_loop(interval=0, max_iters=3, task_every=0, evolve_every=999)

    assert mock_ensure.call_count == 3


@pytest.mark.asyncio
async def test_run_loop_crashes_on_ensure_main_branch_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates errors from ensure_main_branch instead of suppressing them."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with patch.object(
        agent.evolution, "ensure_main_branch", side_effect=RuntimeError("boom"),
    ), pytest.raises(RuntimeError, match="boom"):
        await agent.run_loop(interval=0, max_iters=2, task_every=0, evolve_every=999)

    # Loop crashed on the first iteration — contemplate never ran.
    assert agent.contemplate.call_count == 0  # type: ignore[attr-defined]
    # shutdown() was invoked with crashed=True so lifecycle.json has no last_shutdown.
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_logs_originating_exception_on_crash(  # type: ignore[no-untyped-def]
    make_agent, caplog,
) -> None:
    """A ``BaseException`` escape from a phase is logged at ERROR with exc_info.

    Regression for #1905: ``run_loop``'s outer ``finally`` detects
    ``crashed_unwind`` via ``sys.exc_info()`` and calls
    ``shutdown(crashed=True)``, but historically never logged the
    originating exception. The only operator-visible trace in
    ``maestro.log`` was the generic ``WARNING: Maestro shutting down
    on unhandled exception`` from ``shutdown()`` — masking the failure
    type and stack at the codebase's outermost crash boundary.
    """
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._process_messages = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=asyncio.CancelledError(
            "Cancelled via cancel scope 7832227b2fc0",
        ),
    )
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="maestro"), pytest.raises(
        asyncio.CancelledError,
    ):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    crash_records = [
        r for r in caplog.records
        if r.name == "maestro"
        and r.levelno == logging.ERROR
        and r.exc_info is not None
        and r.exc_info[0] is asyncio.CancelledError
    ]
    assert len(crash_records) == 1
    record = crash_records[0]
    assert "CancelledError" in record.getMessage()
    # shutdown() was invoked with crashed=True so lifecycle.json has no last_shutdown.
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


# --- current_phase exposure tests ---


@pytest.mark.asyncio
async def test_run_loop_tracks_current_phase_during_execution(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``_current_phase`` reflects the phase while its handler is running."""
    agent = make_agent()

    snapshots: dict[str, str | None] = {}

    async def snap_contemplate(*_args: Any, **_kwargs: Any) -> str:
        snapshots["contemplate"] = agent.current_phase
        return "reflection"

    async def snap_evolve(*_args: Any, **_kwargs: Any) -> None:
        snapshots["evolve"] = agent.current_phase

    def snap_ensure_main_branch() -> None:
        snapshots["ensure_main_branch"] = agent.current_phase

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(side_effect=snap_contemplate)  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    stub_evolve_phase(agent, AsyncMock(side_effect=snap_evolve))
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with patch.object(
        agent.evolution, "ensure_main_branch", side_effect=snap_ensure_main_branch,
    ):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=1)

    assert snapshots["ensure_main_branch"] == "ensure_main_branch"
    assert snapshots["contemplate"] == "contemplate"
    assert snapshots["evolve"] == "evolve"


@pytest.mark.asyncio
async def test_run_loop_clears_current_phase_between_iterations(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``_current_phase`` is None after a clean iteration ends."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    assert agent.current_phase is None


@pytest.mark.asyncio
async def test_run_loop_phases_report_own_current_phase_when_not_throttled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Phases whose body runs report their own ``current_phase``.

    ``_maybe_notify_throttle_transition`` is mocked here so the test
    exercises only the phase-reporting path — the actual throttle-active
    gating contract lives in
    ``test_run_loop_gates_heavy_llm_phases_when_throttle_active`` and
    ``test_run_loop_gates_contemplate_when_throttle_active``.
    """
    agent = make_agent()

    snapshots: dict[str, str | None] = {}

    def snap_ensure_main_branch() -> None:
        snapshots["ensure_main_branch"] = agent.current_phase

    async def snap_evolve(*_args: Any, **_kwargs: Any) -> None:
        snapshots["evolve"] = agent.current_phase

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    stub_evolve_phase(agent, AsyncMock(side_effect=snap_evolve))
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    # ``evolve_every=1`` so the gated ``evolve`` phase fires at iteration 1
    # — without it the gate suppresses the body and the snapshot below has
    # nothing to assert against.
    with patch.object(
        agent, "_compute_budget_throttle_multiplier",
        return_value=(1.0, 5.0, None),
    ), patch.object(
        agent, "_maybe_notify_throttle_transition", AsyncMock(),
    ), patch.object(
        agent.evolution, "ensure_main_branch", side_effect=snap_ensure_main_branch,
    ):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=1)

    # Phases that run report their own name — no standby override.
    assert snapshots["ensure_main_branch"] == "ensure_main_branch"
    assert snapshots["evolve"] == "evolve"


@pytest.mark.asyncio
async def test_run_loop_smoke_bypasses_throttle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``--smoke`` bypasses the throttle entirely — multiplier is always 1.0."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent._send_status_update = AsyncMock()  # type: ignore[method-assign]

    # A conservative limit that would normally trip the throttle.
    agent.config.max_loop_budget_usd_per_hour = 1.0
    agent.config.budget_rate_min_cost_usd = 0.0

    def fail_build(*_a: object, **_kw: object) -> None:
        raise AssertionError("smoke mode must not consult cost rates")

    with (
        patch("maestro.agent.build_cost_rates", side_effect=fail_build),
        patch("maestro.loop.lifecycle.build_cost_rates", side_effect=fail_build),
    ):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999, smoke=True,
        )

    assert agent.budget_throttle_active is False


@pytest.mark.asyncio
async def test_shutdown_clears_current_phase(mem_fs: dict[str, str]) -> None:
    """``shutdown()`` nulls ``_current_phase`` so a stopped loop reads idle."""
    agent = _build_agent(mem_fs)
    agent._running = True
    agent._current_phase = "evolve"
    with patch.object(agent.notifier, "notify", AsyncMock()):
        await agent.shutdown()
    assert agent.current_phase is None


# --- TaskRecord source field tests ---


def test_task_record_source_defaults_to_external(mem_fs: dict[str, str]) -> None:
    """TaskRecord.source defaults to 'external' for backward compatibility."""
    from maestro.memory import PlanStep, TaskRecord

    record = TaskRecord(
        task_id="abc",
        goal="test",
        plan=[PlanStep(step_id="s1", description="step")],
        outcome="success",
        result_summary="done",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00Z",
    )
    assert record.source == "external"


def test_task_record_source_self_directed(mem_fs: dict[str, str]) -> None:
    """TaskRecord.source can be set to 'self_directed'."""
    from maestro.memory import PlanStep, TaskRecord

    record = TaskRecord(
        task_id="abc",
        goal="test",
        plan=[PlanStep(step_id="s1", description="step")],
        outcome="success",
        result_summary="done",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00Z",
        source="self_directed",
    )
    assert record.source == "self_directed"


# --- Bug 25: run() should reject empty/whitespace goals ---


@pytest.mark.asyncio
async def test_run_rejects_empty_goal(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Bug 25: run() must raise ValueError when goal is empty string."""
    agent = make_agent()
    with pytest.raises(ValueError, match="goal"):
        await agent.run("")


@pytest.mark.asyncio
async def test_run_rejects_whitespace_goal(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Bug 25: run() must raise ValueError when goal is only whitespace."""
    agent = make_agent()
    with pytest.raises(ValueError, match="goal"):
        await agent.run("   ")


@pytest.mark.asyncio
async def test_run_rejects_empty_goal_does_not_set_running(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Bug 25: run() with empty goal must not leave _running=True after raising."""
    agent = make_agent()
    with pytest.raises(ValueError):
        await agent.run("")
    assert agent.is_running is False
    assert agent.current_task is None


# --- Bug 26: run_loop() should tolerate contemplate() failures ---


@pytest.mark.asyncio
async def test_run_loop_crashes_on_contemplate_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates contemplate() errors so the crash-triage path fires."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    agent.contemplate = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("introspection exploded"),
    )

    with pytest.raises(RuntimeError, match="introspection exploded"):
        await agent.run_loop(interval=0, max_iters=3, task_every=0, evolve_every=999)

    # Crashed on iteration 1; contemplate was only called once.
    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


# --- Bug 27: run_loop() should tolerate self-directed run() failures ---


@pytest.mark.asyncio
async def test_run_loop_crashes_on_self_directed_run_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates self-directed run() errors instead of suppressing them."""
    agent = make_agent()

    from maestro.task_queue import QueuedTask

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]

    async def _fake_generate(session=None):  # type: ignore[no-untyped-def]
        agent.task_queue.save([QueuedTask(task_id="q", goal="Do something")])

    agent.generate_task = AsyncMock(side_effect=_fake_generate)  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    run_call_count = 0

    async def flaky_run(task: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal run_call_count
        run_call_count += 1
        raise RuntimeError("run blew up")

    agent.run = flaky_run  # type: ignore[method-assign]

    # task_every=1 → run_task fires every iteration; loop crashes on the first one.
    with pytest.raises(RuntimeError, match="run blew up"):
        await agent.run_loop(interval=0, max_iters=3, task_every=1, evolve_every=999)

    assert run_call_count == 1
    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


# --- Bug 28: is_running should be True throughout run_loop iterations ---


@pytest.mark.asyncio
async def test_run_loop_is_running_true_during_iterations(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Bug 28: is_running must be True throughout the run_loop execution, not False between steps."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    running_states_after_contemplate: list[bool] = []

    async def recording_contemplate(**kwargs: object) -> str:
        # After contemplate() itself finishes, check if run_loop has restored _running
        result = "reflection"
        # Use a callback to capture state immediately after contemplate returns
        return result

    agent.contemplate = AsyncMock(return_value="reflection", side_effect=None)  # type: ignore[method-assign]

    # Capture is_running between contemplate and implement_approved_plans
    implement_call_count = 0

    async def rec_impl(**_kwargs: object) -> None:
        nonlocal implement_call_count
        implement_call_count += 1
        running_states_after_contemplate.append(agent.is_running)

    agent._implement_approved_plans = rec_impl  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=2, task_every=0, evolve_every=999)

    assert all(running_states_after_contemplate), (
        f"is_running was False between contemplate() and _implement_approved_plans() "
        f"in iterations: {[i for i, v in enumerate(running_states_after_contemplate) if not v]}. "
        "run_loop() must keep _running=True throughout all iterations."
    )


# --- Coverage: signal handler fallback (line 103) ---


@pytest.mark.asyncio
async def test_signal_handler_not_implemented(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_install_signal_handlers degrades gracefully when add_signal_handler raises."""
    agent = make_agent()

    loop = asyncio.get_running_loop()
    with patch.object(loop, "add_signal_handler", side_effect=NotImplementedError):
        agent._install_signal_handlers()

    # Flag is set even when handlers aren't supported (prevents retries)
    assert agent._signal_handlers_installed


@pytest.mark.asyncio
async def test_signal_handler_already_installed_returns_early(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_install_signal_handlers returns early if already installed (line 103)."""
    agent = make_agent()
    agent._signal_handlers_installed = True

    loop = asyncio.get_running_loop()
    with patch.object(loop, "add_signal_handler") as mock_handler:
        agent._install_signal_handlers()

    mock_handler.assert_not_called()


# --- Coverage: empty plan → failure outcome (line 197) ---


@pytest.mark.asyncio
async def test_run_empty_plan_returns_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run() returns failure when planner returns an empty step list."""
    agent = make_agent()

    with patch.object(agent.planner, "decompose", AsyncMock(return_value=[])), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        record = await agent.run("do something")

    assert record.outcome == "failure"


@pytest.mark.asyncio
async def test_run_threads_loop_detected_into_replan_and_warns(  # type: ignore[no-untyped-def]
    make_agent, caplog,
) -> None:
    """A loop-detected step failure warns and steers replan (issue #1184).

    ``StepResult.loop_detected`` previously had no production consumer, so a
    step that aborted on a tool loop was replanned with no explicit steer to
    change approach. ``Agent.run()`` now reads the flag: it logs a loop-specific
    WARNING and threads ``loop_detected=True`` into ``planner.replan``.
    """
    from maestro.executor import StepResult
    from maestro.memory import PlanStep

    agent = make_agent()
    agent.config.loop_max_replans = 1
    agent.planner.decompose = AsyncMock(  # type: ignore[method-assign]
        return_value=[PlanStep("s1", "loop step")],
    )
    agent.planner.replan = AsyncMock(return_value=[])  # type: ignore[method-assign]
    agent.executor.execute = AsyncMock(  # type: ignore[method-assign]
        return_value=StepResult(
            success=False, output="", error="loop detected: Bash",
            loop_detected=True,
        ),
    )
    agent._distill_findings = AsyncMock(  # type: ignore[method-assign]
        return_value="findings",
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        record = await agent.run("do the loop task")

    assert record.outcome == "failure"
    agent.planner.replan.assert_called_once()
    assert agent.planner.replan.call_args.kwargs["loop_detected"] is True
    loop_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "tool loop" in r.getMessage()
    ]
    assert len(loop_warnings) == 1


@pytest.mark.asyncio
async def test_run_concurrent_group_threads_loop_detected_into_replan(  # type: ignore[no-untyped-def]
    make_agent, caplog,
) -> None:
    """The concurrent-group failure branch reads loop_detected too (issue #1184).

    Two independent steps run as one concurrent group; the looping one returns
    ``StepResult(loop_detected=True)``. ``Agent.run()`` must capture that flag
    alongside ``failed_in_group``, log the loop WARNING, and pass
    ``loop_detected=True`` into ``planner.replan``.
    """
    from maestro.executor import StepResult
    from maestro.memory import PlanStep

    agent = make_agent()
    agent.config.loop_max_replans = 1
    # s1 and s2 are both dependency-free and thus ready together; a third step
    # declaring a dependency edge forces the graph path (a plan with NO edges
    # runs one-step-per-group sequentially), so s1 and s2 form one concurrent
    # group and the concurrent-branch code is exercised.
    agent.planner.decompose = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            PlanStep("s1", "ok step"),
            PlanStep("s2", "loop step"),
            PlanStep(step_id="s3", description="final", depends_on=["s1", "s2"]),
        ],
    )
    agent.planner.replan = AsyncMock(return_value=[])  # type: ignore[method-assign]
    agent._distill_findings = AsyncMock(  # type: ignore[method-assign]
        return_value="findings",
    )

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        if step.step_id == "s2":
            return StepResult(
                success=False, output="", error="loop detected: Bash",
                loop_detected=True,
            )
        return StepResult(success=True, output="ok")

    agent.executor.execute = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_exec,
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        await agent.run("do the concurrent loop task")

    agent.planner.replan.assert_called_once()
    assert agent.planner.replan.call_args.kwargs["loop_detected"] is True
    loop_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "tool loop" in r.getMessage()
    ]
    assert len(loop_warnings) == 1


# --- Coverage: evolve() journal write exception (lines 327-328) ---


# --- Proposal 118: multi-PR chain-emission wiring in evolve() ---


@pytest.mark.asyncio
async def test_evolve_multi_pr_flag_on_routes_through_submit_proposal_chain(  # type: ignore[no-untyped-def]
    make_agent, monkeypatch,
) -> None:
    """Flag on + a multi-PR emission routes through submit_proposal_chain."""
    agent = make_agent()
    _fuel_evolve(monkeypatch)
    agent.config.multi_pr_proposals_enabled = True
    proposal = MagicMock()
    slices = [MagicMock()]

    with (
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        patch.object(agent.consciousness, "introspect", AsyncMock(return_value="deep")),
        patch.object(agent.evolution, "propose_plan_chain", AsyncMock(
            return_value=(proposal, slices),
        )) as mock_chain,
        patch.object(agent.evolution, "submit_proposal_chain", AsyncMock()) as mock_submit_chain,
        patch.object(agent.evolution, "propose_plan", AsyncMock()) as mock_propose,
        patch.object(agent.evolution, "submit_for_review", AsyncMock()) as mock_submit,
        patch.object(agent.memory, "get_all_plans", return_value=[]),
    ):
        await agent.evolve()

    mock_chain.assert_called_once()
    mock_submit_chain.assert_called_once_with(
        proposal, slices, exempt_saturation=False,
    )
    mock_propose.assert_not_called()
    mock_submit.assert_not_called()


@pytest.mark.asyncio
async def test_evolve_multi_pr_flag_off_routes_through_propose_plan(  # type: ignore[no-untyped-def]
    make_agent, monkeypatch,
) -> None:
    """Flag off routes through propose_plan → submit_for_review unchanged."""
    agent = make_agent()
    _fuel_evolve(monkeypatch)
    agent.config.multi_pr_proposals_enabled = False
    plan = MagicMock(priority="capability", title="test")

    with (
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        patch.object(agent.consciousness, "introspect", AsyncMock(return_value="deep")),
        patch.object(agent.evolution, "propose_plan", AsyncMock(return_value=plan)) as mock_propose,
        patch.object(agent.evolution, "submit_for_review", AsyncMock()) as mock_submit,
        patch.object(agent.evolution, "propose_plan_chain", AsyncMock()) as mock_chain,
        patch.object(agent.evolution, "submit_proposal_chain", AsyncMock()) as mock_submit_chain,
        patch.object(agent.memory, "get_all_plans", return_value=[]),
    ):
        await agent.evolve()

    mock_propose.assert_called_once()
    mock_submit.assert_called_once_with(
        plan, self_initiated=True,
        exempt_saturation=False,
    )
    mock_chain.assert_not_called()
    mock_submit_chain.assert_not_called()


# --- Coverage: run_loop contribution check failure (lines 579-580) ---


@pytest.mark.asyncio
async def test_run_loop_crashes_on_contribution_check_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates contribution-check errors."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("git broke"),
    )
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="git broke"):
        await agent.run_loop(interval=0, max_iters=2, task_every=0, evolve_every=999)
    assert agent.contemplate.call_count == 0  # type: ignore[attr-defined]
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


# --- Coverage: run_loop merged-PR check ---


@pytest.mark.asyncio
async def test_run_loop_checks_merged_plans(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() calls check_merged_plans and journals merged plans."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    from maestro.memory import EvolutionPlan

    plan = EvolutionPlan(
        plan_id="merged01",
        title="Merged Feature",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="implemented",
        created_at="2025-01-01T00:00:00Z",
        pr_url="https://github.com/o/r/pull/42",
    )
    agent.memory.save_evolution_plan(plan)
    from maestro.evolution import MergeCheckResult
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(merged=["merged01"]),
    )

    count_before = agent.consciousness.load_self_model().evolution_count

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    agent.evolution.check_merged_plans.assert_called()
    entries = agent.journal.read_recent_events(10)
    assert any("Evolution merged" in e.content for e in entries)
    # evolution_count tracks integrations: a merged plan bumps it exactly once.
    count_after = agent.consciousness.load_self_model().evolution_count
    assert count_after == count_before + 1


@pytest.mark.asyncio
async def test_run_loop_rejected_plan_does_not_increment_evolution(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A rejected (PR closed without merging) plan must not bump evolution_count."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    from maestro.evolution import MergeCheckResult
    from maestro.memory import EvolutionPlan

    plan = EvolutionPlan(
        plan_id="closed_count",
        title="Closed Feature",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="rejected",
        rejection_reason="PR closed without merging",
        created_at="2025-01-01T00:00:00Z",
        pr_url="https://github.com/o/r/pull/99",
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(
            rejected=[("closed_count", "PR closed without merging")],
        ),
    )
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]

    count_before = agent.consciousness.load_self_model().evolution_count

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    agent.evolution.learn_from_rejection.assert_called_once()
    count_after = agent.consciousness.load_self_model().evolution_count
    assert count_after == count_before


@pytest.mark.asyncio
async def test_run_loop_crashes_on_merge_journal_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates merge-journal write failures."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    from maestro.evolution import MergeCheckResult
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(merged=["merged01"]),
    )
    agent.journal.record_event = MagicMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]

    with pytest.raises(OSError, match="disk full"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_crashes_on_merged_check_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates check_merged_plans() failures."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("network error"),
    )

    with pytest.raises(RuntimeError, match="network error"):
        await agent.run_loop(interval=0, max_iters=2, task_every=0, evolve_every=999)
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_offloads_check_merged_plans_to_thread(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() must run check_merged_plans via ``asyncio.to_thread``.

    The method fans out N serial ``gh pr view`` subprocess round-trips
    (``is_pr_merged``/``is_pr_closed``); running it inline would block the
    event loop for the whole phase (issue #197). With no implemented plans
    saved, the real method touches neither the host nor the network, so we can
    wrap and assert the *real* bound method is the one handed to ``to_thread``.
    """
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    real_to_thread = asyncio.to_thread

    async def tracking_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        tracking_to_thread.calls.append(getattr(func, "__name__", repr(func)))  # type: ignore[attr-defined]
        return await real_to_thread(func, *args, **kwargs)
    tracking_to_thread.calls = []  # type: ignore[attr-defined]

    with patch("maestro.agent.asyncio.to_thread", tracking_to_thread):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    assert "check_merged_plans" in tracking_to_thread.calls  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_learns_from_closed_pr(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() calls learn_from_rejection when check_merged_plans returns rejected plans."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    from maestro.evolution import MergeCheckResult
    from maestro.memory import EvolutionPlan

    plan = EvolutionPlan(
        plan_id="closed01",
        title="Closed Feature",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="rejected",
        rejection_reason="PR closed without merging",
        created_at="2025-01-01T00:00:00Z",
        pr_url="https://github.com/o/r/pull/99",
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(
            rejected=[("closed01", "PR closed without merging")],
        ),
    )
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    agent.evolution.learn_from_rejection.assert_called_once()
    call_args = agent.evolution.learn_from_rejection.call_args
    assert call_args[0][0].plan_id == "closed01"
    assert call_args[0][1] == "PR closed without merging"


@pytest.mark.asyncio
async def test_run_loop_crashes_on_learn_from_rejection_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates learn_from_rejection failures from check_merged_plans."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    from maestro.evolution import MergeCheckResult
    from maestro.memory import EvolutionPlan

    plan = EvolutionPlan(
        plan_id="closed02",
        title="Closed Feature 2",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="rejected",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(
            rejected=[("closed02", "PR closed without merging")],
        ),
    )
    agent.evolution.learn_from_rejection = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("learning failed"),
    )

    with pytest.raises(RuntimeError, match="learning failed"):
        await agent.run_loop(interval=0, max_iters=2, task_every=0, evolve_every=999)
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_crashes_on_mark_learned_failure_in_merged_check(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates mark_rejection_learned failures during merged-PR check."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    from maestro.evolution import MergeCheckResult
    from maestro.memory import EvolutionPlan

    plan = EvolutionPlan(
        plan_id="closed_mark", title="Closed",
        category="capability", rationale="t", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="rejected", created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(
            rejected=[("closed_mark", "PR closed without merging")],
        ),
    )
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]
    agent.memory.mark_rejection_learned = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("disk full"),
    )

    with pytest.raises(OSError, match="disk full"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)
    agent.memory.mark_rejection_learned.assert_called_with("closed_mark")
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Additional coverage — agent.py uncovered lines
# ---------------------------------------------------------------------------


def test_install_signal_handlers_tolerates_not_implemented(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_install_signal_handlers degrades gracefully when add_signal_handler raises NotImplementedError (lines 103-114)."""
    agent = make_agent()

    mock_loop = MagicMock()
    mock_loop.add_signal_handler.side_effect = NotImplementedError("Windows")
    with patch("asyncio.get_running_loop", return_value=mock_loop):
        # Should not raise; the NotImplementedError is caught internally
        agent._install_signal_handlers()

    assert agent._signal_handlers_installed is True


@pytest.mark.asyncio
async def test_run_returns_failure_when_plan_is_empty(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run() sets outcome='failure' when the planner returns an empty step list (line 197)."""
    agent = make_agent()

    with patch.object(agent.planner, "decompose", AsyncMock(return_value=[])), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        record = await agent.run("do something")

    assert record.outcome == "failure"


@pytest.mark.asyncio
async def test_implement_plan_aborts_when_status_changes(make_agent) -> None:  # type: ignore[no-untyped-def]
    """implement_plan() aborts and calls cleanup_branch when plan status changes after implement (lines 411-416)."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-status-change",
        title="Status Change Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    # After implement(), change the plan status to rejected
    async def _fake_implement(p, session=None):  # type: ignore[no-untyped-def]
        agent.memory.update_plan_status("p-status-change", "rejected", rejection_reason="changed")
        return "base123"

    cleanup_mock = MagicMock()
    with (
        patch.object(agent.evolution, "implement", side_effect=_fake_implement),
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        patch.object(agent.contributions, "get_current_head", return_value="abc123"),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
    ):
        await agent.implement_plan("p-status-change")

    cleanup_mock.assert_called_once_with("p-status-change")


@pytest.mark.asyncio
async def test_implement_plan_journal_write_exception_tolerated(make_agent) -> None:  # type: ignore[no-untyped-def]
    """implement_plan() tolerates journal write exception after PR creation."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-journal-fail",
        title="Journal Fail Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base000")),
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        patch.object(agent.contributions, "get_current_head", return_value="abc123"),
        patch.object(agent.contributions, "get_new_commits", return_value=[]),
        patch.object(agent.memory, "get_plan", return_value=plan),
        patch.object(agent.evolution, "create_pr", AsyncMock(return_value="https://github.com/repo/pull/100")),
        patch.object(agent.journal, "record_event", side_effect=OSError("disk full")),
        patch.object(agent.contributions, "record_own_commit", MagicMock()),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        # Should not raise despite journal write failure
        await agent.implement_plan("p-journal-fail")


@pytest.mark.asyncio
async def test_implement_plan_records_all_intermediate_commits(make_agent) -> None:  # type: ignore[no-untyped-def]
    """implement_plan() records every commit on the evolution branch, not just HEAD.

    The implementation commit and any self-review commits must each land in
    own_commits.txt so the next iteration's contribution check does not
    misclassify them as external work (issue #7).
    """
    from maestro.memory import EvolutionPlan
    from maestro.vcs import CommitInfo

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-multi-commit",
        title="Multi Commit Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    commits = [
        CommitInfo(hash="c1" * 20, subject="impl", author="maestro"),
        CommitInfo(hash="c2" * 20, subject="review fix r1", author="maestro"),
        CommitInfo(hash="c3" * 20, subject="review fix r2", author="maestro"),
    ]
    record_mock = MagicMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base000")),
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        patch.object(agent.contributions, "get_current_head", return_value="head999"),
        patch.object(agent.contributions, "get_new_commits", return_value=commits),
        patch.object(agent.memory, "get_plan", return_value=plan),
        patch.object(
            agent.evolution, "create_pr",
            AsyncMock(return_value="https://github.com/repo/pull/1"),
        ),
        patch.object(agent.contributions, "record_own_commit", record_mock),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-multi-commit")

    # Every intermediate commit recorded in chronological order — not just HEAD.
    assert record_mock.call_count == 3
    assert [c.args[0] for c in record_mock.call_args_list] == [
        "c1" * 20, "c2" * 20, "c3" * 20,
    ]


@pytest.mark.asyncio
async def test_implement_plan_records_head_when_range_unknown(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When base_hash is empty, fall back to recording HEAD once."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-empty-base",
        title="Empty Base Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    record_mock = MagicMock()
    get_new_commits_mock = MagicMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="")),
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        patch.object(agent.contributions, "get_current_head", return_value="head999"),
        patch.object(agent.contributions, "get_new_commits", get_new_commits_mock),
        patch.object(agent.memory, "get_plan", return_value=plan),
        patch.object(
            agent.evolution, "create_pr",
            AsyncMock(return_value="https://github.com/repo/pull/1"),
        ),
        patch.object(agent.contributions, "record_own_commit", record_mock),
        patch.object(agent.consciousness, "increment_evolution", MagicMock()),
    ):
        await agent.implement_plan("p-empty-base")

    # Range walk skipped (base unknown) — fallback records HEAD exactly once.
    get_new_commits_mock.assert_not_called()
    record_mock.assert_called_once_with("head999")


_FAIL_REVIEW = QualityGateResult(
    passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
    raw_output="Ruff: E501 line too long\nMypy: clean\nTests: clean",
)


@pytest.mark.asyncio
async def test_implement_plan_gate_failure_sets_failed_status(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Gate failure after auto_fix must mark the plan failed (not rejected).

    'failed' is distinct from 'rejected' — it does NOT trigger
    rejection-learning or the 7-day cooldown. _implement_approved_plans
    retries the plan up to max_implementation_attempts times.
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-gate-fail",
        title="Gate Fail Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base123")),
        patch.object(agent.evolution, "run_quality_gates", return_value=_FAIL_REVIEW),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
    ):
        await agent.implement_plan("p-gate-fail")

    stored = agent.memory.get_plan("p-gate-fail")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1
    assert stored.last_failure_reason is not None
    assert stored.last_failure_reason.startswith("Quality gate failed:")
    assert stored.rejection_reason is not None
    assert stored.rejection_reason.startswith("Quality gate failed:")
    assert stored.rejection_learned is False


@pytest.mark.asyncio
async def test_implement_plan_auto_fix_transient_exception_not_demoted(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A transient auto_fix SDKError re-raises WITHOUT demoting the plan.

    Transient SDK failures are retried in place by the iteration-level
    handler; demoting here would consume the human approval and strand
    the plan. The transient carve-out runs before the auto-fix
    attribution branch, so the plan status stays unchanged and
    ``_demote_plan_to_failed`` is never called.
    """
    from maestro.memory import EvolutionPlan
    from maestro.sdk import ErrorCategory, SDKError

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-autofix-transient",
        title="Auto-fix Transient",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    transient = SDKError("rate limited", category=ErrorCategory.RATE_LIMIT)
    demote_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base123")),
        patch.object(agent.evolution, "run_quality_gates", return_value=_FAIL_REVIEW),
        patch.object(
            agent.evolution, "auto_fix",
            AsyncMock(side_effect=transient),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent, "_demote_plan_to_failed", demote_mock),
        pytest.raises(SDKError) as excinfo,
    ):
        await agent.implement_plan("p-autofix-transient")

    assert excinfo.value.category is ErrorCategory.RATE_LIMIT
    demote_mock.assert_not_awaited()
    stored = agent.memory.get_plan("p-autofix-transient")
    assert stored is not None
    assert stored.status == "approved"


@pytest.mark.asyncio
async def test_implement_plan_gate_failure_notifies_telegram(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Gate failure must send a Telegram notification so Thomas sees the failure
    immediately (previously this path only opened a silent GitHub issue)."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-gate-notify",
        title="Gate Notify Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    notify_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base123")),
        patch.object(agent.evolution, "run_quality_gates", return_value=_FAIL_REVIEW),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.notifier, "notify", notify_mock),
    ):
        await agent.implement_plan("p-gate-notify")

    notify_mock.assert_awaited_once()
    sent_message = notify_mock.await_args.args[0]
    assert "Gate Notify Test" in sent_message
    assert "Quality gate failed" in sent_message
    assert "attempt 1/" in sent_message


@pytest.mark.asyncio
async def test_implement_plan_gate_failure_notifier_exception_propagates(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A real notifier ``Exception`` on the gate-failure path propagates.

    ``_demote_plan_to_failed`` updates the plan status BEFORE calling
    ``_notify_best_effort``. The status update therefore lands even
    when the notifier raises, but the exception still surfaces — only
    ``CancelledError`` is suppressed by best-effort notification.
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-gate-notifier-down",
        title="Gate Notifier Down",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    create_issue_mock = MagicMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base123")),
        patch.object(agent.evolution, "run_quality_gates", return_value=_FAIL_REVIEW),
        patch.object(agent.evolution, "auto_fix", AsyncMock(return_value=False)),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", create_issue_mock),
        patch.object(
            agent.notifier, "notify",
            AsyncMock(side_effect=RuntimeError("Telegram down")),
        ),
        pytest.raises(RuntimeError, match="Telegram down"),
    ):
        await agent.implement_plan("p-gate-notifier-down")

    stored = agent.memory.get_plan("p-gate-notifier-down")
    assert stored is not None
    assert stored.status == "failed"
    # max_implementation_attempts defaults to 3; this is attempt 1 — not terminal,
    # so create_issue must NOT fire yet.
    create_issue_mock.assert_not_called()


@pytest.mark.asyncio
async def test_implement_plan_non_transient_exception_sets_failed_status(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Non-transient exceptions in implement_plan() must mark the plan 'failed'
    (not 'rejected') so _implement_approved_plans can retry it and
    _process_rejected_plans does NOT incorrectly learn from it. The exception
    must still propagate so run_loop() treats it as a real failure."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-non-transient",
        title="Non-Transient Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    with (
        patch.object(
            agent.evolution, "implement",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.notifier, "notify", AsyncMock()),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await agent.implement_plan("p-non-transient")

    stored = agent.memory.get_plan("p-non-transient")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1
    assert stored.last_failure_reason is not None
    assert stored.last_failure_reason.startswith("Implementation failed:")
    assert stored.rejection_reason is not None
    assert stored.rejection_reason.startswith("Implementation failed:")
    assert stored.rejection_learned is False


# ──────────────────────────────────────────────────────────────────────
# Core agent async tests (run, contemplate, evolve)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_success(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()

    plan_response = json.dumps([{"description": "Step 1"}, {"description": "Step 2"}])
    step_response = "Step completed successfully."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        mock_distill.return_value = MockQueryResult(response="Key findings here")

        record = await agent.run("Test goal")

    assert record.outcome == "success"
    assert record.goal == "Test goal"
    assert len(record.plan) == 2
    assert record.duration_secs >= 0
    assert record.key_findings == "Key findings here"
    # Verify planner was called with the goal
    mock_plan.assert_called_once()
    prompt = mock_plan.call_args.kwargs.get("prompt", mock_plan.call_args[1].get("prompt", ""))
    assert "Test goal" in prompt
    # Verify executor was called for each step
    assert mock_exec.call_count == 2

    # Check journal was written
    entries = agent.journal.read_recent_events()
    assert len(entries) >= 1
    assert "Completed" in entries[-1].content

    # Check memory was recorded
    tasks = agent.memory.get_recent_tasks()
    assert len(tasks) == 1


def _broadcast_moment() -> cw.ConsciousMoment:
    """Assemble a real moment carrying one admitted, above-floor item."""
    item = cw.build_workspace_item(
        item_id="i1", source_kind="audit_finding", source_ref="ref",
        observed_at="2026-01-01T00:00:00+00:00", trust="mechanical_observation",
        confidence=0.5, expires_at=None, summary="a bounded summary",
    )
    return cw.assemble_moment(
        [item], assembled_at="2026-01-01T00:00:00+00:00",
        permission_summary="all", blind_spots=["gap"],
        predecessor_id=None, continuity="genesis",
    )


@pytest.mark.asyncio
async def test_run_broadcasts_conscious_moment_to_task_execution(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """Proposal 130: the task-execution planning decision (``Planner.decompose``
    reached in ``run()``) receives the shared conscious moment. Flag-on: the
    decompose prompt carries the ``## Conscious moment (broadcast to
    task_execution)`` block and exactly one receipt is recorded — consumer
    ``task_execution`` stamped with the moment's ``selection_hash``. Flag-off: no
    block is delivered and no receipt is recorded. ``_broadcast_moment_prefix`` is
    the only broadcast site ``run()`` reaches, so exactly one receipt lands.
    """
    agent = make_agent()
    moment = _broadcast_moment()
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Step completed successfully."

    # --- Flag off: no block prepended, no receipt recorded ---
    agent.config.conscious_workspace_broadcast_enabled = False
    agent._active_conscious_moment = moment
    agent._active_broadcast_receipts = []
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        await agent.run("Do a task")
    prompt_off = mock_plan.call_args.kwargs["prompt"]
    assert "## Conscious moment" not in prompt_off
    assert agent._active_broadcast_receipts == []

    # --- Flag on: block prepended at the front, one task_execution receipt ---
    agent.config.conscious_workspace_broadcast_enabled = True
    agent._active_conscious_moment = moment
    agent._active_broadcast_receipts = []
    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        await agent.run("Do a task")
    prompt_on = mock_plan.call_args.kwargs["prompt"]

    block, _ = cw.render_active_moment(moment, "task_execution")
    assert "## Conscious moment (broadcast to task_execution)" in block
    assert prompt_on.startswith(block + "\n\n")
    assert len(agent._active_broadcast_receipts) == 1
    receipt = agent._active_broadcast_receipts[0]
    assert receipt.consumer == "task_execution"
    assert receipt.moment_hash == moment.selection_hash


@pytest.mark.asyncio
async def test_run_step_failure(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.loop_max_replans = 0

    plan_response = json.dumps([{"description": "Will fail"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.side_effect = RuntimeError("SDK error")

        record = await agent.run("Failing goal")

    assert record.outcome == "failure"
    assert len(record.errors) == 1
    assert "SDK error" in record.errors[0]
    mock_plan.assert_called_once()
    mock_exec.assert_called_once()

    entries = agent.journal.read_recent_events()
    assert any("failed" in e.content.lower() for e in entries)


@pytest.mark.asyncio
async def test_run_partial_failure(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.loop_max_replans = 0

    plan_response = json.dumps([{"description": "Step OK"}, {"description": "Step fail"}])

    call_count = 0

    async def alternating_exec(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return MockQueryResult(response="Done")
        raise RuntimeError("Step 2 failed")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=alternating_exec), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)

        record = await agent.run("Partial goal")

    assert record.outcome == "partial"
    mock_plan.assert_called_once()
    entries = agent.journal.read_recent_events()
    assert any("Partial" in e.content for e in entries)


@pytest.mark.asyncio
async def test_contemplate(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()

    reflection = "I notice I am curious about my own processes."

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        # introspect call, then update_self_model call
        mock_q.side_effect = [
            MockQueryResult(response=reflection),
            MockQueryResult(response=json.dumps({
                "identity_statements": ["I am curious"],
                "values": ["growth"],
            })),
        ]

        result = await agent.contemplate()

    assert result == reflection
    assert mock_q.call_count == 2  # introspect + update_self_model

    entries = agent.journal.read_recent()
    assert len(entries) >= 1
    assert reflection in entries[0].content

    model = agent.consciousness.load_self_model()
    assert "I am curious" in model.identity_statements


@pytest.mark.asyncio
async def test_run_sets_running_flag(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert agent.is_running is False
    assert agent.current_task is None

    plan_response = json.dumps([{"description": "Quick step"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="ok")

        await agent.run("Flag test")

    # After completion, flags should be reset
    assert agent.is_running is False
    assert agent.current_task is None
    mock_plan.assert_called_once()
    mock_exec.assert_called_once()


@pytest.mark.asyncio
async def test_evolve(make_agent, monkeypatch):  # type: ignore[no-untyped-def]
    agent = make_agent()
    _fuel_evolve(monkeypatch)

    plan_json = json.dumps({
        "title": "Deeper reflection",
        "category": "awareness",
        "priority": "resilience",
        "rationale": "Need more depth",
        "proposed_changes": ["Add reflection layers"],
        "files_affected": ["maestro/consciousness.py", "CLAUDE.md"],
        "expected_impact": "Move toward self_reflection",
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_c, \
         patch("maestro.evolution.query", new_callable=AsyncMock) as mock_e, \
         patch("maestro.evolution.subprocess.run") as mock_sub:
        # evolution.propose_plan() only: reflect is deleted (proposal 155).
        mock_e.side_effect = [MockQueryResult(response=plan_json)]
        # self_review subprocess calls (ruff, mypy, pytest)
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )

        await agent.evolve()

    # Proposal 149 deleted the stage assessment: consciousness is not queried.
    assert mock_c.call_count == 0
    # One priced call (propose_plan) and no quality-gate subprocess.
    assert mock_e.call_count == 1
    assert mock_sub.call_count == 0

    # Should have a pending plan
    plans = agent.memory.get_pending_plans()
    assert len(plans) == 1
    assert plans[0].title == "Deeper reflection"
    assert plans[0].priority == "resilience"


@pytest.mark.asyncio
async def test_run_returns_record_even_if_journal_fails(make_agent):  # type: ignore[no-untyped-def]
    """run() should return TaskRecord even when the outcome event write raises."""
    agent = make_agent()

    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock), \
         patch.object(agent.journal, "record_event", side_effect=OSError("disk full")):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="done")

        record = await agent.run("Journalless goal")

    assert record.outcome == "success"
    assert record.goal == "Journalless goal"
    # Memory should still be recorded
    tasks = agent.memory.get_recent_tasks()
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_evolve_does_not_skip_when_resilience_plan_pending(make_agent, monkeypatch):  # type: ignore[no-untyped-def]
    """The bug-first deferral gate has been removed: with a ``resilience``
    plan already pending, ``evolve()`` still runs reflect + propose_plan +
    submit_for_review on the new proposal."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    _fuel_evolve(monkeypatch)

    # Create a pending resilience plan (the legacy "bug_fix" bucket).
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="res1", title="Fix crash",
        category="infrastructure", rationale="crash fix",
        proposed_changes=["Fix it"], files_affected=["maestro/agent.py"],
        expected_impact="stability", priority="resilience",
        created_at="2026-01-01T00:00:00Z",
    ))

    capability_plan = json.dumps({
        "title": "New capability",
        "category": "capability",
        "priority": "capability",
        "rationale": "Would be useful",
        "proposed_changes": ["Add it"],
        "files_affected": ["maestro/agent.py", "CLAUDE.md"],
        "expected_impact": "More capable",
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock), \
         patch("maestro.evolution.query", new_callable=AsyncMock) as mock_e, \
         patch("maestro.evolution.subprocess.run") as mock_sub:
        mock_e.side_effect = [MockQueryResult(response=capability_plan)]
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )

        await agent.evolve()

    # propose_plan fired — the pre-flight skip is gone.
    assert mock_e.call_count == 1
    pending = agent.memory.get_pending_plans()
    titles = {p.title for p in pending}
    assert "New capability" in titles
    assert "Fix crash" in titles


@pytest.mark.asyncio
async def test_evolve_stands_down_under_owned_wip(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """100: evolve()'s reflect/propose/submit tail is a self-initiated
    evolution-plan minter (alongside ``_implement_approved_plans``). With ``wip_primacy_enabled`` on and
    ``_current_task_focus`` reporting a ``TendOwnedPR`` (an open own PR with a
    self-clearable blocker), it stands down: reflect/propose_plan/submit_for_review
    never run and no ``next_task_directive`` is authored. The quality-gate
    and stage-assessment phases still run above the gate, but introspect
    is now relocated *below* the WIP gate (the #1 net-burn caller is spared on a
    refused tick) so it is never awaited here."""
    from maestro.wip_primacy import TendOwnedPR

    agent = make_agent()
    _fuel_evolve(monkeypatch)
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=TendOwnedPR(pr_number=99, owned_blocker="CONFLICTING"),
    )
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(raw_output="health"),
    )
    agent.consciousness.introspect = AsyncMock(  # type: ignore[method-assign]
        return_value="reflection",
    )
    agent.evolution.propose_plan = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]

    await agent.evolve()

    # The quality-gate phase ran above the gate; the minting tail
    # (and the relocated introspect) stood down.
    agent.consciousness.introspect.assert_not_awaited()
    agent.evolution.propose_plan.assert_not_awaited()
    agent.evolution.submit_for_review.assert_not_awaited()
    # No finish-this-PR directive authored — the wip_primacy gate stays sole writer.
    assert agent.consciousness.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_evolve_proposes_under_discretionary_focus(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """100: with the flag on but ``_current_task_focus`` reporting ``Discretionary``
    (no owned WIP), the gate opens and the minting tail runs — parity with the
    flag-off path, so the stand-down is surgical to open self-clearable WIP."""
    from maestro.wip_primacy import Discretionary

    agent = make_agent()
    _fuel_evolve(monkeypatch)
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=Discretionary(),
    )
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(raw_output="health"),
    )
    agent.consciousness.introspect = AsyncMock(  # type: ignore[method-assign]
        return_value="reflection",
    )
    plan = MagicMock()
    agent.evolution.propose_plan = AsyncMock(  # type: ignore[method-assign]
        return_value=plan,
    )
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]

    await agent.evolve()

    agent.evolution.propose_plan.assert_awaited_once()
    agent.evolution.submit_for_review.assert_awaited_once_with(
        plan, self_initiated=True,
        exempt_saturation=False,
    )


@pytest.mark.asyncio
async def test_evolve_flag_off_short_circuits_focus_probe(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """100: with ``wip_primacy_enabled`` off, ``_wip_owns_slot``'s ``and``
    short-circuits before probing ``_current_task_focus`` — the minting tail runs
    and the focus probe (a ``gh pr list`` + per-PR readiness observation) is never
    called, pinning the flag-off path byte-identical to before this change."""
    agent = make_agent()
    _fuel_evolve(monkeypatch)
    agent.config.wip_primacy_enabled = False
    agent._current_task_focus = MagicMock()  # type: ignore[method-assign]
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(raw_output="health"),
    )
    agent.consciousness.introspect = AsyncMock(  # type: ignore[method-assign]
        return_value="reflection",
    )
    agent.evolution.propose_plan = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(),
    )
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]

    await agent.evolve()

    # The tail ran and the flag-off path never probed focus.
    agent.evolution.submit_for_review.assert_awaited_once()
    agent._current_task_focus.assert_not_called()


def _fuel_evolve(monkeypatch, fuel=None) -> None:  # type: ignore[no-untyped-def]
    """Feed evolve one source_budget gap (proposal 155 step 4); WIP and the
    pre-mint gate still judge it."""
    from maestro.evolution_fuel import FuelSelection
    from maestro.loop import self_health

    fuel = fuel or make_fuel(owner_id="maestro:source_budget:maestro/agent.py")
    monkeypatch.setattr(
        self_health, "select_fuel",
        MagicMock(return_value=FuelSelection(selected=fuel)),
    )


def _pending_review_plan(plan_id: str):  # type: ignore[no-untyped-def]
    """A minimal ``pending_review`` evolution plan for saturating the backlog."""
    from maestro.memory import EvolutionPlan

    return EvolutionPlan(
        plan_id=plan_id,
        title="Measure the measurement machinery",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="pending_review",
        created_at="2025-01-01T00:00:00Z",
    )


def _stub_evolve_phases(agent) -> None:  # type: ignore[no-untyped-def]
    """Mock the non-minting evolve() phases (gates, introspect, stage) so the
    test drives only the pre-mint gate + reflect/propose/submit tail."""
    agent.evolution.run_quality_gates = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(raw_output="health"),
    )
    agent.consciousness.introspect = AsyncMock(  # type: ignore[method-assign]
        return_value="reflection",
    )


@pytest.mark.asyncio
async def test_evolve_pre_mint_gate_refuses_saturated_backlog(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """120: with ``pre_mint_generation_gate_enabled`` on and a saturated
    live-work backlog, evolve() declines to mint before spending the priced
    reflect + propose_plan calls — both stay un-awaited and the bounded
    prune/land directive is written into the empty ``next_task_directive`` slot.
    """
    from maestro.generation_gate import GENERATION_STANDDOWN_DIRECTIVE

    agent = make_agent()
    _fuel_evolve(monkeypatch)
    agent.config.pre_mint_generation_gate_enabled = True
    # _wip_owns_slot short-circuits False so the new gate is what stands down.
    agent.config.wip_primacy_enabled = False
    _stub_evolve_phases(agent)
    # Saturate the live-work corpus at the submit-block threshold (default 5).
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_pending_review_plan(f"p{i}") for i in range(5)],
    )
    agent.evolution.propose_plan = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]

    await agent.evolve()

    agent.evolution.propose_plan.assert_not_awaited()
    agent.evolution.submit_for_review.assert_not_awaited()
    assert (
        agent.consciousness.load_self_model().next_task_directive
        == GENERATION_STANDDOWN_DIRECTIVE
    )


@pytest.mark.asyncio
async def test_evolve_pre_mint_gate_admits_unsaturated_backlog(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """120: with the flag on but a non-saturated backlog and no theme
    concentration, the gate admits — reflect/propose/submit all fire and no
    directive is authored."""
    agent = make_agent()
    _fuel_evolve(monkeypatch)
    agent.config.pre_mint_generation_gate_enabled = True
    agent.config.wip_primacy_enabled = False
    _stub_evolve_phases(agent)
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[],
    )
    plan = MagicMock()
    agent.evolution.propose_plan = AsyncMock(  # type: ignore[method-assign]
        return_value=plan,
    )
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]

    await agent.evolve()

    agent.evolution.propose_plan.assert_awaited_once()
    agent.evolution.submit_for_review.assert_awaited_once_with(
        plan, self_initiated=True,
        exempt_saturation=False,
    )
    assert agent.consciousness.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_evolve_pre_mint_gate_flag_off_is_byte_identical(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """120: with the flag off, a saturated backlog is ignored entirely — the
    minting tail runs and no directive is written, byte-identical to today."""
    agent = make_agent()
    _fuel_evolve(monkeypatch)
    agent.config.pre_mint_generation_gate_enabled = False
    agent.config.wip_primacy_enabled = False
    _stub_evolve_phases(agent)
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_pending_review_plan(f"p{i}") for i in range(5)],
    )
    plan = MagicMock()
    agent.evolution.propose_plan = AsyncMock(  # type: ignore[method-assign]
        return_value=plan,
    )
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]

    await agent.evolve()

    agent.evolution.propose_plan.assert_awaited_once()
    agent.evolution.submit_for_review.assert_awaited_once_with(
        plan, self_initiated=True,
        exempt_saturation=False,
    )
    assert agent.consciousness.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_evolve_wip_primacy_preempts_pre_mint_gate(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """120: the ``_wip_owns_slot`` short-circuit precedes the pre-mint gate — an
    owned WIP focus stands down the tail before the new gate runs, so no
    directive is written even though the (unreached) gate would refuse the
    saturated backlog and write one. This pins the ordering."""
    from maestro.wip_primacy import TendOwnedPR

    agent = make_agent()
    _fuel_evolve(monkeypatch)
    agent.config.pre_mint_generation_gate_enabled = True
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=TendOwnedPR(pr_number=99, owned_blocker="CONFLICTING"),
    )
    _stub_evolve_phases(agent)
    agent.memory.get_all_plans = MagicMock(  # type: ignore[method-assign]
        return_value=[_pending_review_plan(f"p{i}") for i in range(5)],
    )
    agent.evolution.propose_plan = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.submit_for_review = AsyncMock()  # type: ignore[method-assign]

    await agent.evolve()

    agent.evolution.propose_plan.assert_not_awaited()
    agent.evolution.submit_for_review.assert_not_awaited()
    # The wip path writes no directive; the pre-mint gate (which would) never ran.
    assert agent.consciousness.load_self_model().next_task_directive is None


# ──────────────────────────────────────────────────────────────────────
# Bug hunt agent tests
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_task_curly_braces_in_goal(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Bug 73: generate_task() must not crash when recent task goals contain curly braces."""
    from maestro.memory import TaskRecord
    from maestro.sdk import QueryResult

    agent = make_agent()

    # Record a task whose goal contains curly braces
    record = TaskRecord(
        task_id="t1",
        goal="Fix the {config} loading issue",
        plan=[],
        outcome="success",
        result_summary="done",
        errors=[],
        duration_secs=1.0,
        timestamp="2024-01-01T00:00:00",
    )
    agent.memory.record_task(record)

    mock_result = QueryResult(response=json.dumps(
        {"prune": [], "edit": [], "enqueue": []},
    ))
    with patch("maestro.loop.task_generation.query", new=AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_empty_step_outputs_produce_no_output_summary(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Bug 74: run() produces 'No output' summary when all step outputs are empty."""
    from maestro.executor import StepResult
    from maestro.memory import PlanStep

    agent = make_agent()

    step1 = PlanStep(step_id="s1", description="step one")
    step2 = PlanStep(step_id="s2", description="step two")

    empty_result = StepResult(success=True, output="")

    with patch.object(agent.planner, "decompose", new=AsyncMock(return_value=[step1, step2])), \
         patch.object(agent.executor, "execute", new=AsyncMock(return_value=empty_result)), \
         patch("maestro.agent.query", new_callable=AsyncMock):

        record = await agent.run("test goal")

    assert record.result_summary == "No output"


def test_no_output_fallback_respects_available_budget(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """#353: the empty-output fallback honors the same 500-char budget as real output.

    The pure ``_cap_task_summary`` helper (extracted from ``run()``'s step-5
    summary construction, mirroring ``_frame_remediation_note``) computes
    ``available = max(0, 500 - len(branch_suffix) - note_reserve)``. Before the
    fix the empty-output branch emitted ``"No output" + branch_suffix`` with no
    budget check, so a task with empty step output and a long kept-branch name
    produced a >500-char summary while the sibling real-output path stayed
    capped. The helper now drops the 9-char ``"No output"`` literal once the
    budget cannot hold it (``available < 9``), and retains it when it can
    (``available >= 9``), preserving Bug-74 behavior.
    """
    agent = make_agent()

    # branch_suffix long enough that available == 0 (< 9): the fallback drops the
    # "No output" literal rather than emitting "No output" + branch_suffix (509+).
    long_suffix = "\n\nBranch: " + "b" * 490  # len == 500
    assert max(0, 500 - len(long_suffix)) < 9
    capped = agent._cap_task_summary("", long_suffix, 0)
    assert capped == long_suffix
    assert len(capped) <= 500
    assert "No output" not in capped

    # Boundary: available == 9 keeps the literal; available == 8 drops it.
    suffix_491 = "x" * 491  # available == 9
    assert agent._cap_task_summary("", suffix_491, 0) == "No output" + suffix_491
    suffix_492 = "x" * 492  # available == 8
    assert agent._cap_task_summary("", suffix_492, 0) == suffix_492

    # note_reserve shrinks the fallback budget too: a short branch plus a large
    # reserve drops the literal, so the reserved note room survives (#2457).
    short_branch = "\n\nBranch: b"  # len == 11; available == 489 without reserve
    assert agent._cap_task_summary("", short_branch, 495) == short_branch

    # Short/absent branch keeps the literal (existing Bug-74 empty-output path).
    assert agent._cap_task_summary("", "", 0) == "No output"

    # Real output is still sliced to the budget and suffixed under the cap.
    real = agent._cap_task_summary("y" * 1000, short_branch, 0)
    assert real == "y" * 489 + short_branch
    assert len(real) == 500


@pytest.mark.asyncio
async def test_empty_exception_message_step_result(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Bug 75: step.result should show 'Unknown error' when exception message is empty."""
    from maestro.executor import StepResult
    from maestro.memory import PlanStep

    agent = make_agent()
    agent.config.loop_max_replans = 0

    step = PlanStep(step_id="s1", description="fail step")

    fail_result = StepResult(success=False, output="", error="")

    with patch.object(agent.planner, "decompose", new=AsyncMock(return_value=[step])), \
         patch.object(agent.executor, "execute", new=AsyncMock(return_value=fail_result)), \
         patch("maestro.agent.query", new_callable=AsyncMock):

        record = await agent.run("failing goal")

    assert record.errors == ["Unknown error"]
    assert record.plan[0].result == "Unknown error"


@pytest.mark.asyncio
async def test_run_completes_task_despite_shutdown_event_set(make_agent):  # type: ignore[no-untyped-def]
    """084 (retiring 057): a SIGTERM mid-task no longer interrupts ``run()``.

    With the shutdown event set before the step-group loop, every step still
    runs to completion and ``run()`` returns a normal outcome (the removed
    ``interrupted`` verdict no longer exists); the completed task is persisted,
    not discarded. ``run_loop`` then stops only at the next phase boundary."""
    from maestro.executor import StepResult
    from maestro.memory import PlanStep

    agent = make_agent()
    steps = [
        PlanStep(step_id="s1", description="step 1"),
        PlanStep(step_id="s2", description="step 2"),
    ]
    shutdown_event = asyncio.Event()
    agent._shutdown_event = shutdown_event

    async def decompose_then_shutdown(*a, **kw):  # type: ignore[no-untyped-def]
        # SIGTERM arrives mid-task: the event is set before any group runs.
        shutdown_event.set()
        return steps

    agent.memory.record_task = MagicMock()  # type: ignore[method-assign]
    ok = StepResult(success=True, output="done", error="")

    with patch.object(agent.planner, "decompose", new=AsyncMock(side_effect=decompose_then_shutdown)), \
         patch.object(agent.executor, "execute", new=AsyncMock(return_value=ok)), \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_distill.return_value = MockQueryResult(response="findings")
        record = await agent.run("interrupted-but-finished goal")

    # Every step ran to completion despite the shutdown request.
    assert [s.status for s in record.plan] == ["done", "done"]
    assert record.outcome == "success"
    # The completed task is persisted — not discarded as a non-event.
    agent.memory.record_task.assert_called_once()


@pytest.mark.asyncio
async def test_generate_task_none_goal_no_crash(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Bug: generate_task must handle tasks with None goal field without crashing."""
    agent = make_agent()

    # Write a corrupted task record with goal=None
    task_data = {
        "task_id": "abc",
        "goal": None,
        "plan": [],
        "outcome": "failure",
        "result_summary": "test",
        "errors": [],
        "duration_secs": 1.0,
        "timestamp": "2025-01-01T00:00:00",
        "source": "external",
    }
    tasks_path = str(agent.memory._tasks_path)
    mem_fs[tasks_path] = json.dumps(task_data) + "\n"

    from maestro.sdk import QueryResult

    mock_result = QueryResult(
        response=json.dumps({
            "prune": [], "edit": [],
            "enqueue": [{"goal": "Do something useful"}],
        }),
    )

    with patch("maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=mock_result):
        # This should NOT crash with TypeError
        await agent.generate_task()
        queue = agent.task_queue.load()
        assert len(queue) == 1
        assert queue[0].goal == "Do something useful"
        assert queue[0].linked_question is None


# --- Audit-stall actuator (proposal 044) ---


def _enable_stall_actuator(agent: MaestroAgent, threshold: int) -> None:
    agent.config.audit_stall_block_enabled = True
    agent.config.audit_stall_threshold = threshold


def _seed_consecutive_audit_history(
    agent: MaestroAgent, statement: str, cycles: int,
) -> str:
    """Append *cycles* consecutive AuditResult rows that all surface *statement*.

    Returns the ``[audit] …`` open-question text that the gate will match.
    """
    from maestro.audit_checks import AuditFinding
    from maestro.memory import AuditResult

    finding = AuditFinding(
        kind="contradicted_claim",
        statement=statement,
        cites=[],
        rule="success_claim_vs_rate",
    )
    for i in range(1, cycles + 1):
        agent.memory.append_audit_result(AuditResult(
            iteration=i, timestamp=f"t{i}",
            findings=[finding],
            rules_fired=["success_claim_vs_rate"],
        ))
    return f"[audit] {finding.kind}: {finding.statement[:160]}"


@pytest.mark.asyncio
async def test_generate_task_blocks_when_stall_threshold_crossed(make_agent) -> None:  # type: ignore[no-untyped-def]
    """At threshold consecutive cycles + flag on → no SDK call, queue untouched."""
    agent = make_agent()
    _enable_stall_actuator(agent, threshold=3)

    audit_q = _seed_consecutive_audit_history(
        agent, "stalled finding statement", cycles=3,
    )
    model = agent.consciousness.load_self_model()
    model.open_questions = [audit_q]
    agent.consciousness.save_self_model(model)

    mock_query = AsyncMock()
    with patch("maestro.loop.task_generation.query", mock_query):
        await agent.generate_task()

    assert mock_query.call_count == 0
    assert agent.task_queue.load() == []


@pytest.mark.asyncio
async def test_generate_task_proceeds_when_under_threshold(make_agent) -> None:  # type: ignore[no-untyped-def]
    """count < threshold → SDK call still runs and the task is enqueued."""
    agent = make_agent()
    _enable_stall_actuator(agent, threshold=5)

    audit_q = _seed_consecutive_audit_history(
        agent, "still warming up", cycles=2,
    )
    model = agent.consciousness.load_self_model()
    model.open_questions = [audit_q]
    agent.consciousness.save_self_model(model)

    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": [{"goal": "Fix the warm-up finding in maestro/agent.py"}],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)) as mq:
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "Fix the warm-up finding in maestro/agent.py"
    assert mq.call_count == 1


@pytest.mark.asyncio
async def test_generate_task_proceeds_when_actuator_disabled(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Flag false: even ten consecutive matching cycles do not block the SDK call."""
    agent = make_agent()
    agent.config.audit_stall_block_enabled = False
    agent.config.audit_stall_threshold = 3

    audit_q = _seed_consecutive_audit_history(
        agent, "would-have-blocked", cycles=10,
    )
    model = agent.consciousness.load_self_model()
    model.open_questions = [audit_q]
    agent.consciousness.save_self_model(model)

    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": [{"goal": "Proceed despite many cycles"}],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)) as mq:
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "Proceed despite many cycles"
    assert mq.call_count == 1


@pytest.mark.asyncio
async def test_generate_task_clears_when_finding_absent_from_latest_cycle(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Latest cycle missing the finding (post-INTEGRATE) → counter resets, gate yields."""
    from maestro.audit_checks import AuditFinding
    from maestro.memory import AuditResult

    agent = make_agent()
    _enable_stall_actuator(agent, threshold=3)

    f_old = AuditFinding(
        kind="contradicted_claim",
        statement="finding that survived three cycles",
        cites=[],
        rule="success_claim_vs_rate",
    )
    audit_q = f"[audit] {f_old.kind}: {f_old.statement[:160]}"

    # Three consecutive matches followed by a most-recent cycle that
    # *no longer* surfaces the finding: the question is still in
    # open_questions (Claude has not yet pruned the entry) but the
    # underlying condition has been integrated, so the gate must yield.
    for i in range(1, 4):
        agent.memory.append_audit_result(AuditResult(
            iteration=i, timestamp=f"t{i}", findings=[f_old],
            rules_fired=["success_claim_vs_rate"],
        ))
    agent.memory.append_audit_result(AuditResult(
        iteration=4, timestamp="t4", findings=[],
        rules_fired=[],
    ))

    model = agent.consciousness.load_self_model()
    model.open_questions = [audit_q]
    agent.consciousness.save_self_model(model)

    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": [{"goal": "Probe the now-cleared finding"}],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)) as mq:
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "Probe the now-cleared finding"
    assert mq.call_count == 1


@pytest.mark.asyncio
async def test_generate_task_block_logs_worst_key_and_count(  # type: ignore[no-untyped-def]
    make_agent, caplog: pytest.LogCaptureFixture,
) -> None:
    """The block path emits an INFO line that names the worst key and its count."""
    agent = make_agent()
    _enable_stall_actuator(agent, threshold=2)

    audit_q = _seed_consecutive_audit_history(
        agent, "log-asserted statement", cycles=2,
    )
    model = agent.consciousness.load_self_model()
    model.open_questions = [audit_q]
    agent.consciousness.save_self_model(model)

    with caplog.at_level(logging.INFO, logger="maestro"), \
         patch("maestro.loop.task_generation.query", AsyncMock()) as mq:
        await agent.generate_task()

    assert mq.call_count == 0
    assert any(
        "audit-stall gate engaged" in rec.getMessage()
        and "log-asserted statement" in rec.getMessage()
        and "threshold=2" in rec.getMessage()
        for rec in caplog.records
    )


# --- task cost-risk gate wiring tests ---


@pytest.mark.asyncio
async def test_generate_task_cost_gate_blocks(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """cost gate fires → (None, None), no SDK call, event recorded."""
    from maestro.memory import TaskRecord

    agent = make_agent()
    # Seed 4 self-directed tasks (the cost gate reads only the discretionary
    # channel, #4573): all failures, all above the expensive-duration floor
    # (triggers rule 1: consecutive expensive failures). Timestamps are fresh
    # (now) so the cost gate's recency horizon (#2566) keeps them in-window —
    # an aged window would release the gate instead, which the horizon tests in
    # test_task_cost_gate.py cover directly.
    fresh_ts = datetime.now(UTC).isoformat()
    for i in range(4):
        agent.memory.record_task(TaskRecord(
            task_id=f"t-exp-{i}", goal=f"Expensive task {i}",
            plan=[], outcome="failure", result_summary="failed",
            errors=["err"], duration_secs=300.0,
            timestamp=fresh_ts, source="self_directed",
        ))

    mock_query = AsyncMock()
    with patch("maestro.loop.task_generation.query", mock_query):
        await agent.generate_task()

    assert mock_query.call_count == 0
    assert agent.task_queue.load() == []
    entries = agent.journal.read_recent_events()
    cost_entries = [e for e in entries if e.triggered_by == "cost_gate"]
    assert len(cost_entries) == 1
    assert "Cost gate blocked" in cost_entries[0].content


@pytest.mark.asyncio
async def test_generate_task_cost_gate_passes(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """cost gate clear → normal SDK flow proceeds."""
    from maestro.memory import TaskRecord

    agent = make_agent()
    # Seed one success so the cost gate has no reason to fire.
    agent.memory.record_task(TaskRecord(
        task_id="t-ok", goal="Easy win",
        plan=[], outcome="success", result_summary="done",
        errors=[], duration_secs=10.0,
        timestamp="2026-01-01T00:00:00",
    ))

    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [], "enqueue": [{"goal": "Do something new"}],
    }))
    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "Do something new"


@pytest.mark.asyncio
async def test_generate_task_cost_gate_blocks_mocked(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Mocked cost gate verdict → no SDK call, queue untouched, event recorded."""
    agent = make_agent()

    mock_query = AsyncMock()
    with (
        patch("maestro.task_gates.task_cost_gate.check_task_cost_risk",
              return_value="3 consecutive expensive failures"),
        patch("maestro.loop.task_generation.query", mock_query),
    ):
        await agent.generate_task()

    mock_query.assert_not_called()
    assert agent.task_queue.load() == []
    entries = agent.journal.read_recent_events()
    cost_entries = [e for e in entries if e.triggered_by == "cost_gate"]
    assert len(cost_entries) == 1
    assert "Cost gate blocked" in cost_entries[0].content
    assert "3 consecutive expensive failures" in cost_entries[0].content


@pytest.mark.asyncio
async def test_generate_task_cost_gate_passes_mocked(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Mocked cost gate returns None → SDK call proceeds normally."""
    agent = make_agent()

    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [], "enqueue": [{"goal": "A fresh new task"}],
    }))
    with (
        patch("maestro.task_gates.task_cost_gate.check_task_cost_risk",
              return_value=None),
        patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)),
    ):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "A fresh new task"


# --- _distill_findings tests ---


@pytest.mark.asyncio
async def test_distill_findings_returns_findings(make_agent):  # type: ignore[no-untyped-def]
    """_distill_findings() returns distilled text from the LLM."""
    from maestro.memory import PlanStep

    agent = make_agent()
    steps = [
        PlanStep(step_id="s1", description="Analyze code", status="done", result="Found 3 bugs in parser.py"),
        PlanStep(step_id="s2", description="Fix bugs", status="done", result="Fixed all 3 bugs"),
    ]

    with patch("maestro.agent.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="Fixed 3 parser bugs in parser.py.")
        findings = await agent._distill_findings("Fix parser bugs", steps, "success")

    assert findings == "Fixed 3 parser bugs in parser.py."
    mock_q.assert_called_once()
    prompt = mock_q.call_args.kwargs.get("prompt", mock_q.call_args[0][0] if mock_q.call_args[0] else "")
    assert "Fix parser bugs" in prompt
    assert "Analyze code" in prompt


@pytest.mark.asyncio
async def test_distill_findings_passes_chars_complete(make_agent):  # type: ignore[no-untyped-def]
    """_distill_findings wires chars_complete(200) through both paths."""
    from maestro.memory import PlanStep
    from maestro.sdk import chars_complete

    agent = make_agent()
    steps = [PlanStep(step_id="s1", description="step", status="done", result="ok")]
    mock_result = MockQueryResult(response="findings text")

    with patch("maestro.agent.query", AsyncMock(return_value=mock_result)) as mock_q:
        await agent._distill_findings("goal", steps, "success")
    assert mock_q.call_args.kwargs.get("complete_on") is chars_complete(200)

    with patch(
        "maestro.agent.query_on_session", AsyncMock(return_value=mock_result),
    ) as mock_qos:
        await agent._distill_findings(
            "goal", steps, "success", session=MagicMock(),
        )
    assert mock_qos.call_args.kwargs.get("complete_on") is chars_complete(200)


@pytest.mark.asyncio
async def test_distill_findings_returns_none_on_failure(make_agent):  # type: ignore[no-untyped-def]
    """_distill_findings() returns None when query() raises."""
    from maestro.memory import PlanStep

    agent = make_agent()
    steps = [PlanStep(step_id="s1", description="step", status="done", result="ok")]

    with patch("maestro.agent.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = RuntimeError("API down")
        findings = await agent._distill_findings("goal", steps, "success")

    assert findings is None


@pytest.mark.asyncio
async def test_distill_findings_reraises_sdk_error(make_agent):  # type: ignore[no-untyped-def]
    """_distill_findings() re-raises SDKError instead of swallowing it."""
    from maestro.memory import PlanStep

    agent = make_agent()
    steps = [PlanStep(step_id="s1", description="step", status="done", result="ok")]

    with patch("maestro.agent.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = SDKError("rate limited")
        with pytest.raises(SDKError, match="rate limited"):
            await agent._distill_findings("goal", steps, "success")


@pytest.mark.asyncio
async def test_distill_findings_truncates_to_1000_chars(make_agent):  # type: ignore[no-untyped-def]
    """_distill_findings() truncates LLM response to 1000 chars."""
    from maestro.memory import PlanStep

    agent = make_agent()
    steps = [PlanStep(step_id="s1", description="step", status="done", result="ok")]

    with patch("maestro.agent.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="x" * 2000)
        findings = await agent._distill_findings("goal", steps, "success")

    assert findings is not None
    assert len(findings) == 1000


@pytest.mark.asyncio
async def test_distill_findings_returns_none_on_whitespace_response(make_agent):  # type: ignore[no-untyped-def]
    """_distill_findings() coerces a whitespace-only response to None.

    Locks the trailing ``or None`` branch so the redundant-``str()`` cleanup
    cannot silently drop the empty-response coercion.
    """
    from maestro.memory import PlanStep

    agent = make_agent()
    steps = [PlanStep(step_id="s1", description="step", status="done", result="ok")]

    with patch("maestro.agent.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="   ")
        findings = await agent._distill_findings("goal", steps, "success")

    assert findings is None


@pytest.mark.asyncio
async def test_run_stores_key_findings_in_record(make_agent):  # type: ignore[no-untyped-def]
    """run() stores key_findings from _distill_findings in the TaskRecord."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="step output")
        mock_distill.return_value = MockQueryResult(response="Produced a summary of step output.")

        record = await agent.run("Test findings")

    assert record.key_findings == "Produced a summary of step output."
    # key_findings should also be persisted in memory
    tasks = agent.memory.get_recent_tasks()
    assert tasks[0].key_findings == "Produced a summary of step output."


@pytest.mark.asyncio
async def test_run_key_findings_none_when_distill_fails(make_agent):  # type: ignore[no-untyped-def]
    """run() sets key_findings=None when _distill_findings fails."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="output")
        mock_distill.side_effect = RuntimeError("LLM down")

        record = await agent.run("Test goal")

    assert record.outcome == "success"
    assert record.key_findings is None


# --- run_loop() should tolerate _implement_approved_plans() failures ---


@pytest.mark.asyncio
async def test_run_loop_crashes_on_implement_approved_plans_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates _implement_approved_plans() errors."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock(side_effect=RuntimeError("plans blew up"))  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="plans blew up"):
        await agent.run_loop(interval=0, max_iters=3, task_every=0, evolve_every=999)

    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_loop_crashes_on_generate_task_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """run_loop() propagates generate_task() errors."""
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.generate_task = AsyncMock(side_effect=RuntimeError("model load failed"))  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="model load failed"):
        await agent.run_loop(interval=0, max_iters=3, task_every=1, evolve_every=999)

    assert agent.contemplate.call_count == 1  # type: ignore[attr-defined]
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# _process_rejected_plans tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_rejected_plans_calls_learn_from_rejection(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_process_rejected_plans() learns from newly rejected plans."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="rej01",
        title="Rejected Feature",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="rejected",
        rejection_reason="Too risky",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]

    await agent._process_rejected_plans()

    agent.evolution.learn_from_rejection.assert_called_once()
    call_args = agent.evolution.learn_from_rejection.call_args
    assert call_args[0][0].plan_id == "rej01"
    assert call_args[0][1] == "Too risky"

    # Plan should be marked as learned
    reloaded = agent.memory.get_plan("rej01")
    assert reloaded is not None
    assert reloaded.rejection_learned is True


@pytest.mark.asyncio
async def test_process_rejected_plans_skips_already_learned(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_process_rejected_plans() skips plans with rejection_learned=True."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="rej02",
        title="Already Learned",
        category="capability",
        rationale="test",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status="rejected",
        rejection_reason="Old reason",
        rejection_learned=True,
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]

    await agent._process_rejected_plans()

    agent.evolution.learn_from_rejection.assert_not_called()


@pytest.mark.asyncio
async def test_process_rejected_plans_propagates_learn_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Loud failures (reversal of H6): ``_process_rejected_plans`` now runs
    ``learn_from_rejection`` and ``mark_rejection_learned`` as plain sequential
    calls.  If ``learn_from_rejection`` raises, the exception propagates out
    of the phase and the loop's outer finally triggers the crash-triage path.

    The trade-off: because ``mark_rejection_learned`` is never reached on the
    failing iteration, a future successful iteration may learn from the same
    rejected plan a second time.  The journal-side idempotency guard in
    ``EvolutionEngine.learn_from_rejection`` (short-circuits on an existing
    ``triggered_by=rejection:{plan_id}`` realization) keeps the duplicate
    learning cheap.
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="rej03",
        title="Learning Fails",
        category="capability",
        rationale="test",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status="rejected",
        rejection_reason="Reason",
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.learn_from_rejection = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("LLM error"),
    )
    agent.memory.mark_rejection_learned = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="LLM error"):
        await agent._process_rejected_plans()

    agent.evolution.learn_from_rejection.assert_called_once()
    # mark_rejection_learned was NOT called because the exception propagated
    # before the sequential call could be reached.  The duplicate-learning
    # risk on next iteration is accepted (idempotent on the journal side).
    agent.memory.mark_rejection_learned.assert_not_called()
    reloaded = agent.memory.get_plan("rej03")
    assert reloaded is not None
    assert reloaded.rejection_learned is False


@pytest.mark.asyncio
async def test_process_rejected_plans_propagates_mark_learned_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Loud failures: ``_process_rejected_plans`` no longer catches failures
    from ``memory.mark_rejection_learned``.  Any OSError (e.g. disk full)
    propagates out of the phase so the crash-triage path can fire.
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    plan = EvolutionPlan(
        plan_id="rej_mark_fail", title="Mark Fails",
        category="capability", rationale="test",
        proposed_changes=[], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="Reason",
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]
    agent.memory.mark_rejection_learned = MagicMock(  # type: ignore[method-assign]
        side_effect=OSError("disk full"),
    )

    with pytest.raises(OSError, match="disk full"):
        await agent._process_rejected_plans()
    agent.memory.mark_rejection_learned.assert_called_once_with("rej_mark_fail")


@pytest.mark.asyncio
async def test_process_rejected_plans_threads_session_through(make_agent) -> None:  # type: ignore[no-untyped-def]
    """``_process_rejected_plans`` forwards its ``session`` kwarg to every
    ``learn_from_rejection`` call so the rejection_session created at the
    phase block is reused across all rejected plans in the cycle."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    for plan_id in ("rej_a", "rej_b"):
        agent.memory.save_evolution_plan(EvolutionPlan(
            plan_id=plan_id, title="r",
            category="capability", rationale="test",
            proposed_changes=[], files_affected=[], expected_impact="",
            status="rejected", rejection_reason="why",
            created_at="2025-01-01T00:00:00Z",
        ))
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]

    fake_session = MagicMock(name="rejection_session")
    await agent._process_rejected_plans(session=fake_session)

    assert agent.evolution.learn_from_rejection.call_count == 2
    for call in agent.evolution.learn_from_rejection.call_args_list:
        assert call.kwargs["session"] is fake_session


@pytest.mark.asyncio
async def test_evolve_propose_plan_parse_error_journal_write_failure(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If propose_plan raises ProposalParseError AND the skip-event write
    fails, the write error propagates (fail loud: the swallowing handler went
    with reflect, proposal 155 step 4)."""
    from maestro.evolution import ProposalParseError

    agent = make_agent()
    _fuel_evolve(monkeypatch)
    with (
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        patch.object(agent.consciousness, "introspect", AsyncMock(return_value="deep")),
        patch.object(agent.evolution, "propose_plan", AsyncMock(
            side_effect=ProposalParseError("invalid json"),
        )),
        patch.object(agent.memory, "get_all_plans", return_value=[]),
        patch.object(agent.journal, "record_event", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        await agent.evolve()


@pytest.mark.asyncio
async def test_evolve_propose_plan_sdk_error_propagates(make_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If propose_plan raises SDKError, evolve() lets it propagate."""
    agent = make_agent()
    _fuel_evolve(monkeypatch)
    with (
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        patch.object(agent.consciousness, "introspect", AsyncMock(return_value="deep")),
        patch.object(agent.evolution, "propose_plan", AsyncMock(
            side_effect=SDKError("rate limit"),
        )),
        patch.object(agent.memory, "get_all_plans", return_value=[]),
        pytest.raises(SDKError, match="rate limit"),
    ):
        await agent.evolve()


@pytest.mark.asyncio
async def test_process_rejected_plans_default_reason(make_agent) -> None:  # type: ignore[no-untyped-def]
    """_process_rejected_plans() uses default reason when rejection_reason is None."""
    from maestro.memory import EvolutionPlan

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="rej04",
        title="No Reason",
        category="capability",
        rationale="test",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status="rejected",
        rejection_reason=None,
    )
    agent.memory.save_evolution_plan(plan)
    agent.evolution.learn_from_rejection = AsyncMock()  # type: ignore[method-assign]

    await agent._process_rejected_plans()

    call_args = agent.evolution.learn_from_rejection.call_args
    assert call_args[0][1] == "No reason provided"


# --- _build_self_directed_pairs / contemplate wiring ---


def _make_task_record(
    *,
    task_id: str,
    source: str = "self_directed",
    linked_question: str | None = None,
    key_findings: str | None = None,
    outcome: str = "success",
    timestamp: str = "2026-01-01T00:00:00+00:00",
):  # type: ignore[no-untyped-def]
    from maestro.memory import TaskRecord
    return TaskRecord(
        task_id=task_id,
        goal="g",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="",
        errors=[],
        duration_secs=1.0,
        timestamp=timestamp,
        source=source,  # type: ignore[arg-type]
        key_findings=key_findings,
        linked_question=linked_question,
    )


# --- _extract_file_paths() tests ---


def test_extract_file_paths_finds_python_paths() -> None:
    """_extract_file_paths extracts paths with at least one directory segment."""
    text = "Fix the bug in maestro/agent.py and update tests/test_agent.py"
    result = _extract_file_paths(text)
    assert result == ["maestro/agent.py", "tests/test_agent.py"]


def test_extract_file_paths_ignores_bare_filenames() -> None:
    """Bare filenames without a slash are ignored (ambiguous location)."""
    text = "Look at setup.py and README.md"
    result = _extract_file_paths(text)
    assert result == []


def test_extract_file_paths_handles_nested_paths() -> None:
    """Deeply nested paths are captured correctly."""
    text = "Check maestro/prompts/defaults.py and src/lib/utils/helpers.py"
    result = _extract_file_paths(text)
    assert result == ["maestro/prompts/defaults.py", "src/lib/utils/helpers.py"]


def test_extract_file_paths_deduplicates() -> None:
    """Duplicate paths are returned only once, in discovery order."""
    text = "maestro/agent.py breaks maestro/agent.py again"
    result = _extract_file_paths(text)
    assert result == ["maestro/agent.py"]


def test_extract_file_paths_handles_dots_and_hyphens() -> None:
    """Segments with dots and hyphens are accepted."""
    text = "See my-pkg/v2.0/core.py for details"
    result = _extract_file_paths(text)
    assert result == ["my-pkg/v2.0/core.py"]


def test_extract_file_paths_empty_text() -> None:
    """Empty text yields no paths."""
    assert _extract_file_paths("") == []


# --- _check_issue_preconditions() tests ---


def test_check_issue_preconditions_no_paths_passes(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """When the task text has no file paths, preconditions pass trivially."""
    agent = make_agent()
    assert agent._check_issue_preconditions("Fix issue #1234 somehow") is True


def test_check_issue_preconditions_valid_file_passes(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """When all referenced files exist and parse, preconditions pass."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/agent.py"] = "x = 1\n"
    assert agent._check_issue_preconditions(
        "Fix bug in maestro/agent.py for #1234",
    ) is True


def test_check_issue_preconditions_missing_file_fails(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """When a referenced file does not exist, preconditions fail."""
    agent = make_agent()
    # Don't add the file to mem_fs — it won't exist
    assert agent._check_issue_preconditions(
        "Fix bug in maestro/nonexistent.py for #1234",
    ) is False


def test_check_issue_preconditions_syntax_error_fails(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """When a referenced file has a syntax error, preconditions fail."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/broken.py"] = "def f(\n"  # invalid Python
    assert agent._check_issue_preconditions(
        "Fix maestro/broken.py for #999",
    ) is False


def test_check_issue_preconditions_multiple_files_one_missing(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """If any referenced file is missing, the whole check fails."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/good.py"] = "x = 1\n"
    # maestro/bad.py is not in mem_fs
    assert agent._check_issue_preconditions(
        "Update maestro/good.py and maestro/bad.py for #500",
    ) is False


def test_check_issue_preconditions_traversal_rejected(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A path that resolves outside the workspace is rejected by the guard.

    The out-of-workspace target is seeded in ``mem_fs`` at the *literal*
    joined path so that, under the pre-guard code, ``exists()`` +
    ``read_text()`` + ``ast.parse`` would all succeed and the gate would
    return ``True``. The containment guard must reject it first, proving the
    guard — not the missing-file path — is what rejects it (#1882).
    """
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/../../etc/passwd.py"] = "x = 1\n"
    assert agent._check_issue_preconditions(
        "See ../../etc/passwd.py for #1882",
    ) is False


def test_check_issue_preconditions_nested_in_workspace_passes(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A nested in-workspace path still passes — the guard does not regress it."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/sub/deep/module.py"] = "y = 2\n"
    assert agent._check_issue_preconditions(
        "Fix maestro/sub/deep/module.py for #1882",
    ) is True


def test_check_issue_preconditions_resolve_failure_fails(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A ``resolve()`` failure is treated as a failed precondition, not a crash."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/agent.py"] = "x = 1\n"
    with patch.object(Path, "resolve", side_effect=OSError("boom")):
        assert agent._check_issue_preconditions(
            "Fix maestro/agent.py for #1882",
        ) is False


# --- _file_reference_exists() tests (existence-only predicate, #2304) ---


def test_file_reference_exists_existing_in_workspace(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """An existing in-workspace path is present (no ast.parse needed)."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    # Deliberately-unparseable content: unlike _file_reference_present this
    # predicate is existence-only, so a syntax error must not make it False.
    mem_fs[f"{workspace}/maestro/agent.py"] = "def f(\n"
    assert agent._file_reference_exists("maestro/agent.py") is True


def test_file_reference_exists_missing_file(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A path with no file on disk is absent."""
    agent = make_agent()
    assert agent._file_reference_exists("maestro/nonexistent.py") is False


def test_file_reference_exists_traversal_rejected(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A path escaping the workspace is rejected by the #1882 guard."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    # Seed at the literal joined path so, without the guard, exists() would be
    # True — proving the containment guard is what rejects it.
    mem_fs[f"{workspace}/../../etc/passwd.py"] = "x = 1\n"
    assert agent._file_reference_exists("../../etc/passwd.py") is False


def test_file_reference_exists_resolve_failure(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A ``resolve()`` failure is treated as absent, not a crash."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/agent.py"] = "x = 1\n"
    with patch.object(Path, "resolve", side_effect=OSError("boom")):
        assert agent._file_reference_exists("maestro/agent.py") is False


# --- Premise report helpers (self-directed goal file-reference check) ---


def test_premise_missing_file_references_returns_missing_path(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A goal citing a nonexistent ``.py`` path surfaces that path as missing."""
    agent = make_agent()
    missing = agent._missing_file_references(
        "Tighten the retry backoff in maestro/does_not_exist.py",
    )
    assert missing == ["maestro/does_not_exist.py"]


def test_premise_missing_file_references_empty_when_all_present(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """When every referenced ``.py`` file exists and parses, none are missing."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/sdk.py"] = "x = 1\n"
    assert agent._missing_file_references(
        "Tighten the retry backoff in maestro/sdk.py",
    ) == []


def test_premise_missing_file_references_empty_when_none_referenced(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """A goal that names no ``.py`` path has no missing references."""
    agent = make_agent()
    assert agent._missing_file_references("Explore the codebase generally") == []


def test_premise_missing_file_references_partial(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """Only the absent path is reported when one referenced file exists."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/sdk.py"] = "x = 1\n"
    missing = agent._missing_file_references(
        "Refactor maestro/sdk.py and maestro/ghost.py together",
    )
    assert missing == ["maestro/ghost.py"]


def test_premise_build_report_names_missing_path(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """The premise report block names each missing referenced path."""
    agent = make_agent()
    report = agent._build_premise_report(
        "Tighten the retry backoff in maestro/does_not_exist.py",
    )
    assert report is not None
    assert report.startswith("## PREMISE REPORT")
    assert "- maestro/does_not_exist.py" in report


def test_premise_build_report_none_when_all_present(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """No block is built when every referenced file is present."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/sdk.py"] = "x = 1\n"
    assert agent._build_premise_report(
        "Tighten the retry backoff in maestro/sdk.py",
    ) is None


def test_premise_build_report_none_when_none_referenced(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """No block is built when the goal references no ``.py`` path."""
    agent = make_agent()
    assert agent._build_premise_report("Explore the codebase generally") is None


def test_premise_build_report_wording_covers_unparseable_file(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """An exists-but-unparseable file is listed under accurate wording.

    ``_file_reference_present`` returns ``False`` for a file that exists but
    fails ``ast.parse`` — the report must not claim it was "not found", so
    the block reads "missing or unusable" and names the ``unparseable`` case.
    """
    agent = make_agent()
    workspace = str(agent.config.workspace)
    # Present on disk but syntactically invalid → unusable, not absent.
    mem_fs[f"{workspace}/maestro/broken.py"] = "def (\n"
    report = agent._build_premise_report(
        "Tighten the retry backoff in maestro/broken.py",
    )
    assert report is not None
    assert "- maestro/broken.py" in report
    assert "missing or unusable" in report
    assert "unparseable" in report


def test_premise_build_report_caps_listed_paths(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """More missing paths than the cap are summarized, not all enumerated."""
    agent = make_agent()
    cap = agent_module._MAX_PREMISE_REPORT_PATHS
    overflow = 3
    paths = [f"maestro/ghost{i}.py" for i in range(cap + overflow)]
    goal = "Refactor " + " and ".join(paths)
    report = agent._build_premise_report(goal)
    assert report is not None
    # Exactly ``cap`` paths enumerated; the rest folded into one summary line.
    listed = [line for line in report.splitlines() if line.startswith("- maestro/")]
    assert len(listed) == cap
    assert f"- … and {overflow} more not shown" in report


# --- Terrain-exhaustion gate tests (per-new-entry, no retry loop) ---


@pytest.mark.asyncio
async def test_terrain_gate_drops_new_entry_when_file_missing(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
) -> None:
    """A new enqueue entry referencing a missing file + issue is dropped."""
    agent = make_agent()

    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": [{"goal": "Fix maestro/ghost.py for #42"}],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)) as mq:
        await agent.generate_task()

    # Stale terrain → entry dropped; queue stays empty. No retry: a single
    # SDK call is enough.
    assert agent.task_queue.load() == []
    assert mq.call_count == 1


@pytest.mark.asyncio
async def test_terrain_gate_passes_when_files_exist(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """Entries whose referenced files exist + parse are enqueued normally."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/real.py"] = "x = 1\n"

    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": [{"goal": "Fix maestro/real.py for #55"}],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "Fix maestro/real.py for #55"


@pytest.mark.asyncio
async def test_terrain_gate_skipped_when_no_issues(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """Precondition check only fires when the entry references issue numbers."""
    agent = make_agent()
    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": [{"goal": "Refactor maestro/ghost.py"}],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "Refactor maestro/ghost.py"


@pytest.mark.asyncio
async def test_terrain_gate_drops_only_stale_entry(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    """When two enqueues are proposed, only the stale one is dropped."""
    agent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/real.py"] = "y = 2\n"

    mock_result = MockQueryResult(response=json.dumps({
        "prune": [], "edit": [],
        "enqueue": [
            {"goal": "Fix maestro/gone.py for #10"},
            {"goal": "Fix maestro/real.py for #20"},
        ],
    }))

    with patch("maestro.loop.task_generation.query", AsyncMock(return_value=mock_result)):
        await agent.generate_task()

    queue = agent.task_queue.load()
    assert [q.goal for q in queue] == ["Fix maestro/real.py for #20"]


# ──────────────────────────────────────────────────────────────────────
# _safe_shutdown_wait tests (#1719)
# ──────────────────────────────────────────────────────────────────────


class TestSafeShutdownWait:
    """Tests for ``_safe_shutdown_wait`` — CancelledError containment
    for non-SDK shutdown-wait sites.
    """

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self, mem_fs: dict[str, str]) -> None:
        """Normal timeout (no shutdown) returns False."""
        agent = _build_agent(mem_fs)
        result = await agent._safe_shutdown_wait(0.01)
        assert result is False

    @pytest.mark.asyncio
    async def test_shutdown_event_set_returns_true(
        self, mem_fs: dict[str, str],
    ) -> None:
        """When shutdown event is set before the wait, returns True."""
        agent = _build_agent(mem_fs)
        agent._ensure_shutdown_event().set()
        result = await agent._safe_shutdown_wait(5.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_leaked_cancelled_error_suppressed(
        self, mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Leaked CancelledError (shutdown NOT set) is suppressed and
        returns False with a warning log.
        """
        agent = _build_agent(mem_fs)

        async def _raise_cancelled(*args: object, **kwargs: object) -> object:
            raise asyncio.CancelledError

        with patch("asyncio.wait_for", side_effect=_raise_cancelled), \
             caplog.at_level("WARNING", logger="maestro.agent"):
            # Shutdown event is NOT set — leaked orphan
            result = await agent._safe_shutdown_wait(1.0)

        assert result is False
        assert any(
            "Residual CancelledError" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_cancelled_error_with_shutdown_reraises(
        self, mem_fs: dict[str, str],
    ) -> None:
        """CancelledError when shutdown IS set is re-raised (genuine
        shutdown).
        """
        agent = _build_agent(mem_fs)
        agent._ensure_shutdown_event().set()

        async def _raise_cancelled(*args: object, **kwargs: object) -> object:
            raise asyncio.CancelledError

        with patch("asyncio.wait_for", side_effect=_raise_cancelled), \
             pytest.raises(asyncio.CancelledError):
            await agent._safe_shutdown_wait(1.0)

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_propagates(
        self, mem_fs: dict[str, str],
    ) -> None:
        """KeyboardInterrupt always propagates unconditionally."""
        agent = _build_agent(mem_fs)

        async def _raise_kb(*args: object, **kwargs: object) -> object:
            raise KeyboardInterrupt

        with patch("asyncio.wait_for", side_effect=_raise_kb), \
             pytest.raises(KeyboardInterrupt):
            await agent._safe_shutdown_wait(1.0)

    @pytest.mark.asyncio
    async def test_system_exit_propagates(
        self, mem_fs: dict[str, str],
    ) -> None:
        """SystemExit always propagates unconditionally."""
        agent = _build_agent(mem_fs)

        async def _raise_se(*args: object, **kwargs: object) -> object:
            raise SystemExit(1)

        with patch("asyncio.wait_for", side_effect=_raise_se), \
             pytest.raises(SystemExit):
            await agent._safe_shutdown_wait(1.0)


_EVOLUTION_REACH_MODULES = (
    ("agent.py",),
    ("loop", "implementation.py"),
    ("loop", "pr_remediation.py"),
    ("loop", "iteration.py"),
    ("loop", "assembly.py"),
)


def _private_evolution_reaches(attr: str, extra: str = "") -> list[str]:
    """Return ``module:line`` for every ``{self,agent}.evolution.<attr>`` reach.

    Scans ``agent.py`` plus the ``maestro/loop/`` modules its bodies moved to
    (proposal 147), whose receiver is spelled ``agent`` rather than ``self``.
    ``extra`` is appended to ``loop/implementation.py``'s source in memory, so a
    seeded reach proves the scan is not vacuous.
    """
    import ast as _ast

    root = Path(__file__).resolve().parent.parent / "maestro"
    offenders: list[str] = []
    for parts in _EVOLUTION_REACH_MODULES:
        source = root.joinpath(*parts).read_text(encoding="utf-8")
        if parts[-1] == "implementation.py":
            source += extra
        for node in _ast.walk(_ast.parse(source)):
            if not isinstance(node, _ast.Attribute) or node.attr != attr:
                continue
            inner = node.value
            if not isinstance(inner, _ast.Attribute) or inner.attr != "evolution":
                continue
            outer = inner.value
            if isinstance(outer, _ast.Name) and outer.id in ("self", "agent"):
                offenders.append(f"{'/'.join(parts)}:{node.lineno}")
    return offenders


def test_agent_uses_public_code_host_accessor() -> None:
    """AST regression: the agent must not reach into ``EvolutionEngine._host``.

    Issue #1512 — ``MaestroAgent`` previously spelled ``self.evolution._host``,
    coupling the agent to a private attribute on the evolution engine. The fix
    introduced ``EvolutionEngine.code_host``. This test walks ``agent.py`` and
    the loop modules its bodies moved to, failing if any ``self.evolution._host``
    or ``agent.evolution._host`` reference reappears.
    """
    offenders = _private_evolution_reaches("_host")
    assert not offenders, (
        f"EvolutionEngine._host reached at {offenders}. Use "
        "``evolution.code_host`` instead — see issue #1512."
    )


def test_agent_uses_public_mcp_servers_accessor() -> None:
    """AST regression: the agent must not reach into ``EvolutionEngine._mcp_servers``.

    Issue #1236 — the implementation session (Session C) previously spelled
    ``self.evolution._mcp_servers``. The fix introduced
    ``EvolutionEngine.mcp_servers``; the call site now lives in
    ``maestro/loop/implementation.py`` with an ``agent`` receiver, so both
    receivers and every moved-to module are scanned.
    """
    offenders = _private_evolution_reaches("_mcp_servers")
    assert not offenders, (
        f"EvolutionEngine._mcp_servers reached at {offenders}. Use "
        "``evolution.mcp_servers`` instead — see issue #1236."
    )


def test_private_evolution_reach_scanner_flags_agent_receiver() -> None:
    """A seeded ``agent.evolution._mcp_servers`` in implementation.py flags."""
    hits = _private_evolution_reaches(
        "_mcp_servers", "\nx = agent.evolution._mcp_servers\n"
    )
    assert len(hits) == 1
    assert hits[0].startswith("loop/implementation.py:")


# --- Stalled-remediation lineage fall-through (_is_stalled_remediation_lineage,
#     _pending_pr_head_stalled, _run_task_phase) -------------------------------
#
# A ``Remediate PR #N`` head of ``pending_pr_requests`` re-serviced
# ``>= STALLED_REMEDIATION_LAP_CAP`` consecutive times — all ``partial`` laps on
# the same unchanged origin head SHA — is externally blocked, unchanged work.
# ``_run_task_phase`` leaves it queued and falls through to the self-directed
# queue so a landable task takes the slot. The brake auto-releases when the head
# SHA advances or the PR merges/closes.

_STALL_HEAD = "a" * 40
_STALL_BASE = "b" * 40


def _stall_rev_parse(ref: str) -> str:
    """``rev_parse`` side-effect for stall fixtures under proposal 099.

    ``origin/main`` resolves to :data:`_STALL_BASE`, every branch ref to
    :data:`_STALL_HEAD`. A lap recorded ``(head=_STALL_HEAD, base=_STALL_BASE)``
    then counts against both the current head *and* an unchanged base — a genuine
    stall under the graduated (default-on) base-advance re-entry keying, which
    breaks the streak on a moved ``origin/main``.
    """
    return _STALL_BASE if ref == "origin/main" else _STALL_HEAD


def _remediation_lap(
    *,
    task_id: str,
    pr_num: int,
    outcome: str = "partial",
    pr_head_sha: str | None = _STALL_HEAD,
    base_head_sha: str | None = None,
):  # type: ignore[no-untyped-def]
    """Build a ``Remediate PR #<n>`` TaskRecord lap for stall-detection tests.

    *base_head_sha* (proposal 099) records the ``origin/main`` SHA the lap raced
    against; ``None`` (the default) reproduces a legacy record from before the
    field existed and is invisible to the head-only stall path.
    """
    from maestro.memory import TaskRecord
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr_num}: fix the threads",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00+00:00",
        source="external",
        pr_head_sha=pr_head_sha,
        base_head_sha=base_head_sha,
    )


# =====================================================================
# Consecutive-remediation cap (max_consecutive_remediation)
# =====================================================================
# ``_run_task_phase`` selects goals in priority order
# ``pending_user_requests`` -> ``pending_pr_requests`` -> queue head. Because
# ``pending_pr_requests`` is re-fed by the tend phase every iteration, an
# unbounded streak of remediation slots starves task-shape diversity
# (remediation-monoculture). ``[loop] max_consecutive_remediation`` caps how
# many *consecutive* slots may be served from ``pending_pr_requests`` before
# one is forced through to the queue head; ``<= 0`` disables the cap.


@pytest.mark.asyncio
async def test_run_task_phase_caps_consecutive_remediation_forces_diversity(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) The (cap+1)th consecutive remediation slot bypasses
    ``pending_pr_requests`` and falls through to the queue head."""
    from maestro.task_queue import QueuedTask

    agent = make_agent()
    agent.config.max_consecutive_remediation = 2
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=f"pr goal {i}", pr_branch=f"maestro/evolve/p{i}",
        )
        for i in range(5)
    ]
    agent.consciousness.save_self_model(model)
    agent.task_queue.save([QueuedTask(task_id="q1", goal="queue goal")])

    goals: list[tuple[str, str]] = []

    async def _capture(goal, **kwargs):  # type: ignore[no-untyped-def]
        goals.append((goal, kwargs["source"]))
        return MagicMock(task_id=None)

    agent.run = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

    await agent._run_task_phase()  # slot 1 — remediation (streak 1)
    await agent._run_task_phase()  # slot 2 — remediation (streak 2 == cap)
    await agent._run_task_phase()  # slot 3 — capped -> queue head

    assert goals == [
        ("pr goal 0", "external"),
        ("pr goal 1", "external"),
        ("queue goal", "self_directed"),
    ]
    # Only two PR entries were consumed; the cap did not drain the rest.
    after = agent.consciousness.load_self_model()
    assert [e.directive for e in after.pending_pr_requests] == [
        "pr goal 2", "pr goal 3", "pr goal 4",
    ]


@pytest.mark.asyncio
async def test_run_task_phase_remediation_streak_resets_after_diversion(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) The streak counter resets after a non-remediation task, so the cap
    measures only *consecutive* remediation slots."""
    from maestro.task_queue import QueuedTask

    agent = make_agent()
    agent.config.max_consecutive_remediation = 2
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=f"pr goal {i}", pr_branch=f"maestro/evolve/p{i}",
        )
        for i in range(4)
    ]
    agent.consciousness.save_self_model(model)
    # Two queue heads so the forced-diversion slot has somewhere to go twice.
    agent.task_queue.save([
        QueuedTask(task_id="q1", goal="queue goal one"),
        QueuedTask(task_id="q2", goal="queue goal two"),
    ])

    goals: list[str] = []

    async def _capture(goal, **kwargs):  # type: ignore[no-untyped-def]
        goals.append(goal)
        return MagicMock(task_id=None)

    agent.run = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

    # 6 slots: R, R, Q (reset), R, R, Q (reset).
    for _ in range(6):
        await agent._run_task_phase()

    assert goals == [
        "pr goal 0", "pr goal 1", "queue goal one",
        "pr goal 2", "pr goal 3", "queue goal two",
    ]


@pytest.mark.asyncio
async def test_run_task_phase_cap_zero_preserves_unconditional_precedence(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(c) ``max_consecutive_remediation <= 0`` disables the cap — remediation
    keeps its unconditional precedence even after a long streak."""
    from maestro.task_queue import QueuedTask

    agent = make_agent()
    agent.config.max_consecutive_remediation = 0
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=f"pr goal {i}", pr_branch=f"maestro/evolve/p{i}",
        )
        for i in range(5)
    ]
    agent.consciousness.save_self_model(model)
    agent.task_queue.save([QueuedTask(task_id="q1", goal="queue goal")])

    goals: list[str] = []

    async def _capture(goal, **kwargs):  # type: ignore[no-untyped-def]
        goals.append(goal)
        return MagicMock(task_id=None)

    agent.run = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

    for _ in range(5):
        await agent._run_task_phase()

    # Every slot drained a PR request; the queue head was never touched.
    assert goals == [f"pr goal {i}" for i in range(5)]
    assert [q.task_id for q in agent.task_queue.load()] == ["q1"]


@pytest.mark.asyncio
async def test_run_task_phase_user_request_never_throttled_by_cap(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(d) ``pending_user_requests`` is never throttled by the remediation cap,
    and a user-request selection resets the streak."""
    agent = make_agent()
    agent.config.max_consecutive_remediation = 2
    # Pre-load the streak to the cap so remediation is currently throttled.
    agent._consecutive_remediation = 2
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["user goal"]
    model.pending_pr_requests = [
        PendingPRRequest(directive="pr goal", pr_branch="maestro/evolve/p1"),
    ]
    agent.consciousness.save_self_model(model)
    agent.run = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(task_id=None),
    )

    await agent._run_task_phase()

    agent.run.assert_awaited_once()
    args, kwargs = agent.run.await_args
    assert args[0] == "user goal"
    assert kwargs["source"] == "external"
    # The non-remediation user-request selection reset the streak.
    assert agent._consecutive_remediation == 0
    # The PR request was left untouched.
    after = agent.consciousness.load_self_model()
    assert [e.directive for e in after.pending_pr_requests] == ["pr goal"]


@pytest.mark.asyncio
async def test_run_task_phase_wip_owned_exempts_remediation_cap(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """092: with ``wip_primacy_enabled`` on and owned self-clearable WIP open, a
    capped remediation streak is exempted — the (cap+1)th slot keeps serving
    ``pending_pr_requests`` instead of falling through to the queue head, because
    finishing owned work outranks task-shape diversity."""
    from maestro.task_queue import QueuedTask
    from maestro.wip_primacy import TendOwnedPR

    agent = make_agent()
    agent.config.max_consecutive_remediation = 2
    agent.config.wip_primacy_enabled = True
    # Owned WIP: an open own PR with a self-clearable CONFLICTING blocker owns
    # the slot, so the cap must not evict remediation onto the queue head.
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=TendOwnedPR(pr_number=99, owned_blocker="CONFLICTING"),
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=f"pr goal {i}", pr_branch=f"maestro/evolve/p{i}",
        )
        for i in range(5)
    ]
    agent.consciousness.save_self_model(model)
    agent.task_queue.save([QueuedTask(task_id="q1", goal="queue goal")])

    goals: list[tuple[str, str]] = []

    async def _capture(goal, **kwargs):  # type: ignore[no-untyped-def]
        goals.append((goal, kwargs["source"]))
        return MagicMock(task_id=None)

    agent.run = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

    await agent._run_task_phase()  # slot 1 — remediation (streak 1)
    await agent._run_task_phase()  # slot 2 — remediation (streak 2 == cap)
    await agent._run_task_phase()  # slot 3 — capped, but owned WIP exempts it

    # Every slot served remediation; the WIP exemption kept the (cap+1)th slot
    # on ``pending_pr_requests`` instead of forcing it to the queue head.
    assert goals == [
        ("pr goal 0", "external"),
        ("pr goal 1", "external"),
        ("pr goal 2", "external"),
    ]
    # The self-directed queue head was never touched.
    assert [q.task_id for q in agent.task_queue.load()] == ["q1"]
    after = agent.consciousness.load_self_model()
    assert [e.directive for e in after.pending_pr_requests] == [
        "pr goal 3", "pr goal 4",
    ]


@pytest.mark.asyncio
async def test_run_task_phase_discretionary_focus_keeps_remediation_cap(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """092: with the flag on but ``_current_task_focus`` reporting
    ``Discretionary`` (no owned WIP), the cap still forces the (cap+1)th slot
    through to the queue head — parity with the flag-off diversity path, so the
    exemption is surgical to open self-clearable WIP."""
    from maestro.task_queue import QueuedTask
    from maestro.wip_primacy import Discretionary

    agent = make_agent()
    agent.config.max_consecutive_remediation = 2
    agent.config.wip_primacy_enabled = True
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=Discretionary(),
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(
            directive=f"pr goal {i}", pr_branch=f"maestro/evolve/p{i}",
        )
        for i in range(5)
    ]
    agent.consciousness.save_self_model(model)
    agent.task_queue.save([QueuedTask(task_id="q1", goal="queue goal")])

    goals: list[tuple[str, str]] = []

    async def _capture(goal, **kwargs):  # type: ignore[no-untyped-def]
        goals.append((goal, kwargs["source"]))
        return MagicMock(task_id=None)

    agent.run = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

    await agent._run_task_phase()  # slot 1 — remediation (streak 1)
    await agent._run_task_phase()  # slot 2 — remediation (streak 2 == cap)
    await agent._run_task_phase()  # slot 3 — capped, no owned WIP -> queue head

    assert goals == [
        ("pr goal 0", "external"),
        ("pr goal 1", "external"),
        ("queue goal", "self_directed"),
    ]
    after = agent.consciousness.load_self_model()
    assert [e.directive for e in after.pending_pr_requests] == [
        "pr goal 2", "pr goal 3", "pr goal 4",
    ]


@pytest.mark.asyncio
async def test_wip_owned_pr_not_evicted_by_mechanical_standdown(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """092: the mechanical stand-down is the *other* eviction face — it too must
    leave a self-clearable owned PR in contention.

    With the flag on and owned WIP present (an open own PR whose sole blocker is
    an owned ``CONFLICTING`` conflict), a stalled same-SHA lineage is refused by
    the mechanical stand-down, which only evicts *externally*-blocked lineages:
    no ``[stood-down]`` verdict is authored and the head entry is not popped. So
    both eviction faces named by this change — the consecutive-remediation cap
    and the mechanical stand-down — leave owned work in contention, never
    rerouting it to busywork. Zero-cost regression guard against a future
    refactor that stands down on owned blockers.
    """
    from maestro.memory import TaskRecord
    from maestro.pr_facts import PRReadiness
    from maestro.remediation_tracker import find_stand_down

    sha = "a" * 40
    agent = make_agent()
    agent.config.wip_primacy_enabled = True
    agent._vcs.rev_parse.return_value = sha
    agent._git_pr_merged = MagicMock(  # type: ignore[method-assign]
        return_value=False,
    )
    # The live observation reads the owned CONFLICTING conflict; the actuator
    # consults it for the live thread count once the cheap conditions hold.
    agent.observe_pr_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=PRReadiness(
            pr_number=2820,
            branch_landed=False,
            branch_behind=False,
            branch_conflicting=True,
            ci_state="GREEN",
            unresolved_threads=0,
            unaddressed_overflow=0,
            awaiting_approval=False,
            ready_and_waiting=False,
            source="host_api",
            observed_at=123.0,
        ),
    )
    # A stalled same-SHA lineage whose named blocker is owned (CONFLICTING).
    for i in range(STALLED_REMEDIATION_LAP_CAP):
        agent.memory.record_task(
            TaskRecord(
                task_id=f"lap{i}",
                goal="Remediate PR #2820: resolve conflict",
                plan=[],
                outcome="partial",
                result_summary=(
                    "PR #2820 not yet landed (merge blocker: CONFLICTING)."
                ),
                errors=[],
                duration_secs=1.0,
                timestamp="2026-06-26T00:00:00+00:00",
                source="external",
                pr_head_sha=sha,
            ),
        )
    entry = PendingPRRequest(
        directive="Remediate PR #2820: resolve conflict",
        pr_branch="maestro/evolve/ep2820",
    )
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = [entry]
    agent.consciousness.save_self_model(model)

    disposed = agent._dispose_mechanical_standdown(model)

    assert disposed is False
    # The owned self-clearable PR stays in contention — not evicted onto busywork.
    assert model.pending_pr_requests == [entry]
    assert find_stand_down(2820, sha, agent.stand_down_verdicts) is None


def _standdown_disable_other_vetoes(agent: object) -> None:
    """Turn off the two non-reachability stand-down vetoes for a clean path.

    So the ahead-of-origin guard is the sole decision at the chosen-stand-down
    write site (user-request / salvage vetoes each short-circuit before it).
    """
    agent.config.user_request_blocks_standdown_enabled = False  # type: ignore[attr-defined]
    agent.config.salvage_before_disengage_enabled = False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_chosen_standdown_refused_when_branch_ahead_of_origin(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """AC3 (ahead-of-origin push actuator): a chosen ``[stood-down]`` on a branch
    git shows ahead of origin now first runs the mechanical push actuator; when
    that push *fails* (or the owned-branches oracle refuses it) the guard falls
    back to the pre-existing behavior — NO stand-down verdict, a ``partial`` with
    the ``[owned-push-available]`` marker and a fire-or-close directive naming the
    branch. An ahead branch is an owned, pushable act."""
    from maestro.loop.pr_remediation import _OwnedPushResult
    from maestro.memory import TaskRecord

    agent = make_agent()
    _standdown_disable_other_vetoes(agent)
    # Fetch-first seam is a no-op in tests (the real one shells out to git); the
    # rev-list count reports the branch as ahead of origin.
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    agent._vcs.commits_not_on_remote.return_value = 3
    # The mechanical push actuator runs BEFORE any verdict is composed; simulate
    # a genuine push failure so the guard retains the loud owned-push fallback.
    agent._push_owned_branch = MagicMock(  # type: ignore[method-assign]
        return_value=_OwnedPushResult(pushed=False, error="push rejected"),
    )

    record = TaskRecord(
        task_id="t1",
        goal="Remediate PR #77: address comments",
        plan=[],
        outcome="partial",
        result_summary="[stood-down] awaiting external approval on PR #77.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-16T00:00:00+00:00",
        source="external",
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #77: address comments",
        pr_branch="maestro/evolve/ep77",
        record=record,
        head_before="abc",
    )

    # Fetch-first, then the rev-list count.
    agent._fetch_origin_for_merge_truth.assert_called_once()
    agent._vcs.commits_not_on_remote.assert_called_once_with("maestro/evolve/ep77")
    # The push actuator was attempted before any verdict was composed.
    agent._push_owned_branch.assert_called_once_with("maestro/evolve/ep77")
    # No chosen stand-down verdict was minted — the ledger file never appeared.
    assert str(agent.config.memory_dir / "stand_down_verdicts.jsonl") not in mem_fs
    # The lap is re-stamped an owned-push partial in place of the false quiet.
    assert record.outcome == "partial"
    assert Marker.OWNED_PUSH_AVAILABLE in record.result_summary
    assert Marker.OWNED_PUSH_AVAILABLE in (record.key_findings or "")
    # A fire-or-close directive naming the branch was written.
    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is not None
    assert "maestro/evolve/ep77" in model.next_task_directive
    assert "#77" in model.next_task_directive


@pytest.mark.asyncio
async def test_chosen_standdown_written_when_branch_not_ahead(
    make_agent, mem_fs,  # type: ignore[no-untyped-def]
) -> None:
    """AC3 regression: with the branch NOT ahead of origin (count 0), the
    existing chosen-stand-down write path is unchanged — a StandDownVerdict row
    is still written (stamped ``branch_ahead_of_origin=False``) and no
    fire-or-close directive is minted."""
    from maestro.memory import TaskRecord
    from maestro.remediation_tracker import find_stand_down

    agent = make_agent()
    _standdown_disable_other_vetoes(agent)
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    # Nothing local to push — a genuinely external quiet.
    agent._vcs.commits_not_on_remote.return_value = 0
    # ``origin/<branch>`` head the verdict is keyed to (default rev_parse sha).
    origin_head = "0" * 40

    record = TaskRecord(
        task_id="t2",
        goal="Remediate PR #78: address comments",
        plan=[],
        outcome="partial",
        result_summary="[stood-down] awaiting external approval on PR #78.",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-16T00:00:00+00:00",
        source="external",
    )

    await agent._record_remediation_outcome(
        goal="Remediate PR #78: address comments",
        pr_branch="maestro/evolve/ep78",
        record=record,
        head_before="abc",
    )

    verdict = find_stand_down(78, origin_head, agent.stand_down_verdicts)
    assert verdict is not None
    assert verdict.branch_ahead_of_origin is False
    # The record was not re-stamped with the owned-push marker.
    assert Marker.OWNED_PUSH_AVAILABLE not in record.result_summary
    # No fire-or-close directive on the ordinary chosen-stand-down path.
    model = agent.consciousness.load_self_model()
    assert not model.next_task_directive


def test_branch_ahead_of_origin_count_memoized(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A per-branch cached count is returned without re-fetching or re-counting."""
    agent = make_agent()
    agent._fetch_origin_for_merge_truth = MagicMock()  # type: ignore[method-assign]
    agent._ahead_of_origin_memo["maestro/evolve/ep5"] = 7

    assert agent._branch_ahead_of_origin_count("maestro/evolve/ep5") == 7
    # Memo hit: neither the fetch seam nor the rev-list count ran.
    agent._fetch_origin_for_merge_truth.assert_not_called()
    agent._vcs.commits_not_on_remote.assert_not_called()


def test_refuse_owned_push_standdown_record_none_and_directive_occupied(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A ``None`` record is tolerated, and an already-set directive is never
    overwritten — the guard writes only into an empty slot."""
    agent = make_agent()
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Pre-existing higher-precedence directive"
    agent.consciousness.save_self_model(model)

    agent._refuse_owned_push_standdown(80, "maestro/evolve/ep80", None, 2)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.next_task_directive == "Pre-existing higher-precedence directive"


def test_refuse_owned_push_standdown_appends_to_existing_findings(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An empty summary is replaced by the note; a non-empty key_findings has the
    note appended (the two remaining note-composition branches)."""
    from maestro.memory import TaskRecord

    agent = make_agent()
    record = TaskRecord(
        task_id="t9",
        goal="Remediate PR #81: address comments",
        plan=[],
        outcome="partial",
        result_summary="",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-07-16T00:00:00+00:00",
        source="external",
        key_findings="prior finding",
    )

    agent._refuse_owned_push_standdown(81, "maestro/evolve/ep81", record, 4)

    assert record.outcome == "partial"
    # Empty summary → the note stands alone.
    assert record.result_summary.startswith(str(Marker.OWNED_PUSH_AVAILABLE))
    # Non-empty key_findings → the note is appended after the prior text.
    assert record.key_findings is not None
    assert record.key_findings.startswith("prior finding ")
    assert Marker.OWNED_PUSH_AVAILABLE in record.key_findings


def test_message_import_is_module_level() -> None:
    """`Message` is imported once at module scope in agent.py, never lazily.

    Regression guard for #393: the lazy ``from maestro.messaging import Message``
    copies inside ``ask_thomas``, ``respond_to_messages``, and ``_on_message``
    were hoisted to a single module-level import. This AST-over-source guard
    keeps the import from silently drifting back into a function body. Pure
    ``ast`` over the source text — no mem_fs, subprocess, network, or sleep.
    """
    source = (Path(__file__).resolve().parent.parent / "maestro" / "agent.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    def _imports_message(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.ImportFrom)
            and node.module == "maestro.messaging"
            and any(alias.name == "Message" for alias in node.names)
        )

    # (a) No function body imports ``Message`` lazily.
    function_local_lines: set[int] = set()
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for child in ast.walk(func):
            if child is not func and _imports_message(child):
                function_local_lines.add(child.lineno)
    assert not function_local_lines, (
        "found function-local `from maestro.messaging import Message` at "
        f"line(s) {sorted(function_local_lines)}; it must live at module scope"
    )

    # (b) Exactly one module-scope import of ``Message``.
    module_level = [node for node in tree.body if _imports_message(node)]
    assert len(module_level) == 1, (
        "expected exactly one module-scope `from maestro.messaging import "
        f"Message`, found {len(module_level)}"
    )
