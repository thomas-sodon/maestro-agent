"""Tests for the mint-death metrics (proposal 136).

``classify_door_refusal_reason`` splits submission-door deaths by family,
``measure_dead_on_arrival_mints`` carries that split additively beside the
proposal-120 saturation keys, and ``measure_rejection_classes`` counts human
rejections in the grounding-failure classes. All pure — no I/O.
"""

from __future__ import annotations

import pytest

from maestro.audit_checks import (
    classify_door_refusal_reason,
    measure_dead_on_arrival_mints,
    measure_rejection_classes,
)
from maestro.memory import EvolutionPlan, RejectionRecord

# One reason rendered from each ``obsolete_reason=`` f-string in
# ``EvolutionEngine.submit_for_review``, with sample values substituted.
_AUTHORED_REASONS: list[tuple[str, str]] = [
    (
        "[ungrounded:duplicate] grounding probe symbol_exists on "
        "maestro/x.py is refuted by a fresh reading of the tree",
        "grounding",
    ),
    (
        "Re-cites source branch task-123 already claimed by a live plan — "
        "land or dispose that plan first",
        "restatement",
    ),
    (
        "Proposes only already-shipped CLI surface (gate-check) already "
        "registered in cli.py",
        "grounding",
    ),
    (
        "Cites unshipped CLI evidence (frob-report) — no such maestro "
        "subcommand on main or in this plan's own diff",
        "grounding",
    ),
    ("Near-duplicate of plan p9 already in flight", "restatement"),
    ("Themed restatement of 3 plans (a, b, c) already in flight", "restatement"),
    ("Theme-clusters with 4 plans already in flight", "restatement"),
    (
        "Admission gate refused net-new mint: backlog saturated (5 >= 5) — land "
        "or dispose an existing plan before proposing more",
        "saturation",
    ),
    (
        "Live-work backlog saturated: 7 live plans in flight (>= 5 cap) — "
        "land or dispose existing plans before proposing more",
        "saturation",
    ),
    (
        "[ungrounded-generation:duplicate] reality-inventory is refuted by the "
        "reality inventory",
        "grounding",
    ),
    (
        "[value-refused:missing] the plan states no admissible net-burn value",
        "value",
    ),
]


@pytest.mark.parametrize(("reason", "family"), _AUTHORED_REASONS)
def test_classify_door_refusal_reason_authored(reason: str, family: str) -> None:
    assert classify_door_refusal_reason(reason) == family


def test_classify_door_refusal_reason_sweep_prune_is_none() -> None:
    """A backlog-sweep prune reason is not a door refusal."""
    assert classify_door_refusal_reason("superseded by a newer plan") is None


@pytest.mark.parametrize("reason", [
    "Pruned: Near-duplicate of plan p9 per curator",
    "The curator judged this a Themed restatement of older work",
    "LLM says it Re-cites source branch task-1",
    "Curator: [value-refused:missing] echoed mid-sentence",
])
def test_classify_door_refusal_reason_mid_string_is_none(reason: str) -> None:
    """A free-text prune merely mentioning a phrasing is not a door death."""
    assert classify_door_refusal_reason(reason) is None


def _plan(
    plan_id: str,
    status: str = "obsolete",
    obsolete_reason: str | None = None,
    source_task_id: str | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title="t",
        category="capability",
        rationale="r",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status=status,  # type: ignore[arg-type]
        obsolete_reason=obsolete_reason,
        source_task_id=source_task_id,
        created_at=created_at,
    )


def test_measure_dead_on_arrival_mints_splits_by_reason() -> None:
    plans = [
        _plan("s", obsolete_reason=_AUTHORED_REASONS[7][0]),
        _plan("g", obsolete_reason=_AUTHORED_REASONS[0][0]),
        _plan("r", obsolete_reason=_AUTHORED_REASONS[4][0]),
        _plan("sweep", obsolete_reason="superseded by a newer plan"),
        _plan("none", obsolete_reason=None),
        _plan("m", status="merged", obsolete_reason=_AUTHORED_REASONS[0][0]),
    ]
    report = measure_dead_on_arrival_mints(plans)
    # Saturation keys unchanged: only the admission-gate plan counts.
    assert report["total"] == 6
    assert report["dead_on_arrival"] == 1
    assert report["by_channel"] == {"evolve": 1}
    assert report["by_reason"] == {
        "saturation": 1, "grounding": 1, "restatement": 1,
    }
    assert report["content_dead"] == 2
    assert report["content_fraction"] == pytest.approx(2 / 6)


def test_measure_dead_on_arrival_mints_zero_total() -> None:
    """An empty plan list yields the zeroed dict, not divide-by-zero."""
    assert measure_dead_on_arrival_mints([]) == {
        "total": 0,
        "dead_on_arrival": 0,
        "doa_fraction": 0.0,
        "by_channel": {},
        "by_reason": {},
        "content_dead": 0,
        "content_fraction": 0.0,
    }


def test_measure_dead_on_arrival_mints_truth_table() -> None:
    """A seeded set reproduces the full report by dict equality.

    p1 (evolve) and p2 (task-anchored) died at the saturated door; p3 was
    obsoleted for a non-door reason and p4 merged — neither counts.
    """
    plans = [
        _plan("p1", obsolete_reason=_AUTHORED_REASONS[7][0],
              created_at="2026-01-01T00:00:00Z"),
        _plan("p2", obsolete_reason=_AUTHORED_REASONS[8][0],
              source_task_id="maestro/task/abc",
              created_at="2026-01-02T00:00:00Z"),
        _plan("p3", obsolete_reason="superseded by a newer plan",
              created_at="2026-01-03T00:00:00Z"),
        _plan("p4", status="merged", created_at="2026-01-04T00:00:00Z"),
    ]
    assert measure_dead_on_arrival_mints(plans) == {
        "total": 4,
        "dead_on_arrival": 2,
        "doa_fraction": 0.5,
        "by_channel": {"evolve": 1, "task-anchored": 1},
        "by_reason": {"saturation": 2},
        "content_dead": 0,
        "content_fraction": 0.0,
    }


def test_measure_dead_on_arrival_mints_window_drops_oldest() -> None:
    """The n-window drops the oldest-by-created_at even when first in list order.

    An n=1 window keeps only the newer merged plan, so neither the saturation
    count nor the reason split sees the list-first door death.
    """
    plans = [
        _plan("old_doa", obsolete_reason=_AUTHORED_REASONS[7][0],
              created_at="2026-01-01T00:00:00Z"),
        _plan("new_ok", status="merged", created_at="2026-06-01T00:00:00Z"),
    ]
    assert measure_dead_on_arrival_mints(plans, n=1) == {
        "total": 1,
        "dead_on_arrival": 0,
        "doa_fraction": 0.0,
        "by_channel": {},
        "by_reason": {},
        "content_dead": 0,
        "content_fraction": 0.0,
    }


def _rejection(reason: str, category: str = "other") -> RejectionRecord:
    return RejectionRecord(
        plan_id="p",
        title="t",
        category="capability",
        priority="medium",
        rejection_reason=reason,
        timestamp="2026-01-01T00:00:00Z",
        rejection_category=category,  # type: ignore[arg-type]
    )


def test_measure_rejection_classes_compound_counts_once() -> None:
    """A compound reason hits two classes but any_grounding_class once."""
    report = measure_rejection_classes([
        _rejection("Duplicates X, which already exists, on a false premise"),
    ])
    assert report["by_class"] == {
        "already_shipped": 1, "false_premise": 1, "not_needed": 0,
    }
    assert report["any_grounding_class"] == 1
    assert report["any_grounding_fraction"] == 1.0


def test_measure_rejection_classes_not_needed_category_path() -> None:
    report = measure_rejection_classes([_rejection("meh", category="not_needed")])
    assert report["by_class"] == {
        "already_shipped": 0, "false_premise": 0, "not_needed": 1,
    }


def test_measure_rejection_classes_not_needed_text_path() -> None:
    report = measure_rejection_classes([_rejection("This is unnecessary")])
    assert report["by_class"] == {
        "already_shipped": 0, "false_premise": 0, "not_needed": 1,
    }


def test_measure_rejection_classes_unmatched_and_empty_reason() -> None:
    report = measure_rejection_classes([
        _rejection("wrong approach"), _rejection(""),
    ])
    assert report["total"] == 2
    assert report["any_grounding_class"] == 0
    assert report["any_grounding_fraction"] == 0.0


def test_measure_rejection_classes_window() -> None:
    """n keeps the newest n records (ledger order is oldest-first)."""
    records = [_rejection("already shipped"), _rejection("wrong approach")]
    report = measure_rejection_classes(records, n=1)
    assert report["total"] == 1
    assert report["any_grounding_class"] == 0


def test_measure_rejection_classes_empty() -> None:
    report = measure_rejection_classes([])
    assert report["total"] == 0
    assert report["any_grounding_class"] == 0
    assert report["any_grounding_fraction"] == 0.0


@pytest.mark.parametrize(("reason", "klass"), [
    ("This doesn\u2019t exist in the tree", "false_premise"),
    ("The feature already exist upstream", "already_shipped"),
])
def test_measure_rejection_classes_human_typed_variants(
    reason: str, klass: str,
) -> None:
    """Curly apostrophes and 'already exist' still classify."""
    report = measure_rejection_classes([_rejection(reason)])
    assert report["by_class"][klass] == 1


def test_measure_rejection_classes_bare_premise_does_not_match() -> None:
    """A reason approving the premise is not counted as false-premise."""
    report = measure_rejection_classes([_rejection("premise is fine, scope wrong")])
    assert report["by_class"]["false_premise"] == 0


def test_measure_dead_on_arrival_mints_counts_generation_grounding() -> None:
    """Proposal 136: an ``[ungrounded-generation:*]`` refusal is a grounding death."""
    plans = [
        _plan(
            "g",
            obsolete_reason=(
                "[ungrounded-generation:duplicate] cli:reality-inventory is "
                "refuted by the reality inventory"
            ),
        ),
    ]
    report = measure_dead_on_arrival_mints(plans)
    assert report["by_reason"] == {"grounding": 1}
    assert report["content_dead"] == 1


def test_measure_rejection_classes_tree_checkable_count() -> None:
    """tree_checkable routes through ``is_tree_checkable_rejection`` (143)."""
    report = measure_rejection_classes([
        _rejection("This already exists in cli.py"),
        _rejection("wrong approach"),
        _rejection(""),
    ])
    assert report["tree_checkable"] == 1


def test_value_refusal_counted_by_reason_but_not_content_dead() -> None:
    """A value refusal is its own family: outside content death and saturation."""
    plans = [
        _plan("v1", obsolete_reason="[value-refused:missing] no value"),
        _plan("g1", obsolete_reason="[ungrounded:duplicate] refuted"),
    ]
    result = measure_dead_on_arrival_mints(plans)
    assert result["by_reason"] == {"value": 1, "grounding": 1}
    assert result["content_dead"] == 1
