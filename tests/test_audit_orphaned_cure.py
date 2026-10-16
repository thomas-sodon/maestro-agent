"""Rule 11 (``orphaned_cure``) settlement-prefilter tests.

Split out of ``tests/test_audit_checks.py`` (a large file held
below ``SPLIT_BUDGET``) so the settlement-aware cases the ``check_orphaned_cure`` pre-filter added
live in a sibling module rather than growing the large one. The broader
orphaned-cure suite (age/lap/backend behaviour) stays in ``test_audit_checks.py``
alongside the rule's other assertions; this module covers only the
``settled_prs`` pre-filter and the ``_plan_pr_number`` parser it relies on. The
small fixtures are duplicated deliberately — a sibling test module does not
import private helpers across files.
"""

from __future__ import annotations

import json
from pathlib import Path

from maestro import audit_checks_phantom
from maestro.audit_checks_phantom import (
    JsonOrphanedCureStore,
    check_orphaned_cure,
)
from maestro.memory import EvolutionPlan, PlanStep, TaskRecord

_ORPHANED_DIR = Path("/fake/orphaned")
_ORPHANED_SIDECAR = str(_ORPHANED_DIR / "orphaned_cure_first_seen.json")
# The default JSON backend for the rule: it writes the same sidecar file, so the
# ``mem_fs[_ORPHANED_SIDECAR]`` seeding and content assertions stay valid.
_ORPHANED_BACKEND = JsonOrphanedCureStore(_ORPHANED_DIR)


def _impl_plan(
    plan_id: str = "ep1",
    status: str = "implemented",
    title: str = "Add the cure",
) -> EvolutionPlan:
    """An evolution plan in *status* (default ``implemented``)."""
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale="r",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status=status,  # type: ignore[arg-type]
    )


def _rem_task(task_id: str = "r1", pr: int = 10) -> TaskRecord:
    """A remediation-shaped task (one of the laps the cure would eliminate)."""
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr}: fix",
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome="partial",  # type: ignore[arg-type]
        result_summary="",
        errors=[],
        duration_secs=0.5,
        timestamp="2025-01-01T00:00:00Z",
        branch=None,
        key_findings=None,
    )


def test_orphaned_cure_silent_when_pr_settled_despite_stale_status(
    mem_fs: dict[str, str],
) -> None:
    """A still-``implemented`` plan whose PR is in *settled_prs* is pre-filtered.

    Reproduces the observed stuck loop: the ``check_merged_plans`` status flip
    lags behind a real merge (the phase starves behind live PR-conflict
    tending), so the cure plan sits ``implemented`` for hundreds of cycles while
    its PR is already merged and recorded in ``settled_verdicts.jsonl``. The
    settlement pre-filter must silence the rule anyway, and prune the plan from
    the age sidecar rather than re-touching it.
    """
    # ep1 is the settled cure (its status flip lags); ep2 is a genuinely-live
    # implemented cure so the rule body runs and prune_and_touch fires.
    mem_fs[_ORPHANED_SIDECAR] = json.dumps({"ep1": 0, "ep2": 0})
    settled = _impl_plan()
    settled.pr_url = "https://github.com/o/r/pull/3911"
    live = _impl_plan(plan_id="ep2")
    findings = check_orphaned_cure(
        [settled, live], [_rem_task()], _ORPHANED_BACKEND, 5,
        age_cycles=3, settled_prs=frozenset({3911}),
    )
    # The settled cure is silenced; only the live one fires.
    assert [f.cites for f in findings] == [["ep2"]]
    # ep1 was excluded before the touch ⇒ pruned from the first-seen map.
    assert json.loads(mem_fs[_ORPHANED_SIDECAR]) == {"ep2": 0}


def test_orphaned_cure_flags_when_pr_not_settled(
    mem_fs: dict[str, str],
) -> None:
    """A settled set that does not contain the plan's PR leaves the rule firing."""
    mem_fs[_ORPHANED_SIDECAR] = json.dumps({"ep1": 0})
    plan = _impl_plan()
    plan.pr_url = "https://github.com/o/r/pull/3911"
    findings = check_orphaned_cure(
        [plan], [_rem_task()], _ORPHANED_BACKEND, 5,
        age_cycles=3, settled_prs=frozenset({9999}),
    )
    assert len(findings) == 1
    assert findings[0].cites == ["ep1"]


def test_orphaned_cure_unparseable_pr_url_still_flags(
    mem_fs: dict[str, str],
) -> None:
    """A plan with no parseable PR URL is never in *settled_prs* → behaves as before."""
    mem_fs[_ORPHANED_SIDECAR] = json.dumps({"ep1": 0})
    plan = _impl_plan()
    plan.pr_url = None
    findings = check_orphaned_cure(
        [plan], [_rem_task()], _ORPHANED_BACKEND, 5,
        age_cycles=3, settled_prs=frozenset({3911}),
    )
    assert len(findings) == 1


def test_plan_pr_number_parsing() -> None:
    """`_plan_pr_number` mirrors the evolution parser but returns None on failure."""
    assert audit_checks_phantom._plan_pr_number("https://github.com/o/r/pull/3911") == 3911
    assert audit_checks_phantom._plan_pr_number("https://github.com/o/r/pull/3911/") == 3911
    assert audit_checks_phantom._plan_pr_number("https://github.com/o/r/pull/12?x=1") == 12
    assert audit_checks_phantom._plan_pr_number(None) is None
    assert audit_checks_phantom._plan_pr_number("") is None
    assert audit_checks_phantom._plan_pr_number("not-a-url") is None
    assert audit_checks_phantom._plan_pr_number("https://github.com/o/r/pull/0") is None
