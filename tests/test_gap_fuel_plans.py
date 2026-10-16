"""A gap plan: its prompt, its context inputs and its pins (proposal 155 step 4)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from tests.conftest import MockQueryResult
from tests.support.gap_fuel import GAP, make_fuel
from tests.test_evolution_impl import _approved_plan
from tests.test_evolution_impl import _make as _make_impl

_DIR = Path("/fake/gap_fuel_plans")

_REPLY = json.dumps({
    "title": "Fix the red gate", "category": "awareness", "priority": "resilience",
    "rationale": "ruff fails on main", "proposed_changes": ["Fix lint; test it"],
    "acceptance_criteria": ["ruff check passes on maestro/ with zero errors"],
    "files_affected": ["maestro/agent.py"], "expected_impact": "green main",
    "issue_number": 999, "problem_ref": "audit:x",
})


def _engine(mem_fs: dict[str, str]) -> tuple[EvolutionEngine, Memory]:
    m = Memory(_DIR)
    c = Consciousness(_DIR)
    c.load_self_model()
    e = EvolutionEngine(
        m, c, Journal(_DIR), NullNotifier(), issues=IssueTracker(github_repo=None),
        config=Config(memory_dir=_DIR),
    )
    return e, m


def _plan(plan_id: str, status: str, **over: object) -> EvolutionPlan:
    fields: dict[str, object] = {
        "plan_id": plan_id, "title": f"title {plan_id}", "category": "capability",
        "rationale": "r", "proposed_changes": ["c"], "files_affected": [],
        "expected_impact": "e", "status": status,
        "created_at": "2026-10-01T00:00:00+00:00",
    }
    fields.update(over)
    return EvolutionPlan(**fields)  # type: ignore[arg-type]


async def _propose(e: EvolutionEngine) -> tuple[EvolutionPlan, str]:
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=_REPLY)
        plan = await e.propose_plan(gap=GAP)
    return plan, str(mock_q.call_args.kwargs["prompt"])


@pytest.mark.asyncio
async def test_gap_plan_is_pinned_to_its_issue(mem_fs: dict[str, str]) -> None:
    """A10: issue and category come from the gap; no problem_ref."""
    e, _m = _engine(mem_fs)
    plan, prompt = await _propose(e)
    assert prompt.startswith("Close this capability gap:\nGap issue #42")
    assert plan.issue_number == 42
    assert plan.category == "maintenance"
    assert plan.problem_ref is None
    assert '"issue_number"' not in prompt
    assert '"category"' not in prompt


@pytest.mark.asyncio
async def test_gap_plan_pr_closes_its_issue(mem_fs: dict[str, str]) -> None:
    """A10: the pinned issue reaches the PR body's ``Closes #N``."""
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/owner/repo/pulls/7"
    e, m = _make_impl(mem_fs, vcs=MagicMock(), host=host)
    plan = _approved_plan(issue_number=GAP.issue.number)
    m.save_evolution_plan(plan)
    await e.create_pr(plan)
    assert f"Closes #{GAP.issue.number}" in host.create_pr.call_args[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("phase", "verb"), [(0, "Refs"), (1, "Closes")])
async def test_gap_chain_only_last_slice_closes(
    mem_fs: dict[str, str], phase: int, verb: str,
) -> None:
    """Only a chain's final slice closes the gap; earlier slices reference it."""
    host = MagicMock()
    host.create_pr.return_value = "https://github.com/owner/repo/pulls/7"
    e, m = _make_impl(mem_fs, vcs=MagicMock(), host=host)
    for idx in (0, 1):
        sibling = _approved_plan(issue_number=GAP.issue.number)
        sibling.plan_id = f"slice{idx}"
        sibling.parent_plan_id = "prop"
        sibling.phase_index = idx
        m.save_evolution_plan(sibling)
    plan = next(p for p in m.get_all_plans() if p.phase_index == phase)
    await e.create_pr(plan)
    body = host.create_pr.call_args[0][1]
    assert f"{verb} #{GAP.issue.number}" in body


def test_reflect_is_gone() -> None:
    """A2: no reflect caller and no pre-mint intent arm remain on the engine."""
    assert not hasattr(EvolutionEngine, "reflect")
    assert not hasattr(EvolutionEngine, "refuses_reflected_intent")
    assert not hasattr(EvolutionEngine, "analyze_rejection_patterns")


def test_context_inputs_read_the_gap_history(mem_fs: dict[str, str]) -> None:
    e, m = _engine(mem_fs)
    m.save_evolution_plan(_plan("ob1", "pending_review", issue_number=42))
    m.mark_plan_obsolete("ob1", "Near-duplicate of x (exact title)")
    m.save_evolution_plan(_plan("live1", "pending_review"))
    m.save_evolution_plan(_plan("other", "pending_review", issue_number=7))
    inputs = e._gap_context_inputs(GAP)
    assert inputs.citing_reasons == [
        "ob1 obsolete: Near-duplicate of x (exact title)",
    ]
    assert list(inputs.live_targets) == ["live1", "other", "issue:42"]


def test_context_inputs_list_rejections_versions_and_uncited_branches(
    mem_fs: dict[str, str],
) -> None:
    e, m = _engine(mem_fs)
    m.save_evolution_plan(_plan(
        "rj1", "rejected", rejection_reason="not needed",
        reviewed_at="2026-10-02T00:00:00+00:00",
    ))
    m.save_evolution_plan(_plan("cites", "approved", source_task_id="t-cited"))
    tasks = [
        MagicMock(task_id="t-kept", goal="build a parser", outcome="success",
                  branch="maestro/task/t-kept"),
        MagicMock(task_id="t-cited", goal="cited", outcome="success",
                  branch="maestro/task/t-cited"),
        MagicMock(task_id="t-fail", goal="failed", outcome="failure",
                  branch="maestro/task/t-fail"),
    ]
    version = MagicMock(plan_title="Shipped thing")
    with patch(
        "maestro.evolution.recent_planning_tasks", return_value=tasks,
    ), patch.object(m, "get_recent_versions", return_value=[version]):
        inputs = e._gap_context_inputs(make_fuel(5, "maestro:source_budget:maestro/a.py"))
    assert inputs.recent_rejections == ["title rj1: not needed"]
    assert inputs.source_task_candidates == [
        "t-kept (maestro/task/t-kept): build a parser",
    ]
    assert inputs.version_tail == ["Shipped thing"]


_HISTORICAL_HASHES = {
    "capability": "ca2d71970d4cd93035c0acb3b95faffd747fa1131e8d6ffd5347fb733235e204",
    "awareness": "5a4adb6707bd7f8c36cf2c72230b9ee5b61483fd6b0de33a7a188a4b64fac7fe",
    "purpose": "ba75b62c76b9d26f436eb85c210eff2fde0f46e391e1b17891bd8ba501a9f999",
    "infrastructure": "32ea3e13ab629c28fe113cbbab3368b47d7e49a1e5f792ab7bf971b9e3e7d807",
}


@pytest.mark.parametrize("category", sorted(_HISTORICAL_HASHES))
def test_historical_category_hashes_hold(category: str) -> None:
    """A14: widening ``Category`` leaves every pre-existing plan hash intact."""
    from maestro.memory import compute_plan_content_hash

    plan = EvolutionPlan(
        plan_id="h1", title="Hash fixture", category=category,  # type: ignore[arg-type]
        rationale="r", proposed_changes=["c"], files_affected=["maestro/a.py"],
        expected_impact="e",
    )
    assert compute_plan_content_hash(plan) == _HISTORICAL_HASHES[category]


@pytest.mark.parametrize(
    "category",
    ["capability", "awareness", "purpose", "infrastructure", "maintenance"],
)
def test_plan_from_spec_admits_live_categories(
    mem_fs: dict[str, str], category: str,
) -> None:
    """A14: a reply may choose any live ``Category``."""
    e, _m = _engine(mem_fs)
    plan = e._plan_from_spec({"category": category}, default_rationale="r")
    assert plan.category == category


def test_plan_from_spec_refuses_retired_revenue_enabler(
    mem_fs: dict[str, str],
) -> None:
    """A reply choosing the retired ``revenue_enabler`` falls back to capability."""
    e, _m = _engine(mem_fs)
    plan = e._plan_from_spec({"category": "revenue_enabler"}, default_rationale="r")
    assert plan.category == "capability"
