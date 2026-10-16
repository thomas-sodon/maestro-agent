"""Proposal 155 step 6 — the curation fingerprint and its two stamping doors."""

from __future__ import annotations

import json

from maestro.memory import (
    compute_curation_fingerprint,
    compute_plan_content_hash,
)
from maestro.plan_feedback import PlanFeedback
from tests.test_memory import FAKE_DIR, _make_memory, _plan, _sample_plan


def _fb(fid: str) -> PlanFeedback:
    return PlanFeedback(feedback_id=fid, text="narrow it", addressed_at=None)


def test_fingerprint_ignores_bookkeeping() -> None:
    plan = _plan("p1", "title", "rationale")
    plan.reviewer_feedback = [_fb("a")]
    before = compute_curation_fingerprint(plan)
    plan.status = "approved"
    plan.last_curated_at = "2026-01-01T00:00:00+00:00"
    plan.reviewer_feedback[0].addressed_at = "2026-01-02T00:00:00+00:00"
    plan.reviewer_feedback[0].response = "done"
    assert compute_curation_fingerprint(plan) == before


def test_fingerprint_moves_on_content_edit() -> None:
    plan = _plan("p1", "title", "rationale")
    before = compute_curation_fingerprint(plan)
    plan.rationale = "different"
    assert compute_curation_fingerprint(plan) != before


def test_fingerprint_moves_on_new_feedback() -> None:
    plan = _plan("p1", "title", "rationale")
    before = compute_curation_fingerprint(plan)
    plan.reviewer_feedback = [_fb("a")]
    assert compute_curation_fingerprint(plan) != before


def test_curated_hash_outside_content_hash() -> None:
    plan = _plan("p1", "title", "rationale")
    bare = compute_plan_content_hash(plan)
    plan.curated_hash = "x" * 64
    assert compute_plan_content_hash(plan) == bare


def test_curated_hash_legacy_and_non_str_load_none(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    base = {
        "title": "t", "stage": "awakening", "category": "capability",
        "rationale": "r", "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "status": "pending_review",
        "created_at": "2025-01-01T00:00:00Z",
    }
    rows = [
        {**base, "plan_id": "legacy"},
        {**base, "plan_id": "bad", "curated_hash": 7},
    ]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in rows
    )
    legacy = mem.get_plan("legacy")
    bad = mem.get_plan("bad")
    assert legacy is not None
    assert bad is not None
    assert legacy.curated_hash is None
    assert bad.curated_hash is None


def test_touch_plan_curated_stamps_live_fingerprint(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.touch_plan_curated("ep1") is True
    got = mem.get_plan("ep1")
    assert got is not None
    assert got.curated_hash == compute_curation_fingerprint(got)


def test_refine_plan_stamps_post_refine_fingerprint(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    assert mem.refine_plan("ep1", rationale="rewritten") is True
    got = mem.get_plan("ep1")
    assert got is not None
    assert got.rationale == "rewritten"
    assert got.curated_hash == compute_curation_fingerprint(got)


def test_touch_non_pending_writes_neither_field(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_sample_plan())
    mem.update_plan_status("ep1", "approved")
    assert mem.touch_plan_curated("ep1") is False
    got = mem.get_plan("ep1")
    assert got is not None
    assert got.curated_hash is None
    assert got.last_curated_at is None
