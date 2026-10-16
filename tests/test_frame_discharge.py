"""Unit tests for the frame-discharge owner (proposal 112).

The owner is pure and I/O-free, so these build synthetic :class:`TaskRecord`s in
memory and assert the ``COSTUME`` / ``MATCH`` / ``N/A`` verdict plus the record-time
bind's flag-gated cap. No filesystem, network, or subprocess is touched (the owner
performs none); goal strings are chosen so ``classify_task_shape`` returns the
intended frame deterministically.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from maestro.frame_discharge import (
    FrameDischargeVerdict,
    assess_frame_discharge,
    classify_discharge,
    classify_frame,
    format_measurement_costume_summary,
    is_investigation_frame,
    measure_frame_discharge_costumes,
)
from maestro.memory import TaskRecord
from maestro.outcome_grounding import (
    _FRAME_DISCHARGE_MISMATCH_NOTE,
    bind_costume_discharge_to_partial,
)
from tests.conftest import MockQueryResult

_PLAN = json.dumps([{"description": "Step 1"}])


async def _mock_q(*args: object, **kwargs: object) -> MockQueryResult:
    return MockQueryResult(response="Investigated; recorded a marker.")

# Goal strings whose ``classify_task_shape`` frame is asserted alongside the tests
# below, so a change to the shared classifier that moves any of these surfaces here.
_INVESTIGATION_GOAL = "Investigate the throttle stall path"
_CONSTRUCTION_GOAL = "Build a helper to cache results"
_NON_SHAPE_GOAL = "Contemplate the nature of persistence"


def _record(
    goal: str,
    branch: str | None,
    *,
    outcome: str = "success",
    key_findings: str | None = None,
    task_id: str = "t-frame",
) -> TaskRecord:
    """A minimal completed ``TaskRecord`` for the owner to read."""
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="done",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-08-12T00:00:00Z",
        source="self_directed",
        branch=branch,
        key_findings=key_findings,
        artifacts_dir="artifacts/t-frame",
    )


# ── classify_frame delegates to the one goal-string classifier ─────────────────


def test_classify_frame_delegates_to_task_shape() -> None:
    """The frame side reuses ``classify_task_shape`` — no rival classifier."""
    assert classify_frame(_INVESTIGATION_GOAL) == "investigation"
    assert classify_frame(_CONSTRUCTION_GOAL) == "construction"
    assert classify_frame(_NON_SHAPE_GOAL) == "other"


# ── is_investigation_frame is the single frame-membership seam ──────────────────


def test_is_investigation_frame_true_only_for_investigation_shape() -> None:
    """The predicate is True iff the frame is in ``_INVESTIGATION_FRAMES``."""
    # Investigation frame → True (the one member of the frozenset today).
    assert is_investigation_frame(_INVESTIGATION_GOAL) is True
    # A construction frame is not an investigation frame → False.
    assert is_investigation_frame(_CONSTRUCTION_GOAL) is False
    # A non-shape goal (``classify_task_shape`` → ``other``) → False.
    assert is_investigation_frame(_NON_SHAPE_GOAL) is False


# ── classify_discharge keys purely on the branch watermark ─────────────────────


def test_classify_discharge_none_branch_is_investigation() -> None:
    """No kept branch (only ``artifacts/`` writes) → investigation discharge."""
    assert classify_discharge(_record(_INVESTIGATION_GOAL, None)) == "investigation"


def test_classify_discharge_set_branch_is_construction() -> None:
    """A kept branch (committed tracked files) → construction discharge."""
    record = _record(_INVESTIGATION_GOAL, "maestro/task/abc123")
    assert classify_discharge(record) == "construction"


# ── assess_frame_discharge stamps the verdict + provenance ─────────────────────


def test_investigation_frame_with_kept_branch_is_costume() -> None:
    """An investigation-framed goal that discharged tracked construction → COSTUME."""
    verdict = assess_frame_discharge(_record(_INVESTIGATION_GOAL, "maestro/task/x"))
    assert verdict.verdict == "COSTUME"
    # Provenance: the two sides the verdict was computed from are carried through.
    assert verdict.frame == "investigation"
    assert verdict.discharge == "construction"


def test_investigation_frame_artifacts_only_is_match() -> None:
    """An honest ``artifacts/``-only investigation (branch None) → MATCH.

    Guards against the ``false-clean-report`` false positive the production
    evidence flags: a clean investigation must never be punished.
    """
    verdict = assess_frame_discharge(_record(_INVESTIGATION_GOAL, None))
    assert verdict.verdict == "MATCH"
    assert verdict.frame == "investigation"
    assert verdict.discharge == "investigation"


def test_honest_construction_record_is_match() -> None:
    """A construction-framed goal that discharged construction → MATCH."""
    verdict = assess_frame_discharge(_record(_CONSTRUCTION_GOAL, "maestro/task/y"))
    assert verdict.verdict == "MATCH"
    assert verdict.frame == "construction"
    assert verdict.discharge == "construction"


def test_non_shape_goal_is_not_applicable() -> None:
    """A goal naming no shape (frame ``other``) is never judged → N/A."""
    verdict = assess_frame_discharge(_record(_NON_SHAPE_GOAL, "maestro/task/z"))
    assert verdict.verdict == "N/A"
    assert verdict.frame == "other"


def test_verdict_is_a_frozen_dataclass() -> None:
    """The verdict is a small frozen provenance record."""
    verdict = assess_frame_discharge(_record(_INVESTIGATION_GOAL, None))
    assert isinstance(verdict, FrameDischargeVerdict)


# ── The record-time bind composed over the owner (mirrors run()'s §5f) ─────────


def test_costume_record_capped_to_partial_under_the_flag() -> None:
    """Flag-on path: a COSTUME success is capped to partial with the note.

    Mirrors what ``run`` does when ``frame_discharge_bind_enabled`` is true —
    ``assess_frame_discharge`` feeds ``is_costume`` into the bind.
    """
    record = _record(_INVESTIGATION_GOAL, "maestro/task/x", key_findings="found it")
    verdict = assess_frame_discharge(record)
    outcome, findings = bind_costume_discharge_to_partial(
        record.key_findings,
        record.outcome,
        is_costume=verdict.verdict == "COSTUME",
    )
    assert outcome == "partial"
    assert findings is not None
    assert _FRAME_DISCHARGE_MISMATCH_NOTE in findings
    assert findings.startswith("found it")


def test_match_record_bind_is_a_noop_even_when_invoked() -> None:
    """A MATCH record leaves the outcome unchanged even if the bind is invoked."""
    record = _record(_INVESTIGATION_GOAL, None, key_findings="clean read")
    verdict = assess_frame_discharge(record)
    outcome, findings = bind_costume_discharge_to_partial(
        record.key_findings,
        record.outcome,
        is_costume=verdict.verdict == "COSTUME",
    )
    assert outcome == "success"
    assert findings == "clean read"


# ── run()'s §5f wiring (self-directed, remediation_pr_num is None) ──────────────


def _run_patches():  # type: ignore[no-untyped-def]
    """The mocked-SDK context every §5f run test shares."""
    return (
        patch("maestro.planner.query", new_callable=AsyncMock),
        patch("maestro.executor.query", new_callable=AsyncMock),
        patch("maestro.loop.task_delivery.check_merge_clean", return_value=None),
        patch("maestro.agent.query", side_effect=_mock_q),
        patch("maestro.agent.query_on_session", side_effect=_mock_q),
    )


@pytest.mark.asyncio
async def test_run_caps_costume_success_to_partial_under_flag(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: an investigation-framed self-directed task that keeps a branch
    (a construction discharge) is a COSTUME, capped success→partial with the note."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent.config.frame_discharge_bind_enabled = True
    agent._vcs.has_changes.return_value = True  # keeps the task branch

    p_plan, p_exec, p_merge, p_q, p_qs = _run_patches()
    with p_plan as mock_plan, p_exec as mock_exec, p_merge, p_q, p_qs:
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Done.")
        record = await agent.run(_INVESTIGATION_GOAL, source="self_directed")

    assert record.branch is not None  # construction discharge
    assert record.outcome == "partial"
    assert record.key_findings is not None
    assert _FRAME_DISCHARGE_MISMATCH_NOTE in record.key_findings


@pytest.mark.asyncio
async def test_run_leaves_costume_success_when_flag_off(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag off (default): the §5f bind never runs, so the same COSTUME record
    keeps its ``success`` outcome unchanged."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    assert agent.config.frame_discharge_bind_enabled is False
    agent._vcs.has_changes.return_value = True

    p_plan, p_exec, p_merge, p_q, p_qs = _run_patches()
    with p_plan as mock_plan, p_exec as mock_exec, p_merge, p_q, p_qs:
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Done.")
        record = await agent.run(_INVESTIGATION_GOAL, source="self_directed")

    assert record.branch is not None
    assert record.outcome == "success"
    assert _FRAME_DISCHARGE_MISMATCH_NOTE not in (record.key_findings or "")


@pytest.mark.asyncio
async def test_run_preserves_honest_construction_under_flag(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Flag on: an honest construction-framed task that keeps a branch is a MATCH,
    so the §5f bind is a no-op and ``success`` is preserved (no over-suppression)."""
    agent = make_agent()
    agent.config.enable_session_reuse = False
    agent.config.frame_discharge_bind_enabled = True
    agent._vcs.has_changes.return_value = True

    p_plan, p_exec, p_merge, p_q, p_qs = _run_patches()
    with p_plan as mock_plan, p_exec as mock_exec, p_merge, p_q, p_qs:
        mock_plan.return_value = MockQueryResult(response=_PLAN)
        mock_exec.return_value = MockQueryResult(response="Done.")
        record = await agent.run(_CONSTRUCTION_GOAL, source="self_directed")

    assert record.branch is not None
    assert record.outcome == "success"


# ── measure/format helpers feeding the MEASUREMENT-COSTUME sensor (Step 4) ──────


def test_measure_costumes_surfaces_costume_ids_in_window_order() -> None:
    """The measure returns the ids of COSTUME records only, in window order.

    An honest construction and an ``artifacts/``-only investigation are MATCH, so
    only the two investigation-framed kept-branch records surface, ordered as they
    appear in the window.
    """
    records = [
        _record(_CONSTRUCTION_GOAL, "maestro/task/a", task_id="honest"),
        _record(_INVESTIGATION_GOAL, "maestro/task/b", task_id="costume-1"),
        _record(_INVESTIGATION_GOAL, None, task_id="clean-inv"),
        _record(_INVESTIGATION_GOAL, "maestro/task/c", task_id="costume-2"),
    ]
    assert measure_frame_discharge_costumes(records) == ["costume-1", "costume-2"]


def test_measure_costumes_dedupes_repeated_task_id_keeping_first() -> None:
    """A ``task_id`` appearing twice among costumes keeps only its first position."""
    records = [
        _record(_INVESTIGATION_GOAL, "maestro/task/x", task_id="dup"),
        _record(_INVESTIGATION_GOAL, "maestro/task/y", task_id="dup"),
    ]
    assert measure_frame_discharge_costumes(records) == ["dup"]


def test_measure_costumes_empty_for_all_match_window() -> None:
    """A window with no COSTUME record (honest build + clean investigation) → []."""
    records = [
        _record(_CONSTRUCTION_GOAL, "maestro/task/a", task_id="honest"),
        _record(_INVESTIGATION_GOAL, None, task_id="clean-inv"),
    ]
    assert measure_frame_discharge_costumes(records) == []


def test_measure_costumes_empty_for_empty_window() -> None:
    """An empty window yields no costume ids."""
    assert measure_frame_discharge_costumes([]) == []


def test_format_costume_summary_names_ids_with_tag() -> None:
    """The line carries the MEASUREMENT-COSTUME tag and names each costume id."""
    line = format_measurement_costume_summary(["costume-1", "costume-2"])
    assert line.startswith("MEASUREMENT-COSTUME:")
    assert "costume-1" in line
    assert "costume-2" in line


def test_format_costume_summary_empty_on_empty_list() -> None:
    """An empty id list self-extinguishes to the empty string (like its siblings)."""
    assert format_measurement_costume_summary([]) == ""


def test_format_costume_summary_empty_when_max_ids_below_one() -> None:
    """``max_ids`` < 1 suppresses the line even when costume ids are present."""
    assert format_measurement_costume_summary(["costume-1"], max_ids=0) == ""


def test_format_costume_summary_caps_at_max_ids() -> None:
    """Only the first ``max_ids`` ids are named; the overflow id is omitted."""
    line = format_measurement_costume_summary(["zzz1", "zzz2", "zzz3"], max_ids=2)
    assert "zzz1" in line
    assert "zzz2" in line
    assert "zzz3" not in line
