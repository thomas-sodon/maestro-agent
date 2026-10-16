"""Tests for the curation input budget (#4476)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.audit_checks import select_curation_target, warn_over_budget_curation
from maestro.memory import (
    _PLAN_CHANGE_ITEM_MAX_CHARS,
    _PLAN_FILE_PATH_MAX_CHARS,
    _PLAN_FILES_AFFECTED_MAX_ITEMS,
    _PLAN_RATIONALE_MAX_CHARS,
    EvolutionPlan,
    Memory,
)
from maestro.prompts.curate_plan import (
    CURATE_PLAN_INPUT_MAX_CHARS,
    CURATE_PROPOSED_CHANGES_MAX_ITEMS,
    curate_input_fits,
    render_curate_prompt,
)
from maestro.sdk import QueryResult
from maestro.types import (
    ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
    ACCEPTANCE_CRITERIA_MAX_ITEMS,
)
from tests.test_evolution import _make_curate_engine, _seed_pending_plan

_TOO_MANY_CHANGES = ["x" * _PLAN_CHANGE_ITEM_MAX_CHARS] * (
    CURATE_PROPOSED_CHANGES_MAX_ITEMS + 1
)


def _plan(**over: object) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=str(over.get("plan_id", "p")),
        title=str(over.get("title", "t")),
        category="capability",
        rationale=str(over.get("rationale", "r")),
        proposed_changes=list(over.get("proposed_changes", ["c"])),  # type: ignore[call-overload]
        files_affected=list(over.get("files_affected", [])),  # type: ignore[call-overload]
        expected_impact="",
        acceptance_criteria=list(over.get("acceptance_criteria", ["a"])),  # type: ignore[call-overload]
    )


def _stored(m: Memory, plan_id: str = "cp1") -> EvolutionPlan:
    plan = m.get_plan(plan_id)
    assert plan is not None
    return plan


def test_in_budget_plan_fits() -> None:
    assert curate_input_fits(_plan()) is True


def test_maximal_capped_plan_fits() -> None:
    plan = _plan(
        title="t" * 500,
        rationale="r" * _PLAN_RATIONALE_MAX_CHARS,
        proposed_changes=["c" * _PLAN_CHANGE_ITEM_MAX_CHARS]
        * CURATE_PROPOSED_CHANGES_MAX_ITEMS,
        acceptance_criteria=["a" * ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS]
        * ACCEPTANCE_CRITERIA_MAX_ITEMS,
        files_affected=["f" * _PLAN_FILE_PATH_MAX_CHARS]
        * _PLAN_FILES_AFFECTED_MAX_ITEMS,
    )
    assert curate_input_fits(plan) is True


@pytest.mark.parametrize(
    "over",
    [
        {"rationale": "r" * (_PLAN_RATIONALE_MAX_CHARS + 1)},
        {"proposed_changes": _TOO_MANY_CHANGES},
        {"proposed_changes": ["c" * (_PLAN_CHANGE_ITEM_MAX_CHARS + 1)]},
        {"acceptance_criteria": ["a" * (ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS + 1)]},
        {"title": "t" * CURATE_PLAN_INPUT_MAX_CHARS},
        {"files_affected": ["f" * CURATE_PLAN_INPUT_MAX_CHARS]},
    ],
)
def test_over_budget_plan_does_not_fit(over: dict[str, object]) -> None:
    assert curate_input_fits(_plan(**over)) is False


def test_files_affected_counts_toward_summed_budget() -> None:
    """``files_affected`` length is part of the summed input budget."""
    base = CURATE_PLAN_INPUT_MAX_CHARS - len("t") - len("r") - (len("c") + 3) - (
        len("a") + 3
    )
    fits = _plan(files_affected=["f" * (base - 3)])
    over = _plan(files_affected=["f" * (base - 2)])
    assert curate_input_fits(fits) is True
    assert curate_input_fits(over) is False


def test_broad_files_affected_still_fits() -> None:
    """Over-count or long paths alone never exclude a plan (only the sum)."""
    plan = _plan(
        files_affected=["f"] * (_PLAN_FILES_AFFECTED_MAX_ITEMS + 1)
        + ["f" * (_PLAN_FILE_PATH_MAX_CHARS + 1)],
    )
    assert curate_input_fits(plan) is True


def test_render_curate_prompt_rejects_over_budget_plan() -> None:
    with pytest.raises(ValueError, match="curation input budget"):
        render_curate_prompt(_plan(proposed_changes=_TOO_MANY_CHANGES))


@pytest.mark.asyncio
@pytest.mark.parametrize("single_use", [False, True])
async def test_oversized_change_list_is_not_sent_to_sdk(
    mem_fs: dict[str, str], single_use: bool,
) -> None:
    e, m, _n = _make_curate_engine(mem_fs)
    e._config.sdk_single_use_evolution_calls_enabled = single_use
    _seed_pending_plan(m, proposed_changes=_TOO_MANY_CHANGES)
    mock_q = AsyncMock(return_value=QueryResult(response='{"action": "keep"}'))
    mock_s = AsyncMock(return_value=QueryResult(response='{"action": "keep"}'))
    with (
        patch("maestro.evolution.query", mock_q),
        patch("maestro.evolution.single_use_call", mock_s),
    ):
        outcome = await e.curate_pending_plans()
    assert outcome is False
    mock_q.assert_not_awaited()
    mock_s.assert_not_awaited()
    assert _stored(m).proposed_changes == _TOO_MANY_CHANGES


def test_warn_over_budget_names_only_over_budget_pending(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plans = [
        _plan(plan_id="ok"),
        _plan(plan_id="big", proposed_changes=_TOO_MANY_CHANGES),
    ]
    for p in plans:
        p.status = "pending_review"
    with caplog.at_level(logging.WARNING, logger="maestro.audit_checks"):
        assert warn_over_budget_curation(plans) == ["big"]
    assert "over the input budget, left for human review: big" in caplog.text


def test_warn_over_budget_silent_when_all_fit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plan = _plan()
    plan.status = "pending_review"
    with caplog.at_level(logging.WARNING, logger="maestro.audit_checks"):
        assert warn_over_budget_curation([plan]) == []
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_agent_phase_warns_when_only_over_budget_plans_pend(
    make_agent, caplog: pytest.LogCaptureFixture,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    big = _plan(plan_id="big", proposed_changes=_TOO_MANY_CHANGES)
    big.status = "pending_review"
    agent.memory.get_all_plans = MagicMock(return_value=[big])
    agent.evolution.curate_pending_plans = AsyncMock()
    with caplog.at_level(logging.WARNING, logger="maestro.audit_checks"):
        result = await agent._curate_pending_plans(session=None)
    assert result is None
    agent.evolution.curate_pending_plans.assert_not_awaited()
    assert "left for human review: big" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "over",
    [
        {"rationale": "r" * (_PLAN_RATIONALE_MAX_CHARS + 1)},
        {"acceptance_criteria": ["a" * (ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS + 1)]},
    ],
)
async def test_oversized_legacy_field_is_not_sent_to_sdk(
    mem_fs: dict[str, str], over: dict[str, object],
) -> None:
    e, m, _n = _make_curate_engine(mem_fs)
    _seed_pending_plan(m, **over)
    before = _stored(m)
    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q:
        outcome = await e.curate_pending_plans()
    assert outcome is False
    mock_q.assert_not_awaited()
    after = _stored(m)
    assert after.rationale == before.rationale
    assert after.acceptance_criteria == before.acceptance_criteria
    assert after.status == "pending_review"


def test_oversized_feedback_plan_does_not_starve_backlog(
    mem_fs: dict[str, str],
) -> None:
    _e, m, _n = _make_curate_engine(mem_fs)
    _seed_pending_plan(m, "big", proposed_changes=_TOO_MANY_CHANGES)
    _seed_pending_plan(m, "peer", created_at="2026-06-01T00:00:00+00:00")
    big = _stored(m, "big")
    assert m.add_plan_feedback(
        "big", "tighten it", source="dashboard",
        content_hash_seen=big.plan_content_hash,
    )
    target = select_curation_target(m.get_all_plans(), prioritize_feedback=True)
    assert target is not None
    assert target.plan_id == "peer"
