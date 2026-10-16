"""Tests for the reviewer-feedback ledger on pending plans (proposal 145)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import audit_checks
from maestro.audit_checks import select_curation_target
from maestro.memory import EvolutionPlan, Memory, compute_plan_content_hash
from maestro.notifications.base import CALLBACK_HASH_PREFIX_LEN
from maestro.plan_feedback import (
    FEEDBACK_RESPONSE_MAX_CHARS,
    FEEDBACK_TEXT_MAX_CHARS,
    MAX_ADDRESSED_FEEDBACK_KEPT,
    MAX_UNADDRESSED_FEEDBACK,
    PlanFeedback,
    PlanRef,
    any_pending_feedback,
    coerce_feedback,
    format_plan_ref,
    has_unaddressed_feedback,
    parse_plan_ref,
)
from tests.test_evolution import _make_curate_engine

FAKE_DIR = Path("/fake/mem")
HASH = "a" * 64


def _plan(
    plan_id: str = "plan-1",
    *,
    status: str = "pending_review",
    created_at: str = "2025-01-01T00:00:00+00:00",
    refined_at: str | None = None,
    feedback: list[PlanFeedback] | None = None,
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=f"Plan {plan_id}",
        category="capability",
        rationale="because",
        proposed_changes=["change one"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status=status,  # type: ignore[arg-type]
        created_at=created_at,
        refined_at=refined_at,
        reviewer_feedback=list(feedback or []),
    )


def _memory(mem_fs: dict[str, str], *plans: EvolutionPlan) -> Memory:
    m = Memory(FAKE_DIR)
    for p in plans:
        m.save_evolution_plan(p)
    return m


def _add(m: Memory, plan_id: str = "plan-1", text: str = "drop step 2") -> bool:
    return m.add_plan_feedback(
        plan_id, text, source="dashboard", content_hash_seen=HASH,
    )


# ── write door ───────────────────────────────────────────────────────


def test_add_feedback_persists_entry_and_keeps_hash(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    before = compute_plan_content_hash(m.get_plan("plan-1"))  # type: ignore[arg-type]

    assert _add(m, text="  drop step 2  ") is True

    plan = m.get_plan("plan-1")
    assert plan is not None
    assert len(plan.reviewer_feedback) == 1
    entry = plan.reviewer_feedback[0]
    assert entry.text == "drop step 2"
    assert entry.source == "dashboard"
    assert entry.addressed_at is None
    assert entry.submitted_at != ""
    assert entry.content_hash_seen == HASH[:CALLBACK_HASH_PREFIX_LEN]
    assert compute_plan_content_hash(plan) == before
    assert plan.refined_at is None


def test_add_feedback_telegram_source(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    assert m.add_plan_feedback(
        "plan-1", "x", source="telegram", content_hash_seen="abc",
    ) is True
    plan = m.get_plan("plan-1")
    assert plan is not None
    assert plan.reviewer_feedback[0].source == "telegram"
    assert plan.reviewer_feedback[0].content_hash_seen == "abc"


def test_add_feedback_refuses_non_pending_plan(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan(status="approved"))
    raw_before = mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")]
    assert _add(m) is False
    assert mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] == raw_before


def test_add_feedback_refuses_missing_plan(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    assert _add(m, plan_id="nope") is False
    plan = m.get_plan("plan-1")
    assert plan is not None
    assert plan.reviewer_feedback == []


def test_add_feedback_refuses_past_unaddressed_cap(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    for i in range(MAX_UNADDRESSED_FEEDBACK):
        assert _add(m, text=f"note {i}") is True
    assert _add(m, text="one too many") is False
    plan = m.get_plan("plan-1")
    assert plan is not None
    assert len(plan.reviewer_feedback) == MAX_UNADDRESSED_FEEDBACK


@pytest.mark.parametrize("text", ["", "   ", "x" * (FEEDBACK_TEXT_MAX_CHARS + 1)])
def test_add_feedback_rejects_bad_text(mem_fs: dict[str, str], text: str) -> None:
    m = _memory(mem_fs, _plan())
    with pytest.raises(ValueError):
        _add(m, text=text)


def test_add_feedback_rejects_non_str_text(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    with pytest.raises(TypeError):
        m.add_plan_feedback(
            "plan-1", 42, source="dashboard", content_hash_seen=HASH,  # type: ignore[arg-type]
        )


def test_add_feedback_rejects_unknown_source(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    with pytest.raises(ValueError):
        m.add_plan_feedback("plan-1", "x", source="email", content_hash_seen=HASH)


# ── addressed stamp ──────────────────────────────────────────────────


def test_mark_addressed_stamps_only_given_ids(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    _add(m, text="first")
    plan = m.get_plan("plan-1")
    assert plan is not None
    first_id = plan.reviewer_feedback[0].feedback_id
    _add(m, text="arrived mid-curation")

    assert m.mark_feedback_addressed("plan-1", [first_id], "done") is True

    plan = m.get_plan("plan-1")
    assert plan is not None
    first, later = plan.reviewer_feedback
    assert first.addressed_at is not None
    assert first.response == "done"
    assert later.addressed_at is None
    assert later.response == ""
    assert has_unaddressed_feedback(plan) is True


def test_mark_addressed_prunes_old_addressed(mem_fs: dict[str, str]) -> None:
    old = [
        PlanFeedback(
            feedback_id=f"f{i}", text="t", addressed_at="2025-01-01T00:00:00+00:00",
        )
        for i in range(MAX_ADDRESSED_FEEDBACK_KEPT)
    ]
    fresh = PlanFeedback(feedback_id="new", text="t")
    m = _memory(mem_fs, _plan(feedback=[*old, fresh]))

    assert m.mark_feedback_addressed("plan-1", ["new"], "ok") is True

    plan = m.get_plan("plan-1")
    assert plan is not None
    ids = [f.feedback_id for f in plan.reviewer_feedback]
    assert len(ids) == MAX_ADDRESSED_FEEDBACK_KEPT
    assert ids[0] == "f1"
    assert ids[-1] == "new"


def test_mark_addressed_prunes_by_addressed_order(mem_fs: dict[str, str]) -> None:
    # Submission order puts the just-answered entry first; it must survive.
    first = PlanFeedback(feedback_id="first", text="t")
    later = [
        PlanFeedback(
            feedback_id=f"f{i}", text="t", addressed_at=f"2025-01-0{i + 1}T00:00:00+00:00",
        )
        for i in range(MAX_ADDRESSED_FEEDBACK_KEPT)
    ]
    m = _memory(mem_fs, _plan(feedback=[first, *later]))

    assert m.mark_feedback_addressed("plan-1", ["first"], "ok") is True

    plan = m.get_plan("plan-1")
    assert plan is not None
    ids = [f.feedback_id for f in plan.reviewer_feedback]
    assert ids == ["first", "f1", "f2"]


def test_coerce_empty_addressed_at_is_unaddressed() -> None:
    [entry] = coerce_feedback([{"feedback_id": "a", "text": "t", "addressed_at": ""}])
    assert entry.addressed_at is None


def test_selector_undated_feedback_sorts_last() -> None:
    dated = PlanFeedback(feedback_id="d", text="t", submitted_at="2025-05-01T00:00:00Z")
    undated = PlanFeedback(feedback_id="u", text="t", submitted_at="")
    plans = [_plan("a", feedback=[undated]), _plan("b", feedback=[dated])]
    target = select_curation_target(plans, prioritize_feedback=True)
    assert target is not None
    assert target.plan_id == "b"


def test_mark_addressed_refuses_non_pending_and_missing(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan(status="approved"))
    assert m.mark_feedback_addressed("plan-1", ["x"], "r") is False
    assert m.mark_feedback_addressed("nope", ["x"], "r") is False


def test_mark_addressed_rejects_over_cap_response(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    with pytest.raises(ValueError):
        m.mark_feedback_addressed(
            "plan-1", ["x"], "r" * (FEEDBACK_RESPONSE_MAX_CHARS + 1),
        )


# ── coercion & round-trip ────────────────────────────────────────────


def test_coerce_non_list_is_empty() -> None:
    assert coerce_feedback(None) == []
    assert coerce_feedback({"feedback_id": "a", "text": "b"}) == []


def test_coerce_drops_junk_items() -> None:
    raw = ["str", 3, {"text": "no id"}, {"feedback_id": "a"}, {"feedback_id": 1, "text": "t"}]
    assert coerce_feedback(raw) == []


def test_coerce_wrong_typed_fields_default() -> None:
    [entry] = coerce_feedback([{
        "feedback_id": "a",
        "text": "t",
        "submitted_at": 5,
        "source": "email",
        "content_hash_seen": 7,
        "curate_attempts": "two",
        "addressed_at": 9,
        "response": [],
    }])
    assert entry == PlanFeedback(feedback_id="a", text="t")


@pytest.mark.parametrize(
    ("raw", "expected"), [(2.0, 2), (2.5, 0), (True, 0), (-1, 0), (4, 4)],
)
def test_coerce_curate_attempts(raw: object, expected: int) -> None:
    [entry] = coerce_feedback([{"feedback_id": "a", "text": "t", "curate_attempts": raw}])
    assert entry.curate_attempts == expected


def test_coerce_truncates_hash_prefix() -> None:
    [entry] = coerce_feedback([{"feedback_id": "a", "text": "t", "content_hash_seen": HASH}])
    assert entry.content_hash_seen == HASH[:CALLBACK_HASH_PREFIX_LEN]


@pytest.mark.parametrize("raw", [[], {}, ["telegram"], {"a": 1}])
def test_coerce_unhashable_source_defaults(raw: object) -> None:
    result = coerce_feedback([{"feedback_id": "a", "text": "t", "source": raw}])
    assert result == [PlanFeedback(feedback_id="a", text="t")]


def test_plan_with_unhashable_feedback_source_still_loads(mem_fs: dict[str, str]) -> None:
    bad = asdict(_plan("plan-1"))
    bad["reviewer_feedback"] = [{"feedback_id": "f1", "text": "Please refine", "source": []}]
    good = asdict(_plan("plan-2"))
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = (
        json.dumps(bad) + "\n" + json.dumps(good) + "\n"
    )
    m = Memory(FAKE_DIR)

    plans = m.get_all_plans()

    assert [p.plan_id for p in plans] == ["plan-1", "plan-2"]
    assert plans[0].reviewer_feedback[0].source == "dashboard"
    loaded = m.get_plan("plan-1")
    assert loaded is not None
    assert loaded.reviewer_feedback[0].source == "dashboard"


def test_legacy_row_loads_empty_ledger() -> None:
    row = asdict(_plan())
    del row["reviewer_feedback"]
    plan = Memory._parse_plan_line(json.dumps(row))
    assert plan is not None
    assert plan.reviewer_feedback == []


def test_round_trip_preserves_every_field() -> None:
    entry = PlanFeedback(
        feedback_id="a",
        text="t",
        submitted_at="2025-01-01T00:00:00+00:00",
        source="telegram",
        content_hash_seen="abcd",
        curate_attempts=2,
        addressed_at="2025-01-02T00:00:00+00:00",
        response="done",
    )
    plan = Memory._parse_plan_line(json.dumps(asdict(_plan(feedback=[entry]))))
    assert plan is not None
    assert plan.reviewer_feedback == [entry]


# ── selector priority ────────────────────────────────────────────────


NOW = datetime(2026, 10, 3, tzinfo=UTC)


def _fixture() -> list[EvolutionPlan]:
    recent = (NOW - timedelta(minutes=1)).isoformat()
    fb = PlanFeedback(feedback_id="f", text="t", submitted_at=recent)
    return [
        _plan("old", created_at="2025-01-01T00:00:00+00:00"),
        _plan("fb", created_at=recent, refined_at=recent, feedback=[fb]),
    ]


def test_selector_prioritizes_feedback_past_grace() -> None:
    target = select_curation_target(
        _fixture(), now=NOW, min_age_hours=24.0, prioritize_feedback=True,
    )
    assert target is not None
    assert target.plan_id == "fb"


def test_selector_flag_off_keeps_todays_choice() -> None:
    target = select_curation_target(_fixture(), now=NOW, min_age_hours=24.0)
    assert target is not None
    assert target.plan_id == "old"


def test_selector_no_feedback_identical_in_both_modes() -> None:
    plans = [
        _plan("b", created_at="2025-02-01T00:00:00+00:00"),
        _plan("a", created_at="2025-03-01T00:00:00+00:00", refined_at="x"),
        _plan("c", created_at="2025-01-01T00:00:00+00:00", status="approved"),
    ]
    on = select_curation_target(plans, now=NOW, prioritize_feedback=True)
    off = select_curation_target(plans, now=NOW)
    assert on is off


def test_selector_addressed_feedback_does_not_prioritize() -> None:
    done = PlanFeedback(feedback_id="f", text="t", addressed_at="2025-01-01")
    plans = [
        _plan("old", created_at="2025-01-01T00:00:00+00:00"),
        _plan("fb", created_at="2025-06-01T00:00:00+00:00", feedback=[done]),
    ]
    target = select_curation_target(plans, prioritize_feedback=True)
    assert target is not None
    assert target.plan_id == "old"


def test_selector_oldest_unaddressed_feedback_wins() -> None:
    def fb(at: str) -> PlanFeedback:
        return PlanFeedback(feedback_id=at, text="t", submitted_at=at)

    plans = [
        _plan("p1", feedback=[fb("2026-10-02")]),
        _plan("p2", feedback=[fb("2026-10-03"), fb("2026-10-01")]),
        _plan("p3", status="approved", feedback=[fb("2026-09-01")]),
    ]
    target = select_curation_target(plans, prioritize_feedback=True)
    assert target is not None
    assert target.plan_id == "p2"


# ── caller wiring ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_passes_flag_to_selector(mem_fs: dict[str, str]) -> None:
    e, _m, _n = _make_curate_engine(mem_fs)
    e._config.loop_plan_feedback_enabled = True
    spy = MagicMock(return_value=None)
    with patch("maestro.evolution.select_curation_target", spy):
        assert await e.curate_pending_plans() is False
    assert spy.call_args.kwargs["prioritize_feedback"] is True


@pytest.mark.asyncio
async def test_agent_precheck_passes_flag_to_selector(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.loop_plan_feedback_enabled = True
    agent.memory.get_all_plans = MagicMock(return_value=[])  # type: ignore[method-assign]
    agent.evolution.curate_pending_plans = AsyncMock()  # type: ignore[method-assign]
    spy = MagicMock(wraps=audit_checks.select_curation_target)
    with patch("maestro.agent.select_curation_target", spy):
        assert await agent._curate_pending_plans(session=None) is None
    assert spy.call_args.kwargs["prioritize_feedback"] is True
    agent.evolution.curate_pending_plans.assert_not_awaited()


def test_feedback_flag_note_names_curator_readers() -> None:
    from maestro.flag_lifecycle import EXPERIMENTAL_FLAGS

    carriers = [f.flag for f in EXPERIMENTAL_FLAGS if "curate_with_feedback" in f.note]
    assert carriers == ["loop_plan_feedback_enabled"]
    note = next(f.note for f in EXPERIMENTAL_FLAGS if f.flag == carriers[0])
    assert "Do not enable" not in note


# ── any_pending_feedback (proposal 145 step 5) ────────────────────────


def _fb_plan(status: str, addressed: list[bool]) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id="p", title="t", category="capability",
        rationale="r", proposed_changes=["c"], files_affected=["maestro/x.py"],
        expected_impact="none", status=status,
        reviewer_feedback=[
            PlanFeedback(
                feedback_id=f"fb-{i}", text="x",
                addressed_at="2025-01-01T00:00:00Z" if a else None,
            )
            for i, a in enumerate(addressed)
        ],
    )


def test_any_pending_feedback_empty() -> None:
    assert any_pending_feedback([]) is False


def test_any_pending_feedback_pending_without_feedback() -> None:
    assert any_pending_feedback([_fb_plan("pending_review", [])]) is False


def test_any_pending_feedback_pending_only_addressed() -> None:
    assert any_pending_feedback([_fb_plan("pending_review", [True, True])]) is False


def test_any_pending_feedback_pending_with_unaddressed() -> None:
    assert any_pending_feedback([_fb_plan("pending_review", [True, False])]) is True


def test_any_pending_feedback_non_pending_unaddressed_is_inert() -> None:
    assert any_pending_feedback([_fb_plan("approved", [False])]) is False


# ── idempotent write + plan-ref marker ───────────────────────────────


def test_add_feedback_with_id_is_idempotent(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    for _ in range(2):
        assert m.add_plan_feedback(
            "plan-1", "drop step 2", source="telegram",
            content_hash_seen=HASH, feedback_id="msg-1",
        ) is True
    plan = m.get_plan("plan-1")
    assert plan is not None
    assert [f.feedback_id for f in plan.reviewer_feedback] == ["msg-1"]


def test_add_feedback_with_id_on_missing_plan_refused(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs)
    assert m.add_plan_feedback(
        "nope", "x", source="telegram", content_hash_seen=HASH, feedback_id="m",
    ) is False


_REF = "[plan plan-1@0123456789abcdef]"


def test_format_plan_ref_round_trips() -> None:
    plan = _plan()
    plan.plan_content_hash = HASH
    ref = parse_plan_ref(f"New Evolution Plan {format_plan_ref(plan)}: T")
    assert ref == PlanRef("plan-1", HASH[:CALLBACK_HASH_PREFIX_LEN])


@pytest.mark.parametrize(
    "text",
    [
        f"New Evolution Plan {_REF}: T\nbody",
        f"Refined — updated fields: x\n\nRefined Evolution Plan {_REF}: T",
    ],
)
def test_parse_plan_ref_within_scanned_lines(text: str) -> None:
    assert parse_plan_ref(text) == PlanRef("plan-1", "0123456789abcdef")


@pytest.mark.parametrize(
    "text",
    [
        f"one\ntwo\n{_REF}",
        "[plan plan-1@0123]",
        "[plan plan-1@0123456789abcdeg]",
        "[plan @0123456789abcdef]",
        None,
        42,
    ],
)
def test_parse_plan_ref_rejects(text: object) -> None:
    assert parse_plan_ref(text) is None
