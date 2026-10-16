"""Repo clients build through the WorkTarget owner (proposal 107, increment 2).

The loop-facing git/GitHub client *construction seams* now read
``config.work_target()`` instead of the raw ``config.workspace`` /
``config.github_repo`` scalars. Because both facets default to the self
values, a default ``Config`` builds byte-identical clients; a ``[work]``
redirect points them at another checkout/repo. These tests prove both, and
that an injected client still wins over construction.

All externals stay mocked and file I/O uses ``mem_fs`` — the client
constructors themselves do no I/O (they only store the workspace/repo), so
the seams are exercised without touching git, ``gh``, or the network.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from maestro.agent import MaestroAgent
from maestro.cli import _build_agent, _build_mcp_serve_registry, cmd_issues
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner
from maestro.vcs import SubprocessCodeHost, SubprocessVCS, VCSClient
from tests.component.mock_vcs import MockCodeHost, MockVCS
from tests.test_cli import _make_config

_EXT_WORKSPACE = "/ext/checkout"
_EXT_REPO = "ext-owner/ext-repo"
_FALLBACK_DIR = Path("/fake/wt_agent")


def _redirect_config(**overrides: object) -> Config:
    """A ``Config`` whose ``[work]`` facets point at an external target."""
    return _make_config(
        github_repo="self-owner/self-repo",
        work_repo_workspace=_EXT_WORKSPACE,
        work_github_repo=_EXT_REPO,
        **overrides,
    )


# ── cli._build_agent (step 1) ───────────────────────────────────────


def test_build_agent_clients_default_to_self_target(mem_fs: dict[str, str]) -> None:
    """With no ``[work]`` override the built clients read the self scalars."""
    config = _make_config(github_repo="self-owner/self-repo")
    agent = _build_agent(config)

    assert isinstance(agent._vcs, SubprocessVCS)
    assert agent._vcs._cwd == str(config.workspace)
    assert agent.issues._repo == config.github_repo
    assert isinstance(agent.evolution._host, SubprocessCodeHost)
    assert agent.evolution._host._repo == config.github_repo
    assert agent.evolution._host._cwd == str(config.workspace)


def test_build_agent_clients_follow_work_redirect(mem_fs: dict[str, str]) -> None:
    """A ``[work]`` redirect points every built client at the target repo."""
    config = _redirect_config()
    agent = _build_agent(config)

    assert isinstance(agent._vcs, SubprocessVCS)
    assert agent._vcs._cwd == _EXT_WORKSPACE
    assert agent.issues._repo == _EXT_REPO
    assert isinstance(agent.evolution._host, SubprocessCodeHost)
    assert agent.evolution._host._repo == _EXT_REPO
    assert agent.evolution._host._cwd == _EXT_WORKSPACE


# ── cli._build_mcp_serve_registry (step 2) ──────────────────────────


def test_mcp_serve_registry_clients_default_to_self_target(
    mem_fs: dict[str, str],
) -> None:
    """The mcp-serve sidecar builds its clients from the self scalars by default."""
    config = _make_config(github_repo="self-owner/self-repo")
    with patch("maestro.cli.SubprocessCodeHost") as mock_ch, \
            patch("maestro.cli.IssueTracker") as mock_it:
        _build_mcp_serve_registry(config)

    assert mock_ch.call_args.args[0] == config.github_repo
    assert mock_ch.call_args.kwargs["workspace"] == config.workspace
    assert mock_it.call_args.args[0] == config.github_repo


def test_mcp_serve_registry_clients_follow_work_redirect(
    mem_fs: dict[str, str],
) -> None:
    """A ``[work]`` redirect flows into the mcp-serve sidecar's clients."""
    config = _redirect_config()
    wt = config.work_target()
    with patch("maestro.cli.SubprocessCodeHost") as mock_ch, \
            patch("maestro.cli.IssueTracker") as mock_it:
        _build_mcp_serve_registry(config)

    assert mock_ch.call_args.args[0] == _EXT_REPO
    assert mock_ch.call_args.kwargs["workspace"] == wt.repo_workspace
    assert mock_it.call_args.args[0] == _EXT_REPO


# ── cli.cmd_issues (step 2) ─────────────────────────────────────────


def test_cmd_issues_default_uses_self_repo(mem_fs: dict[str, str]) -> None:
    """``cmd_issues`` builds its tracker from the self repo by default."""
    config = _make_config(github_repo="self-owner/self-repo")
    args = argparse.Namespace()
    with patch("maestro.cli.IssueTracker") as mock_it:
        mock_it.return_value.list_open.return_value = []
        cmd_issues(args, config)

    assert mock_it.call_args.args[0] == "self-owner/self-repo"


def test_cmd_issues_follows_work_redirect(mem_fs: dict[str, str]) -> None:
    """With only ``[work] github_repo`` set, the guard and build read it.

    The self ``github_repo`` is empty, so a guard still reading the self scalar
    would spuriously bail; reading ``wt.github_repo`` lists the target repo's
    issues instead.
    """
    config = _make_config(github_repo=None, work_github_repo=_EXT_REPO)
    args = argparse.Namespace()
    with patch("maestro.cli.IssueTracker") as mock_it:
        mock_it.return_value.list_open.return_value = []
        cmd_issues(args, config)

    assert mock_it.call_args.args[0] == _EXT_REPO


# ── agent.MaestroAgent.__init__ fallback arms (step 3) ──────────────


def _make_agent(
    config: Config,
    *,
    vcs: VCSClient | None = None,
    issues: IssueTracker | None = None,
) -> MaestroAgent:
    """Build a ``MaestroAgent``; a ``None`` ``vcs`` / ``issues`` hits the fallback."""
    config.ensure_dirs()
    memory = Memory(config.memory_dir)
    consciousness = Consciousness(config.memory_dir)
    journal = Journal(config.memory_dir)
    notifier = NullNotifier()
    executor = Executor()
    planner = Planner()
    evolution = EvolutionEngine(
        memory, consciousness, journal, notifier, config=config,
    )
    return MaestroAgent(
        config=config, consciousness=consciousness, journal=journal,
        memory=memory, executor=executor, planner=planner,
        evolution=evolution, notifier=notifier, vcs=vcs, issues=issues,
    )


def test_agent_fallback_clients_default_to_self_target(mem_fs: dict[str, str]) -> None:
    """The ``issues``/``vcs`` fallback arms read the self scalars by default."""
    config = _make_config(
        memory_dir=_FALLBACK_DIR, workspace=_FALLBACK_DIR,
        github_repo="self-owner/self-repo",
    )
    agent = _make_agent(config)

    assert isinstance(agent._vcs, SubprocessVCS)
    assert agent._vcs._cwd == str(_FALLBACK_DIR)
    assert agent.issues._repo == "self-owner/self-repo"


def test_agent_fallback_clients_follow_work_redirect(mem_fs: dict[str, str]) -> None:
    """A ``[work]`` redirect reaches the ``MaestroAgent`` fallback arms."""
    config = _make_config(
        memory_dir=_FALLBACK_DIR, workspace=_FALLBACK_DIR,
        github_repo="self-owner/self-repo",
        work_repo_workspace=_EXT_WORKSPACE, work_github_repo=_EXT_REPO,
    )
    agent = _make_agent(config)

    assert isinstance(agent._vcs, SubprocessVCS)
    assert agent._vcs._cwd == _EXT_WORKSPACE
    assert agent.issues._repo == _EXT_REPO


def test_agent_injected_clients_win_over_construction(mem_fs: dict[str, str]) -> None:
    """Injected ``vcs`` / ``issues`` win even under a ``[work]`` redirect."""
    config = _make_config(
        memory_dir=_FALLBACK_DIR, workspace=_FALLBACK_DIR,
        github_repo="self-owner/self-repo",
        work_repo_workspace=_EXT_WORKSPACE, work_github_repo=_EXT_REPO,
    )
    injected_vcs = MockVCS(workspace=_FALLBACK_DIR)
    injected_issues = IssueTracker(
        "injected-owner/injected-repo", code_host=MockCodeHost(),
    )
    agent = _make_agent(config, vcs=injected_vcs, issues=injected_issues)

    assert agent._vcs is injected_vcs
    assert agent.issues is injected_issues
