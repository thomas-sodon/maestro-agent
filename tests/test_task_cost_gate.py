"""Tests for maestro/task_cost_gate.py — task cost risk analysis.

Pure-logic gate: analyses recent TaskRecord history for patterns that indicate
high cost risk (expensive failures, escalating durations, wasteful retries)
and returns an optional risk-verdict string.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from maestro.memory import TaskRecord
from maestro.task_cost_gate import (
    COST_RISK_DECAY_HORIZON,
    EXPENSIVE_FAILURE_DURATION_FLOOR,
    MIN_TASKS_FOR_COST_CHECK,
    _consecutive_failure_streak,
    _escalating_failure_durations,
    _failure_cost_ratio,
    _parse_task_timestamp,
    _repeated_expensive_failures,
    check_task_cost_risk,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    outcome: str = "success",
    duration: float = 60.0,
    errors: list[str] | None = None,
    replans: int = 0,
    task_id: str = "t",
    timestamp: str = "2026-01-01T00:00:00",
) -> TaskRecord:
    """Build a minimal TaskRecord stub."""
    return TaskRecord(
        task_id=task_id,
        goal="test goal",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="",
        errors=errors or [],
        duration_secs=duration,
        timestamp=timestamp,
        replans=replans,
    )


# ---------------------------------------------------------------------------
# check_task_cost_risk — main entry point
# ---------------------------------------------------------------------------


class TestCheckTaskCostRiskReturnType:
    """check_task_cost_risk returns None when safe, a string when risky."""

    def test_returns_none_for_empty_list(self) -> None:
        assert check_task_cost_risk([]) is None

    def test_returns_none_for_all_successes(self) -> None:
        tasks = [_task("success", duration=100.0)] * 5
        assert check_task_cost_risk(tasks) is None

    def test_returns_string_when_risk_detected(self) -> None:
        # 3 consecutive expensive failures should trigger
        tasks = [
            _task("failure", duration=300.0, task_id="t1"),
            _task("failure", duration=300.0, task_id="t2"),
            _task("failure", duration=300.0, task_id="t3"),
        ]
        result = check_task_cost_risk(tasks)
        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_none_below_minimum_task_count(self) -> None:
        """Below MIN_TASKS_FOR_COST_CHECK, never flags risk."""
        tasks = [_task("failure", duration=999.0)] * (MIN_TASKS_FOR_COST_CHECK - 1)
        assert check_task_cost_risk(tasks) is None


# ---------------------------------------------------------------------------
# Rule 1: Consecutive expensive failures
# ---------------------------------------------------------------------------


class TestConsecutiveExpensiveFailures:
    """Detect consecutive failures at the tail of the task list."""

    def test_no_failures_returns_zero(self) -> None:
        tasks = [_task("success")] * 5
        assert _consecutive_failure_streak(tasks) == 0

    def test_single_failure_at_end(self) -> None:
        tasks = [_task("success"), _task("failure")]
        assert _consecutive_failure_streak(tasks) == 1

    def test_three_consecutive_failures(self) -> None:
        tasks = [_task("success")] * 3 + [_task("failure")] * 3
        assert _consecutive_failure_streak(tasks) == 3

    def test_broken_by_success(self) -> None:
        tasks = [_task("failure"), _task("success"), _task("failure")]
        assert _consecutive_failure_streak(tasks) == 1

    def test_partial_counts_as_non_failure(self) -> None:
        """'partial' outcome breaks a failure streak."""
        tasks = [_task("failure"), _task("partial"), _task("failure")]
        assert _consecutive_failure_streak(tasks) == 1

    def test_all_failures(self) -> None:
        tasks = [_task("failure")] * 5
        assert _consecutive_failure_streak(tasks) == 5

    def test_empty_list(self) -> None:
        assert _consecutive_failure_streak([]) == 0

    def test_risk_triggered_by_consecutive_expensive_failures(self) -> None:
        """3+ consecutive failures with avg duration above floor trigger risk."""
        tasks = [
            _task("success", duration=60.0),
            _task("failure", duration=EXPENSIVE_FAILURE_DURATION_FLOOR + 10),
            _task("failure", duration=EXPENSIVE_FAILURE_DURATION_FLOOR + 20),
            _task("failure", duration=EXPENSIVE_FAILURE_DURATION_FLOOR + 30),
        ]
        result = check_task_cost_risk(tasks)
        assert result is not None
        assert "consecutive" in result.lower()

    def test_cheap_consecutive_failures_no_risk(self) -> None:
        """3 consecutive failures that are cheap don't trigger this rule."""
        tasks = [
            _task("success", duration=60.0),
            _task("failure", duration=5.0),
            _task("failure", duration=5.0),
            _task("failure", duration=5.0),
        ]
        # Might still not trigger if durations are below floor
        result = _repeated_expensive_failures(tasks)
        assert result is None


# ---------------------------------------------------------------------------
# Rule 2: Escalating failure durations
# ---------------------------------------------------------------------------


class TestEscalatingFailureDurations:
    """Detect monotonically increasing durations among recent failures."""

    def test_no_failures_returns_none(self) -> None:
        tasks = [_task("success", duration=100.0)] * 5
        assert _escalating_failure_durations(tasks) is None

    def test_single_failure_returns_none(self) -> None:
        tasks = [_task("success"), _task("failure", duration=100.0)]
        assert _escalating_failure_durations(tasks) is None

    def test_two_failures_returns_none(self) -> None:
        """Need at least ESCALATION_MIN_FAILURES for escalation detection."""
        tasks = [
            _task("failure", duration=100.0),
            _task("failure", duration=200.0),
        ]
        assert _escalating_failure_durations(tasks) is None

    def test_escalating_three_failures(self) -> None:
        """Three failures with strictly increasing durations trigger."""
        tasks = [
            _task("failure", duration=100.0),
            _task("failure", duration=200.0),
            _task("failure", duration=300.0),
        ]
        result = _escalating_failure_durations(tasks)
        assert result is not None
        assert "escalat" in result.lower()

    def test_flat_failures_no_escalation(self) -> None:
        """Equal durations are not escalating."""
        tasks = [
            _task("failure", duration=100.0),
            _task("failure", duration=100.0),
            _task("failure", duration=100.0),
        ]
        assert _escalating_failure_durations(tasks) is None

    def test_decreasing_failures_no_escalation(self) -> None:
        tasks = [
            _task("failure", duration=300.0),
            _task("failure", duration=200.0),
            _task("failure", duration=100.0),
        ]
        assert _escalating_failure_durations(tasks) is None

    def test_interleaved_success_failures_escalating(self) -> None:
        """Successes between failures don't break escalation detection.

        We look at the failure subsequence only.
        """
        tasks = [
            _task("failure", duration=100.0),
            _task("success", duration=50.0),
            _task("failure", duration=200.0),
            _task("success", duration=50.0),
            _task("failure", duration=300.0),
        ]
        result = _escalating_failure_durations(tasks)
        assert result is not None

    def test_non_monotonic_no_escalation(self) -> None:
        """If any adjacent pair doesn't increase, no escalation."""
        tasks = [
            _task("failure", duration=100.0),
            _task("failure", duration=300.0),
            _task("failure", duration=200.0),  # drop
        ]
        assert _escalating_failure_durations(tasks) is None

    def test_risk_message_includes_durations(self) -> None:
        tasks = [
            _task("failure", duration=60.0),
            _task("failure", duration=120.0),
            _task("failure", duration=240.0),
        ]
        result = _escalating_failure_durations(tasks)
        assert result is not None
        assert "60" in result
        assert "240" in result


# ---------------------------------------------------------------------------
# Rule 3: High failure cost ratio
# ---------------------------------------------------------------------------


class TestFailureCostRatio:
    """Detect when majority of recent task duration is spent on non-success outcomes."""

    def test_all_success_returns_none(self) -> None:
        tasks = [_task("success", duration=100.0)] * 5
        assert _failure_cost_ratio(tasks) is None

    def test_all_failure_returns_risk(self) -> None:
        tasks = [_task("failure", duration=100.0)] * 4
        result = _failure_cost_ratio(tasks)
        assert result is not None
        assert "100%" in result

    def test_below_threshold_returns_none(self) -> None:
        """When failure cost is below FAILURE_COST_RATIO_THRESHOLD, no risk."""
        # 1 failure (100s) + 4 success (100s each) = 20% failure ratio
        tasks = [_task("failure", duration=100.0)] + [
            _task("success", duration=100.0)
        ] * 4
        # 20% < threshold (70%), so no risk
        assert _failure_cost_ratio(tasks) is None

    def test_above_threshold_returns_risk(self) -> None:
        """When failure cost exceeds threshold, risk is flagged."""
        # 4 failures (100s each) + 1 success (10s) = 400/410 ≈ 97.6% failure ratio
        tasks = [_task("failure", duration=100.0)] * 4 + [
            _task("success", duration=10.0)
        ]
        result = _failure_cost_ratio(tasks)
        assert result is not None

    def test_empty_list_returns_none(self) -> None:
        assert _failure_cost_ratio([]) is None

    def test_zero_duration_tasks_returns_none(self) -> None:
        """All zero-duration tasks: no division by zero."""
        tasks = [_task("failure", duration=0.0)] * 5
        assert _failure_cost_ratio(tasks) is None

    def test_partial_counted_in_cost_ratio(self) -> None:
        """'partial' outcomes count as non-success in cost ratio (rule 3)."""
        tasks = [
            _task("partial", duration=100.0),
            _task("partial", duration=100.0),
            _task("partial", duration=100.0),
            _task("success", duration=10.0),
        ]
        result = _failure_cost_ratio(tasks)
        assert result is not None
        assert "non-success" in result

    def test_mixed_partial_and_failure_cost_ratio(self) -> None:
        """Mixed partial + failure outcomes both contribute to cost ratio."""
        tasks = [
            _task("failure", duration=100.0),
            _task("partial", duration=100.0),
            _task("failure", duration=100.0),
            _task("success", duration=10.0),
        ]
        result = _failure_cost_ratio(tasks)
        assert result is not None
        assert "%" in result

    def test_ratio_message_includes_percentage(self) -> None:
        tasks = [_task("failure", duration=100.0)] * 5
        result = _failure_cost_ratio(tasks)
        assert result is not None
        assert "%" in result


# ---------------------------------------------------------------------------
# Integration: check_task_cost_risk selects first triggered rule
# ---------------------------------------------------------------------------


class TestCheckTaskCostRiskIntegration:
    """Integration tests for the main entry point."""

    def test_healthy_history(self) -> None:
        tasks = [
            _task("success", duration=60.0, task_id=f"t{i}")
            for i in range(10)
        ]
        assert check_task_cost_risk(tasks) is None

    def test_single_failure_no_risk(self) -> None:
        tasks = [
            _task("success", duration=60.0),
            _task("success", duration=60.0),
            _task("failure", duration=60.0),
        ]
        assert check_task_cost_risk(tasks) is None

    def test_mixed_moderate_no_risk(self) -> None:
        """Some failures, but not enough pattern to trigger."""
        tasks = [
            _task("success", duration=60.0),
            _task("failure", duration=30.0),
            _task("success", duration=60.0),
            _task("failure", duration=30.0),
            _task("success", duration=60.0),
        ]
        assert check_task_cost_risk(tasks) is None

    def test_multiple_rules_returns_first(self) -> None:
        """When multiple rules fire, the function returns the first verdict."""
        tasks = [
            _task("failure", duration=100.0, task_id="t1"),
            _task("failure", duration=200.0, task_id="t2"),
            _task("failure", duration=300.0, task_id="t3"),
        ]
        result = check_task_cost_risk(tasks)
        assert result is not None
        # Should be a meaningful string
        assert len(result) > 10

    def test_escalation_fires_via_main_entry_when_rule1_silent(self) -> None:
        """Rule 2 (escalation) fires through check_task_cost_risk when
        rule 1 does not (no consecutive tail streak)."""
        tasks = [
            _task("failure", duration=50.0),
            _task("success", duration=50.0),
            _task("failure", duration=100.0),
            _task("success", duration=50.0),
            _task("failure", duration=200.0),
            _task("success", duration=50.0),  # breaks tail streak
        ]
        result = check_task_cost_risk(tasks)
        assert result is not None
        assert "escalat" in result.lower()

    def test_cost_ratio_fires_via_main_entry_when_rules_1_2_silent(self) -> None:
        """Rule 3 (cost ratio) fires through check_task_cost_risk when
        rules 1 and 2 do not."""
        # Failures with equal duration (no escalation), interleaved with
        # a cheap success (no consecutive tail streak), but high ratio.
        tasks = [
            _task("failure", duration=200.0),
            _task("failure", duration=200.0),
            _task("failure", duration=200.0),
            _task("success", duration=10.0),  # breaks tail streak
        ]
        result = check_task_cost_risk(tasks)
        assert result is not None
        assert "%" in result

    def test_deterministic(self) -> None:
        """Same input always produces same output."""
        tasks = [
            _task("failure", duration=200.0, task_id="t1"),
            _task("failure", duration=300.0, task_id="t2"),
            _task("failure", duration=400.0, task_id="t3"),
        ]
        result1 = check_task_cost_risk(tasks)
        result2 = check_task_cost_risk(tasks)
        assert result1 == result2

    def test_partials_do_not_trigger_streak_or_escalation_rules(self) -> None:
        """'partial' outcomes do not trigger rules 1 (streak) or 2 (escalation)."""
        tasks = [
            _task("partial", duration=300.0),
            _task("partial", duration=300.0),
            _task("partial", duration=300.0),
        ]
        # Rule 1: partials don't count as failures in streak
        assert _consecutive_failure_streak(tasks) == 0
        assert _repeated_expensive_failures(tasks) is None
        # Rule 2: partials don't count as failures for escalation
        assert _escalating_failure_durations(tasks) is None

    def test_partials_trigger_cost_ratio_via_main_entry(self) -> None:
        """Rule 3 fires on partial-dominated windows via check_task_cost_risk."""
        tasks = [
            _task("partial", duration=300.0),
            _task("partial", duration=300.0),
            _task("partial", duration=300.0),
            _task("success", duration=10.0),
        ]
        result = check_task_cost_risk(tasks)
        assert result is not None
        assert "%" in result

    def test_expensive_replanned_failures_flagged(self) -> None:
        """Failures with replans suggest the system retried and still failed."""
        tasks = [
            _task("success", duration=60.0),
            _task("failure", duration=EXPENSIVE_FAILURE_DURATION_FLOOR + 50,
                  replans=2, task_id="t1"),
            _task("failure", duration=EXPENSIVE_FAILURE_DURATION_FLOOR + 100,
                  replans=1, task_id="t2"),
            _task("failure", duration=EXPENSIVE_FAILURE_DURATION_FLOOR + 150,
                  replans=2, task_id="t3"),
        ]
        result = check_task_cost_risk(tasks)
        assert result is not None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_exactly_at_threshold_count(self) -> None:
        """Exactly MIN_TASKS_FOR_COST_CHECK tasks should be analyzed."""
        tasks = [
            _task("failure", duration=300.0, task_id=f"t{i}")
            for i in range(MIN_TASKS_FOR_COST_CHECK)
        ]
        # At minimum count, rules should be able to fire
        result = check_task_cost_risk(tasks)
        assert result is not None

    def test_very_long_task_list(self) -> None:
        """Large input doesn't crash or slow down."""
        tasks = [
            _task("success", duration=60.0, task_id=f"t{i}")
            for i in range(200)
        ]
        assert check_task_cost_risk(tasks) is None

    def test_zero_duration_everywhere(self) -> None:
        """All zero-duration tasks: no division errors."""
        tasks = [_task("failure", duration=0.0)] * 5
        # Should not crash — should return None because zero-cost failures
        # don't represent real cost risk
        result = check_task_cost_risk(tasks)
        # Not None would also be acceptable if the streak rule fires,
        # but must not crash
        assert result is None or isinstance(result, str)

    def test_negative_duration_treated_as_zero(self) -> None:
        """Defensive: negative durations shouldn't crash."""
        tasks = [_task("failure", duration=-10.0)] * 5
        result = check_task_cost_risk(tasks)
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# Recency horizon (#2566) — the optional time-bounded evaluation
# ---------------------------------------------------------------------------


class TestRecencyHorizon:
    """A ``reference_time`` drops records aged past COST_RISK_DECAY_HORIZON."""

    def test_stale_window_past_horizon_does_not_block(self) -> None:
        """An aged non-success window releases under a fresh clock (livelock fix)."""
        ref = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        stale_ts = (ref - COST_RISK_DECAY_HORIZON - timedelta(hours=1)).isoformat()
        tasks = [
            _task("failure", duration=300.0, task_id=f"t{i}", timestamp=stale_ts)
            for i in range(3)
        ]
        # Without a clock the frozen window still blocks (3 expensive failures).
        assert check_task_cost_risk(tasks) is not None
        # With a fresh reference_time every record ages out → gate releases.
        assert check_task_cost_risk(tasks, reference_time=ref) is None

    def test_fresh_expensive_failure_window_still_blocks(self) -> None:
        """The same shape, timestamped within the horizon, still trips the gate."""
        ref = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        fresh_ts = (ref - timedelta(hours=1)).isoformat()
        tasks = [
            _task("failure", duration=300.0, task_id=f"t{i}", timestamp=fresh_ts)
            for i in range(3)
        ]
        result = check_task_cost_risk(tasks, reference_time=ref)
        assert result is not None

    def test_no_reference_time_preserves_legacy_behavior(self) -> None:
        """No clock (or explicit None) evaluates the full window with no filter."""
        tasks = [
            _task(
                "failure", duration=300.0, task_id=f"t{i}",
                timestamp="2000-01-01T00:00:00",
            )
            for i in range(3)
        ]
        with_default = check_task_cost_risk(tasks)
        explicit_none = check_task_cost_risk(tasks, reference_time=None)
        assert with_default is not None
        assert explicit_none == with_default

    def test_partial_filter_drops_below_minimum_releases(self) -> None:
        """When filtering drops the window below the minimum, the gate releases."""
        ref = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        stale_ts = (ref - COST_RISK_DECAY_HORIZON - timedelta(hours=1)).isoformat()
        fresh_ts = (ref - timedelta(hours=1)).isoformat()
        tasks = [
            _task("failure", duration=300.0, task_id="t0", timestamp=stale_ts),
            _task("failure", duration=300.0, task_id="t1", timestamp=stale_ts),
            _task("failure", duration=300.0, task_id="t2", timestamp=fresh_ts),
        ]
        # Only one record survives the horizon → below MIN → None.
        assert check_task_cost_risk(tasks, reference_time=ref) is None

    def test_naive_reference_time_coerced_to_utc(self) -> None:
        """A naive reference_time is treated as UTC — no tz-mismatch crash."""
        ref_naive = datetime(2026, 6, 1, 12, 0)
        fresh_ts = "2026-06-01T11:00:00"  # naive, within horizon of the naive clock
        tasks = [
            _task("failure", duration=300.0, task_id=f"t{i}", timestamp=fresh_ts)
            for i in range(3)
        ]
        result = check_task_cost_risk(tasks, reference_time=ref_naive)
        assert result is not None

    def test_undatable_records_dropped_under_horizon(self) -> None:
        """Records with empty/unparseable timestamps are dropped, not kept."""
        ref = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        fresh_ts = (ref - timedelta(hours=1)).isoformat()
        tasks = [
            _task("failure", duration=300.0, task_id="t0", timestamp="not-a-date"),
            _task("failure", duration=300.0, task_id="t1", timestamp=""),
            _task("failure", duration=300.0, task_id="t2", timestamp=fresh_ts),
        ]
        # Two undatable records dropped → one survivor < MIN → None.
        assert check_task_cost_risk(tasks, reference_time=ref) is None


class TestParseTaskTimestamp:
    """The local ISO parser coerces naive to UTC and tolerates damage."""

    def test_empty_returns_none(self) -> None:
        assert _parse_task_timestamp("") is None

    def test_unparseable_returns_none(self) -> None:
        assert _parse_task_timestamp("not-a-date") is None

    def test_naive_coerced_to_utc(self) -> None:
        parsed = _parse_task_timestamp("2026-01-01T00:00:00")
        assert parsed == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    def test_aware_z_suffix_preserved(self) -> None:
        parsed = _parse_task_timestamp("2026-01-01T00:00:00Z")
        assert parsed == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
