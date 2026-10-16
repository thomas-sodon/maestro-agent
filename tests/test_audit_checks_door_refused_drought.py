"""Regression tests for #4157 — rule 18 must not credit door-refused mints.

``submit_for_review`` records a refused freshly minted plan ``obsolete`` and
stamps it ``refused_at_submission``; :func:`maestro.audit_checks.
measure_owned_act_drought` must skip that row, since it retires no plan that
already existed. Split out of ``tests/test_audit_checks.py``, whose size is
pinned shrink-only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from maestro.audit_checks import (
    _banked_obsolete_plan_ids,
    check_owned_act_drought,
    measure_owned_act_drought,
)
from maestro.memory import EvolutionPlan, JsonlVersionArchiveLedger
from maestro.remediation_tracker import JsonlStandDownVerdictLedger

_DIR = Path("/fake/door_refused_drought")
_T_OLD = "2025-06-01T00:00:00+00:00"
_T_MID = "2025-06-05T00:00:00+00:00"


def _seed(mem_fs: dict[str, str], *, refused: bool) -> None:
    """Two in-window tasks plus one obsolete row with the given provenance."""
    mem_fs[str(_DIR / "task_history.jsonl")] = "".join(
        json.dumps({"task_id": f"t{i}", "timestamp": _T_OLD, "outcome": "success"})
        + "\n"
        for i in range(2)
    )
    mem_fs[str(_DIR / "evolution_plans.jsonl")] = json.dumps({
        "plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_MID,
        "refused_at_submission": refused,
    }) + "\n"


def _pending(plan_id: str) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id, title=plan_id,
        category="capability", rationale="r", proposed_changes=["c"],
        files_affected=[], expected_impact="", status="pending_review",
        created_at=_T_OLD,
    )


def _count(predicate: Callable[[str], bool] | None) -> int:
    return measure_owned_act_drought(
        _DIR, 5,
        salvage_disqualifies=predicate,
        stand_down_backend=JsonlStandDownVerdictLedger(_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DIR),
    )


def _findings(predicate: Callable[[str], bool] | None) -> list[str]:
    findings = check_owned_act_drought(
        [_pending("pp1"), _pending("pp2")], _DIR, window=5, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DIR),
        salvage_disqualifies=predicate,
    )
    return [f.rule for f in findings]


@pytest.mark.parametrize("predicate", [None, lambda pr_url: False])
def test_owned_act_drought_ignores_door_refused_mint(
    mem_fs: dict[str, str], predicate: Callable[[str], bool] | None,
) -> None:
    """A door-refused mint neither counts nor silences the rule, on the default
    path and the salvage-predicate path."""
    _seed(mem_fs, refused=True)
    assert _count(predicate) == 0
    assert _findings(predicate) == ["owned_act_drought"]


def test_owned_act_drought_credits_backlog_prune_control(
    mem_fs: dict[str, str],
) -> None:
    """Control: an unmarked obsolete row (a prune of an existing plan) still
    counts and self-extinguishes the rule."""
    _seed(mem_fs, refused=False)
    assert _count(None) == 1
    assert _findings(None) == []


def test_banked_obsolete_plan_ids_skip_door_refused_mints(
    mem_fs: dict[str, str],
) -> None:
    """#4385 — a refused row contributes neither its hash nor its citations."""
    mem_fs[str(_DIR / "evolution_plans.jsonl")] = "".join(
        json.dumps(row) + "\n" for row in (
            {"plan_id": "aaaaaaaaaaaa", "status": "obsolete",
             "title": "Fix #4316", "refused_at_submission": True},
            {"plan_id": "bbbbbbbbbbbb", "status": "obsolete",
             "title": "Fix #4317"},
        )
    )

    hashes, pr_ids = _banked_obsolete_plan_ids(_DIR)

    assert hashes == {"bbbbbbbbbbbb"}
    assert pr_ids == {"#4317"}
