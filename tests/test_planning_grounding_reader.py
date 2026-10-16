"""Tests for the planning-grounding reader (proposal 143, step 2).

Kept out of ``tests/test_audit_checks.py``, which sits at its shrink-only size
pin in ``tests/test_source_file_size.py``.
"""

from __future__ import annotations

import pytest

from maestro import audit_checks
from maestro.audit_checks import (
    is_tree_checkable_rejection,
    measure_planning_grounding,
    measure_rejection_classes,
)
from maestro.memory import EvolutionPlan, RejectionRecord


def _plan(
    plan_id: str,
    status: str = "rejected",
    rejection_reason: str | None = None,
    obsolete_reason: str | None = None,
    files_affected: list[str] | None = None,
    grounded_reads: list[str] | None = None,
    created_at: str | None = "2026-09-01T00:00:00Z",
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title="t",
        category="capability",
        rationale="r",
        proposed_changes=[],
        files_affected=list(files_affected or []),
        expected_impact="",
        status=status,  # type: ignore[arg-type]
        rejection_reason=rejection_reason,
        obsolete_reason=obsolete_reason,
        grounded_reads=grounded_reads,
        created_at=created_at,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("reason", [
    "This is already implemented in evolution.py",
    "already exists",
    "Already shipped last week",
    "the flag is already registered",
    "already wired into the loop",
    "already landed on main",
    "the reader already has that",
    "it already produces this line",
    "already scopes the gate",
    "the door already does this",
    "the guard already refuses it",
    "already mechanically enforced",
    "False premise: nothing is broken",
    "wrong module",
    "Wrong file entirely",
    "wrong seam",
    "wrong path",
    "that logic lives in tend.py",
    "the symbol does not exist",
    "there is no such function",
    "phantom sensor",
    "cites a nonexistent helper",
])
def test_tree_checkable_keyword_families_match(reason: str) -> None:
    assert is_tree_checkable_rejection(reason) is True


def test_tree_checkable_is_case_insensitive() -> None:
    assert is_tree_checkable_rejection("ALREADY IMPLEMENTED") is True


@pytest.mark.parametrize("reason", [None, "", 42])
def test_tree_checkable_rejects_missing_reason(reason: object) -> None:
    assert is_tree_checkable_rejection(reason) is False  # type: ignore[arg-type]


def test_duplicate_only_reason_is_not_tree_checkable() -> None:
    assert is_tree_checkable_rejection("duplicate of the pending plan") is False


def test_reader_reproduces_every_count() -> None:
    plans = [
        _plan("r1", rejection_reason="already implemented"),
        _plan("r2", rejection_reason="false premise"),
        _plan("r3", rejection_reason="too broad"),
        _plan("r4", rejection_reason=None),
        _plan("m1", status="merged"),
        _plan("o1", status="obsolete",
              obsolete_reason="[ungrounded:duplicate] probe refuted"),
        _plan("o2", status="obsolete", obsolete_reason="backlog saturated"),
        _plan("o3", status="obsolete", obsolete_reason=None),
        _plan("p1", status="pending_review"),
    ]
    report = measure_planning_grounding(plans)
    assert report["decided"] == 5
    assert report["rejected"] == 4
    assert report["tree_checkable"] == 2
    assert report["tree_checkable_fraction"] == 0.5
    assert report["door_refusals"] == 1
    assert report["with_reads"] == 0
    assert report["no_reads_recorded"] == 9
    assert report["read_coverage"] is None


def test_empty_window_guards_fraction() -> None:
    report = measure_planning_grounding([])
    assert report["tree_checkable_fraction"] == 0.0
    assert report["read_coverage"] is None


def test_since_window_boundary_and_null_created_at() -> None:
    plans = [
        _plan("before", rejection_reason="phantom", created_at="2026-08-15T23:59:59Z"),
        _plan("on", rejection_reason="phantom", created_at="2026-08-16"),
        _plan("after", rejection_reason="ok", created_at="2026-08-20T00:00:00Z"),
        _plan("null", rejection_reason="phantom", created_at=None),
        _plan("empty", rejection_reason="phantom", created_at=""),
    ]
    report = measure_planning_grounding(plans, since="2026-08-16")
    assert report["rejected"] == 2
    assert report["tree_checkable"] == 1
    unbounded = measure_planning_grounding(plans)
    assert unbounded["rejected"] == 5


def test_read_coverage_full_partial_and_empty_reads() -> None:
    plans = [
        _plan("full", files_affected=["maestro/a.py"],
              grounded_reads=["./maestro/a.py"]),
        _plan("half", files_affected=["./maestro/a.py", "maestro/b.py"],
              grounded_reads=["maestro/a.py", "SomeSymbol"]),
        _plan("none", files_affected=["maestro/c.py"], grounded_reads=[]),
        _plan("no-files", files_affected=[], grounded_reads=["maestro/a.py"]),
        _plan("legacy", files_affected=["maestro/a.py"]),
    ]
    report = measure_planning_grounding(plans)
    assert report["with_reads"] == 4
    assert report["no_reads_recorded"] == 1
    assert report["read_coverage"] == pytest.approx(0.5)


def test_read_coverage_none_when_only_empty_files_affected() -> None:
    plans = [_plan("x", files_affected=[], grounded_reads=["maestro/a.py"])]
    report = measure_planning_grounding(plans)
    assert report["with_reads"] == 1
    assert report["read_coverage"] is None


def test_host_closed_rejection_is_not_a_human_rejection() -> None:
    plans = [
        _plan("h1", rejection_reason="PR closed without merging (host CLOSED-unmerged)"),
        _plan("t1", rejection_reason="Rejected via Telegram"),
    ]
    report = measure_planning_grounding(plans)
    assert report["rejected"] == 1
    assert report["decided"] == 1
    assert report["tree_checkable"] == 0


def test_refusals_by_tag_splits_every_grounding_family() -> None:
    """Proposal 143 step 5: machine refusals split by reason tag."""
    base = "[ungrounded:no-such-defect] grounding probe DEFECT_PRESENT on x.py is refuted"
    plans = [
        _plan("r1", status="obsolete", obsolete_reason=base + " [planner-read]"),
        _plan("r2", status="obsolete", obsolete_reason=base + " [planner-read]"),
        _plan("u1", status="obsolete", obsolete_reason=base + " [planner-unread]"),
        _plan("t1", status="obsolete", obsolete_reason=base),
        _plan("g1", status="obsolete",
              obsolete_reason="[ungrounded-generation:shipped] already exists"),
        _plan("c1", status="obsolete",
              obsolete_reason="Proposes only already-shipped CLI surface: x"),
        _plan("c2", status="obsolete",
              obsolete_reason="Cites unshipped CLI evidence: maestro foo"),
        _plan("other", status="obsolete", obsolete_reason="Near-duplicate of p1"),
        _plan("none", status="obsolete", obsolete_reason=None),
        _plan("live", status="pending_review",
              obsolete_reason=base + " [planner-read]"),
    ]
    report = measure_planning_grounding(plans)
    assert report["refusals_by_tag"] == {
        "ungrounded:read": 2,
        "ungrounded:unread": 1,
        "ungrounded:untagged": 1,
        "ungrounded-generation": 1,
        "cli-shipped": 1,
        "cli-evidence": 1,
    }
    assert report["door_refusals"] == 4


def test_refusals_by_tag_all_zero_on_empty_window() -> None:
    report = measure_planning_grounding([])
    assert report["refusals_by_tag"] == dict.fromkeys(
        ("ungrounded:read", "ungrounded:unread", "ungrounded:untagged",
         "ungrounded-generation", "cli-shipped", "cli-evidence"), 0,
    )


def _rejection(reason: str) -> RejectionRecord:
    return RejectionRecord(
        plan_id="p",
        title="t",
        category="capability",
        priority="medium",
        rejection_reason=reason,
        timestamp="2026-01-01T00:00:00Z",
        rejection_category="other",
    )


@pytest.mark.parametrize(
    ("reason", "expected_tree"),
    [
        ("the module it patches does not exist", True),
        ("the module doesn't exist", True),
        ("the module doesn\u2019t exist", True),
        ("already ships", True),
        ("already shipped", True),
        ("already exist", True),
        ("already exists", True),
        ("misread the code", True),
        ("the premise is false", True),
        ("false premise", True),
        ("not true", True),
        ("wrong module", True),
        ("lives in tend.py", True),
        ("phantom sensor", True),
        ("nonexistent helper", True),
        ("no such function", True),
        ("already registered", True),
        ("already wired", True),
        ("wrong approach", False),
        ("duplicate of the pending plan", False),
        ("This is unnecessary", False),
        ("premise is fine, scope wrong", False),
    ],
)
def test_rejection_breakdown_partitions_tree_checkable(
    reason: str, expected_tree: bool,
) -> None:
    """A reason is tree-checkable iff it lands in a grounding sub-class (#4392)."""
    report = measure_rejection_classes([_rejection(reason)])
    by_class = report["by_class"]
    assert isinstance(by_class, dict)
    in_breakdown = by_class["already_shipped"] + by_class["false_premise"] >= 1
    tree_hit = report["tree_checkable"] == 1
    assert tree_hit == expected_tree
    assert in_breakdown == expected_tree


def test_owner_accepts_previously_forked_spellings() -> None:
    assert is_tree_checkable_rejection("the module doesn't exist") is True
    assert is_tree_checkable_rejection("already ships") is True


def test_tree_checkable_is_union_of_breakdown() -> None:
    expected = (
        audit_checks._ALREADY_SHIPPED_REJECTION_RE.pattern
        + "|"
        + audit_checks._FALSE_PREMISE_REJECTION_RE.pattern
    )
    assert audit_checks._TREE_CHECKABLE_REJECTION_RE.pattern == expected
