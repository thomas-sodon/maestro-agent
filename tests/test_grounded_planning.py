"""Proposal 143 step 4: the planner callers read the tree they propose to change.

With ``tree_reads=True`` and the single-use arm on, ``propose_plan``'s session
carries exactly the bounded read-only ``tree`` server (``tools == []``), the
prompt gains the read-before-claiming fragment, the call streams to the result
message with no text early-exit, and the plan stamps ``grounded_reads`` with the paths
the planner read. Every other path stays tool-free with ``grounded_reads``
``None``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import tree_retrieval
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import (
    _GROUNDED_CURATE_PROMPT,
    _GROUNDED_PLANNING_CHAIN_PROMPT,
    _GROUNDED_PLANNING_PROMPT,
    EvolutionEngine,
)
from maestro.evolution_fuel import FuelSelection
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from maestro.reality_inventory import Problem, RealityInventory
from maestro.sdk import json_complete
from tests.conftest import MockQueryResult
from tests.support.gap_fuel import GAP

FAKE_DIR = Path("/fake/grounded_planning")
REPO = Path("/fake/grounded_planning_repo")

_PLAN_JSON = json.dumps({
    "title": "One focused change",
    "category": "capability",
    "priority": "capability",
    "rationale": "Fits one PR",
    "proposed_changes": ["Edit maestro/foo.py and add a test to verify it"],
    "acceptance_criteria": ["foo() returns 1"],
    "files_affected": ["maestro/foo.py"],
    "expected_impact": "Forward",
})


def _engine(
    *, single_use: bool = True, tracked: frozenset[str] = frozenset(),
    grounded: bool | None = None,
) -> EvolutionEngine:
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    m = Memory(FAKE_DIR)
    c.load_self_model()
    engine = EvolutionEngine(
        m, c, j, NullNotifier(), issues=IssueTracker(github_repo=None),
        github_repo="owner/repo",
        config=Config(
            sdk_single_use_evolution_calls_enabled=single_use,
            grounded_planning_enabled=single_use if grounded is None else grounded,
            workspace=REPO,
        ),
    )
    engine._vcs = MagicMock()
    engine._vcs.tracked_paths.return_value = tracked
    return engine


class _Seam:
    """Patches ``single_use_call``'s session seam; replays tool reads on the turn."""

    def __init__(
        self,
        reads: list[tuple[str, dict[str, Any]]],
        *,
        response: str = _PLAN_JSON,
        preamble: str = "",
    ) -> None:
        self.reads = reads
        self.response = response
        self.preamble = preamble
        self.tool_texts: list[str] = []
        self.registry: dict[str, Any] = {}
        self.create = AsyncMock(return_value=MagicMock(name="session"))
        self.turn = AsyncMock(side_effect=self._turn)
        self.disconnect = AsyncMock()
        self.query = AsyncMock(return_value=MockQueryResult(response=_PLAN_JSON))

    async def _turn(self, *_args: Any, **kwargs: Any) -> MockQueryResult:
        # A preamble text turn precedes the tool turn; an early-exit predicate
        # that fires on it ends the stream there, as the real CLI stream would.
        complete_on = kwargs.get("complete_on")
        if self.preamble and complete_on is not None and complete_on(self.preamble):
            return MockQueryResult(response=self.preamble)
        tools = {t.name: t for t in self.registry.get(tree_retrieval.TREE_SERVER_NAME, [])}
        for name, args in self.reads:
            result = await tools[name].handler(args)
            self.tool_texts.append(result["text"])
        return MockQueryResult(response=self.response)

    def __enter__(self) -> _Seam:
        real_build = tree_retrieval.build_bounded_tree_tool_set

        def _spy(view: tree_retrieval.TreeView, **kwargs: Any) -> Any:
            registry, receipt = real_build(view, **kwargs)
            self.registry = registry
            return registry, receipt

        self._patches = [
            patch("maestro.call_policy.create_session", self.create),
            patch("maestro.call_policy.query_on_session", self.turn),
            patch("maestro.call_policy.disconnect_session", self.disconnect),
            patch("maestro.evolution.query", self.query),
            patch.object(tree_retrieval, "build_bounded_tree_tool_set", _spy),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        for p in reversed(self._patches):
            p.stop()


@pytest.mark.asyncio
async def test_tree_reads_session_carries_only_tree_and_stamps_reads(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(REPO / "maestro/foo.py")] = "def foo():\n    return 1\n"
    mem_fs[str(REPO / "maestro/bar.py")] = "x = 1\n"
    engine = _engine(tracked=frozenset({"maestro/foo.py", "maestro/bar.py"}))
    reads = [
        ("read_source", {"path": "maestro/foo.py"}),
        ("read_source", {"path": "maestro/bar.py"}),
        ("read_source", {"path": "data/secret.json"}),
    ]
    with _Seam(reads) as seam:
        plan = await engine.propose_plan(gap=GAP, tree_reads=True)

    opts = seam.create.call_args.kwargs["options"]
    assert opts.tools == []
    assert set(opts.mcp_servers) == {tree_retrieval.TREE_SERVER_NAME}
    assert opts.allowed_tools
    assert all(n.startswith("mcp__tree__") for n in opts.allowed_tools)
    assert opts.max_turns == tree_retrieval.TREE_SESSION_MAX_TURNS
    assert opts.caller == "propose_plan"
    prompt = seam.turn.call_args.args[1]
    assert prompt.endswith(_GROUNDED_PLANNING_PROMPT)
    assert seam.turn.call_args.kwargs["complete_on"] is None
    assert plan.grounded_reads == ["maestro/foo.py", "maestro/bar.py"]


@pytest.mark.asyncio
async def test_tree_reads_without_any_read_stamps_empty_list(
    mem_fs: dict[str, str],
) -> None:
    engine = _engine(tracked=frozenset({"maestro/foo.py"}))
    with _Seam([]):
        plan = await engine.propose_plan(gap=GAP, tree_reads=True)
    assert plan.grounded_reads == []


async def _tool_free_run(
    engine: EvolutionEngine, *, tree_reads: bool,
) -> tuple[Any, str, Any, Any]:
    with _Seam([]) as seam:
        plan = await engine.propose_plan(gap=GAP, tree_reads=tree_reads)
    opts = seam.create.call_args.kwargs["options"]
    prompt = seam.turn.call_args.args[1]
    return plan, prompt, opts, seam.turn.call_args.kwargs["complete_on"]


@pytest.mark.asyncio
async def test_throttled_call_is_byte_identical_and_tool_free(
    mem_fs: dict[str, str],
) -> None:
    """``tree_reads=False`` (flag off or throttled) builds exactly today's call."""
    engine = _engine(tracked=frozenset({"maestro/foo.py"}))
    plan, prompt, opts, complete_on = await _tool_free_run(engine, tree_reads=False)
    assert plan.grounded_reads is None
    assert _GROUNDED_PLANNING_PROMPT not in prompt
    assert opts.tools == []
    assert opts.mcp_servers is None
    assert opts.allowed_tools is None
    assert complete_on is json_complete
    engine._vcs.tracked_paths.assert_not_called()

    flag_off = _engine(tracked=frozenset({"maestro/foo.py"}), grounded=False)
    with _Seam([]) as seam:
        await flag_off.propose_plan(gap=GAP)
    assert prompt == seam.turn.call_args.args[1]
    assert opts == seam.create.call_args.kwargs["options"]


@pytest.mark.asyncio
async def test_empty_tracked_set_degrades_to_tool_free(
    mem_fs: dict[str, str],
) -> None:
    engine = _engine(tracked=frozenset())
    plan, prompt, opts, complete_on = await _tool_free_run(engine, tree_reads=True)
    assert plan.grounded_reads is None
    assert _GROUNDED_PLANNING_PROMPT not in prompt
    assert opts.mcp_servers is None
    assert complete_on is json_complete


@pytest.mark.asyncio
async def test_tree_reads_ignored_without_single_use_arm(
    mem_fs: dict[str, str],
) -> None:
    """The shared slot-B / stateless arm cannot carry per-call MCP options."""
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    m = Memory(FAKE_DIR)
    c.load_self_model()
    engine = EvolutionEngine(
        m, c, j, NullNotifier(), issues=IssueTracker(github_repo=None),
        github_repo="owner/repo", config=Config(),
    )
    engine._vcs = MagicMock()
    with _Seam([]) as seam:
        plan = await engine.propose_plan(gap=GAP, tree_reads=True)
    assert plan.grounded_reads is None
    seam.create.assert_not_called()
    assert _GROUNDED_PLANNING_PROMPT not in seam.query.call_args.kwargs["prompt"]
    engine._vcs.tracked_paths.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "throttled", "expected"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
async def test_evolve_passes_throttle_aware_tree_reads(
    make_agent: Any, flag: bool, throttled: bool, expected: bool,
) -> None:
    """The evolve phase grants tree reads only with the flag on and unthrottled."""
    from unittest.mock import PropertyMock

    from maestro.agent import MaestroAgent

    agent = make_agent()
    agent.config.grounded_planning_enabled = flag
    agent.evolution.run_quality_gates = MagicMock(
        return_value=MagicMock(raw_output="health"),
    )
    agent._admitted_evolution_fuel = MagicMock(return_value=FuelSelection(selected=GAP))
    agent.evolution.propose_plan = AsyncMock(return_value=MagicMock())
    agent.evolution.submit_for_review = AsyncMock()

    with patch.object(
        MaestroAgent, "budget_throttle_active",
        new_callable=PropertyMock, return_value=throttled,
    ):
        await agent.evolve()

    agent.evolution.propose_plan.assert_awaited_once()
    assert agent.evolution.propose_plan.call_args.kwargs["tree_reads"] is expected


# --- read_reality_inventory (the fifth tree tool) ----------------------------


def test_grounded_fragments_name_the_inventory_tool() -> None:
    assert "read_reality_inventory" in _GROUNDED_PLANNING_PROMPT


@pytest.mark.asyncio
async def test_propose_plan_inventory_tool_renders_loaded_inventory(
    mem_fs: dict[str, str],
) -> None:
    inventory = RealityInventory(
        shipped_capabilities=frozenset({"cli:status"}),
        measured_problems=(
            Problem(id="audit:stale-rule", source="audit", summary="s", weight=3),
        ),
    )
    engine = _engine(tracked=frozenset({"maestro/foo.py"}))
    reads: list[tuple[str, dict[str, Any]]] = [
        ("read_reality_inventory", {}),
        ("read_reality_inventory", {"kind": "cli"}),
    ]
    with (
        patch(
            "maestro.reality_inventory.load_reality_inventory",
            return_value=inventory,
        ) as load,
        _Seam(reads) as seam,
    ):
        plan = await engine.propose_plan(gap=GAP, tree_reads=True)

    load.assert_called_once()
    assert "audit:stale-rule (weight 3): s" in seam.tool_texts[0]
    assert seam.tool_texts[1] == "shipped cli (1):\nstatus"
    assert plan.grounded_reads == []


# --- propose_plan_chain + the grounded_planning_callers subset ---------------

_SLICE = {
    "title": "Slice",
    "category": "capability",
    "priority": "capability",
    "rationale": "Step",
    "proposed_changes": ["Edit maestro/foo.py and add a test to verify it"],
    "acceptance_criteria": ["foo() returns 1"],
    "files_affected": ["maestro/foo.py"],
    "expected_impact": "Forward",
}
_CHAIN_JSON = json.dumps({
    "multi_pr": True,
    "proposal_title": "Initiative",
    "proposal_goal": "Goal",
    "slices": [{**_SLICE, "title": "First"}, {**_SLICE, "title": "Second"}],
})
_LONE_CHAIN_JSON = json.dumps({"multi_pr": False, **_SLICE})


@pytest.mark.asyncio
async def test_chain_lone_plan_on_tree_surface_stamps_reads(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(REPO / "maestro/foo.py")] = "def foo():\n    return 1\n"
    engine = _engine(tracked=frozenset({"maestro/foo.py"}))
    with _Seam(
        [("read_source", {"path": "maestro/foo.py"})], response=_LONE_CHAIN_JSON,
    ) as seam:
        plan = await engine.propose_plan_chain("reflection", tree_reads=True)

    opts = seam.create.call_args.kwargs["options"]
    assert opts.tools == []
    assert set(opts.mcp_servers) == {tree_retrieval.TREE_SERVER_NAME}
    assert opts.caller == "propose_plan_chain"
    assert seam.turn.call_args.args[1].endswith(_GROUNDED_PLANNING_CHAIN_PROMPT)
    assert seam.turn.call_args.kwargs["complete_on"] is None
    assert seam.tool_texts == ["1\tdef foo():\n2\t    return 1"]
    assert isinstance(plan, EvolutionPlan)
    assert plan.grounded_reads == ["maestro/foo.py"]


@pytest.mark.asyncio
async def test_chain_slices_all_stamp_the_shared_reads(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(REPO / "maestro/foo.py")] = "def foo():\n    return 1\n"
    engine = _engine(tracked=frozenset({"maestro/foo.py"}))
    with _Seam(
        [("read_source", {"path": "maestro/foo.py"})], response=_CHAIN_JSON,
    ):
        result = await engine.propose_plan_chain("reflection", tree_reads=True)

    assert isinstance(result, tuple)
    _proposal, slices = result
    assert len(slices) == 2
    assert slices[0].grounded_reads == ["maestro/foo.py"]
    assert slices[1].grounded_reads == ["maestro/foo.py"]


async def _chain_tool_free(
    engine: EvolutionEngine, *, tree_reads: bool,
) -> tuple[Any, str, Any, Any]:
    with _Seam([], response=_CHAIN_JSON) as seam:
        result = await engine.propose_plan_chain("reflection", tree_reads=tree_reads)
    opts = seam.create.call_args.kwargs["options"]
    prompt = seam.turn.call_args.args[1]
    return result, prompt, opts, seam.turn.call_args.kwargs["complete_on"]


@pytest.mark.asyncio
async def test_chain_off_surface_is_byte_identical(mem_fs: dict[str, str]) -> None:
    """``tree_reads=False`` (unlisted or throttled) builds exactly today's call."""
    engine = _engine(tracked=frozenset({"maestro/foo.py"}))
    result, prompt, opts, complete_on = await _chain_tool_free(
        engine, tree_reads=False,
    )
    _proposal, slices = result
    assert slices[0].grounded_reads is None
    assert slices[1].grounded_reads is None
    assert _GROUNDED_PLANNING_CHAIN_PROMPT not in prompt
    assert opts.tools == []
    assert opts.mcp_servers is None
    assert complete_on is json_complete
    engine._vcs.tracked_paths.assert_not_called()

    flag_off = _engine(tracked=frozenset({"maestro/foo.py"}), grounded=False)
    _r, off_prompt, off_opts, _c = await _chain_tool_free(flag_off, tree_reads=False)
    assert prompt == off_prompt
    assert opts == off_opts


@pytest.mark.asyncio
async def test_chain_empty_tracked_set_degrades_to_tool_free(
    mem_fs: dict[str, str],
) -> None:
    engine = _engine(tracked=frozenset())
    result, prompt, opts, complete_on = await _chain_tool_free(
        engine, tree_reads=True,
    )
    assert result[1][0].grounded_reads is None
    assert _GROUNDED_PLANNING_CHAIN_PROMPT not in prompt
    assert opts.mcp_servers is None
    assert complete_on is json_complete


@pytest.mark.asyncio
async def test_chain_tree_reads_ignored_without_single_use_arm(
    mem_fs: dict[str, str],
) -> None:
    """Neither the session arm nor the bare-``query()`` arm carries tree reads."""
    c = Consciousness(FAKE_DIR)
    j = Journal(FAKE_DIR)
    m = Memory(FAKE_DIR)
    c.load_self_model()
    engine = EvolutionEngine(
        m, c, j, NullNotifier(), issues=IssueTracker(github_repo=None),
        github_repo="owner/repo", config=Config(),
    )
    engine._vcs = MagicMock()
    slot_turn = AsyncMock(return_value=MockQueryResult(response=_LONE_CHAIN_JSON))
    with (
        _Seam([]) as seam,
        patch("maestro.evolution.query_on_session", slot_turn),
    ):
        bare = await engine.propose_plan_chain("reflection", tree_reads=True)
        slot = await engine.propose_plan_chain(
            "reflection", session=MagicMock(name="slot"), tree_reads=True,
        )
    seam.create.assert_not_called()
    assert _GROUNDED_PLANNING_CHAIN_PROMPT not in seam.query.call_args.kwargs["prompt"]
    assert _GROUNDED_PLANNING_CHAIN_PROMPT not in slot_turn.call_args.args[1]
    assert isinstance(bare, EvolutionPlan)
    assert isinstance(slot, EvolutionPlan)
    assert bare.grounded_reads is None
    assert slot.grounded_reads is None
    engine._vcs.tracked_paths.assert_not_called()


@pytest.mark.parametrize(
    ("callers", "throttled", "caller", "expected"),
    [
        (["propose_plan"], False, "propose_plan_chain", False),
        (["propose_plan_chain"], False, "propose_plan_chain", True),
        (["propose_plan_chain"], True, "propose_plan_chain", False),
        (["propose_plan"], False, "propose_plan", True),
        (["propose_plan"], False, "curate_pending_plans", False),
        (["curate_pending_plans"], False, "curate_pending_plans", True),
        (["curate_pending_plans"], True, "curate_pending_plans", False),
    ],
)
def test_tree_reads_for_honours_subset_and_throttle(
    mem_fs: dict[str, str],
    callers: list[str], throttled: bool, caller: str, expected: bool,
) -> None:
    engine = _engine()
    engine._config.grounded_planning_callers = callers
    assert engine.tree_reads_for(caller, throttled=throttled) is expected
    engine._config.grounded_planning_enabled = False
    assert engine.tree_reads_for(caller, throttled=throttled) is False


def _evolve_agent(make_agent: Any, callers: list[str], *, multi_pr: bool) -> Any:
    agent = make_agent()
    agent.config.grounded_planning_enabled = True
    agent.config.grounded_planning_callers = callers
    agent.config.multi_pr_proposals_enabled = multi_pr
    agent.evolution.run_quality_gates = MagicMock(
        return_value=MagicMock(raw_output="health"),
    )
    agent._admitted_evolution_fuel = MagicMock(return_value=FuelSelection(selected=GAP))
    agent.evolution.propose_plan = AsyncMock(return_value=MagicMock())
    agent.evolution.propose_plan_chain = AsyncMock(return_value=MagicMock())
    agent.evolution.submit_for_review = AsyncMock()
    return agent


@pytest.mark.asyncio
async def test_evolve_routes_tree_reads_per_caller_lone_plan(
    make_agent: Any,
) -> None:
    """With only ``propose_plan`` listed, propose_plan reads."""
    from unittest.mock import PropertyMock

    from maestro.agent import MaestroAgent

    agent = _evolve_agent(make_agent, ["propose_plan"], multi_pr=False)
    with patch.object(
        MaestroAgent, "budget_throttle_active",
        new_callable=PropertyMock, return_value=False,
    ):
        await agent.evolve()

    assert agent.evolution.propose_plan.call_args.kwargs["tree_reads"] is True
    agent.evolution.propose_plan_chain.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callers", "expected"),
    [
        (["propose_plan"], False),
        (["propose_plan", "propose_plan_chain"], True),
    ],
)
async def test_evolve_routes_tree_reads_per_caller_multi_pr(
    make_agent: Any, callers: list[str], expected: bool,
) -> None:
    from unittest.mock import PropertyMock

    from maestro.agent import MaestroAgent

    agent = _evolve_agent(make_agent, callers, multi_pr=True)
    with patch.object(
        MaestroAgent, "budget_throttle_active",
        new_callable=PropertyMock, return_value=False,
    ):
        await agent.evolve()

    agent.evolution.propose_plan_chain.assert_awaited_once()
    chain_kwargs = agent.evolution.propose_plan_chain.call_args.kwargs
    assert chain_kwargs["tree_reads"] is expected


@pytest.mark.parametrize(
    "callers",
    # ``reflect`` was deleted (proposal 155 step 4), so it is now unknown.
    [["reflect"], ["generate_task"], ["propose_plan", "propose_plan"]],
)
def test_grounded_planning_callers_rejects_unknown_or_duplicate(
    callers: list[str],
) -> None:
    """Validated even with the flag off, so a typo fails before the flip."""
    with pytest.raises(ValueError, match="grounded_planning_callers"):
        Config(grounded_planning_callers=callers).validate()


def test_chain_declares_tree_read_only_surface() -> None:
    from maestro.call_policy import CALL_POLICIES
    from maestro.tool_surface import TREE_READ_ONLY

    assert CALL_POLICIES["propose_plan_chain"].surface is TREE_READ_ONLY


def test_chain_prompt_omits_the_probe_sentence() -> None:
    assert "grounding" not in _GROUNDED_PLANNING_CHAIN_PROMPT
    assert "read_reality_inventory" in _GROUNDED_PLANNING_CHAIN_PROMPT


# ── curation (opt-in caller) ─────────────────────────────────────────

_CURATE_TARGET = EvolutionPlan(
    plan_id="cur-1", title="Old plan",
    category="capability", rationale="why", proposed_changes=["c"],
    files_affected=["maestro/foo.py"], expected_impact="x",
    priority="capability", status="pending_review",
    created_at="2026-01-01T00:00:00Z",
)
_OBSOLETE_JSON = json.dumps({"action": "obsolete", "reason": "foo() already ships"})
_KEEP_JSON = json.dumps({"action": "keep"})


async def _curate(engine: EvolutionEngine, *, tree_reads: bool) -> bool:
    with patch("maestro.evolution.select_curation_target", return_value=_CURATE_TARGET):
        return await engine.curate_pending_plans(session=None, tree_reads=tree_reads)


@pytest.mark.asyncio
async def test_curate_tree_reads_session_carries_only_tree(
    mem_fs: dict[str, str],
) -> None:
    mem_fs[str(REPO / "maestro/foo.py")] = "def foo():\n    return 1\n"
    engine = _engine(tracked=frozenset({"maestro/foo.py"}))
    engine._memory.mark_plan_obsolete = MagicMock(return_value=True)  # type: ignore[method-assign]
    reads = [("read_source", {"path": "maestro/foo.py"})]
    with _Seam(reads, response=_OBSOLETE_JSON) as seam:
        applied = await _curate(engine, tree_reads=True)

    options = seam.create.call_args.kwargs["options"]
    assert list(options.mcp_servers) == [tree_retrieval.TREE_SERVER_NAME]
    assert options.tools == []
    assert seam.turn.call_args.kwargs["complete_on"] is None
    assert seam.turn.call_args.args[1].endswith(_GROUNDED_CURATE_PROMPT)
    assert "return 1" in seam.tool_texts[0]
    assert applied is True
    engine._memory.mark_plan_obsolete.assert_called_once()
    assert engine._memory.mark_plan_obsolete.call_args.args[0] == "cur-1"


@pytest.mark.asyncio
async def test_curate_off_surface_is_byte_identical(mem_fs: dict[str, str]) -> None:
    engine = _engine(tracked=frozenset({"maestro/foo.py"}))
    with _Seam([], response=_KEEP_JSON) as seam:
        await _curate(engine, tree_reads=False)
    grounded_engine = _engine(tracked=frozenset())
    with _Seam([], response=_KEEP_JSON) as empty:
        await _curate(grounded_engine, tree_reads=True)

    for run in (seam, empty):
        assert run.turn.call_args.kwargs["complete_on"] is json_complete
        assert _GROUNDED_CURATE_PROMPT not in run.turn.call_args.args[1]
        assert run.create.call_args.kwargs["options"].mcp_servers is None
    assert seam.turn.call_args.args[1] == empty.turn.call_args.args[1]
    engine._vcs.tracked_paths.assert_not_called()
    grounded_engine._vcs.tracked_paths.assert_called_once()


def test_curate_declares_tree_read_only_surface() -> None:
    from maestro.call_policy import CALL_POLICIES
    from maestro.tool_surface import TREE_READ_ONLY

    assert CALL_POLICIES["curate_pending_plans"].surface is TREE_READ_ONLY


def _rejected_plan(plan_id: str, *, title: str, rationale: str) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale=rationale,
        proposed_changes=["change"],
        files_affected=["maestro/x.py"],
        expected_impact="Forward",
        status="rejected",
        rejection_reason="already shipped",
    )


def test_rejection_corpus_none_when_flag_off(mem_fs: dict[str, str]) -> None:
    engine = _engine(grounded=True)
    engine._config.grounded_planning_rejection_tool_enabled = False
    assert engine._rejection_corpus() is None


def test_rejection_corpus_projects_only_rejected_rows(mem_fs: dict[str, str]) -> None:
    engine = _engine(grounded=True)
    engine._config.grounded_planning_rejection_tool_enabled = True
    engine._memory.save_evolution_plan(
        _rejected_plan("rej-1", title="Dup gate", rationale="a second chokepoint"),
    )
    engine._memory.save_evolution_plan(
        EvolutionPlan(
            plan_id="live-1", title="Live",
            category="capability", rationale="in flight", proposed_changes=["c"],
            files_affected=["maestro/y.py"], expected_impact="Forward",
            status="pending_review",
        ),
    )
    corpus = engine._rejection_corpus()
    assert corpus is not None
    assert [e.title for e in corpus] == ["Dup gate"]
    assert corpus[0].reason == "already shipped"
    assert corpus[0].status == "rejected"


@pytest.mark.asyncio
async def test_build_tree_read_options_passes_corpus_only_under_flag(
    mem_fs: dict[str, str],
) -> None:
    engine = _engine(grounded=True, tracked=frozenset({"maestro/foo.py"}))
    engine._config.grounded_planning_rejection_tool_enabled = True
    engine._memory.save_evolution_plan(
        _rejected_plan("rej-9", title="Refused idea", rationale="theme words here"),
    )
    captured: dict[str, Any] = {}
    real_build = tree_retrieval.build_bounded_tree_tool_set

    def _spy(view: tree_retrieval.TreeView, **kwargs: Any) -> Any:
        captured["rejections"] = kwargs.get("rejections")
        return real_build(view, **kwargs)

    with patch.object(tree_retrieval, "build_bounded_tree_tool_set", _spy):
        engine._build_tree_read_options("propose_plan")
    assert captured["rejections"] is not None
    assert [e.title for e in captured["rejections"]] == ["Refused idea"]

    engine._config.grounded_planning_rejection_tool_enabled = False
    captured.clear()
    with patch.object(tree_retrieval, "build_bounded_tree_tool_set", _spy):
        engine._build_tree_read_options("propose_plan")
    assert captured["rejections"] is None
