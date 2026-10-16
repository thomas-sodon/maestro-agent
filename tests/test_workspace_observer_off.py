"""Proposal 149: the zero-citation workspace observer is off in base.

The ``attend`` phase wrote one ``ConsciousMoment`` episode per pass that no
prompt or decision ever cited. Base now turns the observer off, so the phase
never runs, nothing is appended, and the loop's ``read_conscious_*`` tools get
no backend (their inert branch) instead of a stale historical moment.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.cli import _build_loop_conscious_episode_backend
from maestro.config import Config
from maestro.conscious_workspace import JsonlConsciousEpisodeLedger
from maestro.evolution import MergeCheckResult

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_config_default_observer_off() -> None:
    assert Config().conscious_workspace_observe_enabled is False


def test_committed_toml_observer_off() -> None:
    raw = tomllib.loads((_REPO_ROOT / "maestro.toml").read_text(encoding="utf-8"))
    assert raw["loop"]["conscious_workspace_observe_enabled"] is False


def test_attend_phase_will_not_run_under_defaults(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    will_run = agent._phase_will_run(
        "attend",
        iteration=1,
        smoke=False,
        first_iteration_after_restart=False,
        task_every=0,
        evolve_every=0,
        dream_every=0,
        audit_self_every=0,
    )
    assert will_run is False


@pytest.mark.asyncio
async def test_run_loop_never_dispatches_attend_under_defaults(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="")  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent._process_rejected_plans = AsyncMock()  # type: ignore[method-assign]
    agent._respond_to_messages = AsyncMock()  # type: ignore[method-assign]
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(),
    )
    attend = AsyncMock()
    agent._phase_runners["attend"] = attend
    agent.conscious_episodes.append = MagicMock()  # type: ignore[method-assign]

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999,
    )

    attend.assert_not_awaited()
    agent.conscious_episodes.append.assert_not_called()


def test_loop_backend_withheld_when_observer_off() -> None:
    config = Config(memory_dir=Path("/fake/observer"))
    assert _build_loop_conscious_episode_backend(config) is None


def test_loop_backend_built_when_observer_on() -> None:
    config = Config(
        memory_dir=Path("/fake/observer"),
        conscious_workspace_observe_enabled=True,
    )
    backend = _build_loop_conscious_episode_backend(config)
    assert isinstance(backend, JsonlConsciousEpisodeLedger)
