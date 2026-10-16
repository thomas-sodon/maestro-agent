"""Tests for maestro/stats.py — pure function health summary and noteworthy gate."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import maestro.stats as stats_mod
from maestro.audit_checks import (
    classify_settled_relap,
    format_known_state_summary,
    format_settled_relap_summary,
    measure_relap_rate,
)
from maestro.diagnostics import DiagnosticSnapshot
from maestro.memory import (
    EvolutionPlan,
    EvolutionProposal,
    Memory,
    TaskRecord,
)
from maestro.state_store import SqliteOwnedBacklogLedger, StateStore
from maestro.stats import (
    _MAX_CORRUPT_PLAN_LINES,
    _OWNED_BACKLOG_LEDGER_FILE,
    _PENDING_PLAN_BACKLOG_THRESHOLD,
    BacklogSnapshot,
    JsonlOwnedBacklogLedger,
    _failure_streak,
    _outcome_trend,
    _success_rate,
    format_confirmed_landing_summary,
    format_multi_pr_rollout_summary,
    format_owned_net_delta_summary,
    format_pending_plan_backlog,
    is_health_noteworthy,
    load_all_backlog_snapshots,
    measure_owned_net_delta,
    record_owned_backlog_once,
    record_owned_backlog_snapshot,
    summarize_iteration_health,
)

_RELAP_DIR = Path("/fake/relap")
_CONFIRMED_DIR = Path("/fake/confirmed")
_PROSE_DIR = Path("/fake/prose")
_PLANS_DIR = Path("/fake/plans")


def _task(outcome: str = "success") -> TaskRecord:
    """Build a minimal TaskRecord stub with a given outcome."""
    return TaskRecord(
        task_id="t",
        goal="g",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="",
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
    )


def _snapshot(
    error_count: int = 0,
    cost_trend: str | None = None,
) -> DiagnosticSnapshot:
    return DiagnosticSnapshot(
        recent_error_count=error_count,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="test",
        cost_trend=cost_trend,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# _success_rate
# ---------------------------------------------------------------------------


class TestSuccessRate:
    def test_empty_returns_one(self) -> None:
        assert _success_rate([]) == 1.0

    def test_all_success(self) -> None:
        assert _success_rate([_task("success")] * 5) == 1.0

    def test_all_failure(self) -> None:
        assert _success_rate([_task("failure")] * 4) == 0.0

    def test_mixed(self) -> None:
        tasks = [_task("success"), _task("failure"), _task("success"), _task("partial")]
        assert _success_rate(tasks) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# _failure_streak
# ---------------------------------------------------------------------------


class TestFailureStreak:
    def test_empty(self) -> None:
        assert _failure_streak([]) == 0

    def test_no_failures(self) -> None:
        assert _failure_streak([_task("success")] * 3) == 0

    def test_all_failures(self) -> None:
        assert _failure_streak([_task("failure")] * 4) == 4

    def test_trailing_failures(self) -> None:
        tasks = [_task("success"), _task("failure"), _task("failure")]
        assert _failure_streak(tasks) == 2

    def test_success_breaks_streak(self) -> None:
        tasks = [_task("failure"), _task("success"), _task("failure")]
        assert _failure_streak(tasks) == 1

    def test_partial_counts_as_non_success(self) -> None:
        tasks = [_task("success"), _task("partial")]
        assert _failure_streak(tasks) == 1


# ---------------------------------------------------------------------------
# _outcome_trend
# ---------------------------------------------------------------------------


class TestOutcomeTrend:
    def test_too_few_tasks_returns_stable(self) -> None:
        assert _outcome_trend([_task()] * 3) == "stable"

    def test_stable_when_equal(self) -> None:
        tasks = [_task("success")] * 6
        assert _outcome_trend(tasks) == "stable"

    def test_improving(self) -> None:
        # Older half: all fail. Newer half: all succeed.
        tasks = [_task("failure")] * 3 + [_task("success")] * 3
        assert _outcome_trend(tasks) == "improving"

    def test_declining(self) -> None:
        # Older half: all succeed. Newer half: all fail.
        tasks = [_task("success")] * 3 + [_task("failure")] * 3
        assert _outcome_trend(tasks) == "declining"

    def test_small_delta_stays_stable(self) -> None:
        # 4 tasks: [success, failure, success, failure] -> 50% / 50%
        tasks = [_task("success"), _task("failure"), _task("success"), _task("failure")]
        assert _outcome_trend(tasks) == "stable"


# ---------------------------------------------------------------------------
# is_health_noteworthy
# ---------------------------------------------------------------------------


class TestIsHealthNoteworthy:
    def test_normal_health_not_noteworthy(self) -> None:
        tasks = [_task("success")] * 5
        assert is_health_noteworthy(tasks, _snapshot()) is False

    def test_low_success_rate_noteworthy(self) -> None:
        tasks = [_task("success"), _task("failure"), _task("failure")]
        assert is_health_noteworthy(tasks, _snapshot()) is True

    def test_low_success_rate_with_few_tasks_not_noteworthy(self) -> None:
        """Fewer than 3 tasks does not trigger the success-rate check."""
        tasks = [_task("failure"), _task("failure")]
        assert is_health_noteworthy(tasks, _snapshot()) is False

    def test_failure_streak_noteworthy(self) -> None:
        # 7 success + 3 failure = 70% success rate (not < 70%), but streak == 3
        tasks = [_task("success")] * 7 + [_task("failure")] * 3
        assert is_health_noteworthy(tasks, _snapshot()) is True

    def test_high_error_count_noteworthy(self) -> None:
        assert is_health_noteworthy([], _snapshot(error_count=5)) is True

    def test_moderate_error_count_not_noteworthy(self) -> None:
        assert is_health_noteworthy([], _snapshot(error_count=4)) is False

    def test_cost_spiking_noteworthy(self) -> None:
        assert is_health_noteworthy([], _snapshot(cost_trend="spiking")) is True

    def test_cost_rising_not_noteworthy(self) -> None:
        assert is_health_noteworthy([], _snapshot(cost_trend="rising")) is False

    def test_cost_flat_not_noteworthy(self) -> None:
        tasks = [_task("success")] * 5
        assert is_health_noteworthy(tasks, _snapshot(cost_trend="flat")) is False

    def test_empty_tasks_and_clean_snapshot(self) -> None:
        assert is_health_noteworthy([], _snapshot()) is False


# ---------------------------------------------------------------------------
# summarize_iteration_health
# ---------------------------------------------------------------------------


class TestSummarizeIterationHealth:
    def test_all_success(self) -> None:
        tasks = [_task("success")] * 5
        result = summarize_iteration_health(tasks, _snapshot())
        assert "success_rate=100%" in result
        assert "failure_streak=0" in result
        assert "outcome_trend=stable" in result
        assert "errors=0" in result
        assert "cost_trend=unknown" in result

    def test_mixed_outcomes(self) -> None:
        tasks = [_task("failure")] * 3 + [_task("success")] * 3
        result = summarize_iteration_health(tasks, _snapshot(error_count=7))
        assert "success_rate=50%" in result
        assert "failure_streak=0" in result
        assert "outcome_trend=improving" in result
        assert "errors=7" in result

    def test_empty_tasks(self) -> None:
        result = summarize_iteration_health([], _snapshot())
        assert "success_rate=100%" in result
        assert "failure_streak=0" in result

    def test_cost_trend_shown(self) -> None:
        tasks = [_task("success")] * 5
        result = summarize_iteration_health(tasks, _snapshot(cost_trend="spiking"))
        assert "cost_trend=spiking" in result

    def test_pipe_delimited_format(self) -> None:
        result = summarize_iteration_health([_task()], _snapshot())
        # Verify the pipe-delimited structure
        parts = [p.strip() for p in result.split("|")]
        assert len(parts) == 5
        assert parts[0].startswith("success_rate=")
        assert parts[1].startswith("failure_streak=")
        assert parts[2].startswith("outcome_trend=")
        assert parts[3].startswith("errors=")
        assert parts[4].startswith("cost_trend=")

    def test_no_op_rate_condition_appended(self) -> None:
        """High no-op task rate surfaces as an extra line appended after the pipe-delimited summary."""
        no_op = TaskRecord(
            task_id="t",
            goal="g",
            plan=[],
            outcome="success",  # type: ignore[arg-type]
            result_summary="No changes needed.",
            errors=[],
            duration_secs=1.0,
            timestamp="2026-01-01T00:00:00",
        )
        meaningful = TaskRecord(
            task_id="t",
            goal="g",
            plan=[],
            outcome="success",  # type: ignore[arg-type]
            result_summary="Shipped the feature.",
            errors=[],
            duration_secs=1.0,
            timestamp="2026-01-01T00:00:00",
        )
        tasks = [meaningful] * 6 + [no_op] * 4
        result = summarize_iteration_health(tasks, _snapshot())
        assert "No-op task rate" in result
        assert "4/10" in result
        # Condition is appended on its own line below the pipe summary.
        first_line, _, condition_line = result.partition("\n")
        assert first_line.startswith("success_rate=")
        assert "No-op task rate" in condition_line


# ---------------------------------------------------------------------------
# Settled-relap sub-classification — distinguish re-verifying a decided verdict
# from a genuine no-op at a live fork.
# ---------------------------------------------------------------------------


def _relap_task(
    result_summary: str,
    outcome: str = "success",
) -> dict[str, object]:
    """Build a synthetic task dict for settled-relap classification tests."""
    return {"outcome": outcome, "result_summary": result_summary}


def _relap_record(
    task_id: str,
    result_summary: str,
    outcome: str = "success",
) -> TaskRecord:
    """Build a TaskRecord for measure_relap_rate (disk-reading) tests."""
    return TaskRecord(
        task_id=task_id,
        goal="g",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary=result_summary,
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
    )


class TestClassifySettledRelap:
    """A no-op re-verifying a decided verdict is settled-relap, not live-fork."""

    def test_merged_pr_reconfirmation_is_settled_relap(self) -> None:
        task = _relap_task(
            "No remediation needed — PR #2564 already merged into main.",
        )
        assert classify_settled_relap(task) == "settled_relap"

    def test_already_resolved_thread_is_settled_relap(self) -> None:
        task = _relap_task("Already resolved upstream; nothing to do.")
        assert classify_settled_relap(task) == "settled_relap"

    def test_handed_off_lineage_is_settled_relap(self) -> None:
        task = _relap_task(
            "No code changes — lineage already handed off to Thomas.",
        )
        assert classify_settled_relap(task) == "settled_relap"

    def test_live_fork_genuine_noop_is_not_settled_relap(self) -> None:
        task = _relap_task(
            "Confirmed existing state: gates green, awaiting tend push.",
        )
        assert classify_settled_relap(task) == "live_fork"

    def test_non_noop_task_returns_none(self) -> None:
        task = _relap_task("Shipped the new audit gate and tests.")
        assert classify_settled_relap(task) is None

    def test_missing_result_summary_returns_none(self) -> None:
        assert classify_settled_relap({"outcome": "success"}) is None


class TestMeasureRelapRate:
    """measure_relap_rate returns the settled-relap fraction over no-ops."""

    def test_fraction_over_fixture_records(
        self,
        mem_fs: dict[str, str],
    ) -> None:
        mem = Memory(_RELAP_DIR)
        # 3 no-ops: 2 settled-relap, 1 live-fork → relap_rate == 2/3.
        mem.record_task(
            _relap_record("t0", "No remediation needed — PR already merged."),
        )
        mem.record_task(_relap_record("t1", "Already resolved upstream."))
        mem.record_task(
            _relap_record("t2", "Confirmed existing state, awaiting tend push."),
        )
        # A meaningful task is not a no-op and is excluded from the denominator.
        mem.record_task(_relap_record("t3", "Shipped a new feature."))
        # A failed no-op is excluded by the success-outcome filter.
        mem.record_task(
            _relap_record(
                "t4", "No remediation needed — PR merged.", outcome="failure",
            ),
        )

        result = measure_relap_rate(_RELAP_DIR)
        assert result["total_noop"] == 3
        assert result["settled_relap"] == 2
        assert result["live_fork"] == 1
        assert result["relap_rate"] == pytest.approx(2 / 3)

    def test_empty_history_zero_rate(
        self,
        mem_fs: dict[str, str],
    ) -> None:
        result = measure_relap_rate(_RELAP_DIR)
        assert result["total_noop"] == 0
        assert result["relap_rate"] == pytest.approx(0.0)


class TestFormatSettledRelapSummary:
    """SETTLED-RELAP line appears only above the relap-rate threshold."""

    def test_line_present_above_threshold(self) -> None:
        # 3 settled-relaps, 1 live-fork → rate 0.75 > default 0.4.
        history: list[dict[str, object]] = [
            _relap_task("No remediation needed — PR already merged."),
            _relap_task("Already resolved upstream."),
            _relap_task("No code changes — already handed off."),
            _relap_task("Confirmed existing state, awaiting tend push."),
        ]
        line = format_settled_relap_summary(history)
        assert line.startswith("SETTLED-RELAP:")
        assert "3/4" in line

    def test_line_absent_below_threshold(self) -> None:
        # 1 settled-relap, 3 live-fork → rate 0.25 <= default 0.4.
        history: list[dict[str, object]] = [
            _relap_task("No remediation needed — PR already merged."),
            _relap_task("Confirmed existing state, awaiting tend push."),
            _relap_task("Already passing all tests on the open PR."),
            _relap_task("No code changes; gates green at a live fork."),
        ]
        assert format_settled_relap_summary(history) == ""

    def test_empty_history_returns_empty(self) -> None:
        assert format_settled_relap_summary(None) == ""
        assert format_settled_relap_summary([]) == ""


class TestFormatConfirmedLandingSummary:
    """CONFIRMED-LANDING-RATIO line appears only above the threshold."""

    def test_line_present_above_threshold(self, mem_fs: dict[str, str]) -> None:
        # 3 confirmed, 1 caused → confirmed_rate 0.75 > default 0.5.
        mem = Memory(_CONFIRMED_DIR)
        mem.record_task(_relap_record("c0", "Already merged before this task."))
        mem.record_task(_relap_record("c1", "git shows merged; confirmed."))
        mem.record_task(_relap_record("c2", "PR was already merged."))
        mem.record_task(
            _relap_record("c3", "Merged origin/main and resolved the conflict."),
        )
        line = format_confirmed_landing_summary(_CONFIRMED_DIR)
        assert line.startswith("CONFIRMED-LANDING-RATIO:")
        assert "3/4" in line

    def test_line_absent_below_threshold(self, mem_fs: dict[str, str]) -> None:
        # 1 confirmed, 3 caused → confirmed_rate 0.25 <= default 0.5.
        mem = Memory(_CONFIRMED_DIR)
        mem.record_task(_relap_record("c0", "Already merged before this task."))
        mem.record_task(_relap_record("c1", "Merged origin/main into branch."))
        mem.record_task(_relap_record("c2", "Resolved the merge conflict."))
        mem.record_task(_relap_record("c3", "Committed the fix on-branch."))
        assert format_confirmed_landing_summary(_CONFIRMED_DIR) == ""

    def test_empty_history_returns_empty(self, mem_fs: dict[str, str]) -> None:
        assert format_confirmed_landing_summary(_CONFIRMED_DIR) == ""


class TestProseVerdictGapSensorRemoved:
    """The PROSE-VERDICT-GAP contemplation sensor is gone (subsumed by the mechanical outcome authority).

    Deleting the redundant mirror means three things must hold: the two helper
    symbols no longer exist on :mod:`maestro.stats`, ``format_known_state_summary``
    no longer accepts the keyword that fed the line, and its output never carries
    the ``PROSE-VERDICT-GAP`` substring for inputs that previously triggered it.
    """

    def test_helper_symbols_absent(self) -> None:
        assert not hasattr(stats_mod, "measure_prose_verdict_gap")
        assert not hasattr(stats_mod, "format_prose_verdict_gap_summary")
        assert not hasattr(stats_mod, "_PROSE_VERDICT_GAP_THRESHOLD")
        assert not hasattr(stats_mod, "_PROSE_VERDICT_GAP_KEYWORDS")
        assert not hasattr(stats_mod, "_PROSE_VERDICT_GAP_RE")

    def test_known_state_summary_drops_prose_verdict_gap_param(self) -> None:
        # The keyword that fed the now-deleted line must no longer be accepted.
        with pytest.raises(TypeError):
            format_known_state_summary(
                {}, [], prose_verdict_gap_context="PROSE-VERDICT-GAP: 75% slip",
            )

    def test_known_state_summary_never_emits_prose_verdict_gap(self) -> None:
        # An input that once routed a 75%-slip line through the sensor now
        # produces a section with no PROSE-VERDICT-GAP substring at all.
        section = format_known_state_summary(
            {"forced_quiet": True},
            ["Remediate PR #2737"],
            forced_quiet_context="FORCED-QUIET-RATIO: 15/15 forced",
        )
        assert "PROSE-VERDICT-GAP" not in section


_PLANS_PATH = str(_PLANS_DIR / "evolution_plans.jsonl")
_EXPECTED_BACKLOG = (
    "PENDING-PLAN BACKLOG: {n} unreviewed plans — proposing is "
    "not your bottleneck; landing is."
)


def _seed_plans(
    mem_fs: dict[str, str],
    *,
    pending: int = 0,
    other: int = 0,
    corrupt: int = 0,
) -> None:
    """Write an ``evolution_plans.jsonl`` body into the in-memory filesystem.

    *pending* rows carry ``status="pending_review"``; *other* rows carry a
    terminal status (``merged``) so they must not be counted; *corrupt* rows
    are non-JSON garbage lines appended at the tail to exercise the bounded
    corrupted-record skip. Order is pending → other → corrupt.
    """
    lines: list[str] = []
    for i in range(pending):
        lines.append(json.dumps({"plan_id": f"p{i}", "status": "pending_review"}))
    for i in range(other):
        lines.append(json.dumps({"plan_id": f"m{i}", "status": "merged"}))
    lines.extend("{not valid json" for _ in range(corrupt))
    mem_fs[_PLANS_PATH] = "\n".join(lines) + ("\n" if lines else "")


class TestFormatPendingPlanBacklog:
    """PENDING-PLAN BACKLOG line appears only at/above the threshold."""

    def test_missing_file_returns_empty(self, mem_fs: dict[str, str]) -> None:
        assert format_pending_plan_backlog(_PLANS_DIR) == ""

    def test_empty_file_returns_empty(self, mem_fs: dict[str, str]) -> None:
        _seed_plans(mem_fs)
        assert format_pending_plan_backlog(_PLANS_DIR) == ""

    def test_below_threshold_returns_empty(self, mem_fs: dict[str, str]) -> None:
        # Two pending plans, default threshold is 3 → suppressed.
        _seed_plans(mem_fs, pending=2)
        assert format_pending_plan_backlog(_PLANS_DIR) == ""

    def test_at_threshold_returns_exact_string(
        self, mem_fs: dict[str, str],
    ) -> None:
        _seed_plans(mem_fs, pending=_PENDING_PLAN_BACKLOG_THRESHOLD)
        result = format_pending_plan_backlog(_PLANS_DIR)
        assert result == _EXPECTED_BACKLOG.format(
            n=_PENDING_PLAN_BACKLOG_THRESHOLD,
        )

    def test_above_threshold_counts_only_pending(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Five pending + three merged → count is 5, merged ignored.
        _seed_plans(mem_fs, pending=5, other=3)
        result = format_pending_plan_backlog(_PLANS_DIR)
        assert result == _EXPECTED_BACKLOG.format(n=5)

    def test_custom_threshold_respected(self, mem_fs: dict[str, str]) -> None:
        _seed_plans(mem_fs, pending=1)
        assert format_pending_plan_backlog(_PLANS_DIR, threshold=1) == (
            _EXPECTED_BACKLOG.format(n=1)
        )

    def test_corrupted_records_skipped_bounded(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Valid pending rows precede a corrupted tail larger than the skip cap.
        # The scan must stop after the cap, but the valid rows already counted
        # still surface the backlog line with the correct count.
        _seed_plans(
            mem_fs, pending=4, corrupt=_MAX_CORRUPT_PLAN_LINES + 50,
        )
        result = format_pending_plan_backlog(_PLANS_DIR)
        assert result == _EXPECTED_BACKLOG.format(n=4)

    def test_non_mapping_rows_ignored_without_consuming_budget(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A JSON array parses fine but is not a plan row: it must be ignored
        # without being counted and without spending the corruption budget.
        lines = [json.dumps([1, 2, 3]) for _ in range(3)]
        lines += [
            json.dumps({"plan_id": f"p{i}", "status": "pending_review"})
            for i in range(3)
        ]
        mem_fs[_PLANS_PATH] = "\n".join(lines) + "\n"
        assert format_pending_plan_backlog(_PLANS_DIR) == (
            _EXPECTED_BACKLOG.format(n=3)
        )

    def test_blank_lines_skipped(self, mem_fs: dict[str, str]) -> None:
        # Interior blank lines must be skipped without counting or crashing.
        rows = [
            json.dumps({"plan_id": f"p{i}", "status": "pending_review"})
            for i in range(3)
        ]
        mem_fs[_PLANS_PATH] = f"{rows[0]}\n\n{rows[1]}\n   \n{rows[2]}\n"
        assert format_pending_plan_backlog(_PLANS_DIR) == (
            _EXPECTED_BACKLOG.format(n=3)
        )

    def test_threshold_constant_is_positive(self) -> None:
        assert _PENDING_PLAN_BACKLOG_THRESHOLD > 0


class TestKnownStatePendingPlanBacklogWiring:
    """PENDING-PLAN BACKLOG surfaces in the known-state section via data_dir."""

    def test_line_present_when_seeded_above_threshold(
        self, mem_fs: dict[str, str],
    ) -> None:
        _seed_plans(mem_fs, pending=5)
        section = format_known_state_summary({}, [], data_dir=_PLANS_DIR)
        assert _EXPECTED_BACKLOG.format(n=5) in section

    def test_line_absent_below_threshold(
        self, mem_fs: dict[str, str],
    ) -> None:
        _seed_plans(mem_fs, pending=1)
        section = format_known_state_summary({}, [], data_dir=_PLANS_DIR)
        assert "PENDING-PLAN BACKLOG" not in section

    def test_line_absent_when_data_dir_none(self) -> None:
        section = format_known_state_summary({}, [])
        assert "PENDING-PLAN BACKLOG" not in section


_OWNED_DIR = Path("/fake/owned")
_OWNED_PATH = str(_OWNED_DIR / _OWNED_BACKLOG_LEDGER_FILE)


def _read_ledger_rows(mem_fs: dict[str, str]) -> list[dict[str, object]]:
    """Parse the owned-backlog ledger rows written into the in-memory FS."""
    body = mem_fs.get(_OWNED_PATH, "")
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def _snap(iteration: int, count: int, ts: str = "t") -> BacklogSnapshot:
    """Build a BacklogSnapshot for the substrate-agnostic delta cases."""
    return BacklogSnapshot(iteration=iteration, count=count, timestamp=ts)


class TestRecordOwnedBacklogSnapshot:
    """record_owned_backlog_snapshot appends exactly one row per call."""

    def test_single_row_appended_per_call(self, mem_fs: dict[str, str]) -> None:
        record_owned_backlog_snapshot(_OWNED_DIR, 5, 3, now="2026-01-01T00:00:00")
        rows = _read_ledger_rows(mem_fs)
        assert len(rows) == 1
        assert rows[0] == {
            "iteration": 5,
            "count": 3,
            "timestamp": "2026-01-01T00:00:00",
        }

    def test_appends_never_rewrite_prior_rows(
        self, mem_fs: dict[str, str],
    ) -> None:
        record_owned_backlog_snapshot(_OWNED_DIR, 1, 10, now="t1")
        record_owned_backlog_snapshot(_OWNED_DIR, 2, 12, now="t2")
        record_owned_backlog_snapshot(_OWNED_DIR, 3, 9, now="t3")
        rows = _read_ledger_rows(mem_fs)
        assert len(rows) == 3
        assert [r["count"] for r in rows] == [10, 12, 9]
        assert [r["iteration"] for r in rows] == [1, 2, 3]

    def test_default_timestamp_is_stamped(self, mem_fs: dict[str, str]) -> None:
        # With now=None the current UTC time is stamped; assert the field is a
        # non-empty string rather than a fixed value.
        record_owned_backlog_snapshot(_OWNED_DIR, 7, 4)
        rows = _read_ledger_rows(mem_fs)
        assert len(rows) == 1
        assert isinstance(rows[0]["timestamp"], str)
        assert rows[0]["timestamp"] != ""


class TestMeasureOwnedNetDelta:
    """measure_owned_net_delta returns the signed latest-minus-previous delta.

    Substrate-agnostic: it takes the two most recent snapshots newest-first, so
    these cases construct BacklogSnapshot lists directly (the JSONL/SQLite read
    that produces them is covered by the adapter tests below).
    """

    def test_none_when_no_snapshots(self) -> None:
        assert measure_owned_net_delta([]) is None

    def test_none_when_single_snapshot(self) -> None:
        assert measure_owned_net_delta([_snap(1, 5)]) is None

    def test_positive_delta_when_backlog_grows(self) -> None:
        # newest-first: latest (iter 2) count 40 minus previous (iter 1) 37.
        assert measure_owned_net_delta([_snap(2, 40), _snap(1, 37)]) == 3

    def test_negative_delta_when_backlog_shrinks(self) -> None:
        assert measure_owned_net_delta([_snap(2, 38), _snap(1, 40)]) == -2

    def test_uses_only_the_two_passed(self) -> None:
        # recent(2) hands exactly the two newest; the delta ignores any tail.
        assert measure_owned_net_delta([_snap(3, 8), _snap(2, 5)]) == 3


class TestFormatOwnedNetDeltaSummary:
    """OWNED-NET-DELTA line appears only when >=2 snapshots exist."""

    def test_empty_when_no_snapshots(self) -> None:
        assert format_owned_net_delta_summary([]) == ""

    def test_empty_when_single_snapshot(self) -> None:
        assert format_owned_net_delta_summary([_snap(1, 5)]) == ""

    def test_formatted_line_positive_net(self) -> None:
        assert format_owned_net_delta_summary([_snap(2, 40), _snap(1, 37)]) == (
            "OWNED-NET-DELTA: prev=37 now=40 net=+3"
        )

    def test_formatted_line_negative_net(self) -> None:
        assert format_owned_net_delta_summary([_snap(2, 34), _snap(1, 40)]) == (
            "OWNED-NET-DELTA: prev=40 now=34 net=-6"
        )

    def test_formatted_line_zero_net(self) -> None:
        assert format_owned_net_delta_summary([_snap(2, 12), _snap(1, 12)]) == (
            "OWNED-NET-DELTA: prev=12 now=12 net=+0"
        )


def _proposal(
    proposal_id: str = "prop-1",
    status: str = "rolling_out",
    title: str = "Split the big refactor",
) -> EvolutionProposal:
    return EvolutionProposal(
        proposal_id=proposal_id,
        title=title,
        goal="Land the refactor across ordered slices.",
        status=status,  # type: ignore[arg-type]
        created_at="2026-08-24T00:00:00Z",
    )


def _slice(
    plan_id: str, phase_index: int, status: str = "pending_review",
) -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=f"Slice {phase_index}",
        category="capability",
        rationale="reason",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status=status,  # type: ignore[arg-type]
        parent_plan_id="prop-1",
        phase_index=phase_index,
        created_at="2026-08-24T00:00:00Z",
    )


class TestFormatMultiPrRolloutSummary:
    """MULTI-PR-ROLLOUT line appears only for a ``rolling_out`` proposal."""

    def test_empty_when_no_proposals(self) -> None:
        assert format_multi_pr_rollout_summary([], {}) == ""

    def test_empty_when_no_rolling_out_proposal(self) -> None:
        # A ``slicing``/``complete``/``abandoned`` proposal is not mid-rollout.
        proposals = [
            _proposal("p1", status="slicing"),
            _proposal("p2", status="complete"),
        ]
        assert format_multi_pr_rollout_summary(proposals, {}) == ""

    def test_line_names_proposal_and_slice_progress(self) -> None:
        proposals = [_proposal("prop-1")]
        slices = {
            "prop-1": [
                _slice("s0", 0, status="merged"),
                _slice("s1", 1, status="approved"),
                _slice("s2", 2, status="sliced"),
            ],
        }
        line = format_multi_pr_rollout_summary(proposals, slices)
        assert line == (
            "MULTI-PR-ROLLOUT: prop-1 1/3 slices landed; "
            'next=phase1(approved) — "Split the big refactor"'
        )

    def test_all_slices_landed_reports_none_open(self) -> None:
        proposals = [_proposal("prop-1")]
        slices = {"prop-1": [_slice("s0", 0, status="merged")]}
        line = format_multi_pr_rollout_summary(proposals, slices)
        assert "1/1 slices landed" in line
        assert "next=none(all landed)" in line

    def test_missing_slice_mapping_is_zero_of_zero(self) -> None:
        # A rolling_out proposal with no slices in the mapping still renders.
        line = format_multi_pr_rollout_summary([_proposal("prop-1")], {})
        assert "0/0 slices landed" in line
        assert "next=none(all landed)" in line

    def test_first_rolling_out_proposal_is_chosen(self) -> None:
        proposals = [
            _proposal("p1", status="slicing"),
            _proposal("p2", status="rolling_out", title="Second"),
            _proposal("p3", status="rolling_out", title="Third"),
        ]
        line = format_multi_pr_rollout_summary(
            proposals, {"p2": [_slice("x", 0)]},
        )
        assert "p2" in line
        assert "p3" not in line

    def test_concurrent_rollouts_note_count(self) -> None:
        # Two rolling_out proposals: the line names the first but notes that a
        # second initiative is also mid-rollout, so it is not invisible.
        proposals = [
            _proposal("p1", status="rolling_out", title="First"),
            _proposal("p2", status="rolling_out", title="Second"),
        ]
        line = format_multi_pr_rollout_summary(
            proposals, {"p1": [_slice("s0", 0, status="merged")]},
        )
        assert "MULTI-PR-ROLLOUT: p1" in line
        assert "(1 of 2 rolling out)" in line

    def test_single_rollout_omits_concurrent_note(self) -> None:
        # Exactly one rolling_out proposal: no ``(1 of N rolling out)`` note.
        line = format_multi_pr_rollout_summary(
            [_proposal("prop-1")], {"prop-1": [_slice("s0", 0)]},
        )
        assert "rolling out)" not in line

    def test_line_is_bounded_to_max(self) -> None:
        proposals = [_proposal("prop-1", title="T" * 500)]
        line = format_multi_pr_rollout_summary(
            proposals, {"prop-1": [_slice("s0", 0)]},
        )
        assert len(line) <= 200
        assert line.endswith("…")

    def test_concurrent_note_survives_long_title_truncation(self) -> None:
        # The exact case the concurrent note is designed for: a long (unbounded)
        # title on the named proposal AND a second rolling_out proposal. The
        # ``(1 of N rolling out)`` note must survive right-truncation within the
        # 200-char bound — clipping the title, not the note — else the concurrent
        # initiative becomes silently invisible again.
        proposals = [
            _proposal("p1", status="rolling_out", title="T" * 500),
            _proposal("p2", status="rolling_out", title="Second"),
        ]
        line = format_multi_pr_rollout_summary(
            proposals, {"p1": [_slice("s0", 0, status="merged")]},
        )
        assert len(line) <= 200
        assert line.endswith("(1 of 2 rolling out)")
        assert line.startswith("MULTI-PR-ROLLOUT: p1")

    def test_progress_payload_survives_long_title_truncation(self) -> None:
        # The unbounded title sits *last*, so right-truncation clips the title,
        # never the ``{landed}/{total} slices landed; next=...`` progress payload
        # the sensor exists to surface. A concurrent second rollout is present so
        # the reserved ``(1 of N rolling out)`` note is exercised alongside it —
        # both the primary progress metrics and the concurrent note must survive.
        proposals = [
            _proposal("p1", status="rolling_out", title="T" * 500),
            _proposal("p2", status="rolling_out", title="Second"),
        ]
        line = format_multi_pr_rollout_summary(
            proposals,
            {"p1": [_slice("s0", 0, status="merged"), _slice("s1", 1)]},
        )
        assert len(line) <= 200
        assert "1/2 slices landed" in line
        assert "next=phase1(pending_review)" in line
        assert line.endswith("(1 of 2 rolling out)")


class TestJsonlOwnedBacklogLedger:
    """The default JSONL adapter delegates to the existing write/read core."""

    def test_record_appends_row_and_returns_snapshot(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = JsonlOwnedBacklogLedger(_OWNED_DIR)
        snap = ledger.record(5, 3, now="2026-01-01T00:00:00")
        assert snap == BacklogSnapshot(
            iteration=5, count=3, timestamp="2026-01-01T00:00:00",
        )
        rows = _read_ledger_rows(mem_fs)
        assert rows == [
            {"iteration": 5, "count": 3, "timestamp": "2026-01-01T00:00:00"},
        ]

    def test_record_default_timestamp_is_stamped(
        self, mem_fs: dict[str, str],
    ) -> None:
        # With now=None the returned snapshot and the written row share the
        # single stamped timestamp (no drift between the two).
        snap = JsonlOwnedBacklogLedger(_OWNED_DIR).record(7, 4)
        assert snap.timestamp != ""
        rows = _read_ledger_rows(mem_fs)
        assert rows[0]["timestamp"] == snap.timestamp

    def test_recent_is_newest_first(self, mem_fs: dict[str, str]) -> None:
        ledger = JsonlOwnedBacklogLedger(_OWNED_DIR)
        ledger.record(1, 10, now="t1")
        ledger.record(2, 12, now="t2")
        ledger.record(3, 9, now="t3")
        recent = ledger.recent(2)
        assert recent == [
            BacklogSnapshot(iteration=3, count=9, timestamp="t3"),
            BacklogSnapshot(iteration=2, count=12, timestamp="t2"),
        ]

    def test_recent_non_positive_returns_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = JsonlOwnedBacklogLedger(_OWNED_DIR)
        ledger.record(1, 10, now="t1")
        assert ledger.recent(0) == []
        assert ledger.recent(-1) == []

    def test_recent_skips_corrupt_blank_and_non_mapping_rows(
        self, mem_fs: dict[str, str],
    ) -> None:
        good1 = json.dumps({"iteration": 1, "count": 6, "timestamp": "t1"})
        good2 = json.dumps({"iteration": 2, "count": 9, "timestamp": "t2"})
        mem_fs[_OWNED_PATH] = (
            f"{good1}\n\nnot json\n   \n{json.dumps([1, 2])}\n{good2}\n"
        )
        recent = JsonlOwnedBacklogLedger(_OWNED_DIR).recent(2)
        assert recent == [
            BacklogSnapshot(iteration=2, count=9, timestamp="t2"),
            BacklogSnapshot(iteration=1, count=6, timestamp="t1"),
        ]
        # Delivered to the delta the loop actually computes.
        assert format_owned_net_delta_summary(recent) == (
            "OWNED-NET-DELTA: prev=6 now=9 net=+3"
        )

    def test_recent_coerces_missing_or_ill_typed_fields_to_defaults(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A missing count / non-int count reads as 0, a missing/non-str
        # iteration or timestamp coerces to 0 / "" — degrade, never crash.
        no_count = json.dumps({"iteration": 1, "timestamp": "t1"})
        bad = json.dumps(
            {"iteration": "oops", "count": "nope", "timestamp": 5},
        )
        mem_fs[_OWNED_PATH] = f"{no_count}\n{bad}\n"
        recent = JsonlOwnedBacklogLedger(_OWNED_DIR).recent(2)
        assert recent == [
            BacklogSnapshot(iteration=0, count=0, timestamp=""),
            BacklogSnapshot(iteration=1, count=0, timestamp="t1"),
        ]


class TestOwnedBacklogSubstrateParity:
    """The JSONL and SQLite adapters are read/format-identical (proposal 106)."""

    def test_jsonl_and_sqlite_agree_on_recent_and_delta(
        self, mem_fs: dict[str, str],
    ) -> None:
        sequence = [(1, 40), (2, 37), (3, 41)]
        jsonl = JsonlOwnedBacklogLedger(_OWNED_DIR)
        sqlite = SqliteOwnedBacklogLedger(StateStore(":memory:"))
        for i, (iteration, count) in enumerate(sequence):
            ts = f"t{i}"
            jsonl.record(iteration, count, now=ts)
            sqlite.record(iteration, count, now=ts)
        assert jsonl.recent(2) == sqlite.recent(2)
        assert format_owned_net_delta_summary(
            jsonl.recent(2),
        ) == format_owned_net_delta_summary(sqlite.recent(2))
        assert format_owned_net_delta_summary(sqlite.recent(2)) == (
            "OWNED-NET-DELTA: prev=37 now=41 net=+4"
        )

    def test_load_all_backlog_snapshots_is_oldest_first(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The SQLite seed source must be oldest-first so the store's
        # AUTOINCREMENT id reproduces the JSONL append order.
        ledger = JsonlOwnedBacklogLedger(_OWNED_DIR)
        ledger.record(1, 10, now="t1")
        ledger.record(2, 20, now="t2")
        assert load_all_backlog_snapshots(_OWNED_DIR) == [
            BacklogSnapshot(iteration=1, count=10, timestamp="t1"),
            BacklogSnapshot(iteration=2, count=20, timestamp="t2"),
        ]


class TestRecordOwnedBacklogOnce:
    """Once-per-iteration snapshot guard for resume-in-place retries (#4106)."""

    def test_first_record_for_iteration_appends(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = JsonlOwnedBacklogLedger(_OWNED_DIR)
        assert record_owned_backlog_once(ledger, 5, 3) is True
        assert [s.iteration for s in ledger.recent(5)] == [5]

    def test_same_iteration_is_noop(self, mem_fs: dict[str, str]) -> None:
        ledger = JsonlOwnedBacklogLedger(_OWNED_DIR)
        record_owned_backlog_once(ledger, 5, 3)
        assert record_owned_backlog_once(ledger, 5, 9) is False
        snaps = ledger.recent(5)
        assert len(snaps) == 1
        assert snaps[0].count == 3

    def test_new_iteration_counts(self, mem_fs: dict[str, str]) -> None:
        ledger = JsonlOwnedBacklogLedger(_OWNED_DIR)
        record_owned_backlog_once(ledger, 5, 3)
        assert record_owned_backlog_once(ledger, 6, 2) is True
        assert [s.iteration for s in ledger.recent(5)] == [6, 5]


def test_record_owned_backlog_once_iteration_zero_always_records() -> None:
    """Iteration 0 (non-loop CLI) is never deduped (#4106)."""
    ledger = MagicMock()
    ledger.recent.return_value = [MagicMock(iteration=0)]
    assert record_owned_backlog_once(ledger, 0, 4) is True
    ledger.recent.assert_not_called()
    ledger.record.assert_called_once_with(0, 4)
