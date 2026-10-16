"""Tests for the extracted ``maestro.loop.record_binds.apply_frame_discharge_cap``.

This is ``run``'s ``# 5f.`` block lifted into a named, independently-testable
function: gated behind the default-off ``[loop] frame_discharge_bind_enabled``, it
caps a self-directed task *framed* as a read-only investigation but *discharged*
as tracked construction (a kept branch) from collecting an undeserved
``success``, downgrading it to ``partial`` with a ``[frame-discharge-mismatch]``
note. The extraction is behaviour-preserving, so these tests pin every arm of the
function's own gate — including the domain-disjoint remediation arm the through-
``run`` tests can never reach — plus the in-place ``record`` mutation and the
returned tuple. The function is pure/I-O-free, so records are built in memory; the
COSTUME / MATCH record shapes mirror ``tests/test_frame_discharge.py``.
"""

from __future__ import annotations

from maestro.loop.record_binds import apply_frame_discharge_cap
from maestro.memory import TaskRecord
from maestro.outcome_grounding import _FRAME_DISCHARGE_MISMATCH_NOTE

# Goal strings whose ``classify_task_shape`` frame is fixed by the shared
# classifier (mirrors ``tests/test_frame_discharge.py``): an investigation frame
# discharged with a kept ``branch`` is a COSTUME; a construction frame is a MATCH.
_INVESTIGATION_GOAL = "Investigate the throttle stall path"
_CONSTRUCTION_GOAL = "Build a helper to cache results"


def _record(
    goal: str,
    branch: str | None,
    *,
    outcome: str = "success",
    key_findings: str | None = None,
    task_id: str = "t-cap",
) -> TaskRecord:
    """A minimal completed ``TaskRecord`` for the cap to read and mutate."""
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
        artifacts_dir="artifacts/t-cap",
    )


def test_costume_success_capped_to_partial(make_agent):  # type: ignore[no-untyped-def]
    """(a) COSTUME + flag on + no remediation PR → capped, record mutated, note.

    The returned locals must equal the mutated record fields so the post-record
    ``# 5e.`` / ``# 6.`` tail reads the same values ``record_task`` persisted.
    """
    agent = make_agent()
    agent.config.frame_discharge_bind_enabled = True
    record = _record(
        _INVESTIGATION_GOAL,
        branch="maestro/task/abc123",
        outcome="success",
        key_findings="Found the stall path",
    )

    outcome, key_findings = apply_frame_discharge_cap(
        agent,
        record=record,
        outcome="success",
        key_findings="Found the stall path",
        remediation_pr_num=None,
    )

    # Outcome capped, note appended to the uncapped findings.
    assert outcome == "partial"
    assert key_findings is not None
    assert key_findings.startswith("Found the stall path")
    assert _FRAME_DISCHARGE_MISMATCH_NOTE in key_findings
    # Record mutated in place before ``record_task``.
    assert record.outcome == "partial"
    assert record.key_findings == key_findings
    # Returned locals equal the mutated record fields.
    assert outcome == record.outcome
    assert key_findings == record.key_findings


def test_noop_when_flag_off(make_agent):  # type: ignore[no-untyped-def]
    """(b) Flag off (fixture default) → no-op, ``success`` preserved, no note."""
    agent = make_agent()
    assert agent.config.frame_discharge_bind_enabled is False
    record = _record(
        _INVESTIGATION_GOAL,
        branch="maestro/task/abc123",
        outcome="success",
        key_findings="Found the stall path",
    )

    outcome, key_findings = apply_frame_discharge_cap(
        agent,
        record=record,
        outcome="success",
        key_findings="Found the stall path",
        remediation_pr_num=None,
    )

    assert outcome == "success"
    assert key_findings == "Found the stall path"
    assert _FRAME_DISCHARGE_MISMATCH_NOTE not in (key_findings or "")
    # Record untouched.
    assert record.outcome == "success"
    assert record.key_findings == "Found the stall path"


def test_noop_for_remediation_pr(make_agent):  # type: ignore[no-untyped-def]
    """(c) Flag on but ``remediation_pr_num`` set → no-op (domain-disjoint arm).

    The genuinely-new assertion this extraction unlocks: §5d's mechanical
    authority owns remediation PRs, so the self-directed cap never fires for one.
    """
    agent = make_agent()
    agent.config.frame_discharge_bind_enabled = True
    record = _record(
        _INVESTIGATION_GOAL,
        branch="maestro/task/abc123",
        outcome="success",
        key_findings="Found the stall path",
    )

    outcome, key_findings = apply_frame_discharge_cap(
        agent,
        record=record,
        outcome="success",
        key_findings="Found the stall path",
        remediation_pr_num=123,
    )

    assert outcome == "success"
    assert key_findings == "Found the stall path"
    assert record.outcome == "success"
    assert record.key_findings == "Found the stall path"


def test_noop_for_non_costume_match(make_agent):  # type: ignore[no-untyped-def]
    """(d) Flag on but a MATCH (construction-framed) record → no-op."""
    agent = make_agent()
    agent.config.frame_discharge_bind_enabled = True
    record = _record(
        _CONSTRUCTION_GOAL,
        branch="maestro/task/abc123",
        outcome="success",
        key_findings="Built the helper",
    )

    outcome, key_findings = apply_frame_discharge_cap(
        agent,
        record=record,
        outcome="success",
        key_findings="Built the helper",
        remediation_pr_num=None,
    )

    assert outcome == "success"
    assert key_findings == "Built the helper"
    assert record.outcome == "success"
    assert record.key_findings == "Built the helper"
