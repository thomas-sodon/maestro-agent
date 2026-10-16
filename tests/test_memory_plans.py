"""Tests for Memory persistence — EvolutionPlan, PlanStep, add_plan, update_plan,
get_plan, all_plans, DuplicatePlanError.  Fully mocked I/O via mem_fs."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from unittest.mock import patch

import pytest

from maestro.memory import (
    _PLAN_FILE_PATH_MAX_CHARS,
    _PLAN_FILES_AFFECTED_MAX_ITEMS,
    DuplicatePlanError,
    EvolutionPlan,
    Memory,
    compute_plan_content_hash,
)
from maestro.plan_grounding import GroundingProbe
from maestro.types import PlanStatus

FAKE_DIR = Path("/fake/mem")


def _make_memory(mem_fs: dict[str, str]) -> Memory:
    return Memory(FAKE_DIR)


# ---------------------------------------------------------------------------
# EvolutionPlan lifecycle
# ---------------------------------------------------------------------------


def test_evolution_plan_lifecycle(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="p1",
        title="Test Evolution",
        category="capability",
        rationale="Because",
        proposed_changes=["Change X"],
        files_affected=["file.py"],
        expected_impact="Improve Y",
        created_at="2025-01-01T00:00:00Z",
    )
    mem.save_evolution_plan(plan)

    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    assert plans_path in mem_fs
    assert "Test Evolution" in mem_fs[plans_path]

    pending = mem.get_pending_plans()
    assert len(pending) == 1
    assert pending[0].title == "Test Evolution"

    mem.update_plan_status("p1", "approved", reviewed_at="2025-01-01T01:00:00Z")
    assert len(mem.get_pending_plans()) == 0

    updated = mem.get_plan("p1")
    assert updated is not None
    assert updated.status == "approved"


def test_save_plan_then_load_roundtrip(mem_fs: dict[str, str]) -> None:
    """save_evolution_plan then get_plan preserves all fields."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="p1",
        title="Test plan",
        category="capability",
        rationale="test",
        proposed_changes=["change 1"],
        files_affected=["file.py"],
        expected_impact="improvement",
    )
    mem.save_evolution_plan(plan)

    loaded = mem.get_plan("p1")
    assert loaded is not None
    assert loaded.title == "Test plan"
    assert loaded.proposed_changes == ["change 1"]


def test_evolution_plan_extra_fields(mem_fs: dict[str, str]) -> None:
    """_load_all_plans ignores extra unknown fields in JSON (forward compat)."""
    mem = _make_memory(mem_fs)
    plan_data = {
        "plan_id": "p1", "title": "Test Plan", "stage": "awakening",
        "category": "capability", "rationale": "testing",
        "proposed_changes": ["change1"], "files_affected": ["file.py"],
        "expected_impact": "none",
        "future_unknown_field": "should be ignored",
    }
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(plan_data) + "\n"

    plans = mem._load_all_plans()
    assert len(plans) == 1
    assert plans[0].title == "Test Plan"


def _impact_row(**impact: str) -> dict[str, object]:
    """A minimal plan row whose impact sentence rides on the given key(s)."""
    return {
        "plan_id": "p1", "title": "Impact Plan", "stage": "awakening",
        "category": "capability", "rationale": "testing",
        "proposed_changes": ["change1"], "files_affected": ["file.py"],
        **impact,
    }


def test_parse_plan_line_reads_legacy_impact_key() -> None:
    """Either impact key loads onto the one attribute with an equal hash."""
    via_legacy = Memory._parse_plan_line(
        json.dumps(_impact_row(expected_stage_impact="faster loop")),
    )
    via_attr = Memory._parse_plan_line(
        json.dumps(_impact_row(expected_stage_impact="faster loop")),
    )
    assert via_legacy is not None
    assert via_attr is not None
    assert via_legacy.expected_impact == "faster loop"
    assert via_legacy.expected_impact == via_attr.expected_impact
    assert compute_plan_content_hash(via_legacy) == compute_plan_content_hash(via_attr)


def test_parse_plan_line_attribute_key_wins_over_legacy_key() -> None:
    """A row carrying both keys keeps the new attribute key's value."""
    plan = Memory._parse_plan_line(json.dumps(_impact_row(
        expected_stage_impact="legacy value", expected_impact="attr value",
    )))
    assert plan is not None
    assert plan.expected_impact == "attr value"


#: Digest of the fixture below, captured on the tree before the impact field
#: was renamed — the hash is positional, so the rename must not move it.
#: Re-captured when the value claim left the hash with the revenue lineage.
_PINNED_PLAN_HASH = "f568016134f0c7ab1cd7e86d879bec03595f27c442a75f1fd66a0ede6a0b9b9a"


def _pinned_row(**impact: str) -> dict[str, object]:
    """A plan row with every hashed field populated, impact on the given key."""
    return {
        "plan_id": "p-pin", "title": "Pin Plan", "stage": "awakening",
        "category": "capability", "rationale": "why it matters",
        "proposed_changes": ["change one", "change two"],
        "files_affected": ["maestro/a.py"],
        "acceptance_criteria": ["it holds"], "priority": "capability",
        **impact,
    }


@pytest.mark.parametrize("key", ["expected_stage_impact", "expected_impact"])
def test_plan_content_hash_pinned_across_impact_rename(key: str) -> None:
    """Either impact key hashes to the digest pinned before the rename."""
    plan = Memory._parse_plan_line(
        json.dumps(_pinned_row(**{key: "an observer sees faster loops"})),
    )
    assert plan is not None
    assert compute_plan_content_hash(plan) == _PINNED_PLAN_HASH


@pytest.mark.parametrize("status", get_args(PlanStatus))
def test_plan_content_hash_unchanged_across_stage_deletion(status: str) -> None:
    """Proposal 148 Step 7: a legacy row still carrying ``stage`` loads in every
    status with the key dropped, and hashes to the digest pinned before the
    field's deletion (status is bookkeeping, so one digest covers the corpus)."""
    plan = Memory._parse_plan_line(json.dumps(
        _pinned_row(status=status, expected_impact="an observer sees faster loops"),
    ))
    assert plan is not None
    assert not hasattr(plan, "stage")
    assert compute_plan_content_hash(plan) == _PINNED_PLAN_HASH


def test_parse_plan_line_without_stage_key_loads() -> None:
    """A row that never carried ``stage`` now parses (the field is gone)."""
    row = _pinned_row(expected_impact="x")
    del row["stage"]
    assert Memory._parse_plan_line(json.dumps(row)) is not None


def test_parse_plan_line_missing_both_impact_keys_is_none() -> None:
    """Neither key present still fails the required-field guard."""
    assert Memory._parse_plan_line(json.dumps(_impact_row())) is None


def test_legacy_loaded_plan_resaves_under_attribute_key(mem_fs: dict[str, str]) -> None:
    """A legacy-key row re-saves under ``expected_impact`` only."""
    mem = _make_memory(mem_fs)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    mem_fs[plans_path] = (
        json.dumps(_impact_row(expected_stage_impact="faster loop")) + "\n"
    )

    mem._save_all_plans(mem._load_all_plans())

    row = json.loads(mem_fs[plans_path].strip())
    assert row["expected_impact"] == "faster loop"
    assert "expected_stage_impact" not in row


def test_get_plan_resolves_legacy_impact_key_row(mem_fs: dict[str, str]) -> None:
    """The indexed-seek path parses a legacy-key row through the same remap."""
    mem = _make_memory(mem_fs)
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = (
        json.dumps(_impact_row(expected_stage_impact="faster loop")) + "\n"
    )

    plan = mem.get_plan("p1")
    assert plan is not None
    assert plan.expected_impact == "faster loop"


# ---------------------------------------------------------------------------
# save_evolution_plan — duplicate detection
# ---------------------------------------------------------------------------


def test_save_evolution_plan_raises_on_duplicate_id(mem_fs: dict[str, str]) -> None:
    """save_evolution_plan raises DuplicatePlanError for duplicate plan_id."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="dup-id", title="First",
        category="capability", rationale="", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )
    mem.save_evolution_plan(plan)

    duplicate = EvolutionPlan(
        plan_id="dup-id", title="Second",
        category="capability", rationale="", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )
    with pytest.raises(DuplicatePlanError, match="dup-id"):
        mem.save_evolution_plan(duplicate)

    # Only one copy should exist
    assert sum(1 for p in mem._load_all_plans() if p.plan_id == "dup-id") == 1


def test_save_evolution_plan_raises_on_empty_plan_id(mem_fs: dict[str, str]) -> None:
    """save_evolution_plan raises ValueError when plan_id is empty."""
    mem = _make_memory(mem_fs)
    with pytest.raises(ValueError, match="plan_id"):
        mem.save_evolution_plan(EvolutionPlan(
            plan_id="", title="No ID",
            category="capability", rationale="",
            proposed_changes=[], files_affected=[],
            expected_impact="",
            created_at="2025-01-01T00:00:00Z",
        ))


def test_save_evolution_plan_skips_corrupted_line_in_duplicate_check(
    mem_fs: dict[str, str],
) -> None:
    """save_evolution_plan skips a JSONDecodeError line during duplicate check."""
    mem = _make_memory(mem_fs)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    valid_existing = json.dumps({
        "plan_id": "existing", "title": "Existing", "stage": "awakening",
        "category": "capability", "rationale": "", "proposed_changes": [],
        "files_affected": [], "expected_impact": "",
        "status": "pending_review",
    })
    mem_fs[plans_path] = "NOT JSON\n" + valid_existing + "\n"

    new_plan = EvolutionPlan(
        plan_id="new-one", title="New",
        category="capability", rationale="", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(new_plan)
    assert mem.get_plan("new-one") is not None


def test_save_evolution_plan_duplicate_check_skips_non_dict_lines(
    mem_fs: dict[str, str],
) -> None:
    """save_evolution_plan's duplicate scan skips valid-JSON non-dict rows.

    ``evolution_plans.jsonl`` tolerates lines that are valid JSON but not an
    object (``_parse_plan_line`` / ``Memory.archive`` both preserve them).  The
    writer's hand-rolled scan must inherit that tolerance: a ``"just a
    string"`` / ``42`` / ``null`` / ``[1, 2, 3]`` row must be skipped (not crash
    with ``AttributeError``), while a genuine duplicate ``plan_id`` sitting
    *after* such rows still raises — proving the guard ``continue``s the scan
    rather than short-circuiting it.
    """
    mem = _make_memory(mem_fs)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    valid_existing = json.dumps({
        "plan_id": "existing", "title": "Existing", "stage": "awakening",
        "category": "capability", "rationale": "", "proposed_changes": [],
        "files_affected": [], "expected_impact": "",
        "status": "pending_review",
    })
    non_dict_rows = "\n".join(['"just a string"', "42", "null", "[1, 2, 3]"])
    mem_fs[plans_path] = non_dict_rows + "\n" + valid_existing + "\n"

    new_plan = EvolutionPlan(
        plan_id="new-non-dict-skip", title="New",
        category="capability", rationale="", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(new_plan)
    assert mem.get_plan("new-non-dict-skip") is not None

    # A duplicate placed after the non-dict rows still raises — the guard
    # skips the bad rows and keeps scanning, it does not abandon the scan.
    duplicate = EvolutionPlan(
        plan_id="existing", title="Second",
        category="capability", rationale="", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    with pytest.raises(DuplicatePlanError, match="existing"):
        mem.save_evolution_plan(duplicate)


def test_save_evolution_plan_duplicate_check_skips_blank_lines(mem_fs: dict[str, str]) -> None:
    """save_evolution_plan's duplicate check skips blank lines."""
    mem = _make_memory(mem_fs)
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    valid_existing = json.dumps({
        "plan_id": "existing", "title": "Existing", "stage": "awakening",
        "category": "capability", "rationale": "", "proposed_changes": [],
        "files_affected": [], "expected_impact": "",
        "status": "pending_review",
    })
    mem_fs[plans_path] = "\n   \n" + valid_existing + "\n"

    new_plan = EvolutionPlan(
        plan_id="new-blank-skip", title="New",
        category="capability", rationale="", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(new_plan)
    assert mem.get_plan("new-blank-skip") is not None


def test_save_evolution_plan_duplicate_check_does_not_call_load_all_plans(
    mem_fs: dict[str, str],
) -> None:
    """save_evolution_plan must not call _load_all_plans inside the file lock."""
    mem = _make_memory(mem_fs)
    N = 50
    for i in range(N):
        mem.save_evolution_plan(EvolutionPlan(
            plan_id=f"existing-{i}", title=f"Plan {i}",
            category="capability", rationale="rationale",
            proposed_changes=["change"], files_affected=["file.py"],
            expected_impact="impact",
        ))

    call_count: list[int] = [0]
    original_load = mem._load_all_plans

    def spy_load() -> list[EvolutionPlan]:
        call_count[0] += 1
        return original_load()

    mem._load_all_plans = spy_load  # type: ignore[method-assign]

    mem.save_evolution_plan(EvolutionPlan(
        plan_id="new-plan", title="New Plan",
        category="capability", rationale="rationale",
        proposed_changes=["change"], files_affected=["file.py"],
        expected_impact="impact",
    ))

    assert call_count[0] == 0
    assert mem.get_plan("new-plan") is not None


# ---------------------------------------------------------------------------
# _load_all_plans — edge cases
# ---------------------------------------------------------------------------


def test_corrupted_plan_lines_skipped(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "plan_id": "p1", "title": "ok", "stage": "awakening",
        "category": "capability", "rationale": "", "proposed_changes": [],
        "files_affected": [], "expected_impact": "",
        "status": "pending_review",
    })
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = valid + "\nCORRUPT LINE\n"
    assert len(mem.get_all_plans()) == 1


def test_load_all_plans_skips_non_dict_jsonl_lines(mem_fs: dict[str, str]) -> None:
    """_load_all_plans skips non-dict JSONL lines (strings, ints, nulls, arrays)."""
    mem = _make_memory(mem_fs)
    valid_plan = {
        "plan_id": "p1", "title": "test", "stage": "awakening",
        "category": "infrastructure", "rationale": "r",
        "proposed_changes": ["c"], "files_affected": ["f"],
        "expected_impact": "none", "priority": "capability",
        "status": "pending_review",
    }
    lines = [
        json.dumps(valid_plan),
        '"just a string"',
        "42",
        "null",
        "[1, 2, 3]",
    ]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = "\n".join(lines) + "\n"

    plans = mem._load_all_plans()
    assert len(plans) == 1
    assert plans[0].plan_id == "p1"


def test_get_plan_index_skips_non_dict_lines(mem_fs: dict[str, str]) -> None:
    """get_plan's index build tolerates valid-JSON non-dict rows.

    ``_build_plan_index`` shares the tolerance contract of ``_parse_plan_line``
    and ``_load_all_plans``: a ``"just a string"`` / ``42`` / ``null`` /
    ``[1, 2, 3]`` row contributes no index entry rather than crashing with
    ``AttributeError``, so the indexed ``get_plan`` seek still resolves a real
    plan sitting after such rows and returns ``None`` for a missing id.
    """
    mem = _make_memory(mem_fs)
    valid_plan = {
        "plan_id": "indexed", "title": "test", "stage": "awakening",
        "category": "capability", "rationale": "r",
        "proposed_changes": ["c"], "files_affected": ["f"],
        "expected_impact": "none", "status": "pending_review",
    }
    lines = ['"just a string"', "42", "null", "[1, 2, 3]", json.dumps(valid_plan)]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = "\n".join(lines) + "\n"

    resolved = mem.get_plan("indexed")
    assert resolved is not None
    assert resolved.plan_id == "indexed"
    assert mem.get_plan("does-not-exist") is None


def test_load_all_plans_skips_empty_lines(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    valid = json.dumps({
        "plan_id": "p-blank", "title": "Blank lines test", "stage": "awakening",
        "category": "capability", "rationale": "", "proposed_changes": [],
        "files_affected": [], "expected_impact": "",
        "status": "pending_review",
    })
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = "\n   \n" + valid + "\n\n   \n"
    assert len(mem.get_all_plans()) == 1


def test_load_all_plans_skips_interior_blank_lines(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    valid_a = json.dumps({
        "plan_id": "p-a", "title": "A", "stage": "awakening",
        "category": "capability", "rationale": "", "proposed_changes": [],
        "files_affected": [], "expected_impact": "",
        "status": "pending_review",
    })
    valid_b = json.dumps({
        "plan_id": "p-b", "title": "B", "stage": "awakening",
        "category": "capability", "rationale": "", "proposed_changes": [],
        "files_affected": [], "expected_impact": "",
        "status": "pending_review",
    })
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = valid_a + "\n\n   \n" + valid_b + "\n"

    plans = mem.get_all_plans()
    assert len(plans) == 2


def test_load_all_plans_skips_empty_plan_id(mem_fs: dict[str, str]) -> None:
    """_load_all_plans skips plans with empty plan_id."""
    mem = _make_memory(mem_fs)
    bad_plan = json.dumps({
        "plan_id": "", "title": "No ID plan", "stage": "awakening",
        "category": "capability", "rationale": "",
        "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "status": "pending_review",
    })
    good_plan = json.dumps({
        "plan_id": "good-plan", "title": "Good", "stage": "awakening",
        "category": "capability", "rationale": "",
        "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "status": "pending_review",
    })
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = bad_plan + "\n" + good_plan + "\n"

    plans = mem.get_all_plans()
    assert len(plans) == 1
    assert plans[0].plan_id == "good-plan"


# ---------------------------------------------------------------------------
# _load_all_plans — type coercion
# ---------------------------------------------------------------------------


def test_load_all_plans_filters_non_string_list_items(mem_fs: dict[str, str]) -> None:
    """Non-string items in proposed_changes and files_affected are filtered out."""
    mem = _make_memory(mem_fs)
    raw_plan = json.dumps({
        "plan_id": "p-filter", "title": "Filter test", "stage": "awakening",
        "category": "capability", "rationale": "test",
        "proposed_changes": ["valid change", 42, None, True, "another change"],
        "files_affected": ["file.py", 99, "other.py"],
        "expected_impact": "better", "status": "pending_review",
    })
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = raw_plan + "\n"

    plans = mem.get_all_plans()
    assert len(plans) == 1
    assert plans[0].proposed_changes == ["valid change", "another change"]
    assert plans[0].files_affected == ["file.py", "other.py"]


def test_load_all_plans_null_list_fields_coerced_to_empty(mem_fs: dict[str, str]) -> None:
    """Null proposed_changes and files_affected become []."""
    mem = _make_memory(mem_fs)
    raw_plan = json.dumps({
        "plan_id": "p-null-lists", "title": "Null list test", "stage": "awakening",
        "category": "capability", "rationale": "test",
        "proposed_changes": None, "files_affected": None,
        "expected_impact": "better", "status": "pending_review",
    })
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = raw_plan + "\n"

    plans = mem.get_all_plans()
    assert len(plans) == 1
    assert plans[0].proposed_changes == []
    assert plans[0].files_affected == []


def test_load_all_plans_non_list_non_string_list_field_becomes_empty(
    mem_fs: dict[str, str],
) -> None:
    """Non-list, non-string values for list fields become []."""
    mem = _make_memory(mem_fs)
    raw_plan = json.dumps({
        "plan_id": "p-bad-type", "title": "Bad type test", "stage": "awakening",
        "category": "capability", "rationale": "test",
        "proposed_changes": 12345,
        "files_affected": {"nested": "dict"},
        "expected_impact": "better", "status": "pending_review",
    })
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = raw_plan + "\n"

    plans = mem.get_all_plans()
    assert len(plans) == 1
    assert plans[0].proposed_changes == []
    assert plans[0].files_affected == []


def test_proposed_changes_string_wrapped_in_list(mem_fs: dict[str, str]) -> None:
    """A bare string proposed_changes is wrapped in a list."""
    mem = _make_memory(mem_fs)
    plan_data = {
        "plan_id": "p1", "title": "Test", "stage": "awakening",
        "category": "capability", "rationale": "test",
        "proposed_changes": "add a feature",
        "files_affected": "maestro/foo.py",
        "expected_impact": "none", "priority": "capability",
    }
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(plan_data) + "\n"
    plans = mem.get_all_plans()
    assert len(plans) == 1
    assert isinstance(plans[0].proposed_changes, list)
    assert plans[0].proposed_changes == ["add a feature"]
    assert isinstance(plans[0].files_affected, list)
    assert plans[0].files_affected == ["maestro/foo.py"]


def test_proposed_changes_string_with_newlines_is_split(mem_fs: dict[str, str]) -> None:
    """A newline-delimited string in proposed_changes is split into items."""
    mem = _make_memory(mem_fs)
    raw_plan = json.dumps({
        "plan_id": "p-newline", "title": "Newline Test", "stage": "awakening",
        "category": "capability", "rationale": "test",
        "proposed_changes": "refactor foo\nupdate bar",
        "files_affected": "file_a.py\nfile_b.py",
        "expected_impact": "better", "status": "pending_review",
    })
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = raw_plan + "\n"

    plans = mem.get_all_plans()
    assert len(plans) == 1
    assert plans[0].proposed_changes == ["refactor foo", "update bar"]
    assert plans[0].files_affected == ["file_a.py", "file_b.py"]


def test_load_all_plans_coerces_float_issue_number(mem_fs: dict[str, str]) -> None:
    """Float issue_number (e.g. 42.0) is coerced to int."""
    mem = _make_memory(mem_fs)
    raw_plan = json.dumps({
        "plan_id": "p-issue-float", "title": "Float issue_number",
        "stage": "awakening", "category": "capability", "rationale": "test",
        "proposed_changes": [], "files_affected": [],
        "expected_impact": "better", "issue_number": 42.0,
        "status": "pending_review",
    })
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = raw_plan + "\n"

    plans = mem.get_all_plans()
    assert len(plans) == 1
    assert plans[0].issue_number == 42
    assert isinstance(plans[0].issue_number, int)


# ---------------------------------------------------------------------------
# _save_all_plans
# ---------------------------------------------------------------------------


def test_save_empty_plans_roundtrips(mem_fs: dict[str, str]) -> None:
    """_save_all_plans([]) then get_all_plans() returns []."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="p1", title="Test",
        category="improvement", rationale="r",
        proposed_changes=["c"], files_affected=["f"],
        expected_impact="none",
    )
    mem.save_evolution_plan(plan)
    assert len(mem.get_all_plans()) == 1

    mem._save_all_plans([])
    assert mem.get_all_plans() == []


def test_atomic_save_plans(mem_fs: dict[str, str]) -> None:
    """_save_all_plans uses atomic write (temp + rename)."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="p1", title="Test",
        category="capability", rationale="R", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    )
    mem.save_evolution_plan(plan)
    mem.update_plan_status("p1", "approved")

    plans = mem.get_all_plans()
    assert len(plans) == 1
    assert plans[0].status == "approved"


# ---------------------------------------------------------------------------
# update_plan_status
# ---------------------------------------------------------------------------


def test_update_plan_status_clears_rejection_reason(mem_fs: dict[str, str]) -> None:
    """Passing rejection_reason=None clears a previously set reason."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="pclr", title="Clearable",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(plan)

    mem.update_plan_status("pclr", "rejected", rejection_reason="Bad idea")
    p = mem.get_plan("pclr")
    assert p is not None and p.rejection_reason == "Bad idea"

    mem.update_plan_status("pclr", "pending_review", rejection_reason=None)
    p = mem.get_plan("pclr")
    assert p is not None and p.status == "pending_review" and p.rejection_reason is None


def test_update_plan_status_leaves_fields_unchanged(mem_fs: dict[str, str]) -> None:
    """Not passing optional fields leaves them unchanged."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="pkeep", title="Keep Fields",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(plan)
    mem.update_plan_status("pkeep", "rejected", rejection_reason="Reason X")

    mem.update_plan_status("pkeep", "pending_review")
    p = mem.get_plan("pkeep")
    assert p is not None
    assert p.status == "pending_review"
    assert p.rejection_reason == "Reason X"  # preserved


def test_update_plan_status_sets_pr_url(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="p-prurl", title="PR URL test",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(plan)
    mem.update_plan_status("p-prurl", "implemented", pr_url="https://github.com/repo/pull/1")

    updated = mem.get_plan("p-prurl")
    assert updated is not None
    assert updated.pr_url == "https://github.com/repo/pull/1"
    assert updated.status == "implemented"


def test_update_plan_status_nonexistent_plan(mem_fs: dict[str, str]) -> None:
    """update_plan_status does not crash when plan_id doesn't exist."""
    mem = _make_memory(mem_fs)
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps({
        "plan_id": "p1", "title": "Test", "stage": "awakening",
        "category": "capability", "rationale": "test",
        "proposed_changes": ["change"], "files_affected": [],
        "expected_impact": "test",
    }) + "\n"

    mem.update_plan_status("nonexistent", "approved")
    plans = mem.get_all_plans()
    assert len(plans) == 1
    assert plans[0].status == "pending_review"


def test_update_plan_status_then_get(mem_fs: dict[str, str]) -> None:
    """update_plan_status persists and get_plan reads the update."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="p1", title="Test",
        category="capability", rationale="test",
        proposed_changes=["c1"], files_affected=[],
        expected_impact="",
    )
    mem.save_evolution_plan(plan)
    mem.update_plan_status("p1", "approved")

    loaded = mem.get_plan("p1")
    assert loaded is not None
    assert loaded.status == "approved"


# ---------------------------------------------------------------------------
# update_plan_status — type validation
# ---------------------------------------------------------------------------


def test_update_plan_status_rejects_non_string_pr_url(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="ptype", title="Type Test",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(plan)

    with pytest.raises(TypeError, match="pr_url must be a string or None"):
        mem.update_plan_status("ptype", "implemented", pr_url=12345)  # type: ignore[arg-type]

    p = mem.get_plan("ptype")
    assert p is not None and p.pr_url is None and p.status == "pending_review"


def test_update_plan_status_rejects_bool_pr_url(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="pbool", title="Bool Test",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(plan)

    with pytest.raises(TypeError, match="pr_url must be a string or None"):
        mem.update_plan_status("pbool", "implemented", pr_url=True)  # type: ignore[arg-type]


def test_update_plan_status_rejects_non_string_reviewed_at(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="prat", title="ReviewedAt Test",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(plan)

    with pytest.raises(TypeError, match="reviewed_at must be a string or None"):
        mem.update_plan_status("prat", "approved", reviewed_at=12345)  # type: ignore[arg-type]

    p = mem.get_plan("prat")
    assert p is not None and p.reviewed_at is None


def test_update_plan_status_rejects_bool_reviewed_at(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="pratbool", title="ReviewedAt Bool",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(plan)

    with pytest.raises(TypeError, match="reviewed_at must be a string or None"):
        mem.update_plan_status("pratbool", "approved", reviewed_at=True)  # type: ignore[arg-type]


def _rejection_stamp_plan(mem: Memory, plan_id: str) -> None:
    mem.save_evolution_plan(EvolutionPlan(
        plan_id=plan_id, title=f"Stamp {plan_id}",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    ))
    mem.update_plan_status(plan_id, "implemented", reviewed_at="2025-01-01T00:00:00+00:00")


def test_update_plan_status_stamps_reviewed_at_on_entry_to_rejected(mem_fs: dict[str, str]) -> None:
    """A PR-close rejection passes no reviewed_at; the rejection moment is stamped (#4068)."""
    mem = _make_memory(mem_fs)
    _rejection_stamp_plan(mem, "pstamp")

    before = datetime.now(UTC)
    mem.update_plan_status("pstamp", "rejected")

    p = mem.get_plan("pstamp")
    assert p is not None
    assert p.reviewed_at is not None
    assert datetime.fromisoformat(p.reviewed_at) >= before


def test_update_plan_status_rejected_honours_explicit_reviewed_at(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    _rejection_stamp_plan(mem, "pexplicit")

    mem.update_plan_status("pexplicit", "rejected", reviewed_at="2025-06-01T00:00:00+00:00")

    p = mem.get_plan("pexplicit")
    assert p is not None
    assert p.reviewed_at == "2025-06-01T00:00:00+00:00"


def test_update_plan_status_rejected_to_rejected_keeps_reviewed_at(mem_fs: dict[str, str]) -> None:
    """Re-writing an already-rejected plan must not extend its cooldown."""
    mem = _make_memory(mem_fs)
    _rejection_stamp_plan(mem, "pkeep")
    mem.update_plan_status("pkeep", "rejected", reviewed_at="2025-06-01T00:00:00+00:00")

    mem.update_plan_status("pkeep", "rejected", rejection_reason="again")

    p = mem.get_plan("pkeep")
    assert p is not None
    assert p.reviewed_at == "2025-06-01T00:00:00+00:00"


def test_update_plan_status_rejects_non_string_rejection_reason(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="prr-type", title="RejectionReason Type",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(plan)

    with pytest.raises(TypeError, match="rejection_reason must be a string or None"):
        mem.update_plan_status("prr-type", "rejected", rejection_reason=42)  # type: ignore[arg-type]


def test_update_plan_status_rejects_non_int_pr_reminder_count(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="prc-type", title="ReminderCount Type",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(plan)

    with pytest.raises(TypeError, match="pr_reminder_count must be an int"):
        mem.update_plan_status("prc-type", "implemented", pr_reminder_count="not-an-int")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Plan with long fields
# ---------------------------------------------------------------------------


def test_plan_with_very_long_fields(mem_fs: dict[str, str]) -> None:
    """Very long title/rationale are preserved (no silent truncation)."""
    mem = _make_memory(mem_fs)
    long_title = "T" * 5000
    long_rationale = "R" * 10000

    plan = EvolutionPlan(
        plan_id="p1", title=long_title,
        category="capability", rationale=long_rationale,
        proposed_changes=["c1"], files_affected=[],
        expected_impact="",
    )
    mem.save_evolution_plan(plan)

    loaded = mem.get_plan("p1")
    assert loaded is not None
    assert len(loaded.title) == 5000
    assert len(loaded.rationale) == 10000


# ---------------------------------------------------------------------------
# mark_rejection_learned
# ---------------------------------------------------------------------------


def test_mark_rejection_learned_sets_flag(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="plearn", title="Learn Me",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="rejected", rejection_reason="bad idea",
    )
    mem.save_evolution_plan(plan)
    assert not mem.get_plan("plearn").rejection_learned  # type: ignore[union-attr]

    result = mem.mark_rejection_learned("plearn")
    assert result is True

    reloaded = mem.get_plan("plearn")
    assert reloaded is not None
    assert reloaded.rejection_learned is True


def test_mark_rejection_learned_nonexistent(mem_fs: dict[str, str]) -> None:
    mem = _make_memory(mem_fs)
    assert mem.mark_rejection_learned("missing") is False


def test_rejection_learned_default_false(mem_fs: dict[str, str]) -> None:
    """New plans default to rejection_learned=False."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="pdefault", title="Default",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
    )
    mem.save_evolution_plan(plan)
    loaded = mem.get_plan("pdefault")
    assert loaded is not None
    assert loaded.rejection_learned is False


def test_rejection_learned_survives_roundtrip(mem_fs: dict[str, str]) -> None:
    """rejection_learned persists across load/save cycles."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="prt", title="Roundtrip",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        status="rejected",
    )
    mem.save_evolution_plan(plan)
    mem.mark_rejection_learned("prt")

    # Re-create Memory to force reload from disk
    mem2 = _make_memory(mem_fs)
    loaded = mem2.get_plan("prt")
    assert loaded is not None
    assert loaded.rejection_learned is True


def test_rejection_learned_missing_in_jsonl_defaults_false(mem_fs: dict[str, str]) -> None:
    """Old JSONL records without rejection_learned field load as False."""
    mem = _make_memory(mem_fs)
    # Write a plan record manually without the rejection_learned field
    raw = json.dumps({
        "plan_id": "pold", "title": "Old Plan", "stage": "awakening",
        "category": "capability", "rationale": "test",
        "proposed_changes": [], "files_affected": [],
        "expected_impact": "", "status": "rejected",
    })
    plans_path = str(FAKE_DIR / "evolution_plans.jsonl")
    mem_fs[plans_path] = raw + "\n"

    loaded = mem.get_plan("pold")
    assert loaded is not None
    assert loaded.rejection_learned is False


# ---------------------------------------------------------------------------
# search_plans — BM25 relevance ranking over evolution plans
# ---------------------------------------------------------------------------


def _make_plan(
    plan_id: str,
    title: str = "",
    rationale: str = "",
    proposed_changes: list[str] | None = None,
    expected_impact: str = "",
    rejection_reason: str | None = None,
    status: str = "pending_review",
    category: str = "capability",
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category=category,  # type: ignore[arg-type]
        rationale=rationale,
        proposed_changes=proposed_changes or [],
        files_affected=[],
        expected_impact=expected_impact,
        status=status,  # type: ignore[arg-type]
        rejection_reason=rejection_reason,
    )


def test_search_plans_empty_corpus(mem_fs: dict[str, str]) -> None:
    """search_plans returns [] when no plan history exists."""
    mem = _make_memory(mem_fs)
    assert mem.search_plans("anything") == []


def test_search_plans_empty_query(mem_fs: dict[str, str]) -> None:
    """Empty and whitespace-only queries return []."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_make_plan("p1", title="refactor module"))
    assert mem.search_plans("") == []
    assert mem.search_plans("   ") == []


def test_search_plans_no_match(mem_fs: dict[str, str]) -> None:
    """Query tokens absent from every plan returns []."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_make_plan("p1", title="refactor module"))
    assert mem.search_plans("xyzzy") == []


def test_search_plans_by_title(mem_fs: dict[str, str]) -> None:
    """A query matching a token in `title` finds the plan."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_make_plan(
        "p1", title="Add migration runner for schemas",
    ))
    mem.save_evolution_plan(_make_plan(
        "p2", title="Improve logging output",
    ))
    results = mem.search_plans("migration")
    assert len(results) == 1
    assert results[0].plan_id == "p1"


def test_search_plans_by_rationale(mem_fs: dict[str, str]) -> None:
    """A query matching only in `rationale` still finds the plan."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_make_plan(
        "p1",
        title="Generic improvement",
        rationale="Needed because of heisenbug in the retry path",
    ))
    mem.save_evolution_plan(_make_plan(
        "p2",
        title="Another improvement",
        rationale="Unrelated refactor",
    ))
    results = mem.search_plans("heisenbug")
    assert len(results) == 1
    assert results[0].plan_id == "p1"


def test_search_plans_by_proposed_changes(mem_fs: dict[str, str]) -> None:
    """A query matching only in `proposed_changes` finds the plan."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_make_plan(
        "p1",
        title="Plan one",
        rationale="reason",
        proposed_changes=["install telescopic handler for signals"],
    ))
    mem.save_evolution_plan(_make_plan(
        "p2",
        title="Plan two",
        rationale="reason",
        proposed_changes=["bump dependency versions"],
    ))
    results = mem.search_plans("telescopic")
    assert len(results) == 1
    assert results[0].plan_id == "p1"


def test_search_plans_by_expected_impact(mem_fs: dict[str, str]) -> None:
    """A query matching only in `expected_impact` finds the plan."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_make_plan(
        "p1",
        title="Plan one",
        rationale="reason",
        expected_impact="Advances to axiomatic reasoning",
    ))
    mem.save_evolution_plan(_make_plan(
        "p2",
        title="Plan two",
        rationale="reason",
        expected_impact="No impact",
    ))
    results = mem.search_plans("axiomatic")
    assert len(results) == 1
    assert results[0].plan_id == "p1"


def test_search_plans_by_rejection_reason(mem_fs: dict[str, str]) -> None:
    """A query matching only in `rejection_reason` finds the plan —
    critical for 'why was X rejected?' lookups."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_make_plan(
        "p1",
        title="Plan one",
        rationale="reason",
        status="rejected",
        rejection_reason="Thomas said no: infrastructural grafting conflicts with scope",
    ))
    mem.save_evolution_plan(_make_plan(
        "p2",
        title="Plan two",
        rationale="reason",
    ))
    results = mem.search_plans("grafting")
    assert len(results) == 1
    assert results[0].plan_id == "p1"


def test_search_plans_excludes_non_text_fields(mem_fs: dict[str, str]) -> None:
    """Non-text metadata (category, stage, files_affected) is
    intentionally not indexed — a query matching only those fields
    should not match."""
    mem = _make_memory(mem_fs)
    plan = EvolutionPlan(
        plan_id="p1",
        title="ordinary title",
        category="capability",
        rationale="ordinary rationale",
        proposed_changes=["ordinary change"],
        files_affected=["mystical_file.py"],
        expected_impact="ordinary impact",
    )
    mem.save_evolution_plan(plan)
    # "mystical_file" is only in files_affected — must not match.
    assert mem.search_plans("mystical_file") == []
    # "capability" is only in category — must not match.
    assert mem.search_plans("capability") == []
    # "awakening" is only in stage — must not match.
    assert mem.search_plans("awakening") == []


def test_search_plans_ranking_by_multi_token_match(
    mem_fs: dict[str, str],
) -> None:
    """A plan matching both query tokens ranks above a plan matching
    only one."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_make_plan(
        "p1",
        title="refactor planner module",
        rationale="clean up the planner",
    ))
    mem.save_evolution_plan(_make_plan(
        "p2",
        title="refactor the authentication layer",
        rationale="auth is messy",
    ))
    mem.save_evolution_plan(_make_plan(
        "p3",
        title="update release notes",
        rationale="docs only",
    ))
    mem.save_evolution_plan(_make_plan(
        "p4",
        title="bump dependency versions",
        rationale="security updates",
    ))
    mem.save_evolution_plan(_make_plan(
        "p5",
        title="fix cache eviction",
        rationale="cache bug",
    ))
    results = mem.search_plans("refactor planner")
    # p1 has both tokens, p2 has only "refactor", others have neither.
    assert [r.plan_id for r in results] == ["p1", "p2"]


def test_search_plans_with_rejection_reason_none(
    mem_fs: dict[str, str],
) -> None:
    """Plans without rejection_reason are still indexed correctly."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_make_plan(
        "p1",
        title="Plan one",
        rationale="useful reason",
        rejection_reason=None,
    ))
    results = mem.search_plans("useful")
    assert len(results) == 1
    assert results[0].plan_id == "p1"




# ---------------------------------------------------------------------------
# Plan content hash (proposal 021)
# ---------------------------------------------------------------------------


def _hash_plan(plan_id: str = "ph", **over: object) -> EvolutionPlan:
    """A plan with non-trivial content for hash tests."""
    return EvolutionPlan(
        plan_id=plan_id,
        title=str(over.get("title", "Add hash binding")),
        category=over.get("category", "capability"),  # type: ignore[arg-type]
        rationale=str(over.get("rationale", "Integrity matters")),
        proposed_changes=list(over.get("proposed_changes", ["step 1"])),  # type: ignore[arg-type]
        files_affected=list(over.get("files_affected", ["memory.py"])),  # type: ignore[arg-type]
        expected_impact=str(over.get("expected_impact", "safer")),
        acceptance_criteria=list(over.get("acceptance_criteria", [])),  # type: ignore[arg-type]
        priority=over.get("priority", "capability"),  # type: ignore[arg-type]
        status=over.get("status", "pending_review"),  # type: ignore[arg-type]
        created_at=str(over.get("created_at", "2025-01-01T00:00:00Z")),
        reviewed_at=over.get("reviewed_at"),  # type: ignore[arg-type]
        pr_url=over.get("pr_url"),  # type: ignore[arg-type]
    )


def test_content_hash_whitespace_insensitive() -> None:
    """Leading/trailing whitespace on title and rationale does not change the hash."""
    from maestro.memory import compute_plan_content_hash

    a = _hash_plan(title="Add hash binding", rationale="Integrity matters")
    b = _hash_plan(title="  Add hash binding  ", rationale="\nIntegrity matters\t")
    assert compute_plan_content_hash(a) == compute_plan_content_hash(b)


def test_content_hash_ignores_mutable_bookkeeping() -> None:
    """status / reviewed_at / pr_url differences produce identical hashes."""
    from maestro.memory import compute_plan_content_hash

    a = _hash_plan(status="pending_review")
    b = _hash_plan(
        status="approved",
        reviewed_at="2025-02-01T00:00:00Z",
        pr_url="https://github.com/x/y/pull/1",
    )
    assert compute_plan_content_hash(a) == compute_plan_content_hash(b)


def test_content_hash_sensitive_to_content_fields() -> None:
    """Each content field changes the hash."""
    from maestro.memory import compute_plan_content_hash

    base = compute_plan_content_hash(_hash_plan())
    assert compute_plan_content_hash(_hash_plan(title="Other")) != base
    assert compute_plan_content_hash(_hash_plan(rationale="Other")) != base
    assert compute_plan_content_hash(
        _hash_plan(proposed_changes=["other step"])) != base
    assert compute_plan_content_hash(
        _hash_plan(files_affected=["other.py"])) != base
    assert compute_plan_content_hash(
        _hash_plan(expected_impact="other")) != base
    assert compute_plan_content_hash(
        _hash_plan(acceptance_criteria=["a test passes"])) != base
    assert compute_plan_content_hash(_hash_plan(priority="bug_fix")) != base


def test_slice_linkage_fields_default_to_standalone() -> None:
    """A plan built without the multi-phase fields is a standalone one-shot plan."""
    plan = _hash_plan("standalone")
    assert plan.parent_plan_id is None
    assert plan.phase_index == 0


def test_slice_linkage_excluded_from_content_hash() -> None:
    """Linking a plan into a proposal (parent_plan_id / phase_index) must not
    change its content hash, so an already-approved plan's approval survives."""
    from maestro.memory import compute_plan_content_hash

    standalone = _hash_plan("p")
    linked = _hash_plan("p")
    linked.parent_plan_id = "proposal-abc"
    linked.phase_index = 3
    assert compute_plan_content_hash(linked) == compute_plan_content_hash(standalone)


def test_provenance_fields_excluded_from_content_hash() -> None:
    """issue_number (target-issue linkage) and source_task_id
    (implementation-base linkage) are deliberately outside the content hash,
    so changing either of them must not alter it.

    This pins the existing exclusion as an explicit contract: the current code
    already omits both fields, so the assertion holds today — the test
    guards against a future change that silently folds one into the canonical
    tuple.
    """
    from maestro.memory import compute_plan_content_hash

    base = _hash_plan("p")
    variant = _hash_plan("p")
    variant.issue_number = 4242
    variant.source_task_id = "maestro/task/some-branch"
    assert compute_plan_content_hash(variant) == compute_plan_content_hash(base)


def test_inherited_hold_fields_excluded_from_content_hash() -> None:
    """The proposal-124 hold fields (inherited_hold_main_sha / inherited_hold_count)
    are bookkeeping, so stamping a hold must not change a plan's content hash and
    thus never invalidate an already-approved plan's approval."""
    from maestro.memory import compute_plan_content_hash

    base = _hash_plan("p")
    held = _hash_plan("p")
    held.inherited_hold_main_sha = "deadbeef"
    held.inherited_hold_count = 2
    assert compute_plan_content_hash(held) == compute_plan_content_hash(base)


def test_environment_hold_field_excluded_from_content_hash() -> None:
    """The proposal-133 hold field is bookkeeping, so stamping an environment hold
    must not change a plan's content hash and thus never invalidate an
    already-approved plan's approval binding."""
    from maestro.memory import compute_plan_content_hash

    base = _hash_plan("p")
    held = _hash_plan("p")
    held.environment_hold_fingerprint = "cafe1234"
    assert compute_plan_content_hash(held) == compute_plan_content_hash(base)


def test_attempt_disposition_fields_excluded_from_content_hash() -> None:
    """The proposal-131 fields (resume_count / blocked_question_id /
    blocked_answer) are dormant bookkeeping, so populating them must not change a
    plan's content hash and thus never invalidate an already-approved plan's
    approval binding."""
    from maestro.memory import compute_plan_content_hash

    base = _hash_plan("p")
    disposed = _hash_plan("p")
    disposed.resume_count = 2
    disposed.blocked_question_id = "msg-abc123"
    disposed.blocked_answer = "the environment is fixed, retry"
    assert compute_plan_content_hash(disposed) == compute_plan_content_hash(base)


def test_attempt_disposition_fields_roundtrip(mem_fs: dict[str, str]) -> None:
    """resume_count / blocked_question_id / blocked_answer survive a save + load
    cycle (proposal 131)."""
    mem = _make_memory(mem_fs)
    plan = _hash_plan("disposed")
    plan.resume_count = 4
    plan.blocked_question_id = "msg-xyz789"
    plan.blocked_answer = "here is the missing decision: use the fallback runner"
    mem.save_evolution_plan(plan)

    loaded = mem.get_plan("disposed")
    assert loaded is not None
    assert loaded.resume_count == 4
    assert loaded.blocked_question_id == "msg-xyz789"
    assert loaded.blocked_answer == "here is the missing decision: use the fallback runner"


def test_legacy_row_loads_attempt_disposition_defaults(mem_fs: dict[str, str]) -> None:
    """A pre-existing row lacking the proposal-131 fields loads with the dormant
    defaults (resume_count=0 / blocked_question_id=None / blocked_answer=None)."""
    mem = _make_memory(mem_fs)
    row = asdict(_hash_plan("legacy_disposed"))
    del row["resume_count"]
    del row["blocked_question_id"]
    del row["blocked_answer"]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(row) + "\n"

    loaded = mem.get_plan("legacy_disposed")
    assert loaded is not None
    assert loaded.resume_count == 0
    assert loaded.blocked_question_id is None
    assert loaded.blocked_answer is None


def test_slice_linkage_fields_roundtrip(mem_fs: dict[str, str]) -> None:
    """parent_plan_id / phase_index survive a save + load cycle."""
    mem = _make_memory(mem_fs)
    plan = _hash_plan("sliced")
    plan.parent_plan_id = "proposal-xyz"
    plan.phase_index = 2
    mem.save_evolution_plan(plan)

    loaded = mem.get_plan("sliced")
    assert loaded is not None
    assert loaded.parent_plan_id == "proposal-xyz"
    assert loaded.phase_index == 2


def test_legacy_row_loads_slice_linkage_defaults(mem_fs: dict[str, str]) -> None:
    """A pre-existing row lacking the new fields loads with standalone defaults."""
    mem = _make_memory(mem_fs)
    row = asdict(_hash_plan("legacy_slice"))
    del row["parent_plan_id"]
    del row["phase_index"]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(row) + "\n"

    loaded = mem.get_plan("legacy_slice")
    assert loaded is not None
    assert loaded.parent_plan_id is None
    assert loaded.phase_index == 0


def test_grounding_probe_roundtrips(mem_fs: dict[str, str]) -> None:
    """A plan carrying a grounding probe survives save + load with fields intact."""
    mem = _make_memory(mem_fs)
    plan = _hash_plan("grounded")
    plan.grounding = GroundingProbe(
        kind="DEFECT_PRESENT",
        locator="maestro/foo.py",
        expectation="buggy_call()",
    )
    mem.save_evolution_plan(plan)

    loaded = mem.get_plan("grounded")
    assert loaded is not None
    assert loaded.grounding is not None
    assert loaded.grounding.kind == "DEFECT_PRESENT"
    assert loaded.grounding.locator == "maestro/foo.py"
    assert loaded.grounding.expectation == "buggy_call()"


def test_grounding_none_roundtrips(mem_fs: dict[str, str]) -> None:
    """A plan with grounding=None round-trips unchanged (the standalone default)."""
    mem = _make_memory(mem_fs)
    plan = _hash_plan("no_grounding")
    assert plan.grounding is None
    mem.save_evolution_plan(plan)

    loaded = mem.get_plan("no_grounding")
    assert loaded is not None
    assert loaded.grounding is None


def test_legacy_row_loads_grounding_default_none(mem_fs: dict[str, str]) -> None:
    """A pre-existing row lacking the grounding key loads with the None default."""
    mem = _make_memory(mem_fs)
    row = asdict(_hash_plan("legacy_grounding"))
    del row["grounding"]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(row) + "\n"

    loaded = mem.get_plan("legacy_grounding")
    assert loaded is not None
    assert loaded.grounding is None


def test_phase_index_float_coerced_to_int(mem_fs: dict[str, str]) -> None:
    """A JSON row carrying phase_index as a float loads coerced to int."""
    mem = _make_memory(mem_fs)
    row = asdict(_hash_plan("floaty"))
    row["phase_index"] = 4.0
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(row) + "\n"

    loaded = mem.get_plan("floaty")
    assert loaded is not None
    assert loaded.phase_index == 4
    assert isinstance(loaded.phase_index, int)


def test_save_plan_stamps_and_roundtrips_hash(mem_fs: dict[str, str]) -> None:
    """save_evolution_plan stamps the hash; the loaded copy matches a recompute."""
    from maestro.memory import compute_plan_content_hash

    mem = _make_memory(mem_fs)
    plan = _hash_plan("ph1")
    mem.save_evolution_plan(plan)
    assert plan.plan_content_hash != ""

    loaded = mem.get_plan("ph1")
    assert loaded is not None
    assert loaded.plan_content_hash == plan.plan_content_hash
    assert compute_plan_content_hash(loaded) == loaded.plan_content_hash


def test_load_backfills_empty_hash_in_memory(mem_fs: dict[str, str]) -> None:
    """A legacy row without plan_content_hash loads with a computed hash."""
    from maestro.memory import compute_plan_content_hash

    mem = _make_memory(mem_fs)
    row = asdict(_hash_plan("legacy1"))
    del row["plan_content_hash"]
    del row["notified_content_hash"]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(row) + "\n"

    loaded = mem.get_plan("legacy1")
    assert loaded is not None
    assert loaded.plan_content_hash == compute_plan_content_hash(loaded)
    assert loaded.notified_content_hash == ""


def test_load_never_overwrites_stored_hash(mem_fs: dict[str, str]) -> None:
    """A non-empty stored hash survives load even when it no longer matches
    the content — verification recomputes at decision time instead."""
    mem = _make_memory(mem_fs)
    row = asdict(_hash_plan("legacy2"))
    row["plan_content_hash"] = "deadbeef" * 8
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(row) + "\n"

    loaded = mem.get_plan("legacy2")
    assert loaded is not None
    assert loaded.plan_content_hash == "deadbeef" * 8


def test_refine_plan_restamps_hash(mem_fs: dict[str, str]) -> None:
    """refine_plan rewrites content, so it must restamp the content hash."""
    from maestro.memory import compute_plan_content_hash

    mem = _make_memory(mem_fs)
    plan = _hash_plan("ph2")
    mem.save_evolution_plan(plan)
    original_hash = plan.plan_content_hash

    assert mem.refine_plan("ph2", rationale="A sharper rationale") is True

    refined = mem.get_plan("ph2")
    assert refined is not None
    assert refined.plan_content_hash != original_hash
    assert refined.plan_content_hash == compute_plan_content_hash(refined)


def test_refine_plan_rewrites_files_affected(mem_fs: dict[str, str]) -> None:
    """A ``files_affected``-only refine satisfies the at-least-one-field guard,
    rewrites the list, stamps ``refined_at`` and restamps the hash (#4520)."""
    from maestro.memory import compute_plan_content_hash

    mem = _make_memory(mem_fs)
    plan = _hash_plan("ph5")
    mem.save_evolution_plan(plan)
    original_hash = plan.plan_content_hash

    assert mem.refine_plan("ph5", files_affected=["maestro/memory.py"]) is True

    refined = mem.get_plan("ph5")
    assert refined is not None
    assert refined.files_affected == ["maestro/memory.py"]
    assert refined.refined_at is not None
    assert refined.plan_content_hash != original_hash
    assert refined.plan_content_hash == compute_plan_content_hash(refined)


@pytest.mark.parametrize(
    ("value", "exc"),
    [
        ([], ValueError),
        (["   "], ValueError),
        (["ok.py", ""], ValueError),
        (["x" * (_PLAN_FILE_PATH_MAX_CHARS + 1)], ValueError),
        (["f.py"] * (_PLAN_FILES_AFFECTED_MAX_ITEMS + 1), ValueError),
        (["ok.py", 3], TypeError),
        ("maestro/memory.py", TypeError),
    ],
)
def test_refine_plan_rejects_malformed_files_affected(
    mem_fs: dict[str, str], value: object, exc: type[Exception],
) -> None:
    mem = _make_memory(mem_fs)
    plan = _hash_plan("ph6")
    mem.save_evolution_plan(plan)

    with pytest.raises(exc):
        mem.refine_plan("ph6", files_affected=value)

    stored = mem.get_plan("ph6")
    assert stored is not None
    assert stored.files_affected == plan.files_affected


def test_update_plan_proposed_changes_restamps_hash(
    mem_fs: dict[str, str],
) -> None:
    """The scope-gate replan path rewrites steps, so it restamps the hash."""
    from maestro.memory import compute_plan_content_hash

    mem = _make_memory(mem_fs)
    plan = _hash_plan("ph3")
    mem.save_evolution_plan(plan)
    original_hash = plan.plan_content_hash

    assert mem.update_plan_proposed_changes("ph3", ["rewritten step"]) is True

    updated = mem.get_plan("ph3")
    assert updated is not None
    assert updated.plan_content_hash != original_hash
    assert updated.plan_content_hash == compute_plan_content_hash(updated)


def test_update_plan_status_sets_notified_hash(mem_fs: dict[str, str]) -> None:
    """The notified_content_hash kwarg persists the notify-time snapshot."""
    mem = _make_memory(mem_fs)
    plan = _hash_plan("ph4")
    mem.save_evolution_plan(plan)

    assert mem.update_plan_status(
        "ph4", "pending_review",
        notified_content_hash=plan.plan_content_hash,
    ) is True

    loaded = mem.get_plan("ph4")
    assert loaded is not None
    assert loaded.notified_content_hash == plan.plan_content_hash


def test_update_plan_status_notified_hash_type_error(
    mem_fs: dict[str, str],
) -> None:
    """A non-string notified_content_hash raises TypeError at the boundary."""
    mem = _make_memory(mem_fs)
    plan = _hash_plan("ph5")
    mem.save_evolution_plan(plan)

    with pytest.raises(TypeError):
        mem.update_plan_status(
            "ph5", "pending_review", notified_content_hash=42,
        )


def test_update_plan_status_readmits_blocked_plan(mem_fs: dict[str, str]) -> None:
    """One atomic call re-admits a blocked plan (proposal 131): status back to
    approved, blocked_question_id cleared, and the human answer stamped."""
    mem = _make_memory(mem_fs)
    plan = _hash_plan("ph6")
    plan.status = "blocked"
    plan.blocked_question_id = "msg-blk1"
    mem.save_evolution_plan(plan)

    assert mem.update_plan_status(
        "ph6", "approved",
        blocked_question_id=None,
        blocked_answer="the environment is fixed, retry",
    ) is True

    loaded = mem.get_plan("ph6")
    assert loaded is not None
    assert loaded.status == "approved"
    assert loaded.blocked_question_id is None
    assert loaded.blocked_answer == "the environment is fixed, retry"


def test_update_plan_status_blocked_answer_type_error(
    mem_fs: dict[str, str],
) -> None:
    """A non-string blocked_answer raises TypeError at the boundary."""
    mem = _make_memory(mem_fs)
    plan = _hash_plan("ph7")
    mem.save_evolution_plan(plan)

    with pytest.raises(TypeError):
        mem.update_plan_status(
            "ph7", "approved", blocked_answer=42,
        )


def test_mark_plan_obsolete_from_blocked_requires_allow_blocked(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 131 (r1): a ``blocked`` plan is retired to ``obsolete`` only via
    the ``allow_blocked`` opt-in — the default narrow accepted-status set leaves it
    untouched, so the abandonment route cannot fire from an unintended caller."""
    mem = _make_memory(mem_fs)
    plan = _hash_plan("ph8")
    plan.status = "blocked"
    plan.blocked_question_id = "msg-blk8"
    mem.save_evolution_plan(plan)

    # Default: a blocked plan is not an accepted obsolete source (no-op).
    assert mem.mark_plan_obsolete("ph8", "Thomas: abandon") is False
    unchanged = mem.get_plan("ph8")
    assert unchanged is not None
    assert unchanged.status == "blocked"

    # Opt-in: retires to obsolete with the reason recorded, rejection untouched.
    assert mem.mark_plan_obsolete(
        "ph8", "Thomas: abandon", allow_blocked=True,
    ) is True
    retired = mem.get_plan("ph8")
    assert retired is not None
    assert retired.status == "obsolete"
    assert retired.obsolete_reason == "Thomas: abandon"
    assert retired.rejection_reason is None


def test_load_backfills_hash_on_null_string_fields(
    mem_fs: dict[str, str],
) -> None:
    """A legacy row with JSON null in nominally-str fields still hashes —
    the canonicalisation treats non-str as empty rather than crashing."""
    mem = _make_memory(mem_fs)
    row = asdict(_hash_plan("legacy_null"))
    row["rationale"] = None
    row["expected_impact"] = None
    del row["plan_content_hash"]
    mem_fs[str(FAKE_DIR / "evolution_plans.jsonl")] = json.dumps(row) + "\n"

    loaded = mem.get_plan("legacy_null")
    assert loaded is not None
    assert loaded.plan_content_hash != ""
    assert len(loaded.plan_content_hash) == 64


# ---------------------------------------------------------------------------
# get_plan index freshness across instances (#3966)
# ---------------------------------------------------------------------------


def _index_plan(plan_id: str) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=f"Plan {plan_id}",
        category="capability",
        rationale="Because",
        proposed_changes=["Change X"],
        files_affected=["file.py"],
        expected_impact="Improve Y",
        created_at="2025-01-01T00:00:00Z",
    )


def test_get_plan_sees_plan_appended_by_other_instance(mem_fs: dict[str, str]) -> None:
    writer = _make_memory(mem_fs)
    reader = _make_memory(mem_fs)
    writer.save_evolution_plan(_index_plan("old"))
    assert reader.get_plan("old") is not None
    writer.save_evolution_plan(_index_plan("new"))
    found = reader.get_plan("new")
    assert found is not None
    assert found.plan_id == "new"


def test_get_plan_missing_id_after_rebuild_returns_none(mem_fs: dict[str, str]) -> None:
    reader = _make_memory(mem_fs)
    reader.save_evolution_plan(_index_plan("old"))
    with patch.object(
        reader, "_build_plan_index", wraps=reader._build_plan_index
    ) as build_spy:
        assert reader.get_plan("old") is not None
        assert build_spy.call_count == 1
        assert reader.get_plan("absent") is None
        assert build_spy.call_count == 2
    assert reader._plan_index is not None
    assert list(reader._plan_index) == ["old"]


def test_save_plan_after_partial_tail_keeps_new_plan(mem_fs: dict[str, str]) -> None:
    """A crash fragment at EOF must not swallow the next saved plan (#4164)."""
    mem = _make_memory(mem_fs)
    mem.save_evolution_plan(_make_plan("p0", title="zero"))
    path = str(FAKE_DIR / "evolution_plans.jsonl")
    mem_fs[path] += '{"plan_id": "crashed", "title": "half'

    mem.save_evolution_plan(_make_plan("p1", title="one"))
    assert mem.get_plan("p1") is not None

    mem.update_plan_status("p0", "approved")
    assert mem.get_plan("p1") is not None
