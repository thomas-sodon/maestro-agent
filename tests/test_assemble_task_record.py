"""Tests for the extracted ``maestro.loop.task_record.assemble_task_record``.

This is ``run``'s inline ``record = TaskRecord(...)`` block lifted into a named,
independently-testable stage (proposal 106): it maps the completed-run locals
onto the ``TaskRecord`` fields — with the ``steps`` → ``plan`` /
``summary`` → ``result_summary`` / ``kept_branch`` → ``branch`` /
``artifacts_path`` → ``artifacts_dir`` / ``*_stamp`` → ``pr_head_sha`` /
``base_head_sha`` / ``pr_readiness_snapshot`` renames — and computes the three
derived fields (``timestamp``, ``duration_secs``, ``artifacts_dir``) inside. The
extraction is a verbatim, byte-for-byte lift, so these tests pin the whole field
mapping (previously only assertable end-to-end through a full ``run``), the
derived-field computation, and the append-only optional-field pass-through
including their ``None`` defaults. The method is pure/I-O-free, so no cleanup is
needed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from maestro.agent import MaestroAgent
from maestro.loop.task_record import assemble_task_record
from maestro.memory import PlanStep, TaskRecord

# Module-level so the ``steps`` → ``plan`` mapping can be pinned by identity
# (the lift copies nothing). Annotated to the param types so the mutable literals
# are accepted by mypy without an invariant-dict/list mismatch.
_STEPS: list[PlanStep] = [PlanStep(step_id="s1", description="do the thing")]
_CRITERIA: list[dict[str, str]] = [
    {"criterion": "c1", "verdict": "MET", "evidence": "e"},
]
_READINESS: dict[str, object] = {"threads": 0, "behind": False}


def _assemble(
    agent: MaestroAgent,
    *,
    duration: float = 12.3456,
    kept_branch: str | None = "maestro/task/abc123",
    key_findings: str | None = "found it",
    linked_question: str | None = "what is the record shape?",
    criteria_verdicts: list[dict[str, str]] | None = None,
    pr_head_sha_stamp: str | None = "deadbeef",
    base_head_sha_stamp: str | None = "cafef00d",
    merge_clean: bool | None = True,
    pr_readiness_snapshot_stamp: dict[str, object] | None = None,
    push_delivered: bool | None = True,
    push_error: str | None = "push failed once",
    workspace_moment_id: str | None = "moment-1",
    workspace_item_ids: list[str] | None = None,
) -> TaskRecord:
    """Call the stage with distinct, testable values, overridable per test so a
    per-field assertion catches a swapped, dropped, or mis-renamed mapping."""
    return assemble_task_record(
        agent,
        task_id="t-assemble",
        goal="Extract the record-assembly stage",
        steps=_STEPS,
        outcome="success",
        summary="all steps done",
        errors=["boom"],
        duration=duration,
        source="self_directed",
        kept_branch=kept_branch,
        key_findings=key_findings,
        artifacts_path=Path("/fake/artifacts/t-assemble"),
        replans=2,
        linked_question=linked_question,
        criteria_verdicts=criteria_verdicts,
        pr_head_sha_stamp=pr_head_sha_stamp,
        base_head_sha_stamp=base_head_sha_stamp,
        merge_clean=merge_clean,
        pr_readiness_snapshot_stamp=pr_readiness_snapshot_stamp,
        push_delivered=push_delivered,
        push_error=push_error,
        workspace_moment_id=workspace_moment_id,
        workspace_item_ids=workspace_item_ids,
    )


def test_maps_every_local_to_its_field(make_agent):  # type: ignore[no-untyped-def]
    """Every input local lands on its ``TaskRecord`` field, renames included."""
    agent = make_agent()

    record = _assemble(
        agent,
        criteria_verdicts=_CRITERIA,
        pr_readiness_snapshot_stamp=_READINESS,
        workspace_item_ids=["item-a", "item-b"],
    )

    assert isinstance(record, TaskRecord)
    # Straight-through fields.
    assert record.task_id == "t-assemble"
    assert record.goal == "Extract the record-assembly stage"
    assert record.outcome == "success"
    assert record.errors == ["boom"]
    assert record.source == "self_directed"
    assert record.key_findings == "found it"
    assert record.replans == 2
    assert record.linked_question == "what is the record shape?"
    assert record.criteria_verdicts == _CRITERIA
    assert record.merge_clean is True
    assert record.pr_readiness_snapshot == _READINESS
    assert record.push_delivered is True
    assert record.push_error == "push failed once"
    assert record.workspace_moment_id == "moment-1"
    assert record.workspace_item_ids == ["item-a", "item-b"]
    # Renamed fields.
    assert record.plan is _STEPS
    assert record.result_summary == "all steps done"
    assert record.branch == "maestro/task/abc123"
    assert record.artifacts_dir == "/fake/artifacts/t-assemble"
    assert record.pr_head_sha == "deadbeef"
    assert record.base_head_sha == "cafef00d"


def test_derived_fields(make_agent):  # type: ignore[no-untyped-def]
    """``duration_secs`` is the rounded *duration*; ``timestamp`` is ISO-8601;
    ``artifacts_dir`` is ``str(artifacts_path)``."""
    agent = make_agent()

    record = _assemble(agent, duration=12.3456)

    assert record.duration_secs == 12.35
    # ``timestamp`` parses as a timezone-aware ISO-8601 datetime.
    parsed = datetime.fromisoformat(record.timestamp)
    assert parsed.tzinfo is not None
    assert record.artifacts_dir == "/fake/artifacts/t-assemble"


def test_optional_fields_pass_through_none(make_agent):  # type: ignore[no-untyped-def]
    """The append-only optional locals pass through as ``None`` when unset."""
    agent = make_agent()

    record = _assemble(
        agent,
        kept_branch=None,
        key_findings=None,
        linked_question=None,
        criteria_verdicts=None,
        pr_head_sha_stamp=None,
        base_head_sha_stamp=None,
        merge_clean=None,
        pr_readiness_snapshot_stamp=None,
        push_delivered=None,
        push_error=None,
        workspace_moment_id=None,
        workspace_item_ids=None,
    )

    assert record.branch is None
    assert record.key_findings is None
    assert record.linked_question is None
    assert record.criteria_verdicts is None
    assert record.pr_head_sha is None
    assert record.base_head_sha is None
    assert record.merge_clean is None
    assert record.pr_readiness_snapshot is None
    assert record.push_delivered is None
    assert record.push_error is None
    assert record.workspace_moment_id is None
    assert record.workspace_item_ids is None



