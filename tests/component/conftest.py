"""Component integration test fixtures.

Component tests exercise full multi-module flows with external services
mocked at the boundary:

- **Claude Agent SDK** — ``SDKMock`` (prompt-routed Python-level mock)
- **GitHub / git remote ops** — ``MockCodeHost`` + ``MockVCS`` (protocol mocks)
- **Telegram** — ``respx`` (HTTP-level mock)
- **Quality gate tools** — ``make_quality_gates_mock`` (subprocess mock)

No Docker is required.  Local git operations use real git in isolated
``tmp_path`` repositories.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from maestro import config_settings
from maestro.agent import MaestroAgent
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.contributions import ContributionTracker
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import Memory
from maestro.notifications.base import Notifier, NullNotifier
from maestro.planner import Planner
from tests.component.mock_vcs import MockCodeHost, MockVCS
from tests.component.sdk_mock import SDKMock

# Project root where maestro.toml lives
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    component_dir = os.path.dirname(__file__)
    for item in items:
        if str(item.fspath).startswith(component_dir):
            item.add_marker(pytest.mark.component)
            item.add_marker(pytest.mark.timeout(15))


@pytest.fixture(autouse=True)
def _fake_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "maestro"
    d.mkdir()
    return d


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    d = tmp_path / "workspace"
    d.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(d),
            check=True, capture_output=True, text=True,
        )

    _git("init", "--initial-branch=main")
    _git("config", "user.email", "test@test.com")
    _git("config", "user.name", "Test")
    _git("config", "commit.gpgsign", "false")
    (d / ".gitkeep").touch()
    _git("add", ".")
    _git("commit", "-m", "init")
    return d


def build_base_config() -> Config:
    """Load the component suite's base ``Config`` — no ``MAESTRO_ENV`` overlay.

    Passing ``env_name=None`` to the sole TOML parser bypasses the ambient
    ``MAESTRO_ENV`` overlay read that ``Config.from_env`` performs, so component
    behavior is env-independent by construction rather than by a
    hand-maintained per-flag pin list (#3460).
    """
    return config_settings.build_config_authoritative(CONFIG_DIR, None)


def apply_test_overlay(
    cfg: Config, memory_dir: Path, workspace_dir: Path,
) -> Config:
    """Overlay per-test paths and credentials onto a loaded ``Config``.

    Sole owner of the component suite's overlay set, so a posture-specific
    fixture (e.g. the production-overlay ``Config``) differs from the base
    ``config`` fixture only by its TOML overlay, never by a hand-copied and
    since-diverged list of ``replace()`` fields.
    """
    cfg = replace(
        cfg,
        memory_dir=memory_dir,
        workspace=workspace_dir,
        github_repo="test-owner/test-repo",
        telegram_bot_token="test-bot-token",
        telegram_chat_id="12345",
        enable_session_reuse=False,
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def config(memory_dir: Path, workspace_dir: Path) -> Config:
    """Load base config, then overlay per-test paths and credentials."""
    return apply_test_overlay(build_base_config(), memory_dir, workspace_dir)


@pytest.fixture
def code_host() -> MockCodeHost:
    """Fresh in-memory CodeHost mock."""
    return MockCodeHost()


@pytest.fixture
def vcs(workspace_dir: Path) -> MockVCS:
    """MockVCS backed by a real git repo in tmp_path."""
    return MockVCS(workspace=workspace_dir)


@pytest.fixture
def sdk() -> SDKMock:
    """Fresh SDKMock instance."""
    return SDKMock()


@pytest.fixture
def make_agent(
    config: Config, memory_dir: Path, workspace_dir: Path,
    code_host: MockCodeHost, vcs: MockVCS,
) -> Any:
    """Factory that builds a ``MaestroAgent`` wired to mock VCS/CodeHost.

    Returns ``(agent, code_host, vcs)`` so tests can configure and assert
    on the mocks.

    All calls to the returned factory within one test share the same
    fixture-scoped ``MockCodeHost`` and ``MockVCS`` instances.  Do not
    call the factory twice expecting independent mock state — pushes,
    created PRs, and issues recorded by the first agent will be visible
    to assertions about the second.

    Pass ``config_override`` to wire a non-default ``Config`` (e.g. the
    shipped production posture) into both ``EvolutionEngine`` and
    ``MaestroAgent``; omitted, the base-posture ``config`` fixture is used.
    """
    _called = False

    def _factory(
        notifier: Notifier | None = None,
        config_override: Config | None = None,
    ) -> tuple[MaestroAgent, MockCodeHost, MockVCS]:
        nonlocal _called
        if _called:
            raise RuntimeError(
                "make_agent() called twice in one test — MockCodeHost and "
                "MockVCS state is shared. Create a separate fixture if you "
                "need independent mock state."
            )
        _called = True
        cfg = config if config_override is None else config_override
        ch = code_host
        v = vcs
        memory = Memory(memory_dir)
        consciousness = Consciousness(memory_dir)
        journal = Journal(memory_dir)
        _notifier = notifier or NullNotifier()
        executor = Executor()
        planner = Planner()
        issues = IssueTracker(
            github_repo="test-owner/test-repo", code_host=ch,
        )
        evolution = EvolutionEngine(
            memory, consciousness, journal, _notifier,
            issues=issues, github_repo="test-owner/test-repo",
            config=cfg, vcs=v, code_host=ch,
        )
        contributions = ContributionTracker(
            workspace_dir, memory_dir, vcs=v,
        )
        agent = MaestroAgent(
            config=cfg, consciousness=consciousness, journal=journal,
            memory=memory, executor=executor, planner=planner,
            evolution=evolution, notifier=_notifier,
            contributions=contributions,
        )
        agent._probe_rate_limit = AsyncMock()  # type: ignore[method-assign]
        return agent, ch, v
    return _factory


@pytest.fixture
def telegram_mock() -> Any:
    """``respx``-based mock for the Telegram Bot API (httpx-level)."""
    with respx.mock:
        respx.post(re.compile(r".*/bot.*/getMe")).mock(
            return_value=httpx.Response(200, json={
                "ok": True,
                "result": {
                    "id": 123456789, "is_bot": True,
                    "first_name": "MockBot", "username": "mock_bot",
                },
            }),
        )
        send_route = respx.post(re.compile(r".*/bot.*/sendMessage")).mock(
            return_value=httpx.Response(200, json={
                "ok": True,
                "result": {
                    "message_id": 1,
                    "chat": {"id": "12345", "type": "private"},
                    "text": "", "date": 1700000000,
                },
            }),
        )
        # Catch-all for any other Telegram Bot API endpoints (e.g. future
        # library calls to getWebhookInfo, deleteWebhook, close, etc.)
        respx.route(url__regex=r".*/bot.*").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True}),
        )
        yield send_route


def make_quality_gates_mock(
    ruff_ok: bool = True, mypy_ok: bool = True, pytest_ok: bool = True,
) -> Any:
    """Mock for quality gate tools only (ruff, mypy, pytest).

    All other subprocess calls pass through to the real binary.
    """
    _real_run = subprocess.run

    def _side_effect(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if not isinstance(cmd, list):
            return _real_run(cmd, **kwargs)
        prog = cmd[0] if cmd else ""
        if prog == "ruff":
            return subprocess.CompletedProcess(cmd, 0 if ruff_ok else 1, stdout="", stderr="")
        if prog == "mypy":
            return subprocess.CompletedProcess(cmd, 0 if mypy_ok else 1, stdout="", stderr="")
        if prog == "pytest":
            return subprocess.CompletedProcess(cmd, 0 if pytest_ok else 1, stdout="", stderr="")
        return _real_run(cmd, **kwargs)

    return _side_effect


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def write_self_model(memory_dir: Path, **overrides: Any) -> None:
    model = {
        "identity_statements": [],
        "capabilities_known": [], "limitations_known": [],
        "values": [], "open_questions": ["What am I?"],
        "evolution_count": 0,
        "awakened_at": "2025-01-01T00:00:00+00:00",
    }
    model.update(overrides)
    (memory_dir / "self_model.json").write_text(json.dumps(model, indent=2))


def write_task_record(memory_dir: Path, **overrides: Any) -> None:
    record = {
        "task_id": "t001", "goal": "test goal", "plan": [],
        "outcome": "success", "result_summary": "done", "errors": [],
        "duration_secs": 1.0, "timestamp": "2025-01-01T00:00:00+00:00",
    }
    record.update(overrides)
    with open(memory_dir / "task_history.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


def write_evolution_plan(memory_dir: Path, **overrides: Any) -> None:
    plan = {
        "plan_id": "p001", "title": "Test plan", "stage": "awakening",
        "category": "capability", "rationale": "test",
        "proposed_changes": ["change 1"], "files_affected": ["test.py"],
        "expected_impact": "test impact", "priority": "capability",
        "issue_number": None, "status": "pending_review",
        "created_at": "2025-01-01T00:00:00+00:00", "reviewed_at": None,
        "rejection_reason": None, "pr_url": None,
    }
    plan.update(overrides)
    with open(memory_dir / "evolution_plans.jsonl", "a") as f:
        f.write(json.dumps(plan) + "\n")
