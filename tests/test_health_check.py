"""Tests for maestro/health_check.py — HealthSummary, summarize_health(), and public helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from maestro.diagnostics import DiagnosticSnapshot
from maestro.health_check import (
    HealthSummary,
    failure_streak,
    outcome_trend,
    success_rate,
    summarize_health,
    throttle_residency,
)
from maestro.host_capacity import HostCapacityReading
from maestro.memory import TaskRecord

# ---------------------------------------------------------------------------
# Helpers (matching test_stats.py patterns)
# ---------------------------------------------------------------------------


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
    recent_iteration_costs: list[float] | None = None,
    host_capacity: HostCapacityReading | None = None,
) -> DiagnosticSnapshot:
    return DiagnosticSnapshot(
        recent_error_count=error_count,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="test",
        cost_trend=cost_trend,  # type: ignore[arg-type]
        recent_iteration_costs=recent_iteration_costs or [],
        host_capacity=host_capacity,
    )


# ---------------------------------------------------------------------------
# HealthSummary dataclass fields
# ---------------------------------------------------------------------------


class TestHealthSummaryFields:
    """Verify HealthSummary dataclass has all expected fields."""

    def test_has_success_rate(self) -> None:
        hs = HealthSummary(
            success_rate=0.8,
            failure_streak=0,
            outcome_trend="stable",
            error_count=0,
            cost_trend=None,
            is_noteworthy=False,
        )
        assert hs.success_rate == 0.8

    def test_has_failure_streak(self) -> None:
        hs = HealthSummary(
            success_rate=1.0,
            failure_streak=2,
            outcome_trend="stable",
            error_count=0,
            cost_trend=None,
            is_noteworthy=False,
        )
        assert hs.failure_streak == 2

    def test_has_outcome_trend(self) -> None:
        hs = HealthSummary(
            success_rate=1.0,
            failure_streak=0,
            outcome_trend="improving",
            error_count=0,
            cost_trend=None,
            is_noteworthy=False,
        )
        assert hs.outcome_trend == "improving"

    def test_has_error_count(self) -> None:
        hs = HealthSummary(
            success_rate=1.0,
            failure_streak=0,
            outcome_trend="stable",
            error_count=7,
            cost_trend=None,
            is_noteworthy=False,
        )
        assert hs.error_count == 7

    def test_has_cost_trend(self) -> None:
        hs = HealthSummary(
            success_rate=1.0,
            failure_streak=0,
            outcome_trend="stable",
            error_count=0,
            cost_trend="spiking",
            is_noteworthy=True,
        )
        assert hs.cost_trend == "spiking"

    def test_has_is_noteworthy(self) -> None:
        hs = HealthSummary(
            success_rate=0.5,
            failure_streak=3,
            outcome_trend="declining",
            error_count=10,
            cost_trend="spiking",
            is_noteworthy=True,
        )
        assert hs.is_noteworthy is True


# ---------------------------------------------------------------------------
# summarize_health happy path
# ---------------------------------------------------------------------------


class TestSummarizeHealthHappyPath:
    """summarize_health returns correct HealthSummary for normal inputs."""

    def test_all_success_tasks(self) -> None:
        tasks = [_task("success")] * 5
        result = summarize_health(tasks, _snapshot())
        assert result.success_rate == 1.0
        assert result.failure_streak == 0
        assert result.outcome_trend == "stable"
        assert result.error_count == 0
        assert result.cost_trend is None
        assert result.is_noteworthy is False

    def test_mixed_outcomes(self) -> None:
        # 3 fail + 3 success = 50% rate, improving trend, streak = 0
        tasks = [_task("failure")] * 3 + [_task("success")] * 3
        result = summarize_health(tasks, _snapshot(error_count=7))
        assert result.success_rate == pytest.approx(0.5)
        assert result.failure_streak == 0
        assert result.outcome_trend == "improving"
        assert result.error_count == 7
        assert result.is_noteworthy is True

    def test_cost_trend_propagated(self) -> None:
        tasks = [_task("success")] * 5
        result = summarize_health(tasks, _snapshot(cost_trend="flat"))
        assert result.cost_trend == "flat"
        assert result.is_noteworthy is False

    def test_returns_health_summary_type(self) -> None:
        result = summarize_health([_task()], _snapshot())
        assert isinstance(result, HealthSummary)


# ---------------------------------------------------------------------------
# Boundary cases
# ---------------------------------------------------------------------------


class TestSummarizeHealthBoundary:
    """Boundary conditions that sit exactly at noteworthy thresholds."""

    def test_exactly_three_tasks_at_seventy_percent_not_noteworthy(self) -> None:
        """3 tasks, 2 success + 1 failure = 66.7% < 70% => noteworthy."""
        tasks = [_task("success"), _task("success"), _task("failure")]
        result = summarize_health(tasks, _snapshot())
        # 2/3 = 66.7% which is < 70%, so noteworthy
        assert result.success_rate == pytest.approx(2 / 3)
        assert result.is_noteworthy is True

    def test_exactly_three_tasks_above_seventy_not_noteworthy(self) -> None:
        """3 tasks, all success = 100% >= 70% => not noteworthy (from rate alone)."""
        tasks = [_task("success")] * 3
        result = summarize_health(tasks, _snapshot())
        assert result.success_rate == 1.0
        assert result.is_noteworthy is False

    def test_streak_of_exactly_three_noteworthy(self) -> None:
        """Failure streak of exactly 3 triggers noteworthy."""
        # 7 success + 3 failure: rate=70%, streak=3
        tasks = [_task("success")] * 7 + [_task("failure")] * 3
        result = summarize_health(tasks, _snapshot())
        assert result.failure_streak == 3
        assert result.is_noteworthy is True

    def test_streak_of_two_not_noteworthy_alone(self) -> None:
        """Failure streak of 2 does not trigger noteworthy by itself."""
        # 8 success + 2 failure: rate=80%, streak=2
        tasks = [_task("success")] * 8 + [_task("failure")] * 2
        result = summarize_health(tasks, _snapshot())
        assert result.failure_streak == 2
        assert result.is_noteworthy is False

    def test_error_count_exactly_five_noteworthy(self) -> None:
        """error_count of exactly 5 triggers noteworthy."""
        result = summarize_health([], _snapshot(error_count=5))
        assert result.error_count == 5
        assert result.is_noteworthy is True

    def test_error_count_four_not_noteworthy(self) -> None:
        """error_count of 4 does not trigger noteworthy."""
        result = summarize_health([], _snapshot(error_count=4))
        assert result.error_count == 4
        assert result.is_noteworthy is False

    def test_cost_trend_spiking_noteworthy(self) -> None:
        """cost_trend='spiking' triggers noteworthy regardless of other signals."""
        result = summarize_health([], _snapshot(cost_trend="spiking"))
        assert result.cost_trend == "spiking"
        assert result.is_noteworthy is True

    def test_cost_trend_rising_not_noteworthy(self) -> None:
        """cost_trend='rising' does not trigger noteworthy by itself."""
        tasks = [_task("success")] * 5
        result = summarize_health(tasks, _snapshot(cost_trend="rising"))
        assert result.cost_trend == "rising"
        assert result.is_noteworthy is False

    def test_cost_trend_rising_high_absolute_cost_noteworthy(self) -> None:
        """A sustained 'rising' trend at high absolute cost is still surfaced.

        Regression parity for #1185: reclassifying a sustained cost climb from
        'spiking' to 'rising' must not drop the health-summary alert.  Mirrors
        the ``diagnostics.is_noteworthy`` rising + recent-cost > $0.05 logic.
        """
        tasks = [_task("success")] * 5
        snapshot = _snapshot(
            cost_trend="rising",
            recent_iteration_costs=[0.05, 0.05, 0.50],
        )
        result = summarize_health(tasks, snapshot)
        assert result.cost_trend == "rising"
        assert result.is_noteworthy is True

    def test_cost_trend_rising_low_absolute_cost_not_noteworthy(self) -> None:
        """A 'rising' trend whose recent cost stays below $0.05 is not surfaced."""
        tasks = [_task("success")] * 5
        snapshot = _snapshot(
            cost_trend="rising",
            recent_iteration_costs=[0.01, 0.02, 0.03],
        )
        result = summarize_health(tasks, snapshot)
        assert result.cost_trend == "rising"
        assert result.is_noteworthy is False


# ---------------------------------------------------------------------------
# Empty task list
# ---------------------------------------------------------------------------


class TestSummarizeHealthEmptyTasks:
    """Edge case: no tasks at all."""

    def test_empty_tasks_success_rate_is_one(self) -> None:
        result = summarize_health([], _snapshot())
        assert result.success_rate == 1.0

    def test_empty_tasks_failure_streak_is_zero(self) -> None:
        result = summarize_health([], _snapshot())
        assert result.failure_streak == 0

    def test_empty_tasks_outcome_trend_stable(self) -> None:
        result = summarize_health([], _snapshot())
        assert result.outcome_trend == "stable"

    def test_empty_tasks_not_noteworthy_with_clean_snapshot(self) -> None:
        result = summarize_health([], _snapshot())
        assert result.is_noteworthy is False

    def test_empty_tasks_noteworthy_if_high_errors(self) -> None:
        result = summarize_health([], _snapshot(error_count=10))
        assert result.is_noteworthy is True


# ---------------------------------------------------------------------------
# Public API — canonical imports from maestro.health_check
# ---------------------------------------------------------------------------


class TestPublicAPI:
    """Validate that the three health helpers are importable from health_check
    with their public names and return correct values."""

    def test_success_rate_importable(self) -> None:
        """success_rate is directly importable from maestro.health_check."""
        assert callable(success_rate)

    def test_failure_streak_importable(self) -> None:
        """failure_streak is directly importable from maestro.health_check."""
        assert callable(failure_streak)

    def test_outcome_trend_importable(self) -> None:
        """outcome_trend is directly importable from maestro.health_check."""
        assert callable(outcome_trend)

    def test_success_rate_empty_returns_one(self) -> None:
        assert success_rate([]) == 1.0

    def test_success_rate_all_success(self) -> None:
        assert success_rate([_task("success")] * 5) == 1.0

    def test_success_rate_mixed(self) -> None:
        tasks = [_task("success"), _task("failure")]
        assert success_rate(tasks) == pytest.approx(0.5)

    def test_failure_streak_empty_returns_zero(self) -> None:
        assert failure_streak([]) == 0

    def test_failure_streak_trailing_failures(self) -> None:
        tasks = [_task("success"), _task("failure"), _task("failure")]
        assert failure_streak(tasks) == 2

    def test_failure_streak_success_breaks(self) -> None:
        tasks = [_task("failure"), _task("success"), _task("failure")]
        assert failure_streak(tasks) == 1

    def test_outcome_trend_too_few_stable(self) -> None:
        assert outcome_trend([_task()] * 3) == "stable"

    def test_outcome_trend_improving(self) -> None:
        tasks = [_task("failure")] * 3 + [_task("success")] * 3
        assert outcome_trend(tasks) == "improving"

    def test_outcome_trend_declining(self) -> None:
        tasks = [_task("success")] * 3 + [_task("failure")] * 3
        assert outcome_trend(tasks) == "declining"


# ---------------------------------------------------------------------------
# No-op rate health condition
# ---------------------------------------------------------------------------


def _task_with_summary(
    outcome: str = "success",
    result_summary: str = "",
) -> TaskRecord:
    """Build a TaskRecord with a specific result_summary."""
    return TaskRecord(
        task_id="t",
        goal="g",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary=result_summary,
        errors=[],
        duration_secs=1.0,
        timestamp="2026-01-01T00:00:00",
    )


class TestHealthSurfacesNoOpRate:
    """No-op task rate condition appears in HealthSummary.conditions."""

    def test_above_threshold(self) -> None:
        """4/10 no-op tasks (40%) exceeds 30% threshold → condition present."""
        no_op = _task_with_summary(result_summary="No changes needed.")
        meaningful = _task_with_summary(result_summary="Shipped the feature.")
        tasks = [meaningful] * 6 + [no_op] * 4
        result = summarize_health(tasks, _snapshot())
        assert len(result.conditions) == 1
        assert "No-op task rate" in result.conditions[0]
        assert "4/10" in result.conditions[0]

    def test_silent_below_threshold(self) -> None:
        """2/10 no-op tasks (20%) is below 30% threshold → no condition."""
        no_op = _task_with_summary(result_summary="No changes needed.")
        meaningful = _task_with_summary(result_summary="Shipped the feature.")
        tasks = [meaningful] * 8 + [no_op] * 2
        result = summarize_health(tasks, _snapshot())
        assert result.conditions == []

    def test_exactly_at_threshold_silent(self) -> None:
        """3/10 no-op tasks (30%) is at — not above — threshold → no condition."""
        no_op = _task_with_summary(result_summary="Already passing all gates.")
        meaningful = _task_with_summary(result_summary="Shipped the feature.")
        tasks = [meaningful] * 7 + [no_op] * 3
        result = summarize_health(tasks, _snapshot())
        assert result.conditions == []

    def test_empty_tasks_no_condition(self) -> None:
        """Empty task list produces no conditions."""
        result = summarize_health([], _snapshot())
        assert result.conditions == []

    def test_conditions_field_default_empty(self) -> None:
        """HealthSummary.conditions defaults to empty list."""
        hs = HealthSummary(
            success_rate=1.0,
            failure_streak=0,
            outcome_trend="stable",
            error_count=0,
            cost_trend=None,
            is_noteworthy=False,
        )
        assert hs.conditions == []


# ---------------------------------------------------------------------------
# throttle_residency() — fraction of the trailing window spent in throttle
# ---------------------------------------------------------------------------

# Fixed clock literals — no real time is read; residency is a pure function of
# the passed (window_start, now) and the transition milestones.
_NOW = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
_WINDOW_START = _NOW - timedelta(hours=24)  # 2026-01-01 00:00 UTC


def _ago(hours: float) -> str:
    """ISO timestamp ``hours`` before the fixed ``_NOW``."""
    return (_NOW - timedelta(hours=hours)).isoformat()


class TestThrottleResidency:
    """throttle_residency() pairs enter/exit milestones into throttled time."""

    def test_closed_interval(self) -> None:
        """(a) A fully-closed enter→exit interval within the window."""
        entries = [
            (_ago(12), "budget_throttle_enter"),
            (_ago(6), "budget_throttle_exit"),
        ]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == pytest.approx(6 / 24)

    def test_open_interval_still_throttled_at_now(self) -> None:
        """(b) A trailing enter with no exit is open through ``now``."""
        entries = [(_ago(12), "budget_throttle_enter")]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == pytest.approx(12 / 24)

    def test_exit_whose_enter_predates_window_absent(self) -> None:
        """(c) A lone exit (its enter fell outside the window) clamps to start."""
        entries = [(_ago(18), "budget_throttle_exit")]  # 06:00 → [00:00, 06:00]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == pytest.approx(6 / 24)

    def test_exit_whose_enter_predates_window_present(self) -> None:
        """(c) An enter before window_start clamps to the window start."""
        entries = [
            ((_WINDOW_START - timedelta(hours=3)).isoformat(), "budget_throttle_enter"),
            (_ago(18), "budget_throttle_exit"),  # 06:00 → clamped [00:00, 06:00]
        ]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == pytest.approx(6 / 24)

    def test_empty_entries(self) -> None:
        """(d) No entries → 0.0."""
        r = throttle_residency([], window_start=_WINDOW_START, now=_NOW)
        assert r == 0.0

    def test_no_throttle_tags(self) -> None:
        """(d) Only non-throttle tags → 0.0 (tags filtered out)."""
        entries = [
            (_ago(5), "some_other_tag"),
            (_ago(4), None),
        ]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == 0.0

    def test_open_interval_before_window_clamps_to_full_window(self) -> None:
        """An enter long before the window, still open, saturates to 1.0."""
        entries = [
            ((_WINDOW_START - timedelta(hours=100)).isoformat(), "budget_throttle_enter"),
        ]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == 1.0

    def test_zero_width_window(self) -> None:
        """A zero-width window returns 0.0 rather than dividing by zero."""
        entries = [(_ago(1), "budget_throttle_enter")]
        r = throttle_residency(entries, window_start=_NOW, now=_NOW)
        assert r == 0.0

    def test_unparseable_timestamp_skipped(self) -> None:
        """A milestone with an unparseable timestamp is skipped."""
        entries = [
            ("not-a-timestamp", "budget_throttle_enter"),
            (_ago(6), "budget_throttle_exit"),  # lone exit → [00:00, 18:00]
        ]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == pytest.approx(18 / 24)

    def test_multiple_intervals_summed(self) -> None:
        """Two closed intervals contribute additively."""
        entries = [
            (_ago(20), "budget_throttle_enter"),
            (_ago(16), "budget_throttle_exit"),  # 4h
            (_ago(10), "budget_throttle_enter"),
            (_ago(4), "budget_throttle_exit"),   # 6h
        ]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == pytest.approx(10 / 24)

    def test_out_of_order_entries_sorted(self) -> None:
        """Entries are sorted by timestamp before pairing."""
        entries = [
            (_ago(4), "budget_throttle_exit"),
            (_ago(10), "budget_throttle_enter"),
            (_ago(16), "budget_throttle_exit"),
            (_ago(20), "budget_throttle_enter"),
        ]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == pytest.approx(10 / 24)

    def test_missing_exit_between_two_enters_keeps_earliest_open(self) -> None:
        """Two enters then one exit spans from the earliest open point."""
        entries = [
            (_ago(20), "budget_throttle_enter"),  # 04:00
            (_ago(10), "budget_throttle_enter"),  # 14:00 (ignored, already open)
            (_ago(4), "budget_throttle_exit"),    # 20:00 → [04:00, 20:00] = 16h
        ]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == pytest.approx(16 / 24)

    def test_open_spell_since_before_window_saturates(self) -> None:
        """A spell that opened >24h ago and is still active saturates to 1.0.

        The ``enter`` milestone predates the window, so ``entries`` (in-window
        only) is empty — the regression the ``open_spell_since`` seed fixes:
        without it a >24h-continuous pin would under-report to 0.0.
        """
        open_since = (_WINDOW_START - timedelta(hours=6)).isoformat()
        r = throttle_residency(
            [], window_start=_WINDOW_START, now=_NOW, open_spell_since=open_since,
        )
        assert r == 1.0

    def test_open_spell_since_within_window(self) -> None:
        """A spell opened 12h ago and still active counts 12h — no milestone."""
        r = throttle_residency(
            [], window_start=_WINDOW_START, now=_NOW, open_spell_since=_ago(12),
        )
        assert r == pytest.approx(12 / 24)

    def test_open_spell_since_dedupes_with_milestone(self) -> None:
        """Seeding a spell already present as an in-window enter does not double."""
        entries = [(_ago(12), "budget_throttle_enter")]
        r = throttle_residency(
            entries, window_start=_WINDOW_START, now=_NOW, open_spell_since=_ago(12),
        )
        assert r == pytest.approx(12 / 24)

    def test_open_spell_since_unparseable_ignored(self) -> None:
        """An unparseable open-spell timestamp is skipped, not raised."""
        r = throttle_residency(
            [], window_start=_WINDOW_START, now=_NOW, open_spell_since="not-a-ts",
        )
        assert r == 0.0

    def test_open_spell_since_none_is_default(self) -> None:
        """Omitting open_spell_since leaves residency driven by entries alone."""
        entries = [(_ago(6), "budget_throttle_enter")]
        r = throttle_residency(entries, window_start=_WINDOW_START, now=_NOW)
        assert r == pytest.approx(6 / 24)


# ---------------------------------------------------------------------------
# summarize_health() — throttle-residency condition surfacing
# ---------------------------------------------------------------------------


class TestThrottleResidencyHealthSummary:
    """summarize_health surfaces a residency condition above the threshold."""

    def test_above_threshold_appends_condition_and_flips_noteworthy(self) -> None:
        result = summarize_health([], _snapshot(), throttle_residency_fraction=0.75)
        assert result.conditions == ["Budget-throttle residency 75% of the last 24h"]
        assert result.is_noteworthy is True

    def test_just_above_threshold(self) -> None:
        result = summarize_health([], _snapshot(), throttle_residency_fraction=0.51)
        assert len(result.conditions) == 1
        assert "Budget-throttle residency 51%" in result.conditions[0]

    def test_at_threshold_silent(self) -> None:
        """Exactly 0.5 is at — not above — the threshold → no condition."""
        result = summarize_health([], _snapshot(), throttle_residency_fraction=0.5)
        assert result.conditions == []
        assert result.is_noteworthy is False

    def test_below_threshold_silent(self) -> None:
        result = summarize_health([], _snapshot(), throttle_residency_fraction=0.2)
        assert result.conditions == []
        assert result.is_noteworthy is False

    def test_none_default_no_condition(self) -> None:
        """Omitting throttle_residency_fraction appends nothing (backward compatible)."""
        result = summarize_health([], _snapshot())
        assert result.conditions == []

    def test_full_residency_reads_100_percent(self) -> None:
        result = summarize_health([], _snapshot(), throttle_residency_fraction=1.0)
        assert "Budget-throttle residency 100% of the last 24h" in result.conditions

    def test_coexists_with_no_op_condition(self) -> None:
        """The residency condition is appended alongside the no-op condition."""
        no_op = _task_with_summary(result_summary="No changes needed.")
        meaningful = _task_with_summary(result_summary="Shipped the feature.")
        tasks = [meaningful] * 6 + [no_op] * 4  # 40% no-op
        result = summarize_health(tasks, _snapshot(), throttle_residency_fraction=0.8)
        assert len(result.conditions) == 2
        assert any("No-op task rate" in c for c in result.conditions)
        assert any("Budget-throttle residency 80%" in c for c in result.conditions)


class TestHostCapacityHealthSummary:
    """summarize_health folds host-capacity saturation into conditions (proposal 125)."""

    def test_none_reading_appends_no_condition(self) -> None:
        """A snapshot without a host-capacity reading extends conditions by nothing."""
        result = summarize_health([], _snapshot(host_capacity=None))
        assert result.conditions == []
        assert result.is_noteworthy is False

    def test_healthy_reading_appends_no_condition(self) -> None:
        """A reading below every threshold names nothing and stays silent."""
        healthy = HostCapacityReading(
            load_per_core=0.5,
            memory_used_fraction=0.5,
            swap_used_fraction=0.0,
            docker_root_free_gib=100.0,
        )
        result = summarize_health([], _snapshot(host_capacity=healthy))
        assert result.conditions == []
        assert result.is_noteworthy is False

    def test_saturated_reading_appends_condition_and_flips_noteworthy(self) -> None:
        """A single saturated resource adds exactly one condition and flips noteworthy."""
        saturated = HostCapacityReading(
            load_per_core=0.5,
            memory_used_fraction=0.97,  # the only saturated resource
            swap_used_fraction=0.0,
            docker_root_free_gib=100.0,
        )
        result = summarize_health([], _snapshot(host_capacity=saturated))
        assert len(result.conditions) == 1
        assert "memory" in result.conditions[0]
        assert result.is_noteworthy is True
