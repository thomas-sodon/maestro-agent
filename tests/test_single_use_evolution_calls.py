"""Proposal 139: evolution-lineage calls routed through ``single_use_call``.

Under ``[sdk] single_use_evolution_calls_enabled`` each of the
``SINGLE_USE`` callers runs its own stateless request (same system prompt,
schema and ``complete_on``) on a session opened for that call alone at its
registry policy, ignoring any passed session. Flag-off keeps the existing
session and stateless arms and never reaches the seam.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.call_policy import CALL_POLICIES, evolution_slot_system
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from maestro.prompts.defaults import OBJECTIVE_PROMPT
from tests.conftest import MockQueryResult
from tests.support.gap_fuel import GAP

FAKE_DIR = Path("/fake/single_use")

_PLAN_JSON = json.dumps({
    "multi_pr": False,
    "title": "One focused change",
    "category": "capability",
    "priority": "capability",
    "rationale": "Fits one PR",
    "proposed_changes": ["Edit maestro/foo.py and add a test to verify it"],
    "acceptance_criteria": ["foo() returns 1"],
    "files_affected": ["maestro/foo.py"],
    "expected_impact": "Forward",
})


def _fake_session() -> MagicMock:
    return MagicMock(name="ClaudeSDKClient")


@dataclass
class _Case:
    caller: str
    owner: str  # module whose bare ``query`` / ``query_on_session`` names are patched
    response: str
    invoke: Callable[[bool, Any], Awaitable[object]]


def _stack() -> tuple[Consciousness, Journal, Memory]:
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    m = Memory(FAKE_DIR)
    c.load_self_model()
    return c, j, m


def _engine(flag: bool) -> EvolutionEngine:
    c, j, m = _stack()
    return EvolutionEngine(
        m, c, j, NullNotifier(), issues=IssueTracker(github_repo=None),
        github_repo="owner/repo",
        config=Config(sdk_single_use_evolution_calls_enabled=flag),
    )


async def _propose(flag: bool, session: Any) -> object:
    return await _engine(flag).propose_plan(gap=GAP, session=session)


async def _propose_chain(flag: bool, session: Any) -> object:
    return await _engine(flag).propose_plan_chain("reflection", session=session)


async def _curate(flag: bool, session: Any) -> object:
    target = EvolutionPlan(
        plan_id="cur-1", title="Old plan",
        category="capability", rationale="why", proposed_changes=["c"],
        files_affected=["maestro/agent.py"], expected_impact="x",
        priority="capability", status="pending_review",
        created_at="2026-01-01T00:00:00Z",
    )
    with patch("maestro.evolution.select_curation_target", return_value=target):
        return await _engine(flag).curate_pending_plans(session=session)


CASES = [
    _Case("propose_plan", "maestro.evolution", _PLAN_JSON, _propose),
    _Case("propose_plan_chain", "maestro.evolution", _PLAN_JSON, _propose_chain),
    _Case("curate_pending_plans", "maestro.evolution", json.dumps({"action": "keep"}), _curate),
]


@dataclass
class _Mocks:
    create: AsyncMock
    seam_turn: AsyncMock
    disconnect: AsyncMock
    owner_query: AsyncMock
    owner_turn: AsyncMock
    opened: MagicMock


@pytest.fixture
def mocks() -> Iterator[Callable[[_Case], _Mocks]]:
    active: list[Any] = []

    def _arm(case: _Case) -> _Mocks:
        opened = MagicMock(name="single_use_session")
        result = MockQueryResult(response=case.response)
        m = _Mocks(
            create=AsyncMock(return_value=opened),
            seam_turn=AsyncMock(return_value=result),
            disconnect=AsyncMock(),
            owner_query=AsyncMock(return_value=result),
            owner_turn=AsyncMock(return_value=result),
            opened=opened,
        )
        for target, mock in (
            ("maestro.call_policy.create_session", m.create),
            ("maestro.call_policy.query_on_session", m.seam_turn),
            ("maestro.call_policy.disconnect_session", m.disconnect),
            (f"{case.owner}.query", m.owner_query),
            (f"{case.owner}.query_on_session", m.owner_turn),
        ):
            p = patch(target, mock)
            p.start()
            active.append(p)
        return m

    yield _arm
    for p in reversed(active):
        p.stop()


async def _stateless_request(
    case: _Case, mocks: Callable[[_Case], _Mocks], mem_fs: dict[str, str],
) -> tuple[Any, Any, Any]:
    """The flag-off stateless arm's (prompt, system, output_format) for *case*.

    ``mem_fs`` is restored afterwards so state the probe persists (e.g. a
    persisted digest) cannot change the flag-on run's prompt.
    """
    _stack()  # seed the self-model so both runs share its timestamps
    snapshot = dict(mem_fs)
    m = mocks(case)
    await case.invoke(False, None)
    mem_fs.clear()
    mem_fs.update(snapshot)
    kwargs = m.owner_query.call_args.kwargs
    return kwargs["prompt"], kwargs["system"], kwargs["options"].output_format


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[c.caller for c in CASES])
@pytest.mark.parametrize("with_session", [True, False], ids=["session", "no_session"])
async def test_flag_on_routes_through_single_use_call(
    mem_fs: dict[str, str],
    mocks: Callable[[_Case], _Mocks],
    case: _Case,
    with_session: bool,
) -> None:
    expected_prompt, stateless_system, expected_format = await _stateless_request(
        case, mocks, mem_fs,
    )

    m = mocks(case)
    passed = _fake_session() if with_session else None
    await case.invoke(True, passed)

    policy = CALL_POLICIES[case.caller]
    m.create.assert_awaited_once()
    create_kwargs = m.create.call_args.kwargs
    opts = create_kwargs["options"]
    assert opts.effort == policy.effort
    assert opts.tools == []
    assert opts.caller == case.caller
    assert opts.output_format == expected_format
    assert create_kwargs["phase"] == "evolution"
    # Slot B's charter framing is kept ahead of the stateless system.
    assert create_kwargs["system"] == evolution_slot_system(stateless_system)
    assert create_kwargs["system"].startswith(OBJECTIVE_PROMPT)

    m.seam_turn.assert_awaited_once()
    assert m.seam_turn.call_args.args[0] is m.opened
    assert m.seam_turn.call_args.args[1] == expected_prompt
    assert m.seam_turn.call_args.kwargs["caller"] == case.caller
    m.disconnect.assert_awaited_once_with(m.opened)
    m.owner_turn.assert_not_called()
    m.owner_query.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[c.caller for c in CASES])
async def test_flag_off_session_arm_unchanged(
    mem_fs: dict[str, str],
    mocks: Callable[[_Case], _Mocks],
    case: _Case,
) -> None:
    m = mocks(case)
    sess = _fake_session()
    await case.invoke(False, sess)

    m.owner_turn.assert_awaited_once()
    assert m.owner_turn.call_args.args[0] is sess
    m.owner_query.assert_not_called()
    m.create.assert_not_called()
    m.seam_turn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[c.caller for c in CASES])
async def test_flag_off_stateless_arm_unchanged(
    mem_fs: dict[str, str],
    mocks: Callable[[_Case], _Mocks],
    case: _Case,
) -> None:
    m = mocks(case)
    await case.invoke(False, None)

    m.owner_query.assert_awaited_once()
    assert m.owner_query.call_args.kwargs["options"].caller == case.caller
    m.owner_turn.assert_not_called()
    m.create.assert_not_called()
    m.seam_turn.assert_not_called()


@pytest.mark.parametrize("flag", [True, False])
def test_build_agent_threads_flag_to_owners(mem_fs: dict[str, str], flag: bool) -> None:
    from maestro.cli import _build_agent

    config = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        enable_session_reuse=False,
        publication_enabled=False,
        sdk_single_use_evolution_calls_enabled=flag,
    )
    agent = _build_agent(config)
    assert agent.evolution._config.sdk_single_use_evolution_calls_enabled is flag


@pytest.mark.asyncio
async def test_curate_phase_opens_exactly_one_single_use_session(
    make_agent: Callable[[], Any],
) -> None:
    """Across the agent → engine boundary, a flag-on curate phase opens one
    session — the engine's own single-use one — and never slot B."""
    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.sdk_single_use_evolution_calls_enabled = True
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="cur-1", title="Old plan",
        category="capability", rationale="why", proposed_changes=["c"],
        files_affected=["maestro/agent.py"], expected_impact="x",
        priority="capability", status="pending_review",
        created_at="2026-01-01T00:00:00Z",
    ))
    result = MockQueryResult(response=json.dumps({"action": "keep"}))
    with (
        patch("maestro.call_policy.create_session",
              AsyncMock(return_value=_fake_session())) as create,
        patch("maestro.call_policy.query_on_session", AsyncMock(return_value=result)),
        patch("maestro.call_policy.disconnect_session", AsyncMock()),
        patch("maestro.agent.create_session", AsyncMock()) as agent_create,
    ):
        await agent._phase_curate_pending_plans(iteration=1)

    create.assert_awaited_once()
    assert create.call_args.kwargs["options"].caller == "curate_pending_plans"
    agent_create.assert_not_called()
    assert agent._session_b is None


def test_evolution_slot_system_uses_objective() -> None:
    assert evolution_slot_system("S") == f"{OBJECTIVE_PROMPT}\n\nS"


def _value_claim_owners() -> tuple[Consciousness, EvolutionEngine]:
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    m = Memory(FAKE_DIR)
    c.load_self_model()
    e = EvolutionEngine(
        m, c, j, NullNotifier(), issues=IssueTracker(github_repo=None),
        github_repo="owner/repo",
        config=Config(
            sdk_single_use_evolution_calls_enabled=True,
        ),
    )
    return c, e


async def _nb_propose(_flag: bool, session: Any) -> object:
    _c, e = _value_claim_owners()
    return await e.propose_plan(gap=GAP, session=session)


_VALUE_CLAIM_CASES = [
    _Case("propose_plan", "maestro.evolution", _PLAN_JSON, _nb_propose),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _VALUE_CLAIM_CASES, ids=[c.caller for c in _VALUE_CLAIM_CASES])
async def test_value_claim_propose_frames_single_use_session(
    mem_fs: dict[str, str], mocks: Callable[[_Case], _Mocks], case: _Case,
) -> None:
    m = mocks(case)
    await case.invoke(True, None)
    system = m.create.call_args.kwargs["system"]
    assert system.startswith(OBJECTIVE_PROMPT)
