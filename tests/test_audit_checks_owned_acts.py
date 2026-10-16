"""Owned-act drought, banked reperformance, plan-mill and generative-debt
measures; split out of ``tests/test_audit_checks.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from maestro import audit_checks
from maestro.audit_checks import (
    _DIRECTIVE_MAX_CHARS,
    MAX_FINDINGS_PER_RULE,
    check_banked_owned_act_reperformance,
    check_owned_act_drought,
    classify_plan_mint_channel,
    classify_self_measurement_plan,
    format_dead_on_arrival_mints_summary,
    format_ungrounded_rate_summary,
    limitations_over_generative_debt,
    measure_dead_on_arrival_mints,
    measure_merged_to_created_ratio,
    measure_owned_act_drought,
    measure_plan_theme_saturation,
    measure_stranded_promotion_plans,
    measure_task_coupled_plan_fraction,
    measure_ungrounded_premise_rate,
)
from maestro.audit_checks_calibration import (
    format_self_calibration_summary,
    record_reflection_prediction,
    record_reflection_verdict,
)
from maestro.consciousness import (
    SelfModel,
    _normalize_question,
    limitation_question_candidate,
)
from maestro.memory import (
    EvolutionPlan,
    JsonlVersionArchiveLedger,
    Memory,
    PlanStep,
    QuestionResolution,
    TaskRecord,
)
from maestro.plan_grounding import GroundingProbe
from maestro.remediation_tracker import JsonlStandDownVerdictLedger
from tests.test_audit_checks import (
    FAKE_DIR,
    _impl_plan,
    _prediction_backend,
    _resolution,
    _task,
)

# ---------------------------------------------------------------------------
# Rule 18: owned_act_drought
# ---------------------------------------------------------------------------

_DROUGHT_DIR = Path("/fake/drought")

# Ordered UTC ISO-8601 instants used to place the cycle clock and the owned
# acts relative to it. ``_T_OLD`` < ``_T_MID`` < ``_T_NEW`` lexicographically
# *and* chronologically (same ``+00:00`` offset), which is the comparison the
# measure relies on.
_T_OLD = "2025-06-01T00:00:00+00:00"
_T_MID = "2025-06-05T00:00:00+00:00"
_T_NEW = "2025-06-10T00:00:00+00:00"


def _seed_drought_tasks(
    mem_fs: dict[str, str],
    timestamps: list[str],
    data_dir: Path = _DROUGHT_DIR,
) -> None:
    """Seed ``task_history.jsonl`` with one record per *timestamp* (the clock)."""
    mem_fs[str(data_dir / "task_history.jsonl")] = "".join(
        json.dumps({"task_id": f"t{i}", "timestamp": ts, "outcome": "success"})
        + "\n"
        for i, ts in enumerate(timestamps)
    )


def _seed_obsolete_plans(
    mem_fs: dict[str, str],
    records: list[dict[str, object]],
    data_dir: Path = _DROUGHT_DIR,
) -> None:
    """Seed ``evolution_plans.jsonl`` with raw *records* (one line each)."""
    mem_fs[str(data_dir / "evolution_plans.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in records
    )


def _seed_stand_downs(
    mem_fs: dict[str, str],
    recorded_ats: list[str],
    data_dir: Path = _DROUGHT_DIR,
) -> None:
    """Seed ``stand_down_verdicts.jsonl`` with one verdict per *recorded_at*."""
    mem_fs[str(data_dir / "stand_down_verdicts.jsonl")] = "".join(
        json.dumps({"pr": 100 + i, "head_sha": f"sha{i}", "recorded_at": ts})
        + "\n"
        for i, ts in enumerate(recorded_ats)
    )


def _merged_version_dict(**overrides: Any) -> dict[str, Any]:
    """A full ``VersionRecord`` dict — the shape ``record_version`` persists.

    The backend-based ``_read_merged_pr_timestamps`` hydrates each row through
    ``VersionRecord`` (via ``_parse_version_dict``), so a row must carry every
    required field; production rows always do. Callers override ``merged_at`` (and
    ``version_id``/``plan_id``) to shape the drought window.
    """
    d: dict[str, Any] = {
        "version_id": "v",
        "plan_id": "ep",
        "plan_title": "t",
        "commit_sha": "sha",
        "merged_at": _T_MID,
        "window_tasks_total": 0,
        "window_tasks_succeeded": 0,
        "window_tasks_failed": 0,
        "window_sdk_cost_usd": 0.0,
        "window_sdk_calls": 0,
        "window_cache_hit_rate": 0.0,
        "window_rate_limit_hits": 0,
        "window_error_count": 0,
        "quality_gate_pass": True,
        "notes": None,
    }
    d.update(overrides)
    return d


def _seed_merged_prs(
    mem_fs: dict[str, str],
    merged_ats: list[str],
    data_dir: Path = _DROUGHT_DIR,
) -> None:
    """Seed ``version_archive.jsonl`` with one merged record per *merged_at*."""
    mem_fs[str(data_dir / "version_archive.jsonl")] = "".join(
        json.dumps(_merged_version_dict(
            version_id=f"v{i}", plan_id=f"ep{i}", merged_at=ts,
        )) + "\n"
        for i, ts in enumerate(merged_ats)
    )


def _pending_plan(plan_id: str) -> EvolutionPlan:
    """A prunable ``pending_review`` plan (counts toward the backlog floor)."""
    return _impl_plan(plan_id=plan_id, status="pending_review")


# -- measure_owned_act_drought ---------------------------------------------


def test_measure_owned_act_drought_counts_both_act_types(
    mem_fs: dict[str, str],
) -> None:
    """Both an obsolete plan-status event and a chosen stand-down inside the
    window are counted (criterion 1)."""
    _seed_drought_tasks(mem_fs, [_T_OLD, _T_OLD])
    _seed_obsolete_plans(mem_fs, [
        {"plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_MID},
        {"plan_id": "ep2", "status": "pending_review", "reviewed_at": _T_MID},
    ])
    _seed_stand_downs(mem_fs, [_T_MID])
    # One obsolete event + one stand-down row, both at/after window_start.
    assert measure_owned_act_drought(_DROUGHT_DIR, 5, stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR), version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR)) == 2


def test_measure_owned_act_drought_window_excludes_old_acts(
    mem_fs: dict[str, str],
) -> None:
    """An owned act older than the cycle-clock window is not counted, so the
    measure re-arms as the clock advances."""
    # Clock sits at _T_NEW; the only owned act is older (_T_OLD).
    _seed_drought_tasks(mem_fs, [_T_NEW, _T_NEW])
    _seed_obsolete_plans(mem_fs, [
        {"plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_OLD},
    ])
    _seed_stand_downs(mem_fs, [_T_OLD])
    assert measure_owned_act_drought(_DROUGHT_DIR, 5, stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR), version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR)) == 0


def test_measure_owned_act_drought_non_positive_window_is_zero(
    mem_fs: dict[str, str],
) -> None:
    """A non-positive window short-circuits to 0 with no reads."""
    _seed_obsolete_plans(mem_fs, [
        {"plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_MID},
    ])
    assert measure_owned_act_drought(_DROUGHT_DIR, 0, stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR), version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR)) == 0


def test_measure_owned_act_drought_no_clock_counts_all_acts(
    mem_fs: dict[str, str],
) -> None:
    """With no task clock the window lower bound is empty, so any owned act
    ever recorded counts."""
    _seed_obsolete_plans(mem_fs, [
        {"plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_OLD},
    ])
    _seed_stand_downs(mem_fs, [_T_OLD])
    assert measure_owned_act_drought(_DROUGHT_DIR, 5, stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR), version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR)) == 2


def test_measure_owned_act_drought_zero_when_no_acts(
    mem_fs: dict[str, str],
) -> None:
    """No durable owned-act files at all → count 0 (the drought state)."""
    _seed_drought_tasks(mem_fs, [_T_MID, _T_MID])
    assert measure_owned_act_drought(_DROUGHT_DIR, 5, stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR), version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR)) == 0


def test_read_obsolete_plan_timestamps_tolerates_corruption(
    mem_fs: dict[str, str],
) -> None:
    """Malformed lines, non-dict records, non-obsolete plans are skipped; a
    missing reviewed_at falls back to created_at then the empty string."""
    from maestro.audit_checks import _read_obsolete_plan_timestamps

    mem_fs[str(_DROUGHT_DIR / "evolution_plans.jsonl")] = "\n".join([
        "not json",
        json.dumps([1, 2, 3]),  # non-dict
        json.dumps({"plan_id": "p1", "status": "pending_review"}),  # skipped
        json.dumps({"plan_id": "p2", "status": "obsolete", "reviewed_at": _T_MID}),
        json.dumps({"plan_id": "p3", "status": "obsolete", "created_at": _T_OLD}),
        json.dumps({"plan_id": "p4", "status": "obsolete"}),  # both missing → ""
        "",
    ]) + "\n"
    assert _read_obsolete_plan_timestamps(_DROUGHT_DIR) == [_T_MID, _T_OLD, ""]


def test_read_obsolete_plan_timestamps_missing_file(
    mem_fs: dict[str, str],
) -> None:
    """Absent plan store → empty list."""
    from maestro.audit_checks import _read_obsolete_plan_timestamps

    assert _read_obsolete_plan_timestamps(_DROUGHT_DIR) == []


def test_read_obsolete_plan_timestamps_oserror(
    mem_fs: dict[str, str],
) -> None:
    """An OSError while reading the plan store degrades to an empty list."""
    from maestro.audit_checks import _read_obsolete_plan_timestamps

    _seed_obsolete_plans(mem_fs, [
        {"plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_MID},
    ])
    with patch("builtins.open", side_effect=OSError("disk error")):
        assert _read_obsolete_plan_timestamps(_DROUGHT_DIR) == []


def test_read_obsolete_plan_events_order_and_pr_url_fallback(
    mem_fs: dict[str, str],
) -> None:
    """_read_obsolete_plan_events yields (pr_url, timestamp) per obsolete event in
    file order: a string pr_url is kept; an absent, None, or non-string pr_url
    falls back to ""; non-obsolete, malformed, and non-dict records are skipped;
    the timestamp keeps the reviewed_at → created_at → "" fallback (proposal 094
    step 1)."""
    from maestro.audit_checks import _read_obsolete_plan_events

    mem_fs[str(_DROUGHT_DIR / "evolution_plans.jsonl")] = "\n".join([
        "not json",
        json.dumps([1, 2, 3]),  # non-dict
        json.dumps({"plan_id": "p0", "status": "pending_review"}),  # skipped
        json.dumps({
            "plan_id": "p1", "status": "obsolete",
            "pr_url": "https://github.com/o/r/pull/7", "reviewed_at": _T_NEW,
        }),
        # pr_url absent → ""; reviewed_at absent → created_at fallback.
        json.dumps({"plan_id": "p2", "status": "obsolete", "created_at": _T_OLD}),
        # pr_url None → "".
        json.dumps({
            "plan_id": "p3", "status": "obsolete", "pr_url": None,
            "reviewed_at": _T_MID,
        }),
        # pr_url non-string → ""; both timestamps missing → "".
        json.dumps({"plan_id": "p4", "status": "obsolete", "pr_url": 123}),
        "",
    ]) + "\n"
    assert _read_obsolete_plan_events(_DROUGHT_DIR) == [
        ("https://github.com/o/r/pull/7", _T_NEW),
        ("", _T_OLD),
        ("", _T_MID),
        ("", ""),
    ]


def test_read_obsolete_plan_events_missing_file(
    mem_fs: dict[str, str],
) -> None:
    """Absent plan store → empty list."""
    from maestro.audit_checks import _read_obsolete_plan_events

    assert _read_obsolete_plan_events(_DROUGHT_DIR) == []


def test_read_obsolete_plan_events_oserror(
    mem_fs: dict[str, str],
) -> None:
    """An OSError while reading the plan store degrades to an empty list."""
    from maestro.audit_checks import _read_obsolete_plan_events

    _seed_obsolete_plans(mem_fs, [
        {"plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_MID},
    ])
    with patch("builtins.open", side_effect=OSError("disk error")):
        assert _read_obsolete_plan_events(_DROUGHT_DIR) == []


def test_read_stand_down_timestamps_tolerates_corruption(
    mem_fs: dict[str, str],
) -> None:
    """Malformed lines, non-dict records, and rows lacking recorded_at are
    skipped."""
    from maestro.audit_checks import _read_stand_down_timestamps

    mem_fs[str(_DROUGHT_DIR / "stand_down_verdicts.jsonl")] = "\n".join([
        "not json",
        json.dumps("a string"),  # non-dict
        json.dumps({"pr": 1, "head_sha": "x"}),  # no recorded_at
        json.dumps({"pr": 2, "head_sha": "y", "recorded_at": _T_MID}),
        "",
    ]) + "\n"
    assert _read_stand_down_timestamps(JsonlStandDownVerdictLedger(_DROUGHT_DIR)) == [_T_MID]


def test_read_stand_down_timestamps_missing_file(
    mem_fs: dict[str, str],
) -> None:
    """Absent stand-down log → empty list."""
    from maestro.audit_checks import _read_stand_down_timestamps

    assert _read_stand_down_timestamps(JsonlStandDownVerdictLedger(_DROUGHT_DIR)) == []


def test_read_stand_down_timestamps_oserror(
    mem_fs: dict[str, str],
) -> None:
    """An OSError while reading the stand-down log degrades to an empty list."""
    from maestro.audit_checks import _read_stand_down_timestamps

    _seed_stand_downs(mem_fs, [_T_MID])
    with patch("builtins.open", side_effect=OSError("disk error")):
        assert _read_stand_down_timestamps(JsonlStandDownVerdictLedger(_DROUGHT_DIR)) == []


def test_read_merged_pr_timestamps_tolerates_corruption(
    mem_fs: dict[str, str],
) -> None:
    """Two-layer tolerance: the backend drops structurally-corrupt rows, and the
    reader itself filters records lacking a non-empty string merged_at — mirroring
    _read_stand_down_timestamps, the reader this one replaces in the reward_landing
    branch (ledger r1). Only the well-formed, dated record survives."""
    from maestro.audit_checks import _read_merged_pr_timestamps

    mem_fs[str(_DROUGHT_DIR / "version_archive.jsonl")] = "\n".join([
        "not json",  # malformed → backend drops
        json.dumps([1, 2, 3]),  # non-dict → backend drops
        json.dumps(_merged_version_dict(version_id="v1", merged_at=_T_MID)),
        json.dumps(_merged_version_dict(version_id="v2", merged_at=123)),  # non-str
        json.dumps({"version_id": "v3"}),  # missing fields → backend drops
        json.dumps(_merged_version_dict(version_id="v4", merged_at="")),  # empty
        "",
    ]) + "\n"
    assert _read_merged_pr_timestamps(
        JsonlVersionArchiveLedger(_DROUGHT_DIR),
    ) == [_T_MID]


def test_read_merged_pr_timestamps_missing_file(
    mem_fs: dict[str, str],
) -> None:
    """Absent version archive → empty list (the backend's ``load_all`` yields [])."""
    from maestro.audit_checks import _read_merged_pr_timestamps

    assert _read_merged_pr_timestamps(
        JsonlVersionArchiveLedger(_DROUGHT_DIR),
    ) == []


def test_read_merged_pr_timestamps_oserror(
    mem_fs: dict[str, str],
) -> None:
    """An OSError while reading the version archive degrades to an empty list.

    The tolerance now lives in the backend's ``load_all`` (``load_all_versions``
    swallows the ``OSError`` from the JSONL read), so the reader still yields [].
    """
    from maestro.audit_checks import _read_merged_pr_timestamps

    _seed_merged_prs(mem_fs, [_T_MID])

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk error")

    with patch.object(Path, "read_text", _boom):
        assert _read_merged_pr_timestamps(
            JsonlVersionArchiveLedger(_DROUGHT_DIR),
        ) == []


def test_measure_owned_act_drought_reward_landing_counts_merged_pr(
    mem_fs: dict[str, str],
) -> None:
    """Under reward_landing, an obsolete plan event plus a merged owned PR in
    the window both count (step 4a)."""
    _seed_drought_tasks(mem_fs, [_T_OLD, _T_OLD])
    _seed_obsolete_plans(mem_fs, [
        {"plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_MID},
    ])
    _seed_merged_prs(mem_fs, [_T_MID])
    assert measure_owned_act_drought(
        _DROUGHT_DIR, 5, reward_landing=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    ) == 2


def test_measure_owned_act_drought_reward_landing_ignores_stand_downs(
    mem_fs: dict[str, str],
) -> None:
    """Under reward_landing, a window of only chosen stand-downs counts zero —
    standing-down is no longer a rewarded owned act (step 4b)."""
    _seed_drought_tasks(mem_fs, [_T_OLD, _T_OLD])
    _seed_stand_downs(mem_fs, [_T_MID, _T_MID])
    assert measure_owned_act_drought(
        _DROUGHT_DIR, 5, reward_landing=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    ) == 0


def test_measure_owned_act_drought_reward_landing_excludes_old_merged_pr(
    mem_fs: dict[str, str],
) -> None:
    """Under reward_landing, a merged PR older than the cycle-clock window is not
    counted, so the rule re-arms as the clock advances (step 4c)."""
    _seed_drought_tasks(mem_fs, [_T_NEW, _T_NEW])
    _seed_merged_prs(mem_fs, [_T_OLD])
    assert measure_owned_act_drought(
        _DROUGHT_DIR, 5, reward_landing=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    ) == 0


def test_measure_owned_act_drought_reward_landing_undated_merge_no_clock(
    mem_fs: dict[str, str],
) -> None:
    """Ledger r1 regression: with no cycle clock (empty window_start) an undated
    merge record must not spuriously count as an owned act — the reader's
    empty-merged_at filter drops it, keeping the drought armed rather than falsely
    suppressing the demand."""
    # No task_history → window_start degrades to "". A record with merged_at=""
    # would satisfy `"" >= ""` if the reader didn't filter empty merged_at.
    mem_fs[str(_DROUGHT_DIR / "version_archive.jsonl")] = (
        json.dumps(_merged_version_dict(
            version_id="v1", plan_id="ep1", merged_at="",
        )) + "\n"
    )
    assert measure_owned_act_drought(
        _DROUGHT_DIR, 5, reward_landing=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    ) == 0


def test_measure_owned_act_drought_salvage_disqualifies_drops_obsolete(
    mem_fs: dict[str, str],
) -> None:
    """A salvage_disqualifies predicate drops only the obsolete events whose
    pr_url it marks salvageable; the obsolete backing a non-salvageable PR and a
    stand-down in the same window still count — the predicate filters only the
    obsolete half (proposal 094 step 2)."""
    _seed_drought_tasks(mem_fs, [_T_OLD, _T_OLD])
    _seed_obsolete_plans(mem_fs, [
        {
            "plan_id": "ep_salv", "status": "obsolete", "reviewed_at": _T_MID,
            "pr_url": "https://github.com/o/r/pull/1",
        },
        {
            "plan_id": "ep_dead", "status": "obsolete", "reviewed_at": _T_MID,
            "pr_url": "https://github.com/o/r/pull/2",
        },
    ])
    _seed_stand_downs(mem_fs, [_T_MID])
    salvageable = {"https://github.com/o/r/pull/1"}
    count = measure_owned_act_drought(
        _DROUGHT_DIR, 5,
        salvage_disqualifies=lambda pr_url: pr_url in salvageable,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    )
    # PR #1's obsolete drops; PR #2's obsolete and the stand-down still count.
    assert count == 2


def test_measure_owned_act_drought_salvage_none_parity(
    mem_fs: dict[str, str],
) -> None:
    """salvage_disqualifies=None counts the obsolete half by timestamp alone —
    identical to omitting the predicate (today's default path is unchanged)."""
    _seed_drought_tasks(mem_fs, [_T_OLD, _T_OLD])
    _seed_obsolete_plans(mem_fs, [
        {
            "plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_MID,
            "pr_url": "https://github.com/o/r/pull/1",
        },
    ])
    default = measure_owned_act_drought(_DROUGHT_DIR, 5, stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR), version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR))
    explicit_none = measure_owned_act_drought(
        _DROUGHT_DIR, 5, salvage_disqualifies=None,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    )
    assert default == 1
    assert explicit_none == 1


def test_measure_owned_act_drought_salvage_filters_obsolete_under_reward_landing(
    mem_fs: dict[str, str],
) -> None:
    """The predicate filters the obsolete half in reward_landing mode too: a
    disqualified obsolete drops while a merged owned PR still counts (proposal 094
    step 2 — the filter applies in both counting modes)."""
    _seed_drought_tasks(mem_fs, [_T_OLD, _T_OLD])
    _seed_obsolete_plans(mem_fs, [
        {
            "plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_MID,
            "pr_url": "https://github.com/o/r/pull/1",
        },
    ])
    _seed_merged_prs(mem_fs, [_T_MID])
    count = measure_owned_act_drought(
        _DROUGHT_DIR, 5, reward_landing=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
        salvage_disqualifies=lambda pr_url: True,  # disqualify every obsolete
    )
    # The obsolete drops; the merged PR still counts.
    assert count == 1


# -- check_owned_act_drought ------------------------------------------------


def test_owned_act_drought_fires_on_zero_acts_with_backlog(
    mem_fs: dict[str, str],
) -> None:
    """Zero owned acts + a sufficient prunable backlog → exactly one finding
    naming the window and backlog count (criteria 2, fire branch)."""
    _seed_drought_tasks(mem_fs, [_T_MID, _T_MID])
    plans = [_pending_plan("pp1"), _pending_plan("pp2")]
    findings = check_owned_act_drought(
        plans, _DROUGHT_DIR, window=5, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "owned_act_drought"
    assert finding.rule == "owned_act_drought"
    # Finding text names the window and the backlog count.
    assert "5 cycles" in finding.statement
    assert "2 pending_review" in finding.statement
    # And demands an owned act of either kind.
    assert "mark_plan_obsolete" in finding.statement
    assert "stand-down" in finding.statement


def test_owned_act_drought_reward_landing_statement_names_landing(
    mem_fs: dict[str, str],
) -> None:
    """Under reward_landing, the drought finding demands landing (merge) and
    mark_plan_obsolete — and never a chosen stand-down (step 4d)."""
    _seed_drought_tasks(mem_fs, [_T_MID, _T_MID])
    plans = [_pending_plan("pp1"), _pending_plan("pp2")]
    findings = check_owned_act_drought(
        plans, _DROUGHT_DIR, window=5, min_backlog=2, reward_landing=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    )
    assert len(findings) == 1
    statement = findings[0].statement
    # Names landing (merge) as the rewarded owned act.
    assert "land" in statement
    assert "merge" in statement
    assert "mark_plan_obsolete" in statement
    # And never demands a chosen stand-down under the flag.
    assert "stand-down" not in statement


def test_owned_act_drought_silent_below_backlog(
    mem_fs: dict[str, str],
) -> None:
    """Zero owned acts but a backlog below the floor → no finding (criterion 2,
    below-backlog no-fire branch)."""
    _seed_drought_tasks(mem_fs, [_T_MID, _T_MID])
    findings = check_owned_act_drought(
        [_pending_plan("pp1")], _DROUGHT_DIR, window=5, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    )
    assert findings == []


def test_owned_act_drought_silent_when_obsolete_plan_in_window(
    mem_fs: dict[str, str],
) -> None:
    """A single obsolete plan-status event in the window self-extinguishes the
    rule (criterion 3, obsolete path)."""
    _seed_drought_tasks(mem_fs, [_T_OLD, _T_OLD])
    _seed_obsolete_plans(mem_fs, [
        {"plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_MID},
    ])
    plans = [_pending_plan("pp1"), _pending_plan("pp2")]
    findings = check_owned_act_drought(
        plans, _DROUGHT_DIR, window=5, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    )
    assert findings == []


def test_owned_act_drought_salvage_disqualifies_forwarded_and_refires(
    mem_fs: dict[str, str],
) -> None:
    """check_owned_act_drought forwards salvage_disqualifies to the measure: an
    obsolete event that would normally self-extinguish the rule stops crediting
    the counter when its pr_url is salvageable, so the drought re-fires (proposal
    094 step 3). Without the predicate the same seed stays silent."""
    _seed_drought_tasks(mem_fs, [_T_OLD, _T_OLD])
    _seed_obsolete_plans(mem_fs, [
        {
            "plan_id": "ep1", "status": "obsolete", "reviewed_at": _T_MID,
            "pr_url": "https://github.com/o/r/pull/1",
        },
    ])
    plans = [_pending_plan("pp1"), _pending_plan("pp2")]
    # No predicate: the obsolete counts → self-extinguished, no finding.
    assert check_owned_act_drought(
        plans, _DROUGHT_DIR, window=5, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    ) == []
    # Salvageable predicate: the obsolete stops counting → the drought re-fires.
    findings = check_owned_act_drought(
        plans, _DROUGHT_DIR, window=5, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
        salvage_disqualifies=lambda pr_url: True,
    )
    assert len(findings) == 1
    assert findings[0].rule == "owned_act_drought"


def test_owned_act_drought_silent_when_stand_down_in_window(
    mem_fs: dict[str, str],
) -> None:
    """A single chosen stand-down in the window self-extinguishes the rule
    (criterion 3, stand-down path)."""
    _seed_drought_tasks(mem_fs, [_T_OLD, _T_OLD])
    _seed_stand_downs(mem_fs, [_T_MID])
    plans = [_pending_plan("pp1"), _pending_plan("pp2")]
    findings = check_owned_act_drought(
        plans, _DROUGHT_DIR, window=5, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    )
    assert findings == []


def test_owned_act_drought_disabled_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """The master switch off short-circuits to no findings."""
    _seed_drought_tasks(mem_fs, [_T_MID, _T_MID])
    plans = [_pending_plan("pp1"), _pending_plan("pp2")]
    assert check_owned_act_drought(
        plans, _DROUGHT_DIR, window=5, min_backlog=2, enabled=False,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    ) == []


def test_owned_act_drought_finding_text_reflects_custom_thresholds(
    mem_fs: dict[str, str],
) -> None:
    """The window and backlog values in the statement track the actual args."""
    _seed_drought_tasks(mem_fs, [_T_MID, _T_MID])
    plans = [_pending_plan(f"pp{i}") for i in range(3)]
    findings = check_owned_act_drought(
        plans, _DROUGHT_DIR, window=7, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    )
    assert len(findings) == 1
    assert "7 cycles" in findings[0].statement
    assert "3 pending_review" in findings[0].statement


def test_owned_act_drought_directive_bridge_fills_empty_directive(
    mem_fs: dict[str, str],
) -> None:
    """The drought finding routes a directive demanding an owned act into an
    empty next_task_directive via the audit→directive bridge (criterion 4)."""
    _seed_drought_tasks(mem_fs, [_T_MID, _T_MID])
    plans = [_pending_plan("pp1"), _pending_plan("pp2")]
    findings = check_owned_act_drought(
        plans, _DROUGHT_DIR, window=5, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    )
    assert findings

    # Reproduce the agent.py audit→directive bridge: an empty directive is
    # filled from the first finding.
    model = SelfModel(next_task_directive="")
    if findings and not model.next_task_directive:
        model.next_task_directive = audit_checks.finding_to_directive(
            findings[0],
        )

    directive = model.next_task_directive
    assert directive.startswith("Address audit finding [owned_act_drought]:")
    assert len(directive) <= _DIRECTIVE_MAX_CHARS
    # The routed demand explicitly requires an owned terminal act.
    assert "mark_plan_obsolete" in directive
    assert "stand-down" in directive


def test_owned_act_drought_self_extinguishes_after_orphaned_cure_dispose(
    mem_fs: dict[str, str],
) -> None:
    """One orphaned-cure dispose fire lands an ``obsolete`` status event that
    rule 18 counts, so the drought finding self-extinguishes."""
    mem = Memory(_DROUGHT_DIR)
    mem.save_evolution_plan(EvolutionPlan(
        plan_id="cure",
        title="Retire the treadmill",
        category="capability",
        rationale="close the detect-retire gap",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
    ))
    mem.update_plan_status(
        "cure",
        "implemented",
        pr_url="https://github.com/o/r/pull/1",
        last_conflict_resolution_attempt_at=_T_MID,
    )
    backlog = [_pending_plan("pp1"), _pending_plan("pp2")]

    # Drought state before the fire: zero owned acts, prunable backlog.
    assert check_owned_act_drought(
        backlog, _DROUGHT_DIR, window=5, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    ) != []

    retired = mem.sweep_orphaned_cures(
        {"cure": 0}, 40, 30, deliver_disposition=lambda _plan: True,
    )
    assert retired == ["cure"]

    # No task clock is seeded, so the window lower bound degrades to ""
    # and the fresh obsolete event counts — rule 18 goes silent.
    assert check_owned_act_drought(
        backlog, _DROUGHT_DIR, window=5, min_backlog=2,
        stand_down_backend=JsonlStandDownVerdictLedger(_DROUGHT_DIR),
        version_archive_backend=JsonlVersionArchiveLedger(_DROUGHT_DIR),
    ) == []


# ---------------------------------------------------------------------------
# check_banked_owned_act_reperformance (artifact-id-axis reperformance shield)
# ---------------------------------------------------------------------------

_BANKED_DIR = Path("/fake/banked")

# A real 12-hex ``plan_id`` shape; the plan-hash artifact-id of a banked prune.
_BANKED_PLAN_HASH = "d5bcc3fc2a33"

# A delivered merge-or-close handoff's message-id (``handoff-<pr>-<head_tag>``).
_BANKED_MESSAGE_ID = "handoff-2986-abc1234"


def _seed_banked_stand_downs(
    mem_fs: dict[str, str],
    prs: list[int],
    data_dir: Path = _BANKED_DIR,
) -> None:
    """Seed ``stand_down_verdicts.jsonl`` with one verdict per *pr*."""
    mem_fs[str(data_dir / "stand_down_verdicts.jsonl")] = "".join(
        json.dumps({"pr": pr, "head_sha": f"sha{pr}", "recorded_at": "t"})
        + "\n"
        for pr in prs
    )


def _seed_banked_obsolete(
    mem_fs: dict[str, str],
    records: list[dict[str, object]],
    data_dir: Path = _BANKED_DIR,
) -> None:
    """Seed ``evolution_plans.jsonl`` with raw plan *records* (one line each)."""
    mem_fs[str(data_dir / "evolution_plans.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in records
    )


def _seed_banked_messages(
    mem_fs: dict[str, str],
    records: list[dict[str, object]],
    data_dir: Path = _BANKED_DIR,
) -> None:
    """Seed ``messages.jsonl`` with raw message *records* (one line each)."""
    mem_fs[str(data_dir / "messages.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in records
    )


def test_banked_reperformance_fires_on_plan_hash_directive(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 1 — a directive re-performing a banked obsolete prune by its
    plan-hash id (no ``#NNNN`` present) fires a finding.

    Failing-first intent: the plan-hash carries no ``#NNNN``, so the original
    subject-axis (``#NNNN``-only) matcher would have been silent even though the
    directive names a banked act — exactly the ``loss-axis-is-novelty-not-subject``
    scar. This asserts the reference extractor routes the hash to the plan-hash
    bucket (empty PR bucket → a subject-axis matcher finds nothing) and that the
    widened rule fires on the artifact-id axis.
    """
    _seed_banked_obsolete(mem_fs, [
        {"plan_id": _BANKED_PLAN_HASH, "status": "obsolete"},
    ])
    # A stand-down on an unrelated PR keeps the #NNNN ledger non-empty, proving
    # the rule fires on the plan-hash axis, not by an incidental PR match.
    _seed_banked_stand_downs(mem_fs, [1234])
    directive = (
        f"call Memory.mark_plan_obsolete on the unmerged lapping cure plan "
        f"{_BANKED_PLAN_HASH}"
    )
    prs, hashes, _ = audit_checks._extract_artifact_references(directive)
    assert prs == set()  # no #NNNN → a subject-axis matcher would be silent
    assert _BANKED_PLAN_HASH in hashes
    findings = check_banked_owned_act_reperformance(
        _BANKED_DIR, directive, [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "banked_owned_act_reperformance"
    assert finding.rule == "banked_owned_act_reperformance"
    assert _BANKED_PLAN_HASH in finding.statement


def test_banked_reperformance_fires_on_message_id_directive(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 2 — a directive re-performing a banked handoff by its message-id
    fires a finding."""
    _seed_banked_messages(mem_fs, [
        {"message_id": _BANKED_MESSAGE_ID, "sender": "maestro",
         "content": "merge or close", "message_type": "question"},
    ])
    directive = f"re-deliver the merge-or-close handoff {_BANKED_MESSAGE_ID}"
    findings = check_banked_owned_act_reperformance(
        _BANKED_DIR, directive, [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    )
    assert len(findings) == 1
    assert _BANKED_MESSAGE_ID in findings[0].statement


def test_banked_reperformance_fires_on_unwinnable_race_message_id(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 099 (step 8): an ``unwinnable-race-<pr>`` message — the
    escalation's only durable trace, since the deliverer records no stand-down —
    is recognised as a banked owned act, so a directive re-performing it fires a
    finding. Guards the ``_HANDOFF_MESSAGE_ID_PREFIXES`` registration."""
    unwinnable_id = "unwinnable-race-3112"
    _seed_banked_messages(mem_fs, [
        {"message_id": unwinnable_id, "sender": "maestro",
         "content": "unwinnable-race heads-up", "message_type": "question"},
    ])
    # The banked-handoff reader recognises the id via its registered prefix.
    assert unwinnable_id in audit_checks._banked_handoff_message_ids(_BANKED_DIR)
    directive = f"re-deliver the unwinnable-race heads-up {unwinnable_id}"
    findings = check_banked_owned_act_reperformance(
        _BANKED_DIR, directive, [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    )
    assert len(findings) == 1
    assert unwinnable_id in findings[0].statement


def test_banked_reperformance_fires_on_pr_stand_down_directive(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 4 — an existing ``#NNNN`` stand-down re-performance directive
    still fires (no regression on the subject-axis door)."""
    _seed_banked_stand_downs(mem_fs, [3042])
    findings = check_banked_owned_act_reperformance(
        _BANKED_DIR, "stand down PR #3042 again", [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    )
    assert len(findings) == 1
    assert "#3042" in findings[0].statement


def test_banked_reperformance_fires_on_prune_cited_pr_directive(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 4 — a ``#NNNN`` that a banked obsolete prune cited still fires
    (the prune-by-PR arm of the subject-axis door)."""
    _seed_banked_obsolete(mem_fs, [
        {"plan_id": "aaaaaaaaaaaa", "status": "obsolete",
         "description": "retire the unmerged lapping cure for #2986"},
    ])
    findings = check_banked_owned_act_reperformance(
        _BANKED_DIR, "dispose PR #2986 once more", [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    )
    assert len(findings) == 1
    assert "#2986" in findings[0].statement


def test_banked_reperformance_silent_on_unbanked_ids(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 3 — a directive naming an unbanked plan-hash or message-id (not
    present in the owned-act ledgers) yields no finding, even though other acts
    ARE banked."""
    _seed_banked_obsolete(mem_fs, [
        {"plan_id": _BANKED_PLAN_HASH, "status": "obsolete"},
    ])
    _seed_banked_messages(mem_fs, [
        {"message_id": _BANKED_MESSAGE_ID, "sender": "maestro",
         "content": "merge or close", "message_type": "question"},
    ])
    directive = (
        "call mark_plan_obsolete on plan ffffffffffff and re-deliver "
        "handoff-9999-deadbeef"
    )
    assert check_banked_owned_act_reperformance(
        _BANKED_DIR, directive, [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    ) == []


def test_banked_reperformance_disabled_by_default(
    mem_fs: dict[str, str],
) -> None:
    """The opt-in master switch defaults off — silent unless enabled."""
    _seed_banked_obsolete(mem_fs, [
        {"plan_id": _BANKED_PLAN_HASH, "status": "obsolete"},
    ])
    assert check_banked_owned_act_reperformance(
        _BANKED_DIR, f"prune {_BANKED_PLAN_HASH}", [],
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    ) == []


def test_banked_reperformance_flag_gates_rule(
    mem_fs: dict[str, str],
) -> None:
    """``enabled=False`` suppresses the finding; ``True`` enables it on identical
    state."""
    _seed_banked_obsolete(mem_fs, [
        {"plan_id": _BANKED_PLAN_HASH, "status": "obsolete"},
    ])
    directive = f"prune {_BANKED_PLAN_HASH}"
    assert check_banked_owned_act_reperformance(
        _BANKED_DIR, directive, [], enabled=False,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    ) == []
    assert check_banked_owned_act_reperformance(
        _BANKED_DIR, directive, [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    ) != []


def test_banked_reperformance_self_extinguishes_no_banked_act(
    mem_fs: dict[str, str],
) -> None:
    """With no owned act banked yet the rule is silent (self-extinguishing)."""
    assert check_banked_owned_act_reperformance(
        _BANKED_DIR, f"prune {_BANKED_PLAN_HASH}", [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    ) == []


def test_banked_reperformance_silent_without_directive_or_goal(
    mem_fs: dict[str, str],
) -> None:
    """No directive and no queued goal → nothing to inspect, no finding."""
    _seed_banked_obsolete(mem_fs, [
        {"plan_id": _BANKED_PLAN_HASH, "status": "obsolete"},
    ])
    assert check_banked_owned_act_reperformance(
        _BANKED_DIR, None, [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    ) == []


def test_banked_reperformance_matches_queued_goal(
    mem_fs: dict[str, str],
) -> None:
    """A queued task goal (not just the directive) naming a banked act fires,
    attributed to the queued-goal source."""
    _seed_banked_obsolete(mem_fs, [
        {"plan_id": _BANKED_PLAN_HASH, "status": "obsolete"},
    ])
    findings = check_banked_owned_act_reperformance(
        _BANKED_DIR, None, [f"re-obsolete plan {_BANKED_PLAN_HASH}"],
        enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    )
    assert len(findings) == 1
    assert "queued task goal" in findings[0].statement


def test_banked_reperformance_dedupes_same_artifact(
    mem_fs: dict[str, str],
) -> None:
    """The directive and a queued goal naming the same banked act surface it
    once."""
    _seed_banked_obsolete(mem_fs, [
        {"plan_id": _BANKED_PLAN_HASH, "status": "obsolete"},
    ])
    findings = check_banked_owned_act_reperformance(
        _BANKED_DIR,
        f"prune {_BANKED_PLAN_HASH}",
        [f"prune {_BANKED_PLAN_HASH} again"],
        enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    )
    assert len(findings) == 1


def test_banked_reperformance_ignores_non_maestro_and_non_handoff_messages(
    mem_fs: dict[str, str],
) -> None:
    """Only maestro-sent handoff-family message-ids bank; a Thomas message or a
    non-handoff id is not a banked owned act (criterion 3 boundary)."""
    _seed_banked_messages(mem_fs, [
        # Right id shape but wrong sender — not maestro's owned act.
        {"message_id": "handoff-5555-cafef00d", "sender": "thomas",
         "content": "hi", "message_type": "instruction"},
        # Maestro message but not a handoff-family id — not an owned terminal act.
        {"message_id": "reply-42", "sender": "maestro",
         "content": "ack", "message_type": "reply"},
    ])
    directive = "re-deliver handoff-5555-cafef00d and reply-42"
    assert check_banked_owned_act_reperformance(
        _BANKED_DIR, directive, [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    ) == []


def test_banked_reperformance_caps_findings(
    mem_fs: dict[str, str],
) -> None:
    """Distinct re-performances are capped at the per-rule finding budget."""
    hashes = ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]
    _seed_banked_obsolete(mem_fs, [
        {"plan_id": h, "status": "obsolete"} for h in hashes
    ])
    directive = "re-obsolete plans " + " ".join(hashes)
    findings = check_banked_owned_act_reperformance(
        _BANKED_DIR, directive, [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    )
    assert len(findings) == MAX_FINDINGS_PER_RULE


def test_extract_artifact_references_routes_forms_to_disjoint_buckets() -> None:
    """The extractor routes each id form to exactly one bucket: ``#NNNN`` →
    prs, 12-hex → plan_hashes, kebab-with-hyphen → message_ids."""
    prs, hashes, msgids = audit_checks._extract_artifact_references(
        f"PR #77 plan {_BANKED_PLAN_HASH} handoff {_BANKED_MESSAGE_ID}"
    )
    assert prs == {"#77"}
    assert hashes == {_BANKED_PLAN_HASH}
    assert _BANKED_MESSAGE_ID in msgids


def test_read_jsonl_dicts_tolerates_corruption(
    mem_fs: dict[str, str],
) -> None:
    """Blank lines, non-JSON, and non-dict records are skipped; valid dicts
    survive (corrupted-tail tolerance)."""
    mem_fs[str(_BANKED_DIR / "messages.jsonl")] = "\n".join([
        "",  # blank line
        "not json",  # JSONDecodeError
        json.dumps([1, 2, 3]),  # non-dict
        json.dumps({"message_id": "m1", "sender": "maestro"}),
    ])
    records = audit_checks._read_jsonl_dicts(_BANKED_DIR / "messages.jsonl")
    assert records == [{"message_id": "m1", "sender": "maestro"}]


def test_read_jsonl_dicts_oserror_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """When open() raises OSError on an existing file, the reader returns []."""
    path = str(_BANKED_DIR / "messages.jsonl")
    mem_fs[path] = "content"  # exists so path.exists() is True
    with patch("builtins.open", side_effect=OSError("disk error")):
        assert audit_checks._read_jsonl_dicts(_BANKED_DIR / "messages.jsonl") == []


def test_read_jsonl_dicts_missing_file_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """No file on disk yields an empty list, not a crash."""
    assert audit_checks._read_jsonl_dicts(_BANKED_DIR / "absent.jsonl") == []


def test_extract_artifact_references_empty_text() -> None:
    """Empty text short-circuits to three empty buckets."""
    assert audit_checks._extract_artifact_references("") == (
        set(), set(), set(),
    )


def test_banked_obsolete_plan_ids_skips_non_obsolete_and_maps_forms(
    mem_fs: dict[str, str],
) -> None:
    """Only ``obsolete`` plans bank: a non-obsolete plan contributes neither its
    plan-hash nor its cited PRs; an obsolete plan maps both id forms."""
    _seed_banked_obsolete(mem_fs, [
        # Non-obsolete: skipped entirely (plan-hash and cited #55 must NOT bank).
        {"plan_id": "eeeeeeeeeeee", "status": "pending_review",
         "description": "still open, cites #55"},
        {"plan_id": _BANKED_PLAN_HASH, "status": "obsolete",
         "description": "retired cure for #2986"},
    ])
    hashes, pr_ids = audit_checks._banked_obsolete_plan_ids(_BANKED_DIR)
    assert hashes == {_BANKED_PLAN_HASH}
    assert pr_ids == {"#2986"}


def test_banked_reperformance_registered_and_summarised() -> None:
    """The opt-in banked-owned-act rule IS in ``_registered_audit_checks`` and
    is named in ``format_audit_checks_summary`` — the rule is now a dispatched
    (flag-gated) audit rule, so the reporting registry must claim it. Mirrors
    ``test_phantom_partial_prune_registered_and_summarised``."""
    registered = {
        fn.__name__ for fn in audit_checks._registered_audit_checks()
    }
    assert "check_banked_owned_act_reperformance" in registered
    assert "check_banked_owned_act_reperformance" in (
        audit_checks.format_audit_checks_summary()
    )


# ---------------------------------------------------------------------------
# Plan-mill instrument (proposal 116, increment 1)
# ---------------------------------------------------------------------------


def _mill_plan(
    plan_id: str = "ep1",
    title: str = "Add a widget",
    rationale: str = "r",
    status: str = "pending_review",
    source_task_id: str | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
    obsolete_reason: str | None = None,
    parent_plan_id: str | None = None,
    grounding: GroundingProbe | None = None,
) -> EvolutionPlan:
    """A minimal EvolutionPlan carrying the fields the mill metrics read."""
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale=rationale,
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status=status,  # type: ignore[arg-type]
        source_task_id=source_task_id,
        created_at=created_at,
        obsolete_reason=obsolete_reason,
        parent_plan_id=parent_plan_id,
        grounding=grounding,
    )


# ── classify_self_measurement_plan ─────────────────────────────────


def test_classify_self_measurement_plan_matches_title() -> None:
    """A self-measurement theme word in the title classifies True."""
    assert classify_self_measurement_plan(
        _mill_plan(title="Add a sensor for lap cost"),
    )


def test_classify_self_measurement_plan_matches_rationale() -> None:
    """A theme word only in the rationale still classifies True."""
    assert classify_self_measurement_plan(
        _mill_plan(title="Add a widget", rationale="tune the decoy detector"),
    )


def test_classify_self_measurement_plan_unthemed_is_false() -> None:
    """A plan with no theme vocabulary classifies False."""
    assert not classify_self_measurement_plan(
        _mill_plan(title="Add a widget", rationale="unrelated work"),
    )


def test_classify_self_measurement_plan_word_boundary() -> None:
    """Word-boundary anchoring: 'sensory' does not match 'sensor'."""
    assert not classify_self_measurement_plan(
        _mill_plan(title="Refactor the sensory pathway"),
    )


def test_classify_self_measurement_plan_tolerates_plurals() -> None:
    """The optional trailing 's' matches the common plural forms."""
    assert classify_self_measurement_plan(
        _mill_plan(title="Add decoys and discriminators"),
    )


def test_classify_self_measurement_plan_hyphen_and_space_variants() -> None:
    """Both 'audit-rule' and 'audit rule' match."""
    assert classify_self_measurement_plan(_mill_plan(title="Tune the audit-rule"))
    assert classify_self_measurement_plan(_mill_plan(title="Tune the audit rule"))


# ── measure_plan_theme_saturation ──────────────────────────────────


def _mill_mixed_plans() -> list[EvolutionPlan]:
    """Four plans: 3 themed, 2 merged, 1 coupled, 1 stranded-promotion."""
    return [
        _mill_plan("p1", title="Add a sensor", status="merged",
                   created_at="2026-01-01T00:00:00Z"),
        _mill_plan("p2", title="Add a widget", rationale="track the decoy",
                   status="pending_review", created_at="2026-01-02T00:00:00Z"),
        _mill_plan("p3", title="Add a widget", rationale="r",
                   status="pending_review", source_task_id="maestro/task/abc",
                   created_at="2026-01-03T00:00:00Z"),
        _mill_plan("p4", title="Finish the stranded-branch sensor lineage",
                   status="merged", created_at="2026-01-04T00:00:00Z"),
    ]


def test_measure_plan_theme_saturation_counts_themed() -> None:
    """3 of 4 plans are themed → 0.75 saturation."""
    result = measure_plan_theme_saturation(_mill_mixed_plans())
    assert result == {"total": 4, "themed": 3, "themed_fraction": 0.75}


def test_measure_plan_theme_saturation_empty_is_zero() -> None:
    """An empty plan list yields all-zero results, not divide-by-zero."""
    assert measure_plan_theme_saturation([]) == {
        "total": 0, "themed": 0, "themed_fraction": 0.0,
    }


def test_measure_plan_theme_saturation_window_none_created_at() -> None:
    """A None created_at ranks oldest instead of raising TypeError mid-sort."""
    plans = [
        _mill_plan("new", title="Add a sensor",
                   created_at="2026-03-01T00:00:00Z"),
        _mill_plan("null", title="Add a widget"),
    ]
    plans[1].created_at = None  # type: ignore[assignment]
    # n=1 keeps only the newest; the null-timestamped plan ranks oldest and
    # is dropped. Reaching here at all proves the sort did not raise.
    windowed = measure_plan_theme_saturation(plans, n=1)
    assert windowed == {"total": 1, "themed": 1, "themed_fraction": 1.0}


def test_measure_plan_theme_saturation_window_uses_created_at() -> None:
    """The n-window keeps the most-recent-by-created_at plans, not file order."""
    scrambled = [
        _mill_plan("old", title="Add a sensor",
                   created_at="2026-01-01T00:00:00Z"),
        _mill_plan("new", title="Add a widget",
                   created_at="2026-03-01T00:00:00Z"),
        _mill_plan("mid", title="Add a gadget",
                   created_at="2026-02-01T00:00:00Z"),
    ]
    # n=2 drops the oldest (themed) plan even though it is first in list order.
    windowed = measure_plan_theme_saturation(scrambled, n=2)
    assert windowed == {"total": 2, "themed": 0, "themed_fraction": 0.0}
    # n=0 spans all three; the themed oldest plan is counted.
    full = measure_plan_theme_saturation(scrambled, n=0)
    assert full["total"] == 3
    assert full["themed"] == 1


# ── measure_merged_to_created_ratio ────────────────────────────────


def test_measure_merged_to_created_ratio_counts_merged() -> None:
    """2 of 4 plans reached 'merged' → 0.5 ratio."""
    result = measure_merged_to_created_ratio(_mill_mixed_plans())
    assert result == {"created": 4, "merged": 2, "ratio": 0.5}


def test_measure_merged_to_created_ratio_empty_is_zero() -> None:
    """An empty plan list yields all-zero results."""
    assert measure_merged_to_created_ratio([]) == {
        "created": 0, "merged": 0, "ratio": 0.0,
    }


# ── measure_task_coupled_plan_fraction ─────────────────────────────


def test_measure_task_coupled_plan_fraction_counts_coupled() -> None:
    """1 of 4 plans nominates a source_task_id → 0.25 coupled fraction."""
    result = measure_task_coupled_plan_fraction(_mill_mixed_plans())
    assert result == {"total": 4, "coupled": 1, "coupled_fraction": 0.25}


def test_measure_task_coupled_plan_fraction_empty_string_not_coupled() -> None:
    """An empty-string source_task_id does not count as coupled."""
    plans = [_mill_plan("p1", source_task_id=""),
             _mill_plan("p2", source_task_id=None)]
    result = measure_task_coupled_plan_fraction(plans)
    assert result == {"total": 2, "coupled": 0, "coupled_fraction": 0.0}


def test_measure_task_coupled_plan_fraction_empty_is_zero() -> None:
    """An empty plan list yields all-zero results."""
    assert measure_task_coupled_plan_fraction([]) == {
        "total": 0, "coupled": 0, "coupled_fraction": 0.0,
    }


# ── classify_plan_mint_channel ─────────────────────────────────────

# A persisted submission-door ``obsolete_reason`` signature, verbatim in shape
# from ``evolution_submission.py``'s ``submit_for_review`` generation-admission gate.
_DOA_ADMISSION_REASON = (
    "Admission gate refused net-new mint: backlog saturated (5 >= 5) — land or "
    "dispose an existing plan before proposing more"
)


def test_classify_plan_mint_channel_phase_slice() -> None:
    """A set parent_plan_id classifies as the 'phase-slice' channel."""
    assert classify_plan_mint_channel(
        _mill_plan(parent_plan_id="proposal-099"),
    ) == "phase-slice"


def test_classify_plan_mint_channel_task_anchored() -> None:
    """A source_task_id (no parent) classifies as 'task-anchored'."""
    assert classify_plan_mint_channel(
        _mill_plan(source_task_id="maestro/task/abc"),
    ) == "task-anchored"


def test_classify_plan_mint_channel_evolve() -> None:
    """Neither parent nor source classifies as the standalone 'evolve' mint."""
    assert classify_plan_mint_channel(_mill_plan()) == "evolve"


def test_classify_plan_mint_channel_parent_wins_over_source() -> None:
    """parent_plan_id takes precedence over source_task_id (more specific)."""
    assert classify_plan_mint_channel(
        _mill_plan(parent_plan_id="proposal-099", source_task_id="maestro/task/z"),
    ) == "phase-slice"


# ── measure_dead_on_arrival_mints ──────────────────────────────────


def test_measure_dead_on_arrival_mints_by_channel_phase_slice() -> None:
    """A door-obsoleted phase-slice plan is attributed to 'phase-slice'."""
    plans = [
        _mill_plan("p1", status="obsolete", obsolete_reason=_DOA_ADMISSION_REASON,
                   parent_plan_id="proposal-099"),
    ]
    result = measure_dead_on_arrival_mints(plans)
    assert result["by_channel"] == {"phase-slice": 1}


def test_measure_dead_on_arrival_mints_excludes_non_obsolete() -> None:
    """A non-obsolete plan carrying a door-signature reason is excluded."""
    plans = [
        _mill_plan("p1", status="pending_review",
                   obsolete_reason=_DOA_ADMISSION_REASON),
    ]
    result = measure_dead_on_arrival_mints(plans)
    assert result["dead_on_arrival"] == 0


def test_measure_dead_on_arrival_mints_excludes_obsolete_without_reason() -> None:
    """An obsolete plan with a None obsolete_reason is excluded (guard branch)."""
    plans = [_mill_plan("p1", status="obsolete", obsolete_reason=None)]
    result = measure_dead_on_arrival_mints(plans)
    assert result["dead_on_arrival"] == 0


def test_measure_dead_on_arrival_mints_by_reason_flag_graduation() -> None:
    """Criterion 8: a ``[flag-graduation-premature:...]`` obsolete row buckets
    under ``flag-graduation`` in the by_reason split, no CLI involved."""
    reason = (
        "[flag-graduation-premature:sdk_single_use_evolution_calls_enabled] "
        "graduates experimental flag ... before its review_by window closes"
    )
    plans = [_mill_plan("p1", status="obsolete", obsolete_reason=reason)]
    result = measure_dead_on_arrival_mints(plans)
    by_reason: dict[str, int] = result["by_reason"]  # type: ignore[assignment]
    assert by_reason.get("flag-graduation") == 1



# ── format_dead_on_arrival_mints_summary ───────────────────────────


def test_format_dead_on_arrival_mints_summary_empty_when_none() -> None:
    """Self-suppression: no dead-on-arrival plan yields an empty string."""
    assert format_dead_on_arrival_mints_summary(
        [_mill_plan("p1", status="merged")],
    ) == ""


def test_format_dead_on_arrival_mints_summary_renders_line() -> None:
    """A door-obsoleted plan renders the DEAD-ON-ARRIVAL-MINTS sensor line."""
    line = format_dead_on_arrival_mints_summary(
        [_mill_plan("p1", status="obsolete", obsolete_reason=_DOA_ADMISSION_REASON)],
    )
    assert line.startswith("DEAD-ON-ARRIVAL-MINTS: 1/1")
    assert "evolve=1" in line


# Two persisted ``[ungrounded:<category>]`` obsolete_reason signatures, verbatim
# in shape from ``evolution_submission.py``'s ``submit_for_review`` grounding guard — a
# ``SYMBOL_ABSENT`` locator already defined (``duplicate``) and a
# ``DEFECT_PRESENT`` needle missing (``no-such-defect``). A plan carrying either,
# with a non-null ``grounding`` probe, was refused by a fresh reading of the tree.
_UNGROUNDED_DUPLICATE_REASON = (
    "[ungrounded:duplicate] grounding probe SYMBOL_ABSENT on my_new_gate is "
    "refuted by a fresh reading of the tree"
)
_UNGROUNDED_NO_DEFECT_REASON = (
    "[ungrounded:no-such-defect] grounding probe DEFECT_PRESENT on maestro/x.py "
    "is refuted by a fresh reading of the tree"
)
_PROBE = GroundingProbe(kind="SYMBOL_ABSENT", locator="my_new_gate")


# ── measure_ungrounded_premise_rate ────────────────────────────────


def test_measure_ungrounded_premise_rate_truth_table() -> None:
    """A seeded set reproduces the full report by dict equality.

    p1 (duplicate) and p2 (no-such-defect) are probe-carrying plans the tree
    refuted; p3 is probe-carrying but still pending (not obsoleted); p4 carries no
    probe at all, so it is excluded from the denominator despite being obsolete
    with an ungrounded reason.
    """
    plans = [
        _mill_plan("p1", status="obsolete",
                   obsolete_reason=_UNGROUNDED_DUPLICATE_REASON,
                   grounding=_PROBE, created_at="2026-01-01T00:00:00Z"),
        _mill_plan("p2", status="obsolete",
                   obsolete_reason=_UNGROUNDED_NO_DEFECT_REASON,
                   grounding=_PROBE, created_at="2026-01-02T00:00:00Z"),
        _mill_plan("p3", status="pending_review", grounding=_PROBE,
                   created_at="2026-01-03T00:00:00Z"),
        _mill_plan("p4", status="obsolete",
                   obsolete_reason=_UNGROUNDED_DUPLICATE_REASON,
                   grounding=None, created_at="2026-01-04T00:00:00Z"),
    ]
    assert measure_ungrounded_premise_rate(plans) == {
        "probe_carrying": 3,
        "ungrounded": 2,
        "ungrounded_fraction": 2 / 3,
        "by_category": {"duplicate": 1, "no-such-defect": 1},
    }


def test_measure_ungrounded_premise_rate_empty_is_zero() -> None:
    """An empty plan list yields the zeroed dict, not divide-by-zero."""
    assert measure_ungrounded_premise_rate([]) == {
        "probe_carrying": 0,
        "ungrounded": 0,
        "ungrounded_fraction": 0.0,
        "by_category": {},
    }


def test_measure_ungrounded_premise_rate_excludes_non_ungrounded_reason() -> None:
    """A probe-carrying obsolete plan with a non-ungrounded reason is excluded.

    Exercises the ``match is None`` continue branch: the plan is obsolete and
    carries a probe, but its reason does not match the ``[ungrounded:*]`` prefix.
    """
    plans = [
        _mill_plan("p1", status="obsolete",
                   obsolete_reason="superseded by a newer plan", grounding=_PROBE),
    ]
    result = measure_ungrounded_premise_rate(plans)
    assert result["ungrounded"] == 0
    assert result["probe_carrying"] == 1


def test_measure_ungrounded_premise_rate_excludes_obsolete_without_reason() -> None:
    """A probe-carrying obsolete plan with a None reason is excluded (guard branch).

    Exercises the ``not isinstance(obsolete_reason, str)`` continue branch.
    """
    plans = [
        _mill_plan("p1", status="obsolete", obsolete_reason=None, grounding=_PROBE),
    ]
    result = measure_ungrounded_premise_rate(plans)
    assert result["ungrounded"] == 0
    assert result["probe_carrying"] == 1


# ── format_ungrounded_rate_summary ─────────────────────────────────


def test_format_ungrounded_rate_summary_empty_when_none() -> None:
    """Self-suppression: no tree-refuted plan yields an empty string."""
    assert format_ungrounded_rate_summary(
        [_mill_plan("p1", status="pending_review", grounding=_PROBE)],
    ) == ""


def test_format_ungrounded_rate_summary_renders_line() -> None:
    """A tree-refuted premise-carrying plan renders the UNGROUNDED-RATE line."""
    line = format_ungrounded_rate_summary(
        [_mill_plan("p1", status="obsolete",
                    obsolete_reason=_UNGROUNDED_DUPLICATE_REASON, grounding=_PROBE)],
    )
    assert line.startswith("UNGROUNDED-RATE: 1/1")
    assert "duplicate=1" in line


# ── format_self_calibration_summary ─────────────────────────────────


def test_format_self_calibration_summary_empty_when_unstamped(
    mem_fs: dict[str, str],
) -> None:
    """Self-suppression: no verdict_source-stamped row yields an empty string."""
    b = _prediction_backend(FAKE_DIR)
    record_reflection_prediction(b, "reflection-1", "p", "loss")
    record_reflection_verdict(b, "reflection-1", "correct")  # no source stamp
    assert format_self_calibration_summary(b) == ""


def test_format_self_calibration_summary_renders_without_brier(
    mem_fs: dict[str, str],
) -> None:
    """Stamped rows below the confidence threshold render the line, no Brier clause."""
    b = _prediction_backend(FAKE_DIR)
    record_reflection_prediction(b, "reflection-1", "p", "loss")
    record_reflection_verdict(
        b, "reflection-1", "correct",
        verdict_source="measured", self_verdict="correct",
    )
    record_reflection_prediction(b, "reflection-2", "p", "loss")
    record_reflection_verdict(b, "reflection-2", "correct", verdict_source="narrated")
    line = format_self_calibration_summary(b)
    assert line.startswith("SELF-CALIBRATION: measured verdicts land 1/1 (100%)")
    assert "narrated 1/2 (50%)" in line
    assert "Brier" not in line
    assert line.endswith("the bets I want to make")


def test_format_self_calibration_summary_renders_brier_and_bins(
    mem_fs: dict[str, str],
) -> None:
    """Enough confidence-bearing measured bets render the Brier score and bins."""
    b = _prediction_backend(FAKE_DIR)
    rows = [
        ("reflection-1", 0.8, "correct"),
        ("reflection-2", 0.9, "correct"),
        ("reflection-3", 0.7, "correct"),
        ("reflection-4", 0.2, "wrong"),
        ("reflection-5", 0.1, "wrong"),
    ]
    for pid, conf, verdict in rows:
        record_reflection_prediction(b, pid, "p", "loss", confidence=conf)
        record_reflection_verdict(b, pid, verdict, verdict_source="measured")
    line = format_self_calibration_summary(b)
    assert "Brier 0." in line
    assert "0.67-1.00=3/3" in line
    assert "0.00-0.33=0/2" in line


# ── measure_stranded_promotion_plans ───────────────────────────────


def test_measure_stranded_promotion_plans_matches_and_lists_ids() -> None:
    """A 'finish the stranded … branch' title is counted with its id."""
    result = measure_stranded_promotion_plans(_mill_mixed_plans())
    assert result == {"count": 1, "plan_ids": ["p4"]}


def test_measure_stranded_promotion_plans_promote_into_main() -> None:
    """A 'Promote task branch … into main' title also matches."""
    plans = [
        _mill_plan("a", title="Promote task branch maestro/task/x into main"),
        _mill_plan("b", title="Land the widget"),
        _mill_plan("c", title="Refactor the sensor"),
    ]
    result = measure_stranded_promotion_plans(plans)
    assert result == {"count": 1, "plan_ids": ["a"]}


def test_measure_stranded_promotion_plans_object_first_not_matched() -> None:
    """An object-first title is intentionally not matched (ordering is tight).

    The regex requires the promotion verb before the branch object, so a title
    that names the branch first and the verb second is by-design excluded — see
    the ``_STRANDED_PROMOTION_RE`` comment. Locks in that calibrated behaviour.
    """
    plans = [
        _mill_plan("a", title="Stranded branch maestro/task/x: promote to main"),
        _mill_plan("b", title="Promote task branch maestro/task/x into main"),
    ]
    result = measure_stranded_promotion_plans(plans)
    assert result == {"count": 1, "plan_ids": ["b"]}


def test_measure_stranded_promotion_plans_empty_is_zero() -> None:
    """An empty plan list yields a zero count and no ids."""
    assert measure_stranded_promotion_plans([]) == {"count": 0, "plan_ids": []}


# ---------------------------------------------------------------------------
# limitations_over_generative_debt (proposal 116, increment 3)
# ---------------------------------------------------------------------------

_DEBT_LATEST = "loop-generation plans never land"
_DEBT_W1 = "I cannot reliably land loop-generation plans"
_DEBT_W2 = "loop-gen plans do not land"


def _debt_task(task_id: str, linked_question: str) -> TaskRecord:
    """A TaskRecord anchored to *linked_question* for the debt join."""
    return TaskRecord(
        task_id=task_id,
        goal="goal",
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome="success",  # type: ignore[arg-type]
        result_summary="",
        errors=[],
        duration_secs=0.5,
        timestamp="2026-01-01T00:00:00Z",
        branch=None,
        key_findings=None,
        linked_question=linked_question,
    )


def _debt_lineage() -> list[QuestionResolution]:
    """A two-hop refined_to chain W1 → W2 → the latest ``[limitation]``."""
    latest = limitation_question_candidate(_DEBT_LATEST)
    return [
        _resolution(
            question=limitation_question_candidate(_DEBT_W1),
            disposition="refined",
            refined_to=limitation_question_candidate(_DEBT_W2),
            task_id="rt0", iteration=0,
        ),
        _resolution(
            question=limitation_question_candidate(_DEBT_W2),
            disposition="refined",
            refined_to=latest,
            task_id="rt1", iteration=1,
        ),
    ]


def _debt_key() -> str:
    """The normalised suppression key the latest wording surfaces as."""
    return _normalize_question(limitation_question_candidate(_DEBT_LATEST))


def test_generative_debt_accumulates_across_refined_lineage() -> None:
    """Debt sums across the whole refined_to chain, and the candidate key is
    suppressed at (but not below) the threshold."""
    resolutions = _debt_lineage()
    tasks = [
        _debt_task("tA", limitation_question_candidate(_DEBT_W1)),
        _debt_task("tB", limitation_question_candidate(_DEBT_W2)),
        _debt_task("tC", limitation_question_candidate(_DEBT_LATEST)),
    ]
    plans = [
        _mill_plan("pA", status="obsolete", source_task_id="tA"),
        _mill_plan("pB", status="rejected", source_task_id="tB"),
        _mill_plan("pC", status="obsolete", source_task_id="tC"),
    ]
    # Three lineage-wide unlanded plans exactly meet a threshold of 3.
    assert limitations_over_generative_debt(
        [_DEBT_LATEST], resolutions, tasks, plans, threshold=3,
    ) == {_debt_key()}
    # One more than the accrued debt → below threshold, not suppressed.
    assert limitations_over_generative_debt(
        [_DEBT_LATEST], resolutions, tasks, plans, threshold=4,
    ) == set()


def test_generative_debt_single_wording_below_threshold() -> None:
    """A single wording's lone unlanded plan does not cross a threshold of 3 —
    only the lineage-wide accumulation does."""
    resolutions = _debt_lineage()
    tasks = [_debt_task("tC", limitation_question_candidate(_DEBT_LATEST))]
    plans = [_mill_plan("pC", status="obsolete", source_task_id="tC")]
    assert limitations_over_generative_debt(
        [_DEBT_LATEST], resolutions, tasks, plans, threshold=3,
    ) == set()


def test_generative_debt_ignores_landed_and_open_plans() -> None:
    """Only obsolete/rejected plans count; merged / pending_review / failed /
    approved / implemented plans never add to debt."""
    tasks = [_debt_task("tC", limitation_question_candidate(_DEBT_LATEST))]
    non_terminal = [
        _mill_plan("p1", status="merged", source_task_id="tC"),
        _mill_plan("p2", status="pending_review", source_task_id="tC"),
        _mill_plan("p3", status="failed", source_task_id="tC"),
        _mill_plan("p4", status="approved", source_task_id="tC"),
        _mill_plan("p5", status="implemented", source_task_id="tC"),
    ]
    assert limitations_over_generative_debt(
        [_DEBT_LATEST], [], tasks, non_terminal, threshold=1,
    ) == set()


def test_generative_debt_threshold_below_one_returns_empty() -> None:
    """A threshold below 1 short-circuits to an empty set."""
    tasks = [_debt_task("tC", limitation_question_candidate(_DEBT_LATEST))]
    plans = [_mill_plan("pC", status="obsolete", source_task_id="tC")]
    assert limitations_over_generative_debt(
        [_DEBT_LATEST], [], tasks, plans, threshold=0,
    ) == set()


def test_generative_debt_skips_non_string_and_blank_limitations() -> None:
    """Non-string and whitespace-only limitation entries are skipped."""
    tasks = [_debt_task("tC", limitation_question_candidate(_DEBT_LATEST))]
    plans = [_mill_plan("pC", status="obsolete", source_task_id="tC")]
    assert limitations_over_generative_debt(
        ["", "   ", 42, None],  # type: ignore[list-item]
        [], tasks, plans, threshold=1,
    ) == set()


def test_generative_debt_ignores_plans_without_source_task_id() -> None:
    """An obsolete/rejected plan lacking a source_task_id contributes no debt."""
    tasks = [_debt_task("tC", limitation_question_candidate(_DEBT_LATEST))]
    plans = [
        _mill_plan("p1", status="obsolete", source_task_id=None),
        _mill_plan("p2", status="rejected", source_task_id=""),
    ]
    assert limitations_over_generative_debt(
        [_DEBT_LATEST], [], tasks, plans, threshold=1,
    ) == set()


def test_generative_debt_ignores_tasks_without_linked_question() -> None:
    """A task with no linked_question anchors no lineage debt."""
    tasks = [_task("tC")]  # linked_question defaults to None
    plans = [_mill_plan("pC", status="obsolete", source_task_id="tC")]
    assert limitations_over_generative_debt(
        [_DEBT_LATEST], [], tasks, plans, threshold=1,
    ) == set()


def test_generative_debt_skips_malformed_plan_and_task_rows() -> None:
    """Non-plan / non-task rows and empty task ids are skipped defensively,
    while the well-formed row still accrues its debt."""
    tasks = [
        "not-a-task",  # type: ignore[list-item]
        _debt_task("", limitation_question_candidate(_DEBT_LATEST)),
        _debt_task("tC", limitation_question_candidate(_DEBT_LATEST)),
    ]
    plans = [
        "not-a-plan",  # type: ignore[list-item]
        _mill_plan("pC", status="obsolete", source_task_id="tC"),
    ]
    assert limitations_over_generative_debt(
        [_DEBT_LATEST], [], tasks, plans, threshold=1,
    ) == {_debt_key()}
