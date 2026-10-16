"""Tests for EvolutionEngine — propose, submit, review, learn."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import evolution_quality, evolution_tend, tree_retrieval
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import (
    UNPARSEABLE_PLAN_PLACEHOLDER,
    EvolutionEngine,
    ProposalParseError,
    QualityGateResult,
    _normalise_str_list,
    unread_affected_paths,
)
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import (
    EvolutionPlan,
    Memory,
    RejectionRecord,
)
from maestro.notifications.base import CALLBACK_HASH_PREFIX_LEN, NullNotifier
from maestro.plan_feedback import PlanRef, format_plan_ref, parse_plan_ref
from maestro.sdk import (
    ErrorCategory,
    QueryResult,
    SDKError,
)
from maestro.types import PlanStatus
from maestro.vcs import CodeHost, SubprocessCodeHost, SubprocessVCS, VCSClient
from tests.component.mock_vcs import MockCodeHost, MockVCS
from tests.conftest import MockQueryResult, arm_entry_clean_tree
from tests.support.gap_fuel import GAP
from tests.support.merge_truth import merge_oracle, merge_truth

FAKE_DIR = Path("/fake/evo")


def _make_memory(mem_fs: dict[str, str]) -> Memory:
    return Memory(FAKE_DIR)


def test_evolution_plan_fields() -> None:
    plan = EvolutionPlan(
        plan_id="ep1",
        title="Add self-reflection depth",
        category="awareness",
        rationale="Need deeper introspection",
        proposed_changes=["Modify consciousness.py to add pattern detection"],
        files_affected=["maestro/consciousness.py"],
        expected_impact="Move closer to self-awareness stage",
        created_at="2025-01-01T00:00:00Z",
    )
    assert plan.status == "pending_review"
    assert plan.category == "awareness"
    assert plan.rejection_reason is None


def test_rejection_with_reason(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="ep1",
        title="Risky change",
        category="capability",
        rationale="Test",
        proposed_changes=["Do something"],
        files_affected=[],
        expected_impact="None",
        created_at="2025-01-01T00:00:00Z",
    )
    mem.save_evolution_plan(plan)

    mem.update_plan_status(
        "ep1", "rejected",
        rejection_reason="Too aggressive",
        reviewed_at="2025-01-01T01:00:00Z",
    )

    updated = mem.get_plan("ep1")
    assert updated is not None
    assert updated.status == "rejected"
    assert updated.rejection_reason == "Too aggressive"
    assert updated.reviewed_at == "2025-01-01T01:00:00Z"

    # Verify data persisted in in-memory storage
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    assert plans_path in mem_fs
    assert "Too aggressive" in mem_fs[plans_path]


def test_multiple_plans(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    for i in range(3):
        mem.save_evolution_plan(EvolutionPlan(
            plan_id=f"ep{i}",
            title=f"Plan {i}",
            category="capability",
            rationale=f"Reason {i}",
            proposed_changes=[],
            files_affected=[],
            expected_impact="",
            created_at="2025-01-01T00:00:00Z",
        ))

    all_plans = mem.get_all_plans()
    assert len(all_plans) == 3

    # Approve one
    mem.update_plan_status("ep1", "approved")
    pending = mem.get_pending_plans()
    assert len(pending) == 2


def test_evolution_plan_priority_field() -> None:
    plan = EvolutionPlan(
        plan_id="ep1",
        title="Fix crash",
        category="capability",
        rationale="Bug fix",
        proposed_changes=["Fix the crash"],
        files_affected=["maestro/agent.py"],
        expected_impact="Stability",
        priority="resilience",
        created_at="2025-01-01T00:00:00Z",
    )
    assert plan.priority == "resilience"


def test_evolution_plan_default_priority() -> None:
    plan = EvolutionPlan(
        plan_id="ep1",
        title="New feature",
        category="capability",
        rationale="Add something",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )
    assert plan.priority == "capability"


def test_evolution_plan_issue_number() -> None:
    plan = EvolutionPlan(
        plan_id="ep1",
        title="Fix issue",
        category="infrastructure",
        rationale="Linked to issue",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        priority="resilience",
        issue_number=42,
        created_at="2025-01-01T00:00:00Z",
    )
    assert plan.issue_number == 42


def test_evolution_plan_default_issue_number() -> None:
    plan = EvolutionPlan(
        plan_id="ep1",
        title="Unlinked",
        category="capability",
        rationale="",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )
    assert plan.issue_number is None


def _make_engine(mem_fs: dict[str, str]) -> EvolutionEngine:
    m = Memory(FAKE_DIR)
    c = Consciousness(FAKE_DIR)
    c.load_self_model()
    j = Journal(FAKE_DIR)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    return EvolutionEngine(m, c, j, n, issues=issues, config=Config())


def _engine_with(
    config: Config,
    *,
    vcs: VCSClient | None = None,
    code_host: CodeHost | None = None,
) -> EvolutionEngine:
    """Build an engine with an explicit *config*; omit ``vcs``/``code_host`` to hit fallbacks."""
    m = Memory(FAKE_DIR)
    c = Consciousness(FAKE_DIR)
    c.load_self_model()
    j = Journal(FAKE_DIR)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    return EvolutionEngine(
        m, c, j, n, issues=issues, config=config, vcs=vcs, code_host=code_host,
    )


def test_engine_fallback_workspace_defaults_to_self(mem_fs: dict[str, str]) -> None:
    """Fallback ``SubprocessVCS`` / ``SubprocessCodeHost`` read ``config.workspace``.

    Proposal 107 increment 2: the fallback repo workspace now flows through the
    WorkTarget owner, whose ``repo_workspace`` defaults to ``config.workspace``,
    so a default build is byte-identical.
    """
    config = Config()
    e = _engine_with(config)

    assert isinstance(e._vcs, SubprocessVCS)
    assert e._vcs._cwd == str(config.workspace)
    assert isinstance(e._host, SubprocessCodeHost)
    assert e._host._cwd == str(config.workspace)


def test_engine_fallback_workspace_follows_work_redirect(mem_fs: dict[str, str]) -> None:
    """A ``[work] repo_workspace`` redirect reaches the engine's fallback clients."""
    config = replace(Config(), work_repo_workspace="/ext/checkout")
    e = _engine_with(config)

    assert isinstance(e._vcs, SubprocessVCS)
    assert e._vcs._cwd == "/ext/checkout"
    assert isinstance(e._host, SubprocessCodeHost)
    assert e._host._cwd == "/ext/checkout"


def test_engine_injected_clients_win_over_work_redirect(mem_fs: dict[str, str]) -> None:
    """Injected ``vcs`` / ``code_host`` win even under a ``[work]`` redirect."""
    config = replace(Config(), work_repo_workspace="/ext/checkout")
    injected_vcs = MockVCS(workspace=FAKE_DIR)
    injected_host = MockCodeHost()
    e = _engine_with(config, vcs=injected_vcs, code_host=injected_host)

    assert e._vcs is injected_vcs
    assert e._host is injected_host


def test_engine_fallback_github_repo_follows_work_redirect(mem_fs: dict[str, str]) -> None:
    """A ``[work] github_repo`` redirect reaches the fallback code-host's repo.

    Regression: the fallback ``SubprocessCodeHost`` took its workspace from the
    WorkTarget owner but its ``github_repo`` from the raw constructor param
    (default ``None``), so a direct construction under a ``[work] github_repo``
    redirect targeted ``None`` instead of the target repo. The param now defaults
    to ``wt.github_repo`` when ``None``, mirroring the ``MaestroAgent`` fallbacks.
    """
    config = replace(Config(), work_github_repo="ext-owner/ext-repo")
    e = _engine_with(config)  # no ``github_repo`` param, no injected host

    assert isinstance(e._host, SubprocessCodeHost)
    assert e._host._repo == "ext-owner/ext-repo"
    assert e._github_repo == "ext-owner/ext-repo"


def test_evolution_engine_code_host_property(mem_fs: dict[str, str]) -> None:
    """``EvolutionEngine.code_host`` is a public read-only handle to ``_host``.

    Callers outside the engine (``MaestroAgent``) need the configured
    ``CodeHost`` for PR / publication / doc-proposal flows. Reaching into
    the private ``_host`` attribute would couple the agent to an
    implementation detail; the property gives the same identity through a
    public name. The test asserts identity in both default-construction
    and explicit-``code_host`` shapes.
    """
    e = _make_engine(mem_fs)
    assert e.code_host is e._host

    injected = MagicMock()
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    consciousness.load_self_model()
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e2 = EvolutionEngine(
        memory, consciousness, journal, notifier,
        issues=issues, config=Config(), code_host=injected,
    )
    assert e2.code_host is injected
    assert e2.code_host is e2._host


def test_evolution_engine_mcp_servers_property(mem_fs: dict[str, str]) -> None:
    """``EvolutionEngine.mcp_servers`` is a public read-only handle to ``_mcp_servers``.

    ``MaestroAgent`` configures its implementation session (Session C) with the
    engine's MCP-server dict. Reaching into the private ``_mcp_servers`` attribute
    would couple the agent to an implementation detail; the property gives the
    same identity through a public name (mirroring ``code_host``). The test
    asserts identity in both the default-construction (``None``) and explicit
    ``mcp_servers`` shapes — issue #1236.
    """
    e = _make_engine(mem_fs)
    assert e.mcp_servers is None
    assert e.mcp_servers is e._mcp_servers

    injected = {"github": {"url": "https://example.invalid"}}
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    consciousness.load_self_model()
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e2 = EvolutionEngine(
        memory, consciousness, journal, notifier,
        issues=issues, config=Config(), mcp_servers=injected,
    )
    assert e2.mcp_servers is injected
    assert e2.mcp_servers is e2._mcp_servers


@pytest.mark.asyncio
async def test_propose_plan_non_list_changes_uses_fallback(
    mem_fs: dict[str, str],
) -> None:
    """If proposed_changes is an int (not list/str), the fallback message must be used."""
    e = _make_engine(mem_fs)

    plan_json = json.dumps({
        "title": "Bad plan",
        "category": "capability",
        "rationale": "Testing",
        "proposed_changes": 42,
        "files_affected": 99,
        "expected_impact": "None",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.proposed_changes == [
        "(Could not parse structured plan from Claude response)"
    ]
    assert plan.files_affected == []


def test_normalise_str_list_filters_list() -> None:
    """A list is filtered to the stringified non-blank entries (unstripped)."""
    result = _normalise_str_list(["ok", None, "  ", "", 42, "  keep  "])
    assert result == ["ok", "42", "  keep  "]


def test_normalise_str_list_wraps_non_blank_string() -> None:
    """A non-blank string becomes a one-item list."""
    assert _normalise_str_list("single change") == ["single change"]


def test_normalise_str_list_blank_string_is_empty() -> None:
    """An empty or whitespace-only string yields []."""
    assert _normalise_str_list("") == []
    assert _normalise_str_list("   ") == []


def test_normalise_str_list_non_list_non_str_is_empty() -> None:
    """A value that is neither list nor non-blank string yields []."""
    assert _normalise_str_list(42) == []
    assert _normalise_str_list({}) == []
    assert _normalise_str_list(None) == []


@pytest.mark.asyncio
async def test_propose_plan_empty_normalised_changes_use_placeholder(
    mem_fs: dict[str, str],
) -> None:
    """proposed_changes normalising empty gets the placeholder; files_affected stays []."""
    e = _make_engine(mem_fs)

    plan_json = json.dumps({
        "title": "Blank plan",
        "category": "capability",
        "rationale": "Testing",
        "proposed_changes": ["", "   ", None],
        "files_affected": ["", None],
        "expected_impact": "None",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert plan.proposed_changes == [UNPARSEABLE_PLAN_PLACEHOLDER]
    assert plan.files_affected == []


# ---------------------------------------------------------------------------
# Merged from test_evolution_async.py — EvolutionEngine async methods
# ---------------------------------------------------------------------------


def _make(mem_fs: dict[str, str]) -> tuple[EvolutionEngine, Memory, Consciousness, Journal]:
    d = Path("/fake/evo_async")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(m, c, j, n, issues=issues, config=Config())
    return e, m, c, j


# ---------------------------------------------------------------------------
# Proposal 114 — plan-premise grounding (propose-time emission + submit guard)
# ---------------------------------------------------------------------------


class _RecordingGateBackend:
    """In-memory ``GateActivationBackend`` spy for the grounding-guard tests.

    Satisfies the three-method backend protocol structurally so a test can assert
    the ``plan_grounding`` row the submission guard stamps without touching the FS.
    """

    def __init__(self) -> None:
        self.rows: list[object] = []

    def append(self, record: object) -> None:
        self.rows.append(record)

    def read_all(self) -> list[object]:
        return list(self.rows)

    def read_tail(self, limit: int) -> list[object]:
        return list(self.rows)[-limit:] if limit > 0 else []

    def read_gate_tail(self, gate: str, limit: int) -> list[object]:
        rows = [r for r in self.rows if getattr(r, "gate", None) == gate]
        return rows[-limit:] if limit > 0 else []


def _themed_obsolete(i: int) -> EvolutionPlan:
    """An ``obsolete`` plan on the self-measurement theme, distinct per ``i``.

    Carries a self-measurement keyword (``sensor``) so
    ``classify_self_measurement_plan`` matches, with per-index vocabulary so the
    seeds trip no content guard — isolating the admission gate's theme arm over
    the obsolete graveyard. ``i`` in 1..9 keeps ``created_at`` well-formed.
    """
    return EvolutionPlan(
        plan_id=f"obs{i}", title=f"Add behavioral sensor number {i}",
        category="capability",
        rationale=f"sensor telemetry{i} gauge{i} probe{i} reading{i} signal{i}",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="obsolete", created_at=f"2025-03-0{i}T00:00:00Z",
    )


def _themed_candidate() -> EvolutionPlan:
    """A self-measurement-themed candidate (matches the theme classifier)."""
    return EvolutionPlan(
        plan_id="themedcand", title="Add another audit rule sensor",
        category="capability",
        rationale="new discriminator metric over the servicing laps ledger",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-04-01T00:00:00Z",
    )


# --- Shipped-CLI-surface grounding guard --------------------------------------

def _cli_plan(
    plan_id: str,
    title: str,
    changes: list[str],
    files: list[str] | None = None,
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id, title=title, category="capability",
        rationale="ground the CLI proposal against the shipped surface",
        proposed_changes=changes,
        files_affected=["maestro/cli.py"] if files is None else files,
        expected_impact="", created_at="2025-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Rejection pattern analysis
# ---------------------------------------------------------------------------


def _make_rejection_record(**overrides: object) -> RejectionRecord:
    defaults: dict[str, object] = dict(
        plan_id="ep_1",
        title="Plan",
        category="capability",
        priority="capability",
        rejection_reason="because",
        timestamp="2026-04-23T00:00:00+00:00",
        rejection_category="other",
    )
    defaults.update(overrides)
    return RejectionRecord(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_implement_unapproved_raises(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="p1", title="Not approved",
        category="capability", rationale="", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="pending_review", created_at="",
    )

    with pytest.raises(
        RuntimeError,
        match=r"Cannot implement plan with status='pending_review'",
    ):
        await e.implement(plan)


@pytest.mark.asyncio
async def test_implement_accepts_failed_plan(mem_fs: dict[str, str]) -> None:
    """Failed plans (eligible for retry) must pass the status guard."""
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="pFailedRetry", title="Retry me",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="failed", failure_count=1, created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.get_head_hash.return_value = "base_sha"
    arm_entry_clean_tree(vcs)
    e._vcs = vcs

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="done")
        base = await e.implement(plan)

    assert base == "base_sha"
    mock_q.assert_awaited_once()


def test_run_quality_gates_clean(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)

    with patch("maestro.evolution.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        result = e.run_quality_gates()

    assert result.passed is True
    assert result.ruff_ok is True
    assert result.mypy_ok is True
    assert result.tests_ok is True
    assert "Ruff: clean" in result.raw_output
    assert "Mypy: clean" in result.raw_output
    assert "Tests: clean" in result.raw_output
    assert mock_run.call_count == 3

    pytest_call = mock_run.call_args_list[2]
    pytest_cmd = pytest_call.args[0] if pytest_call.args else pytest_call[0][0]
    assert "--cov=maestro" in pytest_cmd
    assert "--cov-fail-under=97" in pytest_cmd


def test_run_quality_gates_uses_configured_cov_threshold(mem_fs: dict[str, str]) -> None:
    """A non-default ``cov_fail_under`` flows into the pytest gate command.

    ``test_run_quality_gates_clean`` asserts the default threshold (97), so it
    would still pass if the value were hardcoded; a non-default value pins
    ``run_quality_gates`` to reading ``Config.cov_fail_under`` dynamically.
    """
    d = Path("/fake/evo_cov")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues, config=Config(cov_fail_under=80),
    )

    with patch("maestro.evolution.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        result = e.run_quality_gates()

    assert result.passed is True
    pytest_call = mock_run.call_args_list[2]
    pytest_cmd = pytest_call.args[0] if pytest_call.args else pytest_call[0][0]
    assert "--cov-fail-under=80" in pytest_cmd
    assert "--cov-fail-under=97" not in pytest_cmd


def test_run_quality_gates_honours_work_target_atoms(mem_fs: dict[str, str]) -> None:
    """A ``[work]``-overridden gate target swaps the package/tests paths (proposal 107).

    The gate source/tests paths are read from ``Config.work_target()`` rather
    than hardcoded, so overriding the ``[work]`` gate atoms retargets ruff,
    mypy, and pytest (including ``--cov``) at another package by config alone.
    """
    d = Path("/fake/evo_wt")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues,
        config=Config(
            work_gate_source_path="widget/",
            work_gate_tests_path="widget_tests/",
            work_gate_lint_extra_paths=["widget_scripts/"],
        ),
    )

    with patch("maestro.evolution.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        result = e.run_quality_gates()

    assert result.passed is True
    ruff_cmd = mock_run.call_args_list[0].args[0]
    mypy_cmd = mock_run.call_args_list[1].args[0]
    pytest_cmd = mock_run.call_args_list[2].args[0]
    assert ruff_cmd == ["ruff", "check", "widget/", "widget_tests/", "widget_scripts/"]
    assert mypy_cmd == ["mypy", "widget/", "widget_scripts/"]
    assert "widget_tests/" in pytest_cmd
    assert "widget_scripts/" not in pytest_cmd
    assert "--cov=widget" in pytest_cmd
    assert "--cov=maestro" not in pytest_cmd


def test_run_quality_gates_with_failures(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ruff" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="maestro/foo.py:1: E501 line too long", stderr="",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="",
        )

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert result.passed is False
    assert result.ruff_ok is False
    assert result.mypy_ok is True
    assert result.tests_ok is True
    assert "Ruff issues:" in result.raw_output
    assert "E501" in result.raw_output
    assert "Mypy: clean" in result.raw_output


def test_run_quality_gates_stdout_stderr_separated(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ruff" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="stdout_end", stderr="stderr_start",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="",
        )

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert "stdout_endstderr_start" not in result.raw_output
    assert "stdout_end" in result.raw_output
    assert "stderr_start" in result.raw_output


def test_run_quality_gates_timeout_marks_gate_failed(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "mypy" in cmd:
            raise subprocess.TimeoutExpired(cmd, 60)
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="",
        )

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert result.passed is False
    assert result.ruff_ok is True
    assert result.mypy_ok is False
    assert result.tests_ok is True
    assert "Mypy: could not run" in result.raw_output


# ---------------------------------------------------------------------------
# Proposal 124 — gate observation (default-off rollback guarantee + the new fact)
# ---------------------------------------------------------------------------


def _observing_engine(config: Config | None = None) -> EvolutionEngine:
    """An engine with ``gate_observation_enabled`` on, unless *config* overrides."""
    d = Path("/fake/evo_obs")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    return EvolutionEngine(
        m, c, j, n, issues=issues,
        config=config or Config(gate_observation_enabled=True),
    )


# ---------------------------------------------------------------------------
# Rejection-cooldown width fix — a differently-titled but content-restated
# re-proposal of a within-cooldown rejected plan is refused too, reusing
# ``is_plan_restatement``'s existing Jaccard threshold (no new constant).
# ---------------------------------------------------------------------------

# Shared rationale body — long enough that its content words dominate the
# title-plus-rationale token set, so a rework of the *title* alone still trips
# the restatement threshold. Reused verbatim by the refuse/outside-window
# cases so their Jaccard is identical; the accept case swaps in disjoint words.
_SIMILAR_RATIONALE = (
    "Duplicate proposals dominate rejections because capability plans keep "
    "getting minted against stale tree assumptions without rereading disk "
    "first, flooding the review queue with restated losers."
)


# ---------------------------------------------------------------------------
# Merged from bug hunt files — propose_plan edge cases
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(mem_fs: dict[str, str]) -> EvolutionEngine:
    d = Path("/fake/evo_deep")
    config = Config(memory_dir=d, workspace=d)
    config.ensure_dirs()
    memory = Memory(d)
    consciousness = Consciousness(d)
    journal = Journal(d)
    notifier = NullNotifier()
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.has_changes.return_value = True
    vcs.get_head_hash.return_value = "abc123"
    code_host = MagicMock()
    return EvolutionEngine(
        memory, consciousness, journal, notifier,
        config=config, vcs=vcs, code_host=code_host,
    )


@pytest.mark.asyncio
async def test_propose_plan_whitespace_only_changes_string(
    engine: EvolutionEngine,
) -> None:
    llm_response = json.dumps({
        "title": "Test plan", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": "   ",
        "files_affected": ["maestro/foo.py"], "expected_impact": "none",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response=llm_response)
        plan = await engine.propose_plan(gap=GAP)

    for change in plan.proposed_changes:
        assert change.strip()


@pytest.mark.asyncio
async def test_propose_plan_whitespace_only_files_string(
    engine: EvolutionEngine,
) -> None:
    llm_response = json.dumps({
        "title": "Test plan", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["Real change"],
        "files_affected": "   ", "expected_impact": "none",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response=llm_response)
        plan = await engine.propose_plan(gap=GAP)

    for f in plan.files_affected:
        assert f.strip()


@pytest.mark.asyncio
async def test_propose_plan_null_title_uses_fallback(
    engine: EvolutionEngine,
) -> None:
    llm_response = json.dumps({
        "title": None, "category": "capability", "priority": "capability",
        "rationale": "some rationale", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "none",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response=llm_response)
        plan = await engine.propose_plan(gap=GAP)

    assert plan.title != "None"


@pytest.mark.asyncio
async def test_propose_plan_null_rationale_uses_fallback(
    engine: EvolutionEngine,
) -> None:
    llm_response = json.dumps({
        "title": "Valid title", "category": "capability", "priority": "capability",
        "rationale": None, "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "none",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response=llm_response)
        plan = await engine.propose_plan(gap=GAP)

    assert plan.rationale != "None"
    assert plan.rationale == f"Close gap issue #{GAP.issue.number}"


@pytest.mark.asyncio
async def test_propose_plan_none_in_proposed_changes_filtered(
    mem_fs: dict[str, str],
) -> None:
    e = _make_engine(mem_fs)

    mock_result = MockQueryResult(response=json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": [None, "real change"],
        "files_affected": ["a.py"], "expected_impact": "none",
    }))
    with patch("maestro.evolution.query", new_callable=AsyncMock, return_value=mock_result):
        plan = await e.propose_plan(gap=GAP)

    assert "None" not in plan.proposed_changes
    assert "real change" in plan.proposed_changes


# ---------------------------------------------------------------------------
# Merged from bug hunt files — propose_plan & submit edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_plan_stringifies_expected_impact(
    engine: EvolutionEngine,
) -> None:
    """expected_impact from LLM response should be stringified."""
    llm_response = json.dumps({
        "title": "Test plan",
        "category": "capability",
        "priority": "capability",
        "rationale": "Because reasons",
        "proposed_changes": ["change 1"],
        "files_affected": ["file.py"],
        "expected_impact": ["multiple", "impacts"],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response=llm_response)
        plan = await engine.propose_plan(gap=GAP)

    assert isinstance(plan.expected_impact, str)


@pytest.mark.asyncio
async def test_propose_plan_list_response_does_not_crash(
    engine: EvolutionEngine,
) -> None:
    """If parse_json() returns a list instead of a dict, propose_plan should not crash."""
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response='["change 1", "change 2"]')
        plan = await engine.propose_plan(gap=GAP)

    assert plan.title is not None
    assert plan.proposed_changes


@pytest.mark.asyncio
async def test_propose_plan_negative_issue_number_rejected(
    engine: EvolutionEngine,
) -> None:
    """Negative issue_number from LLM should be set to None."""
    llm_response = json.dumps({
        "title": "Fix bug",
        "category": "capability",
        "priority": "resilience",
        "rationale": "Fix issue",
        "proposed_changes": ["change"],
        "files_affected": ["a.py"],
        "expected_impact": "improvement",
        "issue_number": -5,
    })

    # A gap plan pins its issue, so the spec normaliser is exercised directly.
    plan = engine._plan_from_spec(json.loads(llm_response), default_rationale="r")

    assert plan.issue_number is None, (
        f"Negative issue_number {plan.issue_number} should have been set to None"
    )


@pytest.mark.asyncio
async def test_submit_for_review_empty_title_not_deduplicated(
    mem_fs: dict[str, str],
) -> None:
    """Two plans with empty/whitespace-only titles should not be deduplicated."""
    e, m, _c, _j = _make(mem_fs)

    plan1 = EvolutionPlan(
        plan_id="plan1", title="  ",
        category="capability", rationale="First plan",
        proposed_changes=["Change A"], files_affected=[],
        expected_impact="Impact A",
        created_at="2025-01-01T00:00:00Z",
    )
    plan2 = EvolutionPlan(
        plan_id="plan2", title="",
        category="infrastructure", rationale="Completely different plan",
        proposed_changes=["Change B"], files_affected=[],
        expected_impact="Impact B",
        created_at="2025-01-02T00:00:00Z",
    )

    await e.submit_for_review(plan1)
    await e.submit_for_review(plan2)

    all_plans = m.get_all_plans()
    plan_ids = [p.plan_id for p in all_plans]
    assert "plan1" in plan_ids
    assert "plan2" in plan_ids, (
        "Second plan with empty title was incorrectly deduplicated "
        "against first empty-titled plan"
    )


@pytest.mark.asyncio
async def test_propose_plan_all_none_changes_produce_non_empty_list(
    engine: EvolutionEngine,
) -> None:
    """When all proposed_changes items are None, the result should have a fallback."""
    llm_response = json.dumps({
        "title": "Test plan",
        "category": "capability",
        "priority": "capability",
        "rationale": "Test rationale",
        "proposed_changes": [None, None, None],
        "files_affected": ["test.py"],
        "expected_impact": "Impact",
        "issue_number": None,
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response=llm_response)
        plan = await engine.propose_plan(gap=GAP)

    assert len(plan.proposed_changes) > 0, (
        "proposed_changes should not be empty after filtering out None items"
    )


@pytest.mark.asyncio
async def test_propose_plan_empty_string_changes_have_content(
    engine: EvolutionEngine,
) -> None:
    """proposed_changes with only empty strings should have meaningful content."""
    llm_response = json.dumps({
        "title": "Test Plan",
        "category": "capability",
        "priority": "capability",
        "rationale": "test",
        "proposed_changes": ["", "", ""],
        "files_affected": ["a.py"],
        "expected_impact": "none",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response=llm_response)
        plan = await engine.propose_plan(gap=GAP)

    has_content = any(c.strip() for c in plan.proposed_changes)
    assert has_content, (
        f"proposed_changes should not be all empty strings: {plan.proposed_changes}"
    )


@pytest.mark.asyncio
async def test_propose_plan_empty_string_files_affected_filtered(
    engine: EvolutionEngine,
) -> None:
    """Empty and whitespace-only filenames in files_affected should be filtered."""
    llm_response = json.dumps({
        "title": "Test",
        "category": "capability",
        "priority": "capability",
        "rationale": "Test rationale",
        "proposed_changes": ["Change 1"],
        "files_affected": ["maestro/foo.py", "", "  ", "maestro/bar.py"],
        "expected_impact": "None",
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = QueryResult(response=llm_response)
        plan = await engine.propose_plan(gap=GAP)

    assert "" not in plan.files_affected
    assert "  " not in plan.files_affected
    assert "maestro/foo.py" in plan.files_affected
    assert "maestro/bar.py" in plan.files_affected


def test_evolution_branch_format() -> None:
    """evolution_branch should produce valid git branch names."""
    from maestro.vcs import evolution_branch

    result = evolution_branch("abc123")
    assert result == "maestro/evolve/abc123"
    assert "/" in result


# ---------------------------------------------------------------------------
# Closed-PR detection in check_merged_plans
# ---------------------------------------------------------------------------


def test_check_merged_plans_closed_pr_becomes_rejected(
    mem_fs: dict[str, str],
) -> None:
    """check_merged_plans() must transition an implemented plan to 'rejected'
    when the corresponding PR was closed without merging.

    This is the rejection-awareness fix: a plan whose PR is closed (CLOSED
    state, not MERGED) must not stay in 'implemented' forever — that would
    leave Maestro with a false belief about its own history.
    """
    from tests.component.mock_vcs import MockCodeHost

    d = Path("/fake/evo_closed_pr")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    code_host = MockCodeHost()

    e = EvolutionEngine(
        m, c, j, n, issues=issues,
        code_host=code_host,
        config=Config(memory_dir=d, workspace=d),
    )

    plan = EvolutionPlan(
        plan_id="plan-closed-pr-1",
        title="Closed without merging",
        category="capability",
        rationale="Test closed PR detection",
        proposed_changes=["Change something"],
        files_affected=[],
        expected_impact="None",
        created_at="2025-01-01T00:00:00Z",
    )
    m.save_evolution_plan(plan)
    m.update_plan_status(
        "plan-closed-pr-1", "implemented",
        pr_url="https://github.com/test/repo/pull/7",
    )

    result = e.check_merged_plans(
        settled_oracle=merge_oracle(
            {7: merge_truth(7, settled=True, branch_landed=False, state="CLOSED")},
        ),
    )

    updated = m.get_plan("plan-closed-pr-1")
    assert updated is not None
    assert updated.status == "rejected", (
        f"Expected status 'rejected' but got {updated.status!r}: "
        "a closed PR should mark the plan as rejected, not leave it implemented"
    )
    assert "plan-closed-pr-1" not in result.merged, (
        "A closed (not merged) plan should not appear in the merged list"
    )
    assert any(pid == "plan-closed-pr-1" for pid, _ in result.rejected), (
        "A closed PR should appear in the rejected list"
    )


@pytest.mark.asyncio
async def test_check_merged_plans_closed_pr_restarts_rejection_cooldown(
    mem_fs: dict[str, str],
) -> None:
    """A PR closed long after approval must time the rejection cooldown from
    the close, not the approval (#4068): same-title and content-restating
    re-proposals are refused right after the close.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    e, m, _c, _j = _make(mem_fs)

    approved_at = (_dt.now(_UTC) - _td(days=10)).isoformat()
    m.save_evolution_plan(EvolutionPlan(
        plan_id="plan-cool-1", title="Curb Duplicate Proposal Flood",
        category="capability", rationale=_SIMILAR_RATIONALE,
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at=approved_at,
    ))
    m.update_plan_status(
        "plan-cool-1", "implemented",
        pr_url="https://github.com/test/repo/pull/8", reviewed_at=approved_at,
    )

    before = _dt.now(_UTC)
    e.check_merged_plans(
        settled_oracle=merge_oracle(
            {8: merge_truth(8, settled=True, branch_landed=False, state="CLOSED")},
        ),
    )

    closed = m.get_plan("plan-cool-1")
    assert closed is not None
    assert closed.status == "rejected"
    assert closed.reviewed_at is not None
    assert _dt.fromisoformat(closed.reviewed_at) >= before

    same_title = EvolutionPlan(
        plan_id="pretry-title", title="Curb Duplicate Proposal Flood",
        category="capability", rationale="unrelated body",
        proposed_changes=["x"], files_affected=[], expected_impact="",
    )
    restated = EvolutionPlan(
        plan_id="pretry-content", title="Refuse Restated Re-proposals",
        category="capability", rationale=_SIMILAR_RATIONALE,
        proposed_changes=["targeted change"], files_affected=[],
        expected_impact="",
    )
    await e.submit_for_review(same_title)
    await e.submit_for_review(restated)

    saved_ids = {p.plan_id for p in m.get_all_plans()}
    assert "pretry-title" not in saved_ids
    assert "pretry-content" not in saved_ids


def test_check_merged_plans_merged_pr_still_becomes_merged(
    mem_fs: dict[str, str],
) -> None:
    """Existing merged-PR behaviour must not regress: merged PRs → 'merged'."""
    from tests.component.mock_vcs import MockCodeHost

    d = Path("/fake/evo_merged_pr")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    code_host = MockCodeHost()

    e = EvolutionEngine(
        m, c, j, n, issues=issues,
        code_host=code_host,
        config=Config(memory_dir=d, workspace=d),
    )

    plan = EvolutionPlan(
        plan_id="plan-merged-pr-1",
        title="Merged successfully",
        category="capability",
        rationale="Test merged PR still works",
        proposed_changes=["Change something"],
        files_affected=[],
        expected_impact="None",
        created_at="2025-01-01T00:00:00Z",
    )
    m.save_evolution_plan(plan)
    m.update_plan_status(
        "plan-merged-pr-1", "implemented",
        pr_url="https://github.com/test/repo/pull/5",
    )

    result = e.check_merged_plans(
        settled_oracle=merge_oracle(
            {5: merge_truth(5, settled=True, branch_landed=True, state="MERGED")},
        ),
    )

    updated = m.get_plan("plan-merged-pr-1")
    assert updated is not None
    assert updated.status == "merged"
    assert "plan-merged-pr-1" in result.merged


def test_check_merged_plans_open_pr_unchanged(
    mem_fs: dict[str, str],
) -> None:
    """An open (neither merged nor closed) PR must leave the plan 'implemented'."""
    from tests.component.mock_vcs import MockCodeHost

    d = Path("/fake/evo_open_pr")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    # No merged_prs or closed_prs set — PR #9 is open
    code_host = MockCodeHost()

    e = EvolutionEngine(
        m, c, j, n, issues=issues,
        code_host=code_host,
        config=Config(memory_dir=d, workspace=d),
    )

    plan = EvolutionPlan(
        plan_id="plan-open-pr-1",
        title="Still open",
        category="capability",
        rationale="Test open PR leaves plan alone",
        proposed_changes=["Change something"],
        files_affected=[],
        expected_impact="None",
        created_at="2025-01-01T00:00:00Z",
    )
    m.save_evolution_plan(plan)
    m.update_plan_status(
        "plan-open-pr-1", "implemented",
        pr_url="https://github.com/test/repo/pull/9",
    )

    result = e.check_merged_plans(settled_oracle=merge_oracle({}))

    updated = m.get_plan("plan-open-pr-1")
    assert updated is not None
    assert updated.status == "implemented"
    assert "plan-open-pr-1" not in result.merged
    assert result.rejected == []


def test_check_merged_plans_sources_flips_from_oracle_not_primitives(
    mem_fs: dict[str, str],
) -> None:
    """#2958: the merged/rejected flips read the injected chokepoint oracle only.

    Constructs an engine whose ``code_host`` raises ``AssertionError`` the
    moment a raw merge primitive (``is_pr_merged`` / ``is_pr_closed``) is
    called, then injects a ``settled_oracle`` reporting one PR merged and
    another closed.  Both durable flips must fire off the oracle's verdict and
    the raw primitives must stay untouched — proving ``check_merged_plans`` no
    longer bypasses the ``is_pr_settled`` merge-truth chokepoint.
    """
    d = Path("/fake/evo_oracle_only")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    host = MagicMock()
    host.is_pr_merged.side_effect = AssertionError(
        "check_merged_plans must not call the raw is_pr_merged primitive",
    )
    host.is_pr_closed.side_effect = AssertionError(
        "check_merged_plans must not call the raw is_pr_closed primitive",
    )

    e = EvolutionEngine(
        m, c, j, n, issues=issues,
        code_host=host,
        config=Config(memory_dir=d, workspace=d),
    )

    for pid, pr in (("plan-oracle-merged", 11), ("plan-oracle-closed", 22)):
        m.save_evolution_plan(
            EvolutionPlan(
                plan_id=pid,
                title=pid,
                category="capability",
                rationale="Oracle-routed flip",
                proposed_changes=["Change something"],
                files_affected=[],
                expected_impact="None",
                created_at="2025-01-01T00:00:00Z",
            )
        )
        m.update_plan_status(
            pid, "implemented",
            pr_url=f"https://github.com/test/repo/pull/{pr}",
        )

    result = e.check_merged_plans(
        settled_oracle=merge_oracle(
            {
                11: merge_truth(11, settled=True, branch_landed=True, state="MERGED"),
                22: merge_truth(
                    22, settled=True, branch_landed=False, state="CLOSED",
                ),
            },
        ),
    )

    assert result.merged == ["plan-oracle-merged"]
    assert [pid for pid, _ in result.rejected] == ["plan-oracle-closed"]
    merged_plan = m.get_plan("plan-oracle-merged")
    closed_plan = m.get_plan("plan-oracle-closed")
    assert merged_plan is not None
    assert closed_plan is not None
    assert merged_plan.status == "merged"
    assert closed_plan.status == "rejected"
    assert closed_plan.rejection_reason == "PR closed without merging"
    host.is_pr_merged.assert_not_called()
    host.is_pr_closed.assert_not_called()


# ---------------------------------------------------------------------------
# discard_changes exception safety
# ---------------------------------------------------------------------------


def test_discard_changes_does_not_raise_on_non_vcs_error() -> None:
    """discard_changes() must not propagate exceptions other than VCSError.

    The method's docstring promises 'does not raise'.  The original code
    only caught VCSError, so an unexpected RuntimeError from _run() would
    propagate and break the 'does not raise' contract.  After the fix,
    any exception from _run() is absorbed and logged.
    """
    from unittest.mock import patch as _patch

    from maestro.vcs import SubprocessVCS

    vcs = SubprocessVCS(
        Path("/fake/workspace"), timeout=10, quick_timeout=5, push_timeout=10,
    )
    with _patch.object(vcs, "_run", side_effect=RuntimeError("unexpected non-VCS error")):
        # Must not raise — the 'does not raise' promise must hold for all
        # exception types, not just VCSError.
        vcs.discard_changes()


# ---------------------------------------------------------------------------
# Closed-PR detection: check_merged_plans must reject closed PRs
# ---------------------------------------------------------------------------


def _make_ci_engine(
    mem_fs: dict[str, str],
    host: object | None = None,
) -> EvolutionEngine:
    """Helper for CI-related engine tests that need a code_host."""
    from unittest.mock import MagicMock
    d = Path("/fake/evo_ci")
    m = Memory(d)
    c = Consciousness(d)
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    if host is None:
        host = MagicMock()
    return EvolutionEngine(
        m, c, j, n, issues=issues, github_repo="owner/repo",
        config=Config(memory_dir=d, workspace=d), vcs=vcs, code_host=host,
    )


def _ci_plan(**overrides: object) -> EvolutionPlan:
    defaults: dict[str, object] = {
        "plan_id": "ci_plan01",
        "title": "Test CI plan",
        "category": "capability",
        "rationale": "For CI testing",
        "proposed_changes": ["Change X"],
        "files_affected": ["maestro/agent.py"],
        "expected_impact": "Improvement",
        "priority": "capability",
        "status": "implemented",
        "pr_url": "https://github.com/o/r/pull/55",
        "created_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return EvolutionPlan(**defaults)  # type: ignore[arg-type]


def test_check_merged_plans_detects_closed_pr(
    mem_fs: dict[str, str],
) -> None:
    """A closed PR must transition plan status to 'rejected'.

    Post-#2958 the merged/closed decision is sourced from the injected
    ``settled_oracle`` (the ``is_pr_settled`` merge-truth chokepoint), not a
    raw host round-trip: a ``settled`` observation that never ``branch_landed``
    is a closed-without-merging PR and must flip the plan to ``rejected``.  The
    host's raw merge primitives must not be consulted at all.
    """
    from unittest.mock import MagicMock
    host = MagicMock()

    e = _make_ci_engine(mem_fs, host=host)
    plan = _ci_plan(plan_id="closed01", pr_url="https://github.com/o/r/pull/55")
    e._memory.save_evolution_plan(plan)

    result = e.check_merged_plans(
        settled_oracle=merge_oracle(
            {55: merge_truth(55, settled=True, branch_landed=False, state="CLOSED")},
        ),
    )

    assert result.merged == [], "closed PRs must not appear in the merged list"
    assert any(pid == "closed01" for pid, _ in result.rejected), \
        "closed PRs must appear in the rejected list"
    host.is_pr_closed.assert_not_called()
    host.is_pr_merged.assert_not_called()
    updated = e._memory.get_plan("closed01")
    assert updated is not None
    assert updated.status == "rejected", \
        f"Expected 'rejected' but got {updated.status!r} — closed PRs must be detected"
    assert updated.rejection_reason == "PR closed without merging"


def test_check_merged_plans_open_pr_stays_implemented(
    mem_fs: dict[str, str],
) -> None:
    """An open (neither merged nor closed) PR must leave plan as 'implemented'."""
    from unittest.mock import MagicMock
    host = MagicMock()

    e = _make_ci_engine(mem_fs, host=host)
    plan = _ci_plan(plan_id="open01", pr_url="https://github.com/o/r/pull/66")
    e._memory.save_evolution_plan(plan)

    result = e.check_merged_plans(settled_oracle=merge_oracle({}))

    assert result.merged == []
    assert result.rejected == []
    host.is_pr_merged.assert_not_called()
    host.is_pr_closed.assert_not_called()
    updated = e._memory.get_plan("open01")
    assert updated is not None
    assert updated.status == "implemented"


# ---------------------------------------------------------------------------
# discard_changes: must not propagate non-VCSError exceptions
# ---------------------------------------------------------------------------


def test_discard_changes_does_not_propagate_unexpected_exception() -> None:
    """SubprocessVCS.discard_changes() must not raise even when _run raises
    a non-VCSError (e.g. RuntimeError).

    This test was written first (failing) to prove the broken promise:
    before the fix, only VCSError was caught, so any unexpected exception
    from _run would propagate out of discard_changes, violating the
    'Best-effort — does not raise' contract in the docstring.
    """
    from unittest.mock import patch

    from maestro.vcs import SubprocessVCS

    vcs = SubprocessVCS(Path("/fake/ws"))

    # Patch _run to raise RuntimeError — not a VCSError
    with patch.object(vcs, "_run", side_effect=RuntimeError("unexpected OS error")):
        # Must not raise despite the unexpected exception from _run
        vcs.discard_changes()


# ------------------------------------------------------------------
# auto_fix
# ------------------------------------------------------------------

_FAIL_REVIEW = QualityGateResult(
    passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
    raw_output="Ruff issues:\nE501 line too long",
)


# ------------------------------------------------------------------
# amend_commit
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_fix_sets_caller_and_effort_telemetry(
    mem_fs: dict[str, str],
) -> None:
    """auto_fix() must label its SDK call so cost rollups can attribute it."""
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="afT", title="Telemetry test",
        category="capability", rationale="t", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.has_changes.return_value = True
    e._vcs = vcs

    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="fixed")
        await e.auto_fix(plan, _FAIL_REVIEW, round_n=1, max_rounds=3)

    options = mock_q.call_args.kwargs["options"]
    assert options.caller == "auto_fix"
    assert options.effort == "high"


@pytest.mark.asyncio
async def test_auto_fix_pins_cwd_to_workspace(mem_fs: dict[str, str]) -> None:
    """auto_fix()'s bare query must pin the agent to the workspace tree.

    Without ``cwd`` the SDK agent subprocess edits whatever directory the
    loop process was launched from while the surrounding git plumbing
    targets ``config.workspace`` — issue #420.
    """
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="afCwd", title="Cwd test",
        category="capability", rationale="t", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.has_changes.return_value = True
    e._vcs = vcs

    with patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_sub.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="diff", stderr="",
        )
        mock_q.return_value = MockQueryResult(response="fixed")
        await e.auto_fix(plan, _FAIL_REVIEW, round_n=1, max_rounds=3)

    options = mock_q.call_args.kwargs["options"]
    assert options.cwd == str(e._config.workspace)


@pytest.mark.asyncio
async def test_auto_fix_diff_timeout_still_queries(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="af3", title="Diff timeout test",
        category="capability", rationale="test", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="better",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.has_changes.return_value = False
    e._vcs = vcs

    with patch("maestro.evolution.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)), \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="tried to fix")

        result = await e.auto_fix(plan, _FAIL_REVIEW, round_n=1, max_rounds=3)

    assert result is False
    # Prompt should include fallback text when diff can't be retrieved
    prompt = mock_q.call_args.kwargs.get("prompt", "")
    assert "(could not retrieve diff)" in prompt


@pytest.mark.asyncio
async def test_auto_fix_diff_uses_base_branch_on_single_commit(
    mem_fs: dict[str, str],
) -> None:
    """The auto-fix prompt must carry the real implementation diff even when
    ``HEAD~1`` cannot be resolved (a single-commit branch).

    Regression for the masked bad state: ``git diff HEAD~1`` exits non-zero
    with empty stdout on such a branch and — since the call runs without
    ``check=True`` — the model was handed ``(no diff)`` and lost sight of the
    change it must fix. Diffing against the base branch with
    ``origin/main...HEAD`` resolves the full change set regardless of commit
    count. The side-effect returns the old-command bad state for ``HEAD~1`` and
    the real diff for the base-branch command, so this asserts the fixed code
    reads the base-branch form and hands the model the real diff.
    """
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="af4", title="Base-branch diff test",
        category="capability", rationale="test", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="better",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.has_changes.return_value = False
    e._vcs = vcs

    real_diff = "diff --git a/f.py b/f.py\n+the real implementation change\n"

    def _sub_side_effect(
        args: list[str], **_: object,
    ) -> subprocess.CompletedProcess[str]:
        if "HEAD~1" in args:
            # The old command on a single-commit branch: non-zero exit, empty
            # stdout — the masked bad state this fix removes.
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="")
        # The base-branch form resolves the full implementation diff.
        return subprocess.CompletedProcess(args, 0, stdout=real_diff, stderr="")

    with patch(
        "maestro.evolution.subprocess.run", side_effect=_sub_side_effect,
    ), patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="tried to fix")

        result = await e.auto_fix(plan, _FAIL_REVIEW, round_n=1, max_rounds=3)

    assert result is False
    prompt = mock_q.call_args.kwargs.get("prompt", "")
    # The model receives the implementation diff, not the masked empty signal
    # nor a genuine-failure marker.
    assert "the real implementation change" in prompt
    assert "(no diff)" not in prompt
    assert "(could not retrieve diff)" not in prompt


@pytest.mark.asyncio
async def test_auto_fix_diff_git_failure_distinct_from_empty(
    mem_fs: dict[str, str],
) -> None:
    """A non-zero ``git diff`` exit is surfaced as ``(could not retrieve
    diff)``, not the ambiguous ``(no diff)``.

    The step-3 tightening: without ``check=True`` a genuine git failure exits
    non-zero with empty stdout and would otherwise fall into the ``(no diff)``
    branch, handing the model a real error disguised as a legitimately empty
    change set. Checking ``returncode`` keeps the two distinguishable.
    """
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="af5", title="Diff git-failure test",
        category="capability", rationale="test", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="better",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )

    vcs = MagicMock()
    vcs.has_changes.return_value = False
    e._vcs = vcs

    # git exits non-zero with empty stdout — a genuine failure, not an
    # empty diff.
    failing_proc = subprocess.CompletedProcess(
        ["git", "diff", "origin/main...HEAD"], 128, stdout="", stderr="fatal",
    )
    with patch(
        "maestro.evolution.subprocess.run", return_value=failing_proc,
    ), patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="tried to fix")

        result = await e.auto_fix(plan, _FAIL_REVIEW, round_n=1, max_rounds=3)

    assert result is False
    prompt = mock_q.call_args.kwargs.get("prompt", "")
    assert "(could not retrieve diff)" in prompt
    assert "(no diff)" not in prompt


def test_amend_commit(mem_fs: dict[str, str]) -> None:
    from maestro.vcs import SubprocessVCS

    vcs = SubprocessVCS(Path("/fake/ws"))
    with patch.object(vcs, "_run") as mock_run:
        vcs.amend_commit()
    mock_run.assert_called_once_with(["git", "commit", "--amend", "--no-edit"])


# ---------------------------------------------------------------------------
# _format_version_tail
# ---------------------------------------------------------------------------


def _make_version_record(**overrides):  # type: ignore[no-untyped-def]
    from maestro.memory import VersionRecord

    defaults = dict(
        version_id="v1",
        plan_id="ep_1",
        plan_title="A plan",
        commit_sha="sha",
        merged_at="2026-01-01T00:00:00+00:00",
        window_tasks_total=4,
        window_tasks_succeeded=3,
        window_tasks_failed=1,
        window_sdk_cost_usd=2.75,
        window_sdk_calls=10,
        window_cache_hit_rate=0.6,
        window_rate_limit_hits=0,
        window_error_count=1,
        quality_gate_pass=True,
        notes=None,
    )
    defaults.update(overrides)
    return VersionRecord(**defaults)


@pytest.mark.asyncio
async def test_create_pr_catches_cancelled_error(mem_fs: dict[str, str]) -> None:
    """CancelledError from notification must not propagate out of create_pr."""
    e, m, _c, _j = _make(mem_fs)

    plan = EvolutionPlan(
        plan_id="pr-cancel-1", title="Cancel test",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    m.save_evolution_plan(plan)

    vcs = MagicMock()
    vcs.porcelain_status.return_value = ""
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/test/repo/pull/1"
    e._vcs = vcs
    e._host = host

    notifier = AsyncMock()
    notifier.notify.side_effect = asyncio.CancelledError()
    e._notifier = notifier

    pr_url = await e.create_pr(plan)

    assert pr_url == "https://github.com/test/repo/pull/1"
    notifier.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_pr_propagates_generic_notification_exception(
    mem_fs: dict[str, str],
) -> None:
    """A generic ``Exception`` from notification crashes ``create_pr`` loud.

    Only ``asyncio.CancelledError`` (the documented SDK-teardown leak)
    is suppressed. Real bugs in the notifier propagate so they surface
    for root-cause analysis instead of being hidden under WARNING logs.
    """
    e, m, _c, _j = _make(mem_fs)

    plan = EvolutionPlan(
        plan_id="pr-raise-1", title="Raise test",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    m.save_evolution_plan(plan)

    vcs = MagicMock()
    vcs.porcelain_status.return_value = ""
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/test/repo/pull/1"
    e._vcs = vcs
    e._host = host

    notifier = AsyncMock()
    notifier.notify.side_effect = RuntimeError("telegram exploded")
    e._notifier = notifier

    with pytest.raises(RuntimeError, match="telegram exploded"):
        await e.create_pr(plan)

    notifier.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_pr_body_includes_acceptance_criteria(
    mem_fs: dict[str, str],
) -> None:
    """When acceptance_criteria is non-empty, the PR body carries an
    ``### Acceptance Criteria`` bullet list so human and maestro_review
    reviewers see the same binding contract the self-review used."""
    e, m, _c, _j = _make(mem_fs)

    plan = EvolutionPlan(
        plan_id="pr-ac-1", title="With criteria",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        acceptance_criteria=["criterion alpha", "criterion beta"],
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    m.save_evolution_plan(plan)

    vcs = MagicMock()
    vcs.porcelain_status.return_value = ""
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/test/repo/pull/1"
    e._vcs = vcs
    e._host = host

    await e.create_pr(plan)

    sent_body = host.create_pr.call_args.args[1]
    assert "### Acceptance Criteria" in sent_body
    assert "- criterion alpha" in sent_body
    assert "- criterion beta" in sent_body


@pytest.mark.asyncio
async def test_create_pr_body_omits_section_for_legacy_plan(
    mem_fs: dict[str, str],
) -> None:
    """Legacy plans (no acceptance_criteria) emit a body with no
    Acceptance Criteria section — the section is opt-in to keep
    pre-migration plans visually clean."""
    e, m, _c, _j = _make(mem_fs)

    plan = EvolutionPlan(
        plan_id="pr-noac-1", title="No criteria",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    m.save_evolution_plan(plan)

    vcs = MagicMock()
    vcs.porcelain_status.return_value = ""
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/test/repo/pull/1"
    e._vcs = vcs
    e._host = host

    await e.create_pr(plan)

    sent_body = host.create_pr.call_args.args[1]
    assert "### Acceptance Criteria" not in sent_body


@pytest.mark.asyncio
async def test_send_pr_reminder_catches_cancelled_error(
    mem_fs: dict[str, str],
) -> None:
    """CancelledError from notification must not propagate out of _send_pr_reminder."""
    e, _m, _c, _j = _make(mem_fs)

    plan = EvolutionPlan(
        plan_id="pr-cancel-2", title="Reminder cancel",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="implemented", pr_url="https://github.com/test/repo/pull/2",
        created_at="2025-01-01T00:00:00Z",
    )

    notifier = AsyncMock()
    notifier.notify.side_effect = asyncio.CancelledError()
    e._notifier = notifier

    # Must complete without raising
    await evolution_tend._send_pr_reminder(e, plan, MagicMock(url="https://github.com/test/repo/pull/2"))

    notifier.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_pr_reminder_propagates_generic_notification_exception(
    mem_fs: dict[str, str],
) -> None:
    """A generic ``Exception`` from the reminder notifier propagates.

    Only ``asyncio.CancelledError`` (the documented SDK-teardown leak)
    is suppressed; everything else is a bug that must surface.
    """
    e, _m, _c, _j = _make(mem_fs)

    plan = EvolutionPlan(
        plan_id="pr-raise-2", title="Reminder raise",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="implemented", pr_url="https://github.com/test/repo/pull/2",
        created_at="2025-01-01T00:00:00Z",
    )

    notifier = AsyncMock()
    notifier.notify.side_effect = RuntimeError("telegram exploded")
    e._notifier = notifier

    with pytest.raises(RuntimeError, match="telegram exploded"):
        await evolution_tend._send_pr_reminder(
            e,
            plan, MagicMock(url="https://github.com/test/repo/pull/2"),
        )

    notifier.notify.assert_awaited_once()


# ---------------------------------------------------------------------------
# Priority taxonomy migration — legacy remap, fallback, signature guards.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("bug_fix", "resilience"),
        ("security", "resilience"),
        ("performance", "capability"),
        ("feature", "capability"),
    ],
)
def test_legacy_priority_remap_at_parse(
    mem_fs: dict[str, str], legacy: str, expected: str,
) -> None:
    """``Memory._parse_plan_line`` lazily remaps deprecated priority values
    so legacy ``evolution_plans.jsonl`` rows hydrate into the new taxonomy
    without a bulk migration."""
    d = Path("/fake/legacy_priority")
    memory = Memory(d)
    raw = {
        "plan_id": "legacy1", "title": "Legacy", "stage": "awakening",
        "category": "capability", "rationale": "r",
        "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "priority": legacy,
        "status": "pending_review", "created_at": "2025-01-01T00:00:00Z",
    }
    mem_fs[str(memory._plans_path)] = json.dumps(raw) + "\n"
    plan = memory.get_plan("legacy1")
    assert plan is not None
    assert plan.priority == expected


@pytest.mark.asyncio
async def test_propose_plan_unknown_priority_falls_back_to_capability(
    mem_fs: dict[str, str],
) -> None:
    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "garbage",
        "rationale": "Test", "proposed_changes": [],
        "files_affected": [], "expected_impact": "",
    })
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)
    assert plan.priority == "capability"


# ---------------------------------------------------------------------------
# validate_success_criteria — vacuous-criteria filter
# ---------------------------------------------------------------------------


def test_validate_success_criteria_filters_vacuous() -> None:
    """Length-gate, filler-only, and generic-noun criteria are all rejected."""
    criteria = [
        # Too short (< 25 chars)
        "improve performance",
        # Filler-dominated (> 40% filler words)
        "it should be properly handled and working well with the system",
        # Generic-noun-only non-filler tokens
        "the code and the tests and the system should handle the errors",
        # Legitimate — survives all three checks
        "task_cost_gate rule 3 counts both `failure` and `partial` outcomes in the failure-cost numerator",
    ]

    result = EvolutionEngine.validate_success_criteria(criteria)

    assert len(result) == 1
    assert "task_cost_gate" in result[0]


def test_validate_success_criteria_keeps_specific() -> None:
    """Concrete, testable criteria survive the filter."""
    criteria = [
        "run_quality_gates returns non-zero exit code when ruff reports E302",
        "BM25 scorer ranks exact-match documents above partial-match by 2x",
        "_CRITERIA_MIN_CHARS constant equals 25 in evolution.py",
    ]

    result = EvolutionEngine.validate_success_criteria(criteria)

    assert result == criteria


def test_validate_success_criteria_empty_input() -> None:
    """Empty list in, empty list out."""
    assert EvolutionEngine.validate_success_criteria([]) == []


def test_validate_success_criteria_whitespace_only() -> None:
    """Whitespace-only strings are dropped."""
    assert EvolutionEngine.validate_success_criteria(["   ", "\t", "\n"]) == []


def test_validate_success_criteria_is_public_api() -> None:
    """The criterion validator is a public staticmethod (no leading underscore).

    Both ``EvolutionEngine.propose_plan`` and ``Agent.generate_task`` consume
    this helper, so the API surface must be honest about cross-module reuse.
    """
    import inspect

    # The public name resolves to a callable…
    assert callable(EvolutionEngine.validate_success_criteria)
    # …and the leading-underscore alias is gone — would-be private callers
    # are forced through the public surface.
    assert not hasattr(EvolutionEngine, "_validate_success_criteria")
    # And it is a staticmethod, not bound to an instance.
    assert isinstance(
        inspect.getattr_static(EvolutionEngine, "validate_success_criteria"),
        staticmethod,
    )


@pytest.mark.asyncio
async def test_propose_plan_filters_vacuous_criteria(
    mem_fs: dict[str, str],
) -> None:
    """propose_plan wires validate_success_criteria so vacuous entries
    are stripped before the EvolutionPlan is constructed."""
    e, _m, _c, _j = _make(mem_fs)
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": [
            "improve performance",  # too short
            "task_cost_gate rule 3 counts both failure and partial outcomes in the numerator",
        ],
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert len(plan.acceptance_criteria) == 1
    assert "task_cost_gate" in plan.acceptance_criteria[0]


@pytest.mark.asyncio
async def test_propose_plan_filter_runs_before_item_cap(
    mem_fs: dict[str, str],
) -> None:
    """Regression for #1955: when the LLM reply over-produces beyond the
    5-item cap with vacuous entries at the head, the vacuous filter must
    run *before* the cap so specific criteria at the tail are not silently
    dropped in favour of the head vacuous entries."""
    e, _m, _c, _j = _make(mem_fs)
    vacuous = [
        "improve performance",
        "make it better",
        "the system works",
    ]
    specific = [
        "maestro/evolution.py propose_plan caps acceptance_criteria at 10 entries via ACCEPTANCE_CRITERIA_MAX_ITEMS",
        "maestro/agent.py generate_task populates QueuedTask.acceptance_criteria with validated items in lockstep with propose_plan",
        "tests/test_evolution.py asserts validate_success_criteria runs before the truncation slice on the propose_plan path",
        "tests/test_agent_lifecycle.py asserts validate_success_criteria runs before the truncation slice on the generate_task path",
        "tests/test_vacuous_criteria.py continues to pass without modification since validate_success_criteria itself is unchanged",
    ]
    plan_json = json.dumps({
        "title": "Test", "category": "capability", "priority": "capability",
        "rationale": "test", "proposed_changes": ["change"],
        "files_affected": [], "expected_impact": "",
        "acceptance_criteria": vacuous + specific,
    })

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=plan_json)
        plan = await e.propose_plan(gap=GAP)

    assert len(plan.acceptance_criteria) == len(specific)
    for entry in specific:
        assert entry in plan.acceptance_criteria


# ---------------------------------------------------------------------------
# curate_pending_plans — proposal 060 step 3
# ---------------------------------------------------------------------------


def _make_curate_engine(
    mem_fs: dict[str, str],
) -> tuple[EvolutionEngine, Memory, MagicMock]:
    """Engine + memory + a notifier whose ``notify_plan_refined`` is spyable."""
    d = Path("/fake/curate")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    notifier = MagicMock()
    notifier.notify_plan_refined = AsyncMock()
    notifier.notify = AsyncMock()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(m, c, j, notifier, issues=issues, config=Config())
    return e, m, notifier


def _seed_pending_plan(m: Memory, plan_id: str = "cp1", **over: object) -> EvolutionPlan:
    plan = EvolutionPlan(
        plan_id=plan_id,
        title=str(over.get("title", "Cache prompt fragments")),
        category="capability",
        rationale=str(over.get("rationale", "Repeated fragments recompute every call.")),
        proposed_changes=list(
            over.get("proposed_changes", ["Hash fragments", "Reuse on hit"]),  # type: ignore[arg-type]
        ),
        files_affected=[],
        expected_impact="",
        acceptance_criteria=list(
            over.get("acceptance_criteria", ["A test asserts the cache hit path"]),  # type: ignore[arg-type]
        ),
        created_at=str(over.get("created_at", "2026-05-01T00:00:00+00:00")),
    )
    m.save_evolution_plan(plan)
    return plan


# ---------------------------------------------------------------------------
# Notify-time content-hash snapshot (proposal 021)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_for_review_stamps_notified_hash(
    mem_fs: dict[str, str],
) -> None:
    """submit_for_review snapshots the content hash the reviewer will see."""
    e, m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="pnh1", title="Notify Hash",
        category="capability", rationale="Test", proposed_changes=["Change X"],
        files_affected=[], expected_impact="Improve",
        created_at="2025-01-01T00:00:00Z",
    )

    await e.submit_for_review(plan)

    saved = m.get_plan("pnh1")
    assert saved is not None
    assert saved.plan_content_hash != ""
    assert saved.notified_content_hash == saved.plan_content_hash


@pytest.mark.asyncio
async def test_curate_refine_restamps_notified_hash(
    mem_fs: dict[str, str],
) -> None:
    """A refine re-notification snapshots the *post-refine* content hash so
    Telegram approval of the refreshed plan is not refused as drift."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    before = m.get_plan("cp1")
    assert before is not None
    verdict = json.dumps({"action": "refine", "rationale": "Sharper rationale."})

    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ):
        result = await e.curate_pending_plans()

    assert result is True
    updated = m.get_plan("cp1")
    assert updated is not None
    assert updated.plan_content_hash != before.plan_content_hash
    assert updated.notified_content_hash == updated.plan_content_hash
    notifier.notify_plan_refined.assert_awaited_once()
    _plan_id, msg, content_hash = notifier.notify_plan_refined.await_args.args
    # The buttons bind to the refined snapshot, and the message renders
    # that refined body rather than just the changed field names (#4146).
    assert content_hash == updated.plan_content_hash
    assert msg.startswith("Refined — updated fields: rationale")
    assert f"Refined Evolution Plan {format_plan_ref(updated)}:" in msg
    assert parse_plan_ref(msg) == PlanRef(
        "cp1", updated.plan_content_hash[:CALLBACK_HASH_PREFIX_LEN],
    )
    assert "Sharper rationale." in msg


@pytest.mark.asyncio
async def test_curate_refine_skips_notified_stamp_when_plan_vanishes(
    mem_fs: dict[str, str],
) -> None:
    """If the refined plan cannot be re-read, both the notified-hash stamp
    and the refined notification are skipped — buttons with no snapshot
    would approve unseen content (#4146)."""
    e, m, notifier = _make_curate_engine(mem_fs)
    _seed_pending_plan(m)
    verdict = json.dumps({"action": "refine", "rationale": "Sharper rationale."})

    with patch(
        "maestro.evolution.query",
        AsyncMock(return_value=MockQueryResult(response=verdict)),
    ), patch.object(e._memory, "get_plan", return_value=None):
        result = await e.curate_pending_plans()

    assert result is True
    notifier.notify_plan_refined.assert_not_awaited()
    # The stamp was skipped — the persisted row keeps an empty snapshot.
    refreshed = m.get_plan("cp1")
    assert refreshed is not None
    assert refreshed.notified_content_hash == ""


def _interleave_decision(
    m: Memory, method: str, plan_id: str, decision: PlanStatus,
    *, before: bool,
) -> AbstractContextManager[object]:
    """Patch ``m.<method>`` so a human decision commits around the real call.

    ``before=True`` lands the decision before the real call runs, else after
    it — modelling a dashboard/Telegram decision racing the curator (#4473).
    """
    real = getattr(m, method)
    decide = m.update_plan_status

    def _side_effect(*args: object, **kwargs: object) -> object:
        if before:
            decide(plan_id, decision, expect_status="pending_review")
            return real(*args, **kwargs)
        result = real(*args, **kwargs)
        decide(plan_id, decision, expect_status="pending_review")
        return result

    return patch.object(m, method, side_effect=_side_effect)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected"])
@pytest.mark.parametrize(
    ("method", "before"), [("get_plan", False), ("refine_plan", False)],
)
async def test_curate_refine_never_reverts_interleaved_decision(
    mem_fs: dict[str, str], decision: PlanStatus, method: str, before: bool,
) -> None:
    """A human decision landing between the refine and the notified-hash
    stamp survives: the stamp is refused and neither the refined-plan
    controls nor the feedback notice are sent (#4473)."""
    e, m, notifier = _make_curate_engine(mem_fs)
    target = _seed_pending_plan(m)

    with _interleave_decision(m, method, "cp1", decision, before=before):
        result = await e._apply_curate_refine(
            target, {"rationale": "Sharper rationale."},
            response_notice="Thanks for the feedback.",
        )

    assert result is True
    persisted = m.get_plan("cp1")
    assert persisted is not None
    assert persisted.status == decision
    assert persisted.notified_content_hash == ""
    notifier.notify_plan_refined.assert_not_awaited()
    notifier.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_curate_refine_refuses_stamp_on_content_hash_drift(
    mem_fs: dict[str, str],
) -> None:
    """A second refine after the snapshot re-read moves the content hash, so
    the stamp of the stale snapshot is refused and no buttons are sent."""
    e, m, notifier = _make_curate_engine(mem_fs)
    target = _seed_pending_plan(m)
    real_get = m.get_plan

    def _stale_snapshot(plan_id: str) -> EvolutionPlan | None:
        snapshot = real_get(plan_id)
        m.refine_plan(plan_id, rationale="A later, different rationale.")
        return snapshot

    with patch.object(m, "get_plan", side_effect=_stale_snapshot):
        result = await e._apply_curate_refine(
            target, {"rationale": "Sharper rationale."},
        )

    assert result is True
    persisted = m.get_plan("cp1")
    assert persisted is not None
    assert persisted.status == "pending_review"
    assert persisted.notified_content_hash == ""
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_for_review_never_reverts_interleaved_decision(
    mem_fs: dict[str, str],
) -> None:
    """A decision landing before submit_for_review's stamp is kept and no
    review notification is sent (#4473)."""
    e, m, _c, _j = _make(mem_fs)
    e._notifier.notify_plan_for_review = AsyncMock()  # type: ignore[method-assign]
    plan = EvolutionPlan(
        plan_id="pnh2", title="Notify Hash Race",
        category="capability", rationale="Test", proposed_changes=["Change X"],
        files_affected=[], expected_impact="Improve",
        created_at="2025-01-01T00:00:00Z",
    )

    with _interleave_decision(m, "update_plan_status", "pnh2", "approved", before=True):
        await e.submit_for_review(plan)

    saved = m.get_plan("pnh2")
    assert saved is not None
    assert saved.status == "approved"
    assert saved.notified_content_hash == ""
    e._notifier.notify_plan_for_review.assert_not_awaited()


# ---------------------------------------------------------------------------
# #4535: StructuredOutput exhaustion routes into ProposalParseError
# ---------------------------------------------------------------------------


async def _call_planner(e: EvolutionEngine, method: str) -> object:
    if method == "propose_plan":
        return await e.propose_plan(gap=GAP)
    return await e.propose_plan_chain("reflection")


@pytest.mark.asyncio
@pytest.mark.parametrize("single_use", [True, False])
@pytest.mark.parametrize("method", ["propose_plan", "propose_plan_chain"])
async def test_structured_output_exhaustion_raises_parse_error(
    mem_fs: dict[str, str], method: str, single_use: bool,
) -> None:
    """A schema-exhausted call surfaces as the caller's unparseable-reply
    contract, chaining the typed ``SDKError``, on both call arms."""
    e = _engine_with(Config(sdk_single_use_evolution_calls_enabled=single_use))
    err = SDKError(
        "schema exhausted",
        category=ErrorCategory.STRUCTURED_OUTPUT_INVALID,
        caller=method,
    )
    target = "single_use_call" if single_use else "query"
    with patch(
        f"maestro.evolution.{target}", new_callable=AsyncMock, side_effect=err,
    ), pytest.raises(ProposalParseError) as ei:
        await _call_planner(e, method)
    assert ei.value.__cause__ is err
    assert "structured output rejected" in str(ei.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["propose_plan", "propose_plan_chain"])
async def test_other_sdk_error_still_propagates(
    mem_fs: dict[str, str], method: str,
) -> None:
    """Any other category is not a parse failure: it re-raises unchanged."""
    e = _engine_with(Config(sdk_single_use_evolution_calls_enabled=True))
    err = SDKError("boom", category=ErrorCategory.UNKNOWN, caller=method)
    with patch(
        "maestro.evolution.single_use_call",
        new_callable=AsyncMock, side_effect=err,
    ), pytest.raises(SDKError) as ei:
        await _call_planner(e, method)
    assert ei.value is err


# ---------------------------------------------------------------------------
# Proposal 150: grounded re-read re-prompt for unread files_affected targets
# ---------------------------------------------------------------------------


def test_unread_affected_paths_returns_unread_existing_py(
    mem_fs: dict[str, str],
) -> None:
    """Returns an existing ``.py`` the receipt never read; excludes a read one."""
    mem_fs["maestro/foo.py"] = "x = 1\n"
    mem_fs["maestro/bar.py"] = "y = 2\n"
    receipt = tree_retrieval.TreeReadReceipt()
    receipt.entries.append(("read_source", "maestro/bar.py", True))
    out = unread_affected_paths(
        ["maestro/foo.py", "maestro/bar.py"], receipt, root=Path("."),
    )
    assert out == ["maestro/foo.py"]


def test_unread_affected_paths_grep_find_do_not_mark_read(
    mem_fs: dict[str, str],
) -> None:
    """A ``grep_source`` / ``find_definition`` hit never marks a path read."""
    mem_fs["maestro/foo.py"] = "x = 1\n"
    receipt = tree_retrieval.TreeReadReceipt()
    receipt.entries.append(("grep_source", "pattern", True))
    receipt.entries.append(("find_definition", "symbol", True))
    out = unread_affected_paths(["maestro/foo.py"], receipt, root=Path("."))
    assert out == ["maestro/foo.py"]


def test_unread_affected_paths_excludes_nonexistent_and_non_py(
    mem_fs: dict[str, str],
) -> None:
    """A nonexistent ``.py`` and any non-``.py`` entry are never returned."""
    mem_fs["maestro/foo.py"] = "x = 1\n"
    receipt = tree_retrieval.TreeReadReceipt()
    out = unread_affected_paths(
        ["maestro/ghost.py", "docs/notes.md", "maestro/foo.py"], receipt,
        root=Path("."),
    )
    assert out == ["maestro/foo.py"]


def test_unread_affected_paths_roots_existence_at_work_target(
    mem_fs: dict[str, str],
) -> None:
    """Thread 4232062768: the existence check must be rooted at the work-target
    ``repo_workspace``, not the process CWD.

    A ``files_affected`` entry (``maestro/foo.py``) exists on disk *only* under
    a ``[work]`` redirect root (``/work-tree``), not relative to CWD. A check
    rooted at ``repo_workspace`` would find it and return it as unread; a
    CWD-relative ``Path(normalised).exists()`` misses it and returns ``[]``.
    """
    work_root = "/work-tree"
    # Present under the work tree only; absent at the bare CWD-relative key.
    mem_fs[f"{work_root}/maestro/foo.py"] = "x = 1\n"
    receipt = tree_retrieval.TreeReadReceipt()
    out = unread_affected_paths(
        ["maestro/foo.py"], receipt, root=Path(work_root),
    )
    assert out == ["maestro/foo.py"]


_GROUNDED_REPO = Path("/repo-ground")

_REREAD_PLAN_JSON = json.dumps({
    "title": "One focused change",
    "category": "capability",
    "priority": "capability",
    "rationale": "Fits one PR",
    "proposed_changes": ["Edit maestro/foo.py and add a test to verify it"],
    "acceptance_criteria": ["foo() returns 1"],
    "files_affected": ["maestro/foo.py"],
    "expected_impact": "Forward",
})


def _grounded_engine(
    *, single_use: bool = True, grounded: bool = True,
    tracked: frozenset[str] = frozenset({"maestro/foo.py"}),
) -> EvolutionEngine:
    e = _engine_with(Config(
        sdk_single_use_evolution_calls_enabled=single_use,
        grounded_planning_enabled=grounded,
        workspace=_GROUNDED_REPO,
    ))
    e._vcs = MagicMock()
    e._vcs.tracked_paths.return_value = tracked
    return e


@contextmanager
def _reread_seam(
    seeded: list[tuple[str, str, bool]], *, response: str = _REREAD_PLAN_JSON,
) -> Iterator[AsyncMock]:
    """Patch ``single_use_call`` and seed the tree receipt with *seeded* reads."""
    receipt = tree_retrieval.TreeReadReceipt()
    receipt.entries.extend(seeded)
    real_build = tree_retrieval.build_bounded_tree_tool_set

    def _spy(view: tree_retrieval.TreeView, **kwargs: object) -> object:
        registry, _ = real_build(view, **kwargs)  # type: ignore[arg-type]
        return registry, receipt

    call = AsyncMock(return_value=MockQueryResult(response=response))
    with patch("maestro.evolution.single_use_call", call), patch.object(
        tree_retrieval, "build_bounded_tree_tool_set", _spy,
    ):
        yield call


@pytest.mark.asyncio
async def test_reread_fires_once_for_unread_cited_py(mem_fs: dict[str, str]) -> None:
    """Draft citing an unread existing ``.py`` triggers exactly one extra call."""
    mem_fs[str(_GROUNDED_REPO / "maestro/foo.py")] = "def foo():\n    return 1\n"
    e = _grounded_engine()
    with _reread_seam([]) as call:
        plan = await e.propose_plan(gap=GAP, tree_reads=True)
    assert call.await_count == 2
    second_prompt = call.await_args_list[1].args[1]
    assert "READ BEFORE YOU RE-EMIT" in second_prompt
    assert "maestro/foo.py" in second_prompt
    assert plan.grounded_reads == []


@pytest.mark.asyncio
async def test_reread_skipped_when_all_cited_paths_read(
    mem_fs: dict[str, str],
) -> None:
    """A draft whose cited paths are all already read triggers no extra call."""
    mem_fs[str(_GROUNDED_REPO / "maestro/foo.py")] = "def foo():\n    return 1\n"
    e = _grounded_engine()
    with _reread_seam([("read_source", "maestro/foo.py", True)]) as call:
        await e.propose_plan(gap=GAP, tree_reads=True)
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_reread_skipped_when_cited_paths_nonexistent(
    mem_fs: dict[str, str],
) -> None:
    """A draft citing only nonexistent ``.py`` paths triggers no extra call."""
    response = json.dumps({
        "title": "One focused change",
        "category": "capability",
        "priority": "capability",
        "rationale": "Fits one PR",
        "proposed_changes": ["Edit maestro/ghost.py and verify it"],
        "acceptance_criteria": ["ghost works"],
        "files_affected": ["maestro/ghost.py"],
        "expected_impact": "Forward",
    })
    e = _grounded_engine()
    with _reread_seam([], response=response) as call:
        await e.propose_plan(gap=GAP, tree_reads=True)
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_reread_skipped_when_tree_read_none(mem_fs: dict[str, str]) -> None:
    """Flag-off (``tree_read`` None) makes no extra call on the single-use arm."""
    mem_fs["maestro/foo.py"] = "def foo():\n    return 1\n"
    e = _grounded_engine(grounded=False)
    with _reread_seam([]) as call:
        plan = await e.propose_plan(gap=GAP, tree_reads=False)
    assert call.await_count == 1
    assert plan.grounded_reads is None


@pytest.mark.asyncio
async def test_reread_skipped_on_session_branch(mem_fs: dict[str, str]) -> None:
    """On the session-attached branch (single-use off) no extra call fires."""
    mem_fs["maestro/foo.py"] = "def foo():\n    return 1\n"
    e = _grounded_engine(single_use=False, grounded=False)
    with patch("maestro.evolution.single_use_call", new_callable=AsyncMock) as call, \
            patch(
                "maestro.evolution.query_on_session",
                new_callable=AsyncMock,
                return_value=MockQueryResult(response=_REREAD_PLAN_JSON),
            ):
        plan = await e.propose_plan(
            gap=GAP, session=MagicMock(name="slot"), tree_reads=True,
        )
    assert call.await_count == 0
    assert plan.grounded_reads is None


def test_unread_affected_paths_skips_non_str_entry(
    mem_fs: dict[str, str],
) -> None:
    """A non-``str`` ``files_affected`` entry is skipped (``continue``)."""
    mem_fs["maestro/foo.py"] = "x = 1\n"
    receipt = tree_retrieval.TreeReadReceipt()
    # A non-``str`` entry (``123``) exercises the ``isinstance`` guard's skip.
    entries = [123, "maestro/foo.py"]
    out = unread_affected_paths(entries, receipt, root=Path("."))  # type: ignore[list-item]
    assert out == ["maestro/foo.py"]


@pytest.mark.asyncio
async def test_reread_draft_invalid_json_sets_draft_none(
    mem_fs: dict[str, str],
) -> None:
    """An unparseable draft on the grounded arm leaves ``draft`` None, so no
    re-read fires and the unparseable reply surfaces as ``ProposalParseError``."""
    e = _grounded_engine()
    with _reread_seam([], response="not valid json at all") as call, \
            pytest.raises(ProposalParseError):
        await e.propose_plan(gap=GAP, tree_reads=True)
    assert call.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ErrorCategory.STRUCTURED_OUTPUT_INVALID, ProposalParseError),
        (ErrorCategory.UNKNOWN, SDKError),
    ],
)
async def test_reread_second_call_sdk_error(
    mem_fs: dict[str, str],
    category: ErrorCategory,
    expected: type[Exception],
) -> None:
    """An ``SDKError`` on the re-read call routes a schema exhaustion into
    ``ProposalParseError`` and re-raises any other category unchanged."""
    mem_fs[str(_GROUNDED_REPO / "maestro/foo.py")] = "def foo():\n    return 1\n"
    e = _grounded_engine()
    err = SDKError("boom", category=category, caller="propose_plan")
    with _reread_seam([]) as call:
        call.side_effect = [MockQueryResult(response=_REREAD_PLAN_JSON), err]
        with pytest.raises(expected):
            await e.propose_plan(gap=GAP, tree_reads=True)
    assert call.await_count == 2
