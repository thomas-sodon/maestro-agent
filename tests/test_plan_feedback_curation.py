"""Tests for the curator answering reviewer feedback (proposal 145)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.audit_checks import select_curation_target
from maestro.memory import (
    _OBSOLETE_REASON_MAX_CHARS,
    _PLAN_CHANGE_ITEM_MAX_CHARS,
    _PLAN_FILE_PATH_MAX_CHARS,
    _PLAN_FILES_AFFECTED_MAX_ITEMS,
    _PLAN_RATIONALE_MAX_CHARS,
    EvolutionPlan,
    Memory,
)
from maestro.notifications.base import NotifierTransportError
from maestro.plan_feedback import (
    FEEDBACK_ENTRIES_MAX_CHARS,
    FEEDBACK_RESPONSE_MAX_CHARS,
    FEEDBACK_TEXT_MAX_CHARS,
    FEEDBACK_UNSERVICEABLE_RESPONSE,
    MAX_FEEDBACK_CURATE_ATTEMPTS,
    MAX_UNADDRESSED_FEEDBACK,
    PlanFeedback,
    render_feedback_section,
)
from maestro.plan_feedback_curation import notify_feedback_response
from maestro.prompts.curate_plan import (
    CURATE_FEEDBACK_SECTION,
    CURATE_PLAN_PROMPT,
    render_curate_prompt,
)
from maestro.sdk import QueryResult
from maestro.types import (
    ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
    ACCEPTANCE_CRITERIA_MAX_ITEMS,
)
from tests.test_evolution import _make_curate_engine, _seed_pending_plan


def _verdict(**data: Any) -> QueryResult:
    return QueryResult(response=json.dumps(data))


def _setup(
    mem_fs: dict[str, str], *, flag: bool = True, feedback: bool = True,
) -> tuple[Any, Memory, MagicMock, EvolutionPlan]:
    e, m, notifier = _make_curate_engine(mem_fs)
    e._config.loop_plan_feedback_enabled = flag
    _seed_pending_plan(m)
    if feedback:
        plan = m.get_plan("cp1")
        assert plan is not None
        assert m.add_plan_feedback(
            "cp1", "tighten the criteria", source="dashboard",
            content_hash_seen=plan.plan_content_hash,
        )
    plan = m.get_plan("cp1")
    assert plan is not None
    return e, m, notifier, plan


async def _curate(e: Any, result: QueryResult | Any) -> tuple[bool, AsyncMock]:
    mock_q = AsyncMock(side_effect=result) if callable(result) else AsyncMock(
        return_value=result,
    )
    with patch("maestro.evolution.query", mock_q):
        outcome = await e.curate_pending_plans()
    return outcome, mock_q


def _feedback(m: Memory) -> list[PlanFeedback]:
    plan = m.get_plan("cp1")
    assert plan is not None
    return plan.reviewer_feedback


def _legacy_render(plan: EvolutionPlan) -> str:
    return CURATE_PLAN_PROMPT.format(
        plan_id=plan.plan_id,
        title=plan.title,
        created_at=plan.created_at,
        rationale=plan.rationale or "(none)",
        proposed_changes="\n".join(f"- {c}" for c in plan.proposed_changes) or "(none)",
        acceptance_criteria=(
            "\n".join(f"- {c}" for c in plan.acceptance_criteria) or "(none)"
        ),
        files_affected="\n".join(f"- {f}" for f in plan.files_affected) or "(none)",
        file_path_max_chars=_PLAN_FILE_PATH_MAX_CHARS,
        files_affected_max_items=_PLAN_FILES_AFFECTED_MAX_ITEMS,
        rationale_max_chars=_PLAN_RATIONALE_MAX_CHARS,
        change_item_max_chars=_PLAN_CHANGE_ITEM_MAX_CHARS,
        acceptance_criteria_item_max_chars=ACCEPTANCE_CRITERIA_ITEM_MAX_CHARS,
        obsolete_reason_max_chars=_OBSOLETE_REASON_MAX_CHARS,
        acceptance_criteria_max_items=ACCEPTANCE_CRITERIA_MAX_ITEMS,
    )


# ── golden prompt ────────────────────────────────────────────────────


def test_render_curate_prompt_matches_legacy_inline_format(
    mem_fs: dict[str, str],
) -> None:
    _e, _m, _n, plan = _setup(mem_fs, feedback=False)
    assert render_curate_prompt(plan) == _legacy_render(plan)


def test_render_curate_prompt_empty_fields_render_none() -> None:
    plan = EvolutionPlan(
        plan_id="p", title="t", category="capability",
        rationale="", proposed_changes=[], files_affected=[],
        expected_impact="",
    )
    assert render_curate_prompt(plan) == _legacy_render(plan)


@pytest.mark.asyncio
async def test_flag_off_prompt_unchanged(mem_fs: dict[str, str]) -> None:
    e, m, _n, plan = _setup(mem_fs, flag=False)
    _out, mock_q = await _curate(e, _verdict(action="keep"))
    assert mock_q.call_args.kwargs["prompt"] == _legacy_render(plan)
    assert _feedback(m)[0].addressed_at is None


@pytest.mark.asyncio
async def test_addressed_feedback_prompt_unchanged(mem_fs: dict[str, str]) -> None:
    e, m, _n, plan = _setup(mem_fs)
    assert m.mark_feedback_addressed("cp1", [plan.reviewer_feedback[0].feedback_id], "ok")
    plan = m.get_plan("cp1")
    assert plan is not None
    _out, mock_q = await _curate(e, _verdict(action="keep"))
    assert mock_q.call_args.kwargs["prompt"] == _legacy_render(plan)


@pytest.mark.asyncio
async def test_feedback_section_appended(mem_fs: dict[str, str]) -> None:
    e, _m, _n, plan = _setup(mem_fs)
    _out, mock_q = await _curate(e, _verdict(action="keep", response="fine"))
    prompt = mock_q.call_args.kwargs["prompt"]
    assert prompt.startswith(_legacy_render(plan))
    assert "Reviewer feedback — address it" in prompt
    assert "tighten the criteria" in prompt


# ── verdicts ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refine_with_feedback(mem_fs: dict[str, str]) -> None:
    e, m, notifier, _plan = _setup(mem_fs)
    order = MagicMock()
    order.attach_mock(notifier.notify, "notify")
    order.attach_mock(notifier.notify_plan_refined, "refined")
    out, _q = await _curate(e, _verdict(
        action="refine", acceptance_criteria=["A test asserts the hit"],
        response="Tightened the criteria.",
    ))
    assert out is True
    refined = m.get_plan("cp1")
    assert refined is not None
    assert refined.acceptance_criteria == ["A test asserts the hit"]
    entry = refined.reviewer_feedback[0]
    assert entry.addressed_at is not None
    assert entry.response == "Tightened the criteria."
    names = [c[0] for c in order.mock_calls]
    assert names == ["notify", "refined"]
    assert "Tightened the criteria." in notifier.notify.call_args.args[0]
    refined_msg = notifier.notify_plan_refined.call_args.args[1]
    assert refined_msg.startswith("Refined — updated fields: acceptance_criteria")


@pytest.mark.asyncio
async def test_files_affected_only_refine_addresses_feedback(
    mem_fs: dict[str, str],
) -> None:
    """Scope-correcting feedback answered by a ``files_affected``-only refine
    is marked addressed, not counted as a failed attempt (#4520)."""
    e, m, _n, plan = _setup(mem_fs)
    out, _q = await _curate(e, _verdict(
        action="refine", files_affected=["maestro/memory.py"],
        response="Dropped maestro/cli.py from the scope.",
    ))
    assert out is True
    refined = m.get_plan("cp1")
    assert refined is not None
    assert refined.files_affected == ["maestro/memory.py"]
    assert refined.plan_content_hash != plan.plan_content_hash
    entry = refined.reviewer_feedback[0]
    assert entry.addressed_at is not None
    assert entry.curate_attempts == 0


@pytest.mark.asyncio
async def test_keep_with_feedback(mem_fs: dict[str, str]) -> None:
    e, m, notifier, plan = _setup(mem_fs)
    out, _q = await _curate(e, _verdict(action="keep", response="Already tight."))
    assert out is False
    after = m.get_plan("cp1")
    assert after is not None
    assert after.plan_content_hash == plan.plan_content_hash
    assert after.reviewer_feedback[0].response == "Already tight."
    assert after.last_curated_at is not None
    notifier.notify.assert_awaited_once()
    notifier.notify_plan_refined.assert_not_awaited()


@pytest.mark.asyncio
async def test_obsolete_downgraded_to_keep(mem_fs: dict[str, str]) -> None:
    e, m, _n, _plan = _setup(mem_fs)
    with patch.object(m, "mark_plan_obsolete") as spy:
        out, _q = await _curate(e, _verdict(
            action="obsolete", reason="stale", response="Keeping it per your note.",
        ))
    assert out is False
    spy.assert_not_called()
    after = m.get_plan("cp1")
    assert after is not None
    assert after.status == "pending_review"
    assert after.reviewer_feedback[0].response == "Keeping it per your note."


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    None, "", "   ", 7, "x" * (FEEDBACK_RESPONSE_MAX_CHARS + 1),
])
async def test_missing_or_bad_response_counts_attempt(
    mem_fs: dict[str, str], response: object,
) -> None:
    e, m, notifier, plan = _setup(mem_fs)
    data: dict[str, Any] = {"action": "refine", "rationale": "new"}
    if response is not None:
        data["response"] = response
    out, _q = await _curate(e, _verdict(**data))
    assert out is False
    after = m.get_plan("cp1")
    assert after is not None
    assert after.rationale == plan.rationale
    assert after.reviewer_feedback[0].addressed_at is None
    assert after.reviewer_feedback[0].curate_attempts == 1
    notifier.notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    "not json at all", "[1, 2]", '{"action": "nope", "response": "r"}',
    '{"action": [], "response": "r"}', '{"action": {}, "response": "r"}',
    '{"action": 1, "response": "r"}',
])
async def test_malformed_verdict_counts_attempt(
    mem_fs: dict[str, str], raw: str,
) -> None:
    e, m, _n, _plan = _setup(mem_fs)
    out, _q = await _curate(e, QueryResult(response=raw))
    assert out is False
    assert _feedback(m)[0].curate_attempts == 1


@pytest.mark.asyncio
async def test_refine_not_landing_counts_attempt(mem_fs: dict[str, str]) -> None:
    e, m, notifier, _plan = _setup(mem_fs)
    out, _q = await _curate(e, _verdict(
        action="refine", rationale="x" * (_PLAN_RATIONALE_MAX_CHARS + 1),
        response="Rewrote it.",
    ))
    assert out is False
    entry = _feedback(m)[0]
    assert entry.addressed_at is None
    assert entry.curate_attempts == 1
    notifier.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_three_strike_release(mem_fs: dict[str, str]) -> None:
    e, m, notifier, _plan = _setup(mem_fs)
    for _ in range(MAX_FEEDBACK_CURATE_ATTEMPTS):
        await _curate(e, QueryResult(response="garbage"))
    entry = _feedback(m)[0]
    assert entry.addressed_at is not None
    assert entry.response == FEEDBACK_UNSERVICEABLE_RESPONSE
    notifier.notify.assert_awaited_once()
    assert FEEDBACK_UNSERVICEABLE_RESPONSE in notifier.notify.call_args.args[0]
    # The plan no longer jumps the grace floor once released.
    target = select_curation_target(
        m.get_all_plans(), now=datetime.now(UTC), min_age_hours=10_000.0,
        prioritize_feedback=True,
    )
    assert target is None


@pytest.mark.asyncio
async def test_unhashable_action_three_strike_release(
    mem_fs: dict[str, str],
) -> None:
    e, m, notifier, plan = _setup(mem_fs)
    for _ in range(MAX_FEEDBACK_CURATE_ATTEMPTS):
        await _curate(e, QueryResult(response='{"action": [], "response": "r"}'))
    after = m.get_plan("cp1")
    assert after is not None
    assert after.plan_content_hash == plan.plan_content_hash
    entry = after.reviewer_feedback[0]
    assert entry.addressed_at is not None
    assert entry.response == FEEDBACK_UNSERVICEABLE_RESPONSE
    notifier.notify.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    {"rationale": ""}, {"proposed_changes": "not a list"},
])
async def test_invalid_refine_field_counts_attempt(
    mem_fs: dict[str, str], field: dict[str, Any],
) -> None:
    e, m, notifier, plan = _setup(mem_fs)
    out, _q = await _curate(e, _verdict(action="refine", response="Done.", **field))
    assert out is False
    after = m.get_plan("cp1")
    assert after is not None
    assert after.plan_content_hash == plan.plan_content_hash
    entry = after.reviewer_feedback[0]
    assert entry.addressed_at is None
    assert entry.curate_attempts == 1
    notifier.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_is_per_entry_budget(mem_fs: dict[str, str]) -> None:
    e, m, notifier, plan = _setup(mem_fs)
    for _ in range(MAX_FEEDBACK_CURATE_ATTEMPTS - 1):
        await _curate(e, QueryResult(response="garbage"))
    assert m.add_plan_feedback(
        "cp1", "newer note", source="dashboard",
        content_hash_seen=plan.plan_content_hash,
    )
    await _curate(e, QueryResult(response="garbage"))
    old, new = _feedback(m)
    assert old.response == FEEDBACK_UNSERVICEABLE_RESPONSE
    assert new.addressed_at is None
    assert new.curate_attempts == 1
    notifier.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_keep_on_plan_left_pending_skips_notify(
    mem_fs: dict[str, str],
) -> None:
    e, m, notifier, _plan = _setup(mem_fs)

    async def _side_effect(**_kw: Any) -> QueryResult:
        assert m.mark_plan_obsolete("cp1", "gone")
        return _verdict(action="keep", response="Answered.")

    out, _q = await _curate(e, _side_effect)
    assert out is False
    notifier.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_mid_call_feedback_stays_open(mem_fs: dict[str, str]) -> None:
    e, m, _n, plan = _setup(mem_fs)

    async def _side_effect(**_kw: Any) -> QueryResult:
        assert m.add_plan_feedback(
            "cp1", "late note", source="telegram",
            content_hash_seen=plan.plan_content_hash,
        )
        return _verdict(action="keep", response="Answered.")

    await _curate(e, _side_effect)
    entries = _feedback(m)
    assert entries[0].addressed_at is not None
    assert entries[1].text == "late note"
    assert entries[1].addressed_at is None


@pytest.mark.asyncio
async def test_failure_on_vanished_plan_skips_release(mem_fs: dict[str, str]) -> None:
    e, m, notifier, _plan = _setup(mem_fs)

    async def _side_effect(**_kw: Any) -> QueryResult:
        assert m.mark_plan_obsolete("cp1", "gone")
        return QueryResult(response="garbage")

    out, _q = await _curate(e, _side_effect)
    assert out is False
    notifier.notify.assert_not_awaited()


# ── section rendering ────────────────────────────────────────────────


def test_stale_hash_entry_flagged_not_dropped(mem_fs: dict[str, str]) -> None:
    _e, _m, _n, plan = _setup(mem_fs, feedback=False)
    entries = [
        PlanFeedback(feedback_id="a", text="old note", content_hash_seen="deadbeef"),
        PlanFeedback(
            feedback_id="b", text="fresh note",
            content_hash_seen=plan.plan_content_hash[:8],
        ),
        PlanFeedback(feedback_id="c", text="no hash"),
    ]
    section = render_feedback_section(plan, entries)
    assert "old note" in section
    assert "fresh note" in section
    assert section.count("written against an earlier version") == 1
    assert "(unknown)" in section
    assert "obsolete\" is NOT allowed" in section


def test_section_within_cap(mem_fs: dict[str, str]) -> None:
    _e, _m, _n, plan = _setup(mem_fs, feedback=False)
    entries = [
        PlanFeedback(
            feedback_id=str(i), text="y" * FEEDBACK_TEXT_MAX_CHARS,
            submitted_at="2026-10-03T00:00:00+00:00", content_hash_seen="dead",
        )
        for i in range(MAX_UNADDRESSED_FEEDBACK + 1)
    ]
    section = render_feedback_section(plan, entries)
    assert len(section) <= len(CURATE_FEEDBACK_SECTION) + FEEDBACK_ENTRIES_MAX_CHARS
    # The block cap never clips a max-length entry's text.
    assert section.count("y" * FEEDBACK_TEXT_MAX_CHARS) == MAX_UNADDRESSED_FEEDBACK


def test_section_caps_oversized_timestamp(mem_fs: dict[str, str]) -> None:
    _e, _m, _n, plan = _setup(mem_fs, feedback=False)
    entries = [
        PlanFeedback(
            feedback_id=str(i), text="y" * FEEDBACK_TEXT_MAX_CHARS,
            submitted_at="t" * 5000, content_hash_seen="dead",
        )
        for i in range(MAX_UNADDRESSED_FEEDBACK)
    ]
    section = render_feedback_section(plan, entries)
    assert section.count("y" * FEEDBACK_TEXT_MAX_CHARS) == MAX_UNADDRESSED_FEEDBACK


# ── memory door + notifier ───────────────────────────────────────────


def test_record_attempt_increments_only_given_unaddressed(
    mem_fs: dict[str, str],
) -> None:
    _e, m, _n, plan = _setup(mem_fs)
    assert m.add_plan_feedback(
        "cp1", "second", source="dashboard", content_hash_seen=plan.plan_content_hash,
    )
    first, _second = _feedback(m)
    assert m.record_feedback_attempt("cp1", [first.feedback_id]) == {
        first.feedback_id: 1,
    }
    assert m.record_feedback_attempt("cp1", [first.feedback_id]) == {
        first.feedback_id: 2,
    }
    assert m.record_feedback_attempt("cp1", ["unknown"]) == {}
    after_first, after_second = _feedback(m)
    assert after_first.curate_attempts == 2
    assert after_second.curate_attempts == 0
    assert m.mark_feedback_addressed("cp1", [first.feedback_id], "done")
    assert m.record_feedback_attempt("cp1", [first.feedback_id]) == {}


def test_record_attempt_refuses_non_pending_and_missing(
    mem_fs: dict[str, str],
) -> None:
    _e, m, _n, _plan = _setup(mem_fs)
    assert m.record_feedback_attempt("missing", ["x"]) is None
    assert m.mark_plan_obsolete("cp1", "gone")
    assert m.record_feedback_attempt("cp1", ["x"]) is None


@pytest.mark.asyncio
async def test_notify_response_absorbs_transport_error() -> None:
    notifier = MagicMock()
    notifier.notify = AsyncMock(side_effect=NotifierTransportError("down"))
    await notify_feedback_response(notifier, "hi")
    notifier.notify.assert_awaited_once_with("hi")


# ---------------------------------------------------------------------------
# Proposal 155 step 6 — serving feedback does not buy a second pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_with_feedback_then_skipped(mem_fs: dict[str, str]) -> None:
    e, m, _n, _plan = _setup(mem_fs)
    _out, q = await _curate(e, _verdict(action="keep", response="Fine as is."))
    assert q.await_count == 1
    assert _feedback(m)[0].addressed_at is not None
    assert select_curation_target(m.get_all_plans(), prioritize_feedback=True) is None


@pytest.mark.asyncio
async def test_malformed_feedback_reserved_until_release_then_skipped(
    mem_fs: dict[str, str],
) -> None:
    e, m, _n, _plan = _setup(mem_fs)
    for _ in range(MAX_FEEDBACK_CURATE_ATTEMPTS):
        target = select_curation_target(m.get_all_plans(), prioritize_feedback=True)
        assert target is not None
        assert target.plan_id == "cp1"
        await _curate(e, QueryResult(response="garbage"))
    assert _feedback(m)[0].addressed_at is not None
    assert select_curation_target(m.get_all_plans(), prioritize_feedback=True) is None
