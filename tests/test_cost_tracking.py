"""Tests for maestro.cost_tracking — cost sample recording and rate computation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import call, patch

import pytest

from maestro import cost_tracking
from maestro.cost_tracking import (
    CALLER_GROUPS,
    BudgetBreakdown,
    CallerRate,
    CostRateSnapshot,
    CostSample,
    JsonlCostSampleStore,
    PlanHeadroom,
    ShapeCost,
    TaskQuality,
    _as_utc,
    _clear_cost_rate_cache,
    _filter_records_since,
    _split_records_at,
    aggregate_cache_hit_rate,
    aggregate_caller_counts,
    aggregate_task_quality,
    append_cost_sample,
    build_budget_breakdown,
    build_cost_rates,
    cost_per_task,
    cost_per_task_shape,
    format_burn_baseline,
    format_burn_comparison,
    generate_burn_baseline,
    generate_burn_comparison,
    generate_cost_report,
    generate_cost_report_data,
    log_metering_divergence,
)
from maestro.state_store import SqliteCostSampleStore, StateStore

FAKE_DIR = Path("/fake/cost")
SAMPLES_PATH = FAKE_DIR / "cost_samples.jsonl"


def _backend() -> JsonlCostSampleStore:
    """The JSONL cost-sample backend over ``SAMPLES_PATH``.

    The windowed builders now take a ``CostSampleBackend`` rather than a raw
    path; a fresh wrapper per call is stateless and reproduces the former
    ``(path, mtime)`` cache key exactly, so caching behaviour is unchanged.
    """
    return JsonlCostSampleStore(SAMPLES_PATH)


@pytest.fixture(autouse=True)
def _isolate_cost_rate_cache() -> Any:
    """Drop the module-level TTL cache before and after every test.

    ``mem_fs`` does not advance ``mtime`` when files are rewritten in place,
    so without this isolation step a fresh fixture in one test would be
    masked by the cached snapshot from the previous test.
    """
    _clear_cost_rate_cache()
    yield
    _clear_cost_rate_cache()


def _make_sample(
    *,
    ts: datetime | None = None,
    cost: float = 0.10,
    caller: str = "test_caller",
    estimated: bool = False,
    unmetered: bool = False,
    provider: str | None = None,
) -> CostSample:
    return CostSample(
        timestamp=(ts or datetime.now(UTC)).isoformat(),
        cost_usd=cost,
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=500,
        cache_creation_tokens=100,
        caller=caller,
        duration_ms=1500,
        estimated=estimated,
        unmetered=unmetered,
        provider=provider,
    )


class TestAppendCostSample:
    def test_creates_file_and_appends(self, mem_fs: dict[str, str]) -> None:
        sample = _make_sample()
        append_cost_sample(SAMPLES_PATH, sample)
        content = mem_fs[str(SAMPLES_PATH)]
        lines = content.strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["cost_usd"] == 0.10
        assert data["caller"] == "test_caller"

    def test_creates_parent_directory(self, mem_fs: dict[str, str]) -> None:
        """The helper makes its own parent directory before appending.

        ``mem_fs``'s ``fake_open`` does not model directory existence — it
        never raises ``FileNotFoundError`` for a missing parent — and its
        ``fake_mkdir`` is a no-op, so appending under a missing directory
        would pass with or without the fix. Assert the ``mkdir`` call itself
        instead, plus that the sample still lands so the patch does not mask
        the write.

        ``autospec=True`` is load-bearing: a bare ``patch.object(Path,
        "mkdir")`` does not bind the receiver, so the recorded call omits
        ``self`` and an implementation calling ``path.mkdir(...)`` on the
        *file* path — which creates a directory where the JSONL must go —
        would satisfy the assertion. Autospec records the receiver, pinning
        the parent as the mkdir target.
        """
        nested = FAKE_DIR / "nested" / "cost_samples.jsonl"
        with patch.object(Path, "mkdir", autospec=True) as mock_mkdir:
            append_cost_sample(nested, _make_sample())
        mock_mkdir.assert_called_once_with(
            nested.parent, parents=True, exist_ok=True,
        )
        assert json.loads(mem_fs[str(nested)].strip())["caller"] == "test_caller"

    def test_appends_multiple(self, mem_fs: dict[str, str]) -> None:
        append_cost_sample(SAMPLES_PATH, _make_sample(cost=0.05))
        append_cost_sample(SAMPLES_PATH, _make_sample(cost=0.15))
        content = mem_fs[str(SAMPLES_PATH)]
        lines = content.strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["cost_usd"] == 0.05
        assert json.loads(lines[1])["cost_usd"] == 0.15

    def test_uses_file_lock(self, mem_fs: dict[str, str]) -> None:
        """The append serializes on the same lock ``Memory.archive`` holds.

        ``archive`` rotates ``cost_samples.jsonl`` under ``file_lock`` across
        ``read_text`` -> ``atomic_write``.  This is the highest-frequency
        appender on a rotated file — every metered SDK call writes a sample —
        so an unlocked append has the widest window to land inside a rotation
        and be destroyed by the rename.
        """
        locked: list[Any] = []

        @contextmanager
        def tracking_lock(path: Any) -> Iterator[None]:
            locked.append(path)
            yield

        with patch("maestro.cost_tracking.file_lock", tracking_lock):
            append_cost_sample(SAMPLES_PATH, _make_sample())

        assert len(locked) == 1
        assert str(locked[0]).endswith("cost_samples.jsonl")
        assert json.loads(mem_fs[str(SAMPLES_PATH)].strip())["caller"] == "test_caller"


class TestAsUtc:
    """The single owner of the stored cost-sample stamp → UTC-aware coercion."""

    def test_naive_is_read_as_utc(self) -> None:
        # Not host-local: both write paths emit UTC, so a naive stamp is
        # stamped UTC rather than re-interpreted through the host offset.
        assert _as_utc("2026-03-01T09:00:00") == datetime(
            2026, 3, 1, 9, 0, 0, tzinfo=UTC,
        )

    def test_aware_utc_is_preserved(self) -> None:
        assert _as_utc("2026-03-01T09:00:00+00:00") == datetime(
            2026, 3, 1, 9, 0, 0, tzinfo=UTC,
        )

    def test_aware_offset_is_converted_to_utc(self) -> None:
        parsed = _as_utc("2026-03-01T11:00:00+02:00")
        assert parsed == datetime(2026, 3, 1, 9, 0, 0, tzinfo=UTC)
        assert parsed.tzinfo is UTC

    def test_naive_and_aware_are_mutually_comparable(self) -> None:
        # The defect this helper exists to kill: min()/max()/subtraction over
        # a mixed list used to raise TypeError.
        stamps = [_as_utc("2026-03-01T09:00:00"), _as_utc("2026-03-01T10:00:00+00:00")]
        assert (max(stamps) - min(stamps)) == timedelta(hours=1)

    def test_unparseable_propagates_value_error(self) -> None:
        # Fail loud: no new handler is introduced here; a tolerant caller owns
        # its own skip.
        with pytest.raises(ValueError):
            _as_utc("not-a-timestamp")

    def test_non_string_propagates_type_error(self) -> None:
        with pytest.raises(TypeError):
            _as_utc(12345)  # type: ignore[arg-type]


class TestBuildCostRates:
    def test_missing_file_returns_zeroed(self, mem_fs: dict[str, str]) -> None:
        snapshot = build_cost_rates(_backend())
        assert isinstance(snapshot, CostRateSnapshot)
        assert snapshot.total_calls == 0
        assert snapshot.cost_per_hour == 0.0
        assert snapshot.by_caller == {}

    def test_empty_file_returns_zeroed(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(SAMPLES_PATH)] = ""
        snapshot = build_cost_rates(_backend())
        assert snapshot.total_calls == 0

    def test_computes_correct_rates(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        t1 = now - timedelta(minutes=30)
        t2 = now - timedelta(minutes=15)
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=t1, cost=1.0))
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=t2, cost=2.0))

        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        assert snapshot.total_calls == 2
        assert snapshot.total_cost_usd == pytest.approx(3.0)
        assert snapshot.total_input_tokens == 2000
        assert snapshot.total_output_tokens == 400

        # Samples span 15 minutes but window_hours=1.0, so minimum window
        # is 3600s (1 hour).  Rate = 3.0 / 1.0 = 3.0/hr.
        assert snapshot.cost_per_hour == pytest.approx(3.0)
        assert snapshot.cost_per_day == pytest.approx(3.0 * 24)
        assert snapshot.cost_per_month == pytest.approx(3.0 * 24 * 30)

    def test_mixed_naive_and_aware_timestamps_aggregate(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A legacy/hand-edited naive row sitting beside aware rows used to
        # crash min()/max() with "can't compare offset-naive and offset-aware
        # datetimes", taking the burn-rate throttle down with it.
        now = datetime.now(UTC)
        backend = _backend()
        backend.append(
            _make_sample(ts=(now - timedelta(minutes=30)).replace(tzinfo=None), cost=1.0),
        )
        backend.append(_make_sample(ts=now - timedelta(minutes=10), cost=2.0))

        snapshot = build_cost_rates(backend, window_hours=1.0)

        assert snapshot.total_calls == 2
        assert snapshot.total_cost_usd == pytest.approx(3.0)
        assert datetime.fromisoformat(snapshot.window_start).tzinfo is not None
        assert datetime.fromisoformat(snapshot.window_end).tzinfo is not None
        assert datetime.fromisoformat(
            snapshot.window_start,
        ).utcoffset() == timedelta(0)
        assert datetime.fromisoformat(snapshot.window_end).utcoffset() == timedelta(0)

    def test_filters_by_window(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(hours=3)
        recent = now - timedelta(minutes=10)
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=old, cost=5.0))
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=recent, cost=1.0))

        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        assert snapshot.total_calls == 1
        assert snapshot.total_cost_usd == pytest.approx(1.0)

    def test_aggregates_by_caller(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        t1 = now - timedelta(minutes=20)
        t2 = now - timedelta(minutes=10)
        t3 = now - timedelta(minutes=5)
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=t1, cost=1.0, caller="a"))
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=t2, cost=2.0, caller="b"))
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=t3, cost=3.0, caller="a"))

        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        assert snapshot.by_caller["a"].cost_usd == pytest.approx(4.0)
        assert snapshot.by_caller["a"].calls == 2
        assert snapshot.by_caller["b"].cost_usd == pytest.approx(2.0)
        assert snapshot.by_caller["b"].calls == 1
        # A call count is a cardinal, not a measured quantity:
        # ``pytest.approx(2.0)`` passes against both ``2`` and ``2.0`` and so
        # cannot express the bug (#1154). Pin the runtime type explicitly.
        assert isinstance(snapshot.by_caller["a"].calls, int)
        assert isinstance(snapshot.by_caller["b"].calls, int)

    def test_by_caller_calls_serializes_as_int(self, mem_fs: dict[str, str]) -> None:
        """``asdict`` is what ``/api/cost-rates`` returns verbatim, so the
        integer narrowing must survive serialization — that boundary is where
        the float count became publicly visible (#1154)."""
        now = datetime.now(UTC)
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(ts=now - timedelta(minutes=20), cost=1.0, caller="a"),
        )
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(ts=now - timedelta(minutes=5), cost=3.0, caller="a"),
        )

        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        payload = asdict(snapshot)

        assert payload["by_caller"]["a"] == {"cost_usd": pytest.approx(4.0), "calls": 2}
        assert isinstance(payload["by_caller"]["a"]["calls"], int)

    def test_skips_blank_lines(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        sample = _make_sample(ts=now - timedelta(minutes=5), cost=1.0)
        line = json.dumps({
            "timestamp": sample.timestamp,
            "cost_usd": sample.cost_usd,
            "input_tokens": sample.input_tokens,
            "output_tokens": sample.output_tokens,
            "cache_read_tokens": sample.cache_read_tokens,
            "cache_creation_tokens": sample.cache_creation_tokens,
            "caller": sample.caller,
            "duration_ms": sample.duration_ms,
        })
        mem_fs[str(SAMPLES_PATH)] = f"\n{line}\n\n"
        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        assert snapshot.total_calls == 1

    def test_skips_malformed_lines(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        good_sample = _make_sample(ts=now - timedelta(minutes=5), cost=1.0)
        # Write a good line, a bad line, another good line
        lines = [
            json.dumps({
                "timestamp": good_sample.timestamp,
                "cost_usd": good_sample.cost_usd,
                "input_tokens": good_sample.input_tokens,
                "output_tokens": good_sample.output_tokens,
                "cache_read_tokens": good_sample.cache_read_tokens,
                "cache_creation_tokens": good_sample.cache_creation_tokens,
                "caller": good_sample.caller,
                "duration_ms": good_sample.duration_ms,
            }),
            "not valid json {{{",
            json.dumps({"timestamp": "bad", "missing": "fields"}),
            json.dumps({
                "timestamp": (now - timedelta(minutes=2)).isoformat(),
                "cost_usd": 2.0,
                "input_tokens": 500,
                "output_tokens": 100,
                "cache_read_tokens": 250,
                "cache_creation_tokens": 50,
                "caller": "ok",
                "duration_ms": 800,
            }),
        ]
        mem_fs[str(SAMPLES_PATH)] = "\n".join(lines) + "\n"

        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        assert snapshot.total_calls == 2
        assert snapshot.total_cost_usd == pytest.approx(3.0)

    def test_single_sample_uses_minimum_window(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=now, cost=1.0))
        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        assert snapshot.total_calls == 1
        # With a single sample, window_seconds is clamped to window_hours * 3600
        assert snapshot.window_seconds == pytest.approx(3600.0)

    def test_single_sample_rate_not_inflated(self, mem_fs: dict[str, str]) -> None:
        """A single sample in a 1-hour window must not produce an inflated rate.

        With window_hours=1.0 and a single $0.05 sample, the rate should be
        at most $0.05/hr -- not $180/hr (the 3600x inflation caused by clamping
        window_seconds to 1.0 instead of window_hours * 3600).
        """
        now = datetime.now(UTC)
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=now, cost=0.05))
        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        assert snapshot.total_calls == 1
        assert snapshot.total_cost_usd == pytest.approx(0.05)
        # The rate for a single $0.05 call in a 1-hour window should be $0.05/hr
        assert snapshot.cost_per_hour == pytest.approx(0.05)
        assert snapshot.cost_per_day == pytest.approx(0.05 * 24)
        assert snapshot.cost_per_month == pytest.approx(0.05 * 24 * 30)


class TestBuildBudgetBreakdown:
    def test_missing_file_returns_zeroed(self, mem_fs: dict[str, str]) -> None:
        b = build_budget_breakdown(_backend())
        assert isinstance(b, BudgetBreakdown)
        assert b.total_calls == 0
        assert b.total_cost_usd == 0.0
        assert b.by_caller == []
        assert b.by_group == []
        assert b.window_hours == 1.0

    def test_empty_window_returns_zeroed(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        # One sample 3h ago, but window_hours=1.0 so nothing falls in.
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(ts=now - timedelta(hours=3), cost=1.0, caller="reflect"),
        )
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        assert b.total_calls == 0
        assert b.by_caller == []
        assert b.by_group == []

    def test_mixed_naive_and_aware_timestamps_aggregate(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Same mixed-domain window as TestBuildCostRates: the second aggregator
        # re-parses the identical strings, so it failed on the identical input.
        now = datetime.now(UTC)
        backend = _backend()
        backend.append(
            _make_sample(
                ts=(now - timedelta(minutes=30)).replace(tzinfo=None),
                cost=1.0,
                caller="reflect",
            ),
        )
        backend.append(
            _make_sample(ts=now - timedelta(minutes=10), cost=2.0, caller="introspect"),
        )

        b = build_budget_breakdown(backend, window_hours=1.0)

        assert b.total_calls == 2
        assert b.total_cost_usd == pytest.approx(3.0)
        assert {c.caller for c in b.by_caller} == {"reflect", "introspect"}
        assert datetime.fromisoformat(b.window_start).utcoffset() == timedelta(0)
        assert datetime.fromisoformat(b.window_end).utcoffset() == timedelta(0)

    def test_known_callers_assigned_to_groups(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        samples = [
            ("reflect", 0.50),
            ("introspect", 0.30),
            ("dream", 0.10),
            ("execute_step", 0.05),
        ]
        for i, (caller, cost) in enumerate(samples):
            append_cost_sample(
                SAMPLES_PATH,
                _make_sample(
                    ts=now - timedelta(minutes=5 + i), cost=cost, caller=caller,
                ),
            )
        b = build_budget_breakdown(_backend(), window_hours=1.0)

        by_caller = {c.caller: c for c in b.by_caller}
        assert by_caller["reflect"].group == "Evolution"
        assert by_caller["introspect"].group == "Contemplation"
        assert by_caller["dream"].group == "Dreaming"
        assert by_caller["execute_step"].group == "Planning & Execution"

        groups = {g.group for g in b.by_group}
        assert groups == {"Evolution", "Contemplation", "Dreaming", "Planning & Execution"}

    def test_unknown_caller_falls_through_to_other(
        self, mem_fs: dict[str, str],
    ) -> None:
        now = datetime.now(UTC)
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(ts=now, cost=1.0, caller="some_brand_new_caller"),
        )
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        assert b.by_caller[0].group == "Other"
        assert b.by_group[0].group == "Other"

    def test_reported_and_estimated_split(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(
                ts=now - timedelta(minutes=1), cost=2.0, caller="reflect",
                estimated=False,
            ),
        )
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(
                ts=now - timedelta(minutes=2), cost=1.0, caller="reflect",
                estimated=True,
            ),
        )
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        assert b.total_cost_usd == pytest.approx(3.0)
        assert b.reported_cost_usd == pytest.approx(2.0)
        assert b.estimated_cost_usd == pytest.approx(1.0)

        reflect = b.by_caller[0]
        assert reflect.caller == "reflect"
        assert reflect.calls == 2
        assert reflect.reported_cost_usd == pytest.approx(2.0)
        assert reflect.estimated_cost_usd == pytest.approx(1.0)

    def test_per_caller_share_sums_to_one(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        for i, (caller, cost) in enumerate([
            ("reflect", 1.5),
            ("introspect", 0.75),
            ("dream", 0.25),
        ]):
            append_cost_sample(
                SAMPLES_PATH,
                _make_sample(
                    ts=now - timedelta(minutes=i + 1), cost=cost, caller=caller,
                ),
            )
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        total_share = sum(c.share_of_total for c in b.by_caller)
        assert total_share == pytest.approx(1.0)
        group_share = sum(g.share_of_total for g in b.by_group)
        assert group_share == pytest.approx(1.0)

    def test_sorted_desc_by_cost(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        for i, (caller, cost) in enumerate([
            ("dream", 0.10),
            ("reflect", 2.00),
            ("introspect", 0.50),
        ]):
            append_cost_sample(
                SAMPLES_PATH,
                _make_sample(
                    ts=now - timedelta(minutes=i + 1), cost=cost, caller=caller,
                ),
            )
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        caller_order = [c.caller for c in b.by_caller]
        assert caller_order == ["reflect", "introspect", "dream"]
        group_costs = [g.cost_usd for g in b.by_group]
        assert group_costs == sorted(group_costs, reverse=True)

    def test_skips_blank_and_malformed_lines(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        good = _make_sample(ts=now - timedelta(minutes=5), cost=1.0, caller="reflect")
        good_line = json.dumps({
            "timestamp": good.timestamp,
            "cost_usd": good.cost_usd,
            "input_tokens": good.input_tokens,
            "output_tokens": good.output_tokens,
            "cache_read_tokens": good.cache_read_tokens,
            "cache_creation_tokens": good.cache_creation_tokens,
            "caller": good.caller,
            "duration_ms": good.duration_ms,
        })
        mem_fs[str(SAMPLES_PATH)] = (
            "\n"
            f"{good_line}\n"
            "not valid json {{{\n"
            "\n"
        )
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        assert b.total_calls == 1
        assert b.by_caller[0].caller == "reflect"

    def test_caller_groups_map_known_strings(self) -> None:
        # Sanity check the map covers the main phase groups.  A few callers
        # that are known to exist in the codebase must be present so the
        # dashboard rollup does not fall entirely into "Other".
        for required in (
            "introspect", "evolution_introspect", "reflect", "propose_plan",
            "implement_plan",
            "dream", "generate_task", "startup_probe",
        ):
            assert required in CALLER_GROUPS

    def test_caller_groups_cover_every_registered_caller(self) -> None:
        """Regression #4135: every registered caller has a group; extras are retired."""
        from maestro.call_policy import CALL_POLICIES
        from maestro.cost_tracking import RETIRED_CALLERS

        assert set(CALL_POLICIES) - set(CALLER_GROUPS) == set()
        assert set(CALLER_GROUPS) - set(CALL_POLICIES) == set(RETIRED_CALLERS)

    def test_retired_evolution_introspect_still_rolls_up(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Proposal 149 Step 1: the unpriced introspect arm is retired, not erased.

        No live call site stamps ``evolution_introspect`` any more, so it leaves
        ``CALL_POLICIES``; its historical cost rows must still roll up to
        "Contemplation" rather than falling into "Other".
        """
        from maestro.call_policy import CALL_POLICIES
        from maestro.cost_tracking import RETIRED_CALLERS

        assert "evolution_introspect" not in CALL_POLICIES
        assert "evolution_introspect" in RETIRED_CALLERS
        append_cost_sample(SAMPLES_PATH, _make_sample(caller="evolution_introspect"))
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        assert b.by_caller[0].group == "Contemplation"
        assert [g.group for g in b.by_group] == ["Contemplation"]

    def test_retired_reflect_still_rolls_up(self, mem_fs: dict[str, str]) -> None:
        """Proposal 155 Step 4 deleted reflect; its history stays "Evolution"."""
        from maestro.call_policy import CALL_POLICIES, EFFORT_TIERS, EffortTier
        from maestro.cost_tracking import RETIRED_CALLERS

        assert "reflect" not in CALL_POLICIES
        assert "reflect" not in EFFORT_TIERS[EffortTier.PLANNING]
        assert "reflect" in RETIRED_CALLERS
        append_cost_sample(SAMPLES_PATH, _make_sample(caller="reflect"))
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        assert b.by_caller[0].group == "Evolution"

    @pytest.mark.parametrize("caller", ["dream", "dream_validate"])
    def test_retired_dream_callers_still_roll_up(
        self, mem_fs: dict[str, str], caller: str,
    ) -> None:
        """Proposal 149 Step 5: the priced dream digest is deleted, not erased.

        Neither caller is registered any more, but historical cost rows must
        still roll up to "Dreaming" rather than falling into "Other".
        """
        from maestro.call_policy import CALL_POLICIES
        from maestro.cost_tracking import RETIRED_CALLERS

        assert caller not in CALL_POLICIES
        assert caller in RETIRED_CALLERS
        append_cost_sample(SAMPLES_PATH, _make_sample(caller=caller))
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        assert b.by_caller[0].group == "Dreaming"


class TestReverseTailShortCircuit:
    """Verify the reverse walk stops at the cutoff and never parses earlier lines."""

    def test_old_garbage_at_start_is_never_parsed(
        self, mem_fs: dict[str, str], caplog: Any,
    ) -> None:
        """A non-JSON sentinel at the start of the file must not be reached.

        Forward-scan (the previous implementation) would parse every line and
        log a warning for the sentinel.  Reverse-walk-with-cutoff terminates
        the walk once it encounters a sample older than ``cutoff``, so the
        sentinel — which is even older — is never read or decoded.
        """
        import logging

        now = datetime.now(UTC)
        old_sample = _make_sample(ts=now - timedelta(hours=2), cost=5.0)
        recent_sample = _make_sample(ts=now - timedelta(minutes=5), cost=1.0)

        old_line = json.dumps({
            "timestamp": old_sample.timestamp,
            "cost_usd": old_sample.cost_usd,
            "input_tokens": old_sample.input_tokens,
            "output_tokens": old_sample.output_tokens,
            "cache_read_tokens": old_sample.cache_read_tokens,
            "cache_creation_tokens": old_sample.cache_creation_tokens,
            "caller": old_sample.caller,
            "duration_ms": old_sample.duration_ms,
        })
        recent_line = json.dumps({
            "timestamp": recent_sample.timestamp,
            "cost_usd": recent_sample.cost_usd,
            "input_tokens": recent_sample.input_tokens,
            "output_tokens": recent_sample.output_tokens,
            "cache_read_tokens": recent_sample.cache_read_tokens,
            "cache_creation_tokens": recent_sample.cache_creation_tokens,
            "caller": recent_sample.caller,
            "duration_ms": recent_sample.duration_ms,
        })
        mem_fs[str(SAMPLES_PATH)] = (
            "GARBAGE_SENTINEL_NEVER_PARSED\n"
            f"{old_line}\n"
            f"{recent_line}\n"
        )

        with (
            caplog.at_level(logging.WARNING, logger="maestro.fileutil"),
            caplog.at_level(logging.WARNING, logger="maestro.cost_tracking"),
        ):
            snap = build_cost_rates(_backend(), window_hours=1.0)

        assert snap.total_calls == 1
        assert snap.total_cost_usd == pytest.approx(1.0)
        # The sentinel sits earlier in the file than the cutoff sample, so
        # it must never be decoded — no malformed-line warning should fire.
        assert all(
            "GARBAGE_SENTINEL" not in r.message
            and "malformed" not in r.message
            for r in caplog.records
        )


class TestCostRateCache:
    @pytest.mark.parametrize(
        ("builder", "cache_name"),
        [(build_cost_rates, "_cost_rate_cache"), (build_budget_breakdown, "_budget_cache")],
    )
    def test_expired_window_entries_are_evicted(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
        builder: Any, cache_name: str,
    ) -> None:
        """Entries for other windows are dropped once their TTL elapses."""
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=datetime.now(UTC)))
        clock = [1000.0]
        monkeypatch.setattr(cost_tracking.time, "monotonic", lambda: clock[0])

        for i in range(50):
            builder(_backend(), window_hours=0.1 + i)
            clock[0] += cost_tracking._CACHE_TTL_SECONDS + 0.1

        assert len(getattr(cost_tracking, cache_name)) == 1

    @pytest.mark.parametrize(
        ("builder", "cache_name"),
        [(build_cost_rates, "_cost_rate_cache"), (build_budget_breakdown, "_budget_cache")],
    )
    def test_live_entries_bounded_by_capacity(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
        builder: Any, cache_name: str,
    ) -> None:
        """Live entries are capped; the oldest-inserted key is evicted first."""
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=datetime.now(UTC)))
        monkeypatch.setattr(cost_tracking.time, "monotonic", lambda: 1000.0)
        cap = cost_tracking._CACHE_MAX_ENTRIES

        for i in range(cap + 1):
            builder(_backend(), window_hours=float(i + 1))

        cache = getattr(cost_tracking, cache_name)
        windows = [key[1] for key in cache]
        assert len(cache) == cap
        assert 1.0 not in windows
        assert windows[0] == 2.0

    def test_refreshed_entry_moves_to_back(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rebuilding an existing key re-inserts it as the newest entry."""
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=datetime.now(UTC)))
        monkeypatch.setattr(cost_tracking.time, "monotonic", lambda: 1000.0)
        build_cost_rates(_backend(), window_hours=1.0)
        build_cost_rates(_backend(), window_hours=2.0)
        cost_tracking._cost_rate_cache[next(iter(cost_tracking._cost_rate_cache))].version = -1

        build_cost_rates(_backend(), window_hours=1.0)

        windows = [key[1] for key in cost_tracking._cost_rate_cache]
        assert windows == [2.0, 1.0]

    def test_repeat_call_within_ttl_is_cache_hit(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Calls within ``_CACHE_TTL_SECONDS`` reuse the cached snapshot."""
        from maestro import cost_tracking

        now = datetime.now(UTC)
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=now, cost=1.0))

        call_count = 0
        original_iter = cost_tracking.iter_jsonl_reverse

        def counting_iter(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return original_iter(*args, **kwargs)

        monkeypatch.setattr(cost_tracking, "iter_jsonl_reverse", counting_iter)

        first = build_cost_rates(_backend(), window_hours=1.0)
        second = build_cost_rates(_backend(), window_hours=1.0)

        assert first is not second  # deepcopy returns a distinct object
        assert first == second  # but with equal contents
        assert call_count == 1  # second call did not re-read the file

    def test_ttl_expiry_forces_rebuild(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once the TTL has elapsed, the next call rebuilds from disk."""
        from maestro import cost_tracking

        now = datetime.now(UTC)
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=now, cost=1.0))

        # Simulate monotonic clock advancing past the TTL between calls.
        clock = [1000.0]

        def fake_monotonic() -> float:
            return clock[0]

        monkeypatch.setattr(cost_tracking.time, "monotonic", fake_monotonic)

        call_count = 0
        original_iter = cost_tracking.iter_jsonl_reverse

        def counting_iter(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return original_iter(*args, **kwargs)

        monkeypatch.setattr(cost_tracking, "iter_jsonl_reverse", counting_iter)

        build_cost_rates(_backend(), window_hours=1.0)
        clock[0] += cost_tracking._CACHE_TTL_SECONDS + 0.1
        build_cost_rates(_backend(), window_hours=1.0)

        assert call_count == 2  # rebuilt after TTL expiry

    def test_mtime_change_invalidates_cache(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A new mtime forces a rebuild even within the TTL window."""
        from maestro import cost_tracking

        now = datetime.now(UTC)
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=now, cost=1.0))

        mtime_seq = [10_000_000_000]

        def fake_stat(path: Path) -> int | None:
            return mtime_seq[0]

        monkeypatch.setattr(cost_tracking, "_stat_mtime_ns", fake_stat)

        call_count = 0
        original_iter = cost_tracking.iter_jsonl_reverse

        def counting_iter(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return original_iter(*args, **kwargs)

        monkeypatch.setattr(cost_tracking, "iter_jsonl_reverse", counting_iter)

        build_cost_rates(_backend(), window_hours=1.0)
        mtime_seq[0] += 1_000_000  # writer touched the file
        build_cost_rates(_backend(), window_hours=1.0)

        assert call_count == 2  # mtime change forced a rebuild

    def test_dict_with_valid_timestamp_but_missing_fields_logged(
        self, mem_fs: dict[str, str], caplog: Any,
    ) -> None:
        """A row whose timestamp parses but whose cost/token fields are missing
        is skipped via the second except branch with a warning."""
        import logging

        now = datetime.now(UTC)
        good = _make_sample(ts=now - timedelta(minutes=2), cost=1.0)
        good_line = json.dumps({
            "timestamp": good.timestamp,
            "cost_usd": good.cost_usd,
            "input_tokens": good.input_tokens,
            "output_tokens": good.output_tokens,
            "cache_read_tokens": good.cache_read_tokens,
            "cache_creation_tokens": good.cache_creation_tokens,
            "caller": good.caller,
            "duration_ms": good.duration_ms,
        })
        # Valid timestamp + cost_usd as a non-numeric string forces a
        # ``ValueError`` inside ``CostSample`` construction.
        partial = json.dumps({
            "timestamp": (now - timedelta(minutes=5)).isoformat(),
            "cost_usd": "not-a-number",
            "input_tokens": 1, "output_tokens": 1,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "caller": "x", "duration_ms": 1,
        })
        mem_fs[str(SAMPLES_PATH)] = good_line + "\n" + partial + "\n"

        with caplog.at_level(logging.WARNING, logger="maestro.cost_tracking"):
            snap = build_cost_rates(_backend(), window_hours=1.0)

        assert snap.total_calls == 1
        assert any(
            "malformed cost sample line" in r.message for r in caplog.records
        )

    def test_budget_breakdown_uses_independent_cache(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cost-rates and budget-breakdown caches don't share state."""
        from maestro import cost_tracking

        now = datetime.now(UTC)
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=now, cost=1.0))

        call_count = 0
        original_iter = cost_tracking.iter_jsonl_reverse

        def counting_iter(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return original_iter(*args, **kwargs)

        monkeypatch.setattr(cost_tracking, "iter_jsonl_reverse", counting_iter)

        a1 = build_cost_rates(_backend(), window_hours=1.0)
        b1 = build_budget_breakdown(_backend(), window_hours=1.0)
        a2 = build_cost_rates(_backend(), window_hours=1.0)
        b2 = build_budget_breakdown(_backend(), window_hours=1.0)

        # 2 distinct caches, so first call to each rebuilds; second hits cache.
        assert call_count == 2
        assert a1 is not a2  # deepcopy returns distinct objects
        assert a1 == a2  # but with equal contents
        assert b1 is not b2
        assert b1 == b2

    def test_mutating_snapshot_by_caller_does_not_affect_cache(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Mutating the ``by_caller`` dict returned by a cache hit must not
        corrupt the cached value for subsequent callers.

        The entries are frozen :class:`CallerRate` values, so the interior is
        immutable by construction — the guarantee narrowed rather than
        disappeared. The outer dict is still mutable, so the ``copy.deepcopy``
        on the cache-hit path remains the thing under test: rebinding a whole
        entry, and adding a new one, must both stay local to the copy.
        """
        now = datetime.now(UTC)
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(ts=now, cost=1.0, caller="reflect"),
        )

        # First call populates the cache; second call is a cache hit.
        build_cost_rates(_backend(), window_hours=1.0)
        cached_copy = build_cost_rates(_backend(), window_hours=1.0)
        assert "reflect" in cached_copy.by_caller

        # Mutate the outer dict on the returned copy.
        cached_copy.by_caller["reflect"] = CallerRate(cost_usd=999.0, calls=99)
        cached_copy.by_caller["injected"] = CallerRate(cost_usd=0.0, calls=0)

        # Next cache-hit call must return the original, uncorrupted data.
        fresh = build_cost_rates(_backend(), window_hours=1.0)
        assert fresh.by_caller["reflect"].cost_usd == pytest.approx(1.0)
        assert fresh.by_caller["reflect"].calls == 1
        assert "injected" not in fresh.by_caller

    def test_mutating_breakdown_lists_does_not_affect_cache(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Mutating the ``by_caller`` / ``by_group`` lists returned by a
        cache hit must not corrupt the cached value for subsequent callers."""
        now = datetime.now(UTC)
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(ts=now, cost=1.0, caller="reflect"),
        )

        # First call populates the cache; second call is a cache hit.
        build_budget_breakdown(_backend(), window_hours=1.0)
        cached_copy = build_budget_breakdown(_backend(), window_hours=1.0)
        assert len(cached_copy.by_caller) == 1
        assert len(cached_copy.by_group) == 1

        # Mutate the interior mutable lists on the returned copy.
        cached_copy.by_caller.clear()
        cached_copy.by_group.pop()

        # Next cache-hit call must return the original, uncorrupted data.
        fresh = build_budget_breakdown(_backend(), window_hours=1.0)
        assert len(fresh.by_caller) == 1
        assert fresh.by_caller[0].caller == "reflect"
        assert len(fresh.by_group) == 1


class TestFrozenDataclasses:
    """Frozen dataclasses reject direct attribute assignment."""

    def test_cost_rate_snapshot_frozen(self) -> None:
        snapshot = CostRateSnapshot(
            window_start="", window_end="", window_seconds=0.0,
            total_cost_usd=0.0, total_input_tokens=0, total_output_tokens=0,
            total_cache_read_tokens=0, total_calls=0, cost_per_hour=0.0,
            cost_per_day=0.0, cost_per_month=0.0, tokens_in_per_hour=0.0,
            tokens_out_per_hour=0.0, by_caller={},
        )
        with pytest.raises(AttributeError, match="cannot assign to field"):
            snapshot.total_cost_usd = 99.0  # type: ignore[misc]

    def test_budget_breakdown_frozen(self) -> None:
        breakdown = BudgetBreakdown(
            window_start="", window_end="", window_hours=1.0,
            total_cost_usd=0.0, reported_cost_usd=0.0,
            estimated_cost_usd=0.0, total_calls=0,
            by_caller=[], by_group=[],
        )
        with pytest.raises(AttributeError, match="cannot assign to field"):
            breakdown.total_cost_usd = 99.0  # type: ignore[misc]

    def test_caller_spend_frozen(self) -> None:
        from maestro.cost_tracking import CallerSpend
        cs = CallerSpend(
            caller="x", group="Other", cost_usd=1.0, calls=1,
            reported_cost_usd=1.0, estimated_cost_usd=0.0,
            share_of_total=1.0,
        )
        with pytest.raises(AttributeError, match="cannot assign to field"):
            cs.cost_usd = 99.0  # type: ignore[misc]

    def test_group_spend_frozen(self) -> None:
        from maestro.cost_tracking import GroupSpend
        gs = GroupSpend(group="Other", cost_usd=1.0, calls=1, share_of_total=1.0)
        with pytest.raises(AttributeError, match="cannot assign to field"):
            gs.cost_usd = 99.0  # type: ignore[misc]


class TestCostPerTask:
    """Tests for :func:`cost_per_task`."""

    def test_multi_task_grouping(self) -> None:
        records = [
            {"task_id": "aaa", "total_cost_usd": 1.0},
            {"task_id": "bbb", "total_cost_usd": 2.5},
            {"task_id": "aaa", "total_cost_usd": 0.5},
        ]
        result = cost_per_task(records)
        assert result["aaa"] == pytest.approx(1.5)
        assert result["bbb"] == pytest.approx(2.5)
        assert len(result) == 2

    def test_empty_input(self) -> None:
        assert cost_per_task([]) == {}

    def test_none_task_id_grouping(self) -> None:
        records = [
            {"task_id": None, "total_cost_usd": 0.3},
            {"task_id": None, "total_cost_usd": 0.7},
        ]
        result = cost_per_task(records)
        assert result[None] == pytest.approx(1.0)
        assert len(result) == 1

    def test_single_record(self) -> None:
        records = [{"task_id": "only", "total_cost_usd": 4.2}]
        result = cost_per_task(records)
        assert result == {"only": pytest.approx(4.2)}

    def test_missing_task_id_key_treated_as_none(self) -> None:
        records = [
            {"total_cost_usd": 1.0},
            {"task_id": "abc", "total_cost_usd": 2.0},
        ]
        result = cost_per_task(records)
        assert result[None] == pytest.approx(1.0)
        assert result["abc"] == pytest.approx(2.0)

    def test_missing_cost_field_treated_as_zero(self) -> None:
        records = [{"task_id": "x"}]
        result = cost_per_task(records)
        assert result["x"] == pytest.approx(0.0)

    def test_non_numeric_cost_treated_as_zero(self) -> None:
        records = [{"task_id": "x", "total_cost_usd": "not-a-number"}]
        result = cost_per_task(records)
        assert result["x"] == pytest.approx(0.0)

    def test_non_string_task_id_treated_as_none(self) -> None:
        records = [{"task_id": 42, "total_cost_usd": 1.0}]
        result = cost_per_task(records)
        assert result[None] == pytest.approx(1.0)
        assert len(result) == 1

    def test_unmetered_record_contributes_zero(self) -> None:
        """An unmetered iteration registers its task_id but adds 0 cost."""
        records = [
            {"task_id": "x", "total_cost_usd": 5.0},
            {"task_id": "x", "total_cost_usd": 5.0, "unmetered": True},
        ]
        result = cost_per_task(records)
        # The metered row counts; the unmetered row's cost is unknown, not $5.
        assert result["x"] == pytest.approx(5.0)

    def test_unmetered_only_task_registers_zero(self) -> None:
        """A task seen only via unmetered rows still appears, summing to 0."""
        records = [{"task_id": "y", "total_cost_usd": 9.9, "unmetered": True}]
        result = cost_per_task(records)
        assert result["y"] == pytest.approx(0.0)


class TestUnmeteredSamples:
    """Unmetered samples (usage_metering=False providers) are honoured as
    *unknown* cost rather than measured-as-zero (proposal 058 Step 13)."""

    def test_round_trips_through_append(self, mem_fs: dict[str, str]) -> None:
        append_cost_sample(SAMPLES_PATH, _make_sample(unmetered=True))
        data = json.loads(mem_fs[str(SAMPLES_PATH)].strip())
        assert data["unmetered"] is True

    def test_cost_rates_exclude_unmetered(self, mem_fs: dict[str, str]) -> None:
        """build_cost_rates drops unmetered samples so the burn-rate gate
        never reads their placeholder figures as spend."""
        now = datetime.now(UTC)
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(ts=now - timedelta(minutes=10), cost=2.0, caller="reflect"),
        )
        # A nonzero cost on an unmetered sample is still excluded — the flag,
        # not the value, drives the exclusion.
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(
                ts=now - timedelta(minutes=5), cost=99.0,
                caller="cli_phase", unmetered=True,
            ),
        )
        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        assert snapshot.total_calls == 1
        assert snapshot.total_cost_usd == pytest.approx(2.0)
        assert snapshot.cost_per_hour == pytest.approx(2.0)

    def test_all_unmetered_window_is_zeroed(self, mem_fs: dict[str, str]) -> None:
        """A window of only unmetered calls yields a zeroed rate snapshot,
        so the burn-rate multiplier stays at 1.0."""
        now = datetime.now(UTC)
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(ts=now, cost=0.0, caller="cli_phase", unmetered=True),
        )
        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        assert snapshot.total_calls == 0
        assert snapshot.cost_per_hour == 0.0

    def test_breakdown_flags_unmetered_caller_and_group(
        self, mem_fs: dict[str, str],
    ) -> None:
        now = datetime.now(UTC)
        # ``introspect`` is metered; ``dream`` is entirely unmetered. They are
        # in different groups so the group flag is unambiguous.
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(ts=now - timedelta(minutes=3), cost=1.0, caller="introspect"),
        )
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(
                ts=now - timedelta(minutes=2), cost=0.0,
                caller="dream", unmetered=True,
            ),
        )
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        by_caller = {c.caller: c for c in b.by_caller}
        assert by_caller["introspect"].unmetered is False
        assert by_caller["dream"].unmetered is True
        by_group = {g.group: g for g in b.by_group}
        assert by_group["Contemplation"].unmetered is False
        assert by_group["Dreaming"].unmetered is True

    def test_breakdown_partial_metering_stays_metered(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A caller with even one metered sample is not flagged unmetered —
        its cost is a real (if partial) figure, not unknown."""
        now = datetime.now(UTC)
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(ts=now - timedelta(minutes=3), cost=1.0, caller="reflect"),
        )
        append_cost_sample(
            SAMPLES_PATH,
            _make_sample(
                ts=now - timedelta(minutes=2), cost=0.0,
                caller="reflect", unmetered=True,
            ),
        )
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        reflect = next(c for c in b.by_caller if c.caller == "reflect")
        assert reflect.unmetered is False
        assert reflect.calls == 2


# ── Provider attribution on cost samples (proposal 067 Step 2) ──────────────


class TestProviderAttribution:
    """Cost samples carry the producing provider; the breakdown surfaces it."""

    def test_provider_round_trips_through_sample_file(
        self, mem_fs: dict[str, str],
    ) -> None:
        append_cost_sample(SAMPLES_PATH, _make_sample(provider="cli_paced"))
        data = json.loads(mem_fs[str(SAMPLES_PATH)].strip())
        assert data["provider"] == "cli_paced"

    def test_legacy_sample_without_provider_parses_none(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A pre-067 JSONL line (no provider key) deserializes as None."""
        sample = _make_sample()
        line = json.dumps({
            k: v for k, v in asdict(sample).items() if k != "provider"
        })
        mem_fs[str(SAMPLES_PATH)] = line + "\n"
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        caller = next(c for c in b.by_caller if c.caller == "test_caller")
        assert caller.providers == []

    def test_non_string_provider_parses_none(
        self, mem_fs: dict[str, str],
    ) -> None:
        sample = asdict(_make_sample())
        sample["provider"] = 42
        mem_fs[str(SAMPLES_PATH)] = json.dumps(sample) + "\n"
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        caller = next(c for c in b.by_caller if c.caller == "test_caller")
        assert caller.providers == []

    def test_breakdown_attributes_distinct_providers_sorted(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A caller served by two providers lists both, sorted; an unmetered
        cli_paced caller is attributed so its '—' cost is readable as
        unknown-by-design rather than silently zero."""
        now = datetime.now(UTC)
        append_cost_sample(SAMPLES_PATH, _make_sample(
            ts=now - timedelta(minutes=4), caller="introspect",
            provider="cli_paced",
        ))
        append_cost_sample(SAMPLES_PATH, _make_sample(
            ts=now - timedelta(minutes=3), caller="introspect",
            provider="anthropic",
        ))
        append_cost_sample(SAMPLES_PATH, _make_sample(
            ts=now - timedelta(minutes=2), cost=0.0, caller="dream",
            unmetered=True, provider="cli_paced",
        ))
        b = build_budget_breakdown(_backend(), window_hours=1.0)
        by_caller = {c.caller: c for c in b.by_caller}
        assert by_caller["introspect"].providers == ["anthropic", "cli_paced"]
        assert by_caller["dream"].providers == ["cli_paced"]
        assert by_caller["dream"].unmetered is True

    def test_burn_rate_input_unchanged_by_attribution(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Attribution is display-only: build_cost_rates still excludes
        unmetered samples and aggregates metered ones identically."""
        now = datetime.now(UTC)
        append_cost_sample(SAMPLES_PATH, _make_sample(
            ts=now - timedelta(minutes=3), cost=2.0, provider="anthropic",
        ))
        append_cost_sample(SAMPLES_PATH, _make_sample(
            ts=now - timedelta(minutes=2), cost=0.0, caller="cli_phase",
            unmetered=True, provider="cli_paced",
        ))
        snapshot = build_cost_rates(_backend(), window_hours=1.0)
        assert snapshot.total_calls == 1
        assert snapshot.total_cost_usd == 2.0


# ── /cost-vs-sidecar metering divergence (proposal 062 Step 5) ──────────────


class TestLogMeteringDivergence:
    """The thin cross-check helper: log past the threshold, never override."""

    def test_no_log_when_probe_reading_missing(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A /cost parse miss compares nothing — missing is not divergence."""
        with caplog.at_level("WARNING", logger="maestro.cost_tracking"):
            logged = log_metering_divergence(None, 1_000)
        assert logged is False
        assert caplog.records == []

    def test_no_log_when_sidecar_reading_missing(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No metered sidecar count → nothing to diverge from."""
        with caplog.at_level("WARNING", logger="maestro.cost_tracking"):
            logged = log_metering_divergence(1_000, None)
        assert logged is False
        assert caplog.records == []

    def test_no_log_on_agreement_within_threshold(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A gap at (not past) the default 10% threshold logs nothing."""
        with caplog.at_level("WARNING", logger="maestro.cost_tracking"):
            logged = log_metering_divergence(1_100, 1_000)
        assert logged is False
        assert caplog.records == []

    def test_logs_exactly_one_warning_past_threshold(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A gap past the threshold emits exactly one metering_divergence WARNING
        naming both readings and the caller."""
        with caplog.at_level("WARNING", logger="maestro.cost_tracking"):
            logged = log_metering_divergence(2_000, 1_000, caller="evolve")
        assert logged is True
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == "WARNING"
        assert "metering_divergence" in record.getMessage()
        assert "2000" in record.getMessage()
        assert "1000" in record.getMessage()
        assert "caller=evolve" in record.getMessage()

    def test_caller_defaults_to_unknown(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING", logger="maestro.cost_tracking"):
            logged = log_metering_divergence(2_000, 1_000)
        assert logged is True
        assert "caller=unknown" in caplog.records[0].getMessage()

    def test_zero_sidecar_agrees_with_zero_probe(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Two zero readings agree (no division, no log)."""
        with caplog.at_level("WARNING", logger="maestro.cost_tracking"):
            logged = log_metering_divergence(0, 0)
        assert logged is False
        assert caplog.records == []

    def test_zero_sidecar_diverges_from_nonzero_probe(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A zero sidecar total against a non-zero probe reading is an unbounded
        relative gap → one WARNING (and no ZeroDivisionError)."""
        with caplog.at_level("WARNING", logger="maestro.cost_tracking"):
            logged = log_metering_divergence(5, 0)
        assert logged is True
        assert len(caplog.records) == 1

    def test_custom_threshold_widens_agreement(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An explicit threshold overrides the module default."""
        with caplog.at_level("WARNING", logger="maestro.cost_tracking"):
            logged = log_metering_divergence(2_000, 1_000, threshold=1.5)
        assert logged is False
        assert caplog.records == []


class TestPlanHeadroom:
    """The read-only /usage plan-headroom signal type (proposal 062 Step 5)."""

    def test_defaults_are_honest_no_reading(self) -> None:
        headroom = PlanHeadroom()
        assert headroom.percent_used is None
        assert headroom.label is None

    def test_carries_parsed_reading(self) -> None:
        headroom = PlanHeadroom(percent_used=45.0, label="Weekly")
        assert headroom.percent_used == 45.0
        assert headroom.label == "Weekly"


USAGE_PATH = FAKE_DIR / "usage.jsonl"


def _usage_row(
    *,
    iteration: int,
    cost: float,
    started_at: str,
    providers: list[str] | None = None,
    unmetered: bool = False,
) -> dict[str, Any]:
    """Build a minimal usage.jsonl row (IterationUsage.to_dict shape)."""
    return {
        "iteration": iteration,
        "started_at": started_at,
        "calls": 1,
        "total_cost_usd": cost,
        "unmetered": unmetered,
        "providers": providers if providers is not None else ["anthropic"],
    }


def _write_usage(mem_fs: dict[str, str], rows: list[Any]) -> None:
    """Serialize *rows* (dicts JSON-encoded, str passed through) to USAGE_PATH."""
    lines = [r if isinstance(r, str) else json.dumps(r) for r in rows]
    mem_fs[str(USAGE_PATH)] = "\n".join(lines) + "\n"


class TestGenerateCostReport:
    """``generate_cost_report`` aggregates usage.jsonl for the operator."""

    def test_missing_file_returns_no_data(self, mem_fs: dict[str, str]) -> None:
        report = generate_cost_report(USAGE_PATH)
        assert report == "No usage data available"

    def test_empty_file_returns_no_data(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(USAGE_PATH)] = ""
        report = generate_cost_report(USAGE_PATH)
        assert report == "No usage data available"

    def test_aggregates_total_average_and_count(
        self, mem_fs: dict[str, str],
    ) -> None:
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        rows = [
            _usage_row(
                iteration=i,
                cost=0.10 * i,
                started_at=(now - timedelta(days=i)).isoformat(),
            )
            for i in range(1, 6)
        ]
        _write_usage(mem_fs, rows)

        report = generate_cost_report(USAGE_PATH, days=30, now=now)

        # 5 rows; total = 0.1*(1+2+3+4+5) = 1.5; average = 0.30.
        assert "Iterations processed: 5" in report
        assert "Total cost: $1.5000" in report
        assert "Average cost per iteration: $0.3000" in report
        assert "Malformed rows skipped: 0" in report
        assert "skipped" in report

    def test_per_date_breakdown_respects_window(
        self, mem_fs: dict[str, str],
    ) -> None:
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        rows = [
            _usage_row(
                iteration=1, cost=0.50,
                started_at=datetime(2026, 6, 14, 1, 0, tzinfo=UTC).isoformat(),
            ),
            _usage_row(
                iteration=2, cost=0.25,
                started_at=datetime(2026, 6, 13, 1, 0, tzinfo=UTC).isoformat(),
            ),
            # 40 days old — outside the 7-day window but still in totals.
            _usage_row(
                iteration=3, cost=9.00,
                started_at=datetime(2026, 5, 5, 1, 0, tzinfo=UTC).isoformat(),
            ),
        ]
        _write_usage(mem_fs, rows)

        report = generate_cost_report(USAGE_PATH, days=7, now=now)

        assert "  2026-06-14: $0.5000" in report
        assert "  2026-06-13: $0.2500" in report
        # The stale row is absent from the per-date section...
        assert "2026-05-05" not in report
        # ...but its cost is still in the all-rows total.
        assert "Total cost: $9.7500" in report

    def test_per_provider_breakdown(self, mem_fs: dict[str, str]) -> None:
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        rows = [
            _usage_row(
                iteration=1, cost=1.00, started_at=now.isoformat(),
                providers=["anthropic"],
            ),
            _usage_row(
                iteration=2, cost=2.00, started_at=now.isoformat(),
                providers=["cli_paced"],
            ),
            # No provider stamp → rolls up under "unknown".
            _usage_row(
                iteration=3, cost=0.50, started_at=now.isoformat(),
                providers=[],
            ),
        ]
        _write_usage(mem_fs, rows)

        report = generate_cost_report(USAGE_PATH, now=now)

        assert "  cli_paced: $2.0000" in report
        assert "  anthropic: $1.0000" in report
        assert "  unknown: $0.5000" in report

    def test_skips_malformed_row_and_reports_skip_count(
        self, mem_fs: dict[str, str],
    ) -> None:
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        rows: list[Any] = [
            _usage_row(iteration=1, cost=0.40, started_at=now.isoformat()),
            "{ this is not valid json",  # malformed line
            _usage_row(iteration=2, cost=0.60, started_at=now.isoformat()),
        ]
        _write_usage(mem_fs, rows)

        report = generate_cost_report(USAGE_PATH, now=now)

        assert "Iterations processed: 2" in report
        assert "Total cost: $1.0000" in report
        assert "Malformed rows skipped: 1" in report
        assert "skipped" in report

    def test_non_object_json_line_is_skipped(
        self, mem_fs: dict[str, str],
    ) -> None:
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        rows: list[Any] = [
            _usage_row(iteration=1, cost=0.40, started_at=now.isoformat()),
            "[1, 2, 3]",  # valid JSON but not an object
        ]
        _write_usage(mem_fs, rows)

        report = generate_cost_report(USAGE_PATH, now=now)

        assert "Iterations processed: 1" in report
        assert "Malformed rows skipped: 1" in report
        assert "skipped" in report

    def test_unparseable_started_at_excluded_from_dates_only(
        self, mem_fs: dict[str, str],
    ) -> None:
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        rows = [
            _usage_row(iteration=1, cost=0.30, started_at=""),
        ]
        _write_usage(mem_fs, rows)

        report = generate_cost_report(USAGE_PATH, now=now)

        # Counted in totals, but no date line and the window-empty marker shows.
        assert "Iterations processed: 1" in report
        assert "Total cost: $0.3000" in report
        assert "(no iterations in window)" in report

    def test_only_malformed_rows_surfaces_skip_count(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A non-empty file whose every row is unreadable must surface the
        skip count, not masquerade as the genuinely-empty no-data case
        (PR #2647 review thread, cost_tracking.py:698).
        """
        _write_usage(mem_fs, ["not json", "also not json"])
        report = generate_cost_report(USAGE_PATH)
        # Still anchored on the no-data sentinel for backward compatibility...
        assert report.startswith("No usage data available")
        # ...but the corruption is surfaced rather than silently swallowed.
        assert report != "No usage data available"
        assert "2 malformed rows skipped" in report

    def test_naive_started_at_treated_as_utc(
        self, mem_fs: dict[str, str],
    ) -> None:
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        rows = [
            # No tz offset → assumed UTC, lands on 2026-06-14.
            _usage_row(
                iteration=1, cost=0.20, started_at="2026-06-14T08:00:00",
            ),
        ]
        _write_usage(mem_fs, rows)

        report = generate_cost_report(USAGE_PATH, now=now)
        assert "  2026-06-14: $0.2000" in report

    def test_singular_day_label(self, mem_fs: dict[str, str]) -> None:
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        _write_usage(
            mem_fs,
            [_usage_row(iteration=1, cost=0.10, started_at=now.isoformat())],
        )
        report = generate_cost_report(USAGE_PATH, days=1, now=now)
        assert "Cost Report (last 1 day)" in report
        assert "last 1 days" not in report

    def test_blank_lines_are_skipped_not_counted(
        self, mem_fs: dict[str, str],
    ) -> None:
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        row = json.dumps(
            _usage_row(iteration=1, cost=0.10, started_at=now.isoformat()),
        )
        # Blank/whitespace-only lines surround the single valid row.
        mem_fs[str(USAGE_PATH)] = f"\n{row}\n   \n"

        report = generate_cost_report(USAGE_PATH, now=now)
        assert "Iterations processed: 1" in report
        assert "Malformed rows skipped: 0" in report
        assert "skipped" in report


class TestGenerateCostReportReviewConcerns:
    """Failing reproductions of PR #2647's unresolved review threads.

    Each test encodes the concern as the reviewer's suggested fix expects.
    All three fail against the current ``generate_cost_report`` and pass
    once the corresponding suggestion is applied.
    """

    def test_multi_provider_iteration_reconciles_with_total(
        self, mem_fs: dict[str, str],
    ) -> None:
        """🐛 cost_tracking.py:691 — a single iteration that names several
        providers must not add its *full* cost to each. The per-provider
        section must reconcile with the reported total (the invariant the
        docstring promises). The reviewer's fix splits the cost evenly
        across the participating providers (``cost / len(providers)``).

        Pre-fix the loop adds the full $1.00 to BOTH providers, so the
        per-provider sum is $2.00 and no longer reconciles with the total.
        """
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        _write_usage(
            mem_fs,
            [
                _usage_row(
                    iteration=1, cost=1.00, started_at=now.isoformat(),
                    providers=["anthropic", "cli_paced"],
                ),
            ],
        )

        report = generate_cost_report(USAGE_PATH, now=now)

        # One iteration, $1.00 — the total is unambiguous.
        assert "Total cost: $1.0000" in report
        # Even split keeps the per-provider section reconciled to the total.
        assert "  anthropic: $0.5000" in report
        assert "  cli_paced: $0.5000" in report

    def test_unmetered_iteration_contributes_zero_cost(
        self, mem_fs: dict[str, str],
    ) -> None:
        """🐛 cost_tracking.py:685 — an unmetered row's ``total_cost_usd`` is
        a lower bound, not a measured cost, so it must contribute 0, matching
        ``cost_per_task`` and the dashboard. The reviewer's fix is
        ``cost = 0.0 if usage.unmetered else usage.total_cost_usd``.

        Pre-fix the unmetered $5.00 is counted as real spend, inflating the
        total to $7.00 and the unmetered provider's figure to $5.00.
        """
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        _write_usage(
            mem_fs,
            [
                _usage_row(iteration=1, cost=2.00, started_at=now.isoformat()),
                _usage_row(
                    iteration=2, cost=5.00, started_at=now.isoformat(),
                    providers=["cli_paced"], unmetered=True,
                ),
            ],
        )

        report = generate_cost_report(USAGE_PATH, now=now)

        # Only the metered $2.00 is measured spend.
        assert "Total cost: $2.0000" in report
        # Both rows are still processed iterations...
        assert "Iterations processed: 2" in report
        # ...but the average reflects measured spend only ($2.00 / 2).
        assert "Average cost per iteration: $1.0000" in report
        # The unmetered provider contributes 0, not its lower-bound $5.00.
        assert "  cli_paced: $0.0000" in report

    def test_all_malformed_file_surfaces_skip_count(
        self, mem_fs: dict[str, str],
    ) -> None:
        """💡 cost_tracking.py:698 — a non-empty file whose every row is
        unreadable must report that rows were skipped, distinct from the
        genuinely empty/missing case. The reviewer's fix appends the skip
        count to the sentinel when ``iterations == 0`` but ``skip_count``
        is nonzero.

        Pre-fix this returns the bare ``No usage data available`` sentinel,
        hiding the corruption exactly when it should be surfaced.
        """
        # Two unparseable lines plus a valid-JSON-but-non-object line.
        _write_usage(mem_fs, ["{ not json", "still not json", "[1, 2, 3]"])

        report = generate_cost_report(USAGE_PATH)

        assert report.startswith("No usage data available")
        assert report != "No usage data available"
        assert "malformed" in report.lower()
        assert "3" in report

    def test_zero_spend_date_excluded_from_per_date_section(
        self, mem_fs: dict[str, str],
    ) -> None:
        """💡 cost_tracking.py:723 — acceptance criterion #1 limits the
        per-date section to dates with *nonzero* spend. An unmetered row
        forces its cost to 0.0, so a date whose only activity is unmetered
        must not render a misleading ``$0.0000`` line presenting
        unknown/unmetered spend as measured-zero spend. The reviewer's fix
        adds a ``cost > 0`` guard before recording the by_date entry.

        Pre-fix the unmetered 2026-06-13 row produces ``2026-06-13: $0.0000``.
        """
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        _write_usage(
            mem_fs,
            [
                _usage_row(
                    iteration=1, cost=2.00,
                    started_at=datetime(2026, 6, 14, 1, 0, tzinfo=UTC).isoformat(),
                ),
                # Unmetered → cost forced to 0.0; its date must not appear.
                _usage_row(
                    iteration=2, cost=5.00,
                    started_at=datetime(2026, 6, 13, 1, 0, tzinfo=UTC).isoformat(),
                    providers=["cli_paced"], unmetered=True,
                ),
            ],
        )

        report = generate_cost_report(USAGE_PATH, days=7, now=now)

        # The metered date renders; the zero-spend date is suppressed.
        assert "  2026-06-14: $2.0000" in report
        assert "2026-06-13" not in report

    def test_window_cutoff_floors_to_midnight_of_earliest_day(
        self, mem_fs: dict[str, str],
    ) -> None:
        """✏️ cost_tracking.py:676 — the per-date window is a 'last N days'
        span, so the cutoff floors to midnight of the earliest in-window day.
        This lists exactly N calendar dates (not N+1) and reflects each
        date's full-day spend. The reviewer's fix is
        ``(reference - timedelta(days=days - 1)).replace(hour=0, ...)``.

        With days=2 anchored at 2026-06-14 12:00 the window is the two dates
        2026-06-13 and 2026-06-14. A row at 2026-06-12 18:00 sits inside the
        old rolling cutoff (2026-06-12 12:00) but outside the floored one, so
        pre-fix it leaks in as a spurious third calendar date.
        """
        now = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        _write_usage(
            mem_fs,
            [
                _usage_row(
                    iteration=1, cost=0.50,
                    started_at=datetime(2026, 6, 14, 9, 0, tzinfo=UTC).isoformat(),
                ),
                _usage_row(
                    iteration=2, cost=0.25,
                    started_at=datetime(2026, 6, 13, 9, 0, tzinfo=UTC).isoformat(),
                ),
                # Inside the old rolling 2*24h cutoff (06-12 12:00) but before
                # the floored midnight cutoff (06-13 00:00) → must be excluded.
                _usage_row(
                    iteration=3, cost=0.40,
                    started_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC).isoformat(),
                ),
            ],
        )

        report = generate_cost_report(USAGE_PATH, days=2, now=now)

        assert "  2026-06-14: $0.5000" in report
        assert "  2026-06-13: $0.2500" in report
        # The pre-cutoff date does not leak in as an N+1 calendar date...
        assert "2026-06-12" not in report
        # ...but its cost still rolls into the all-rows total.
        assert "Total cost: $1.1500" in report

    def test_existing_but_unreadable_file_returns_graceful_message(
        self, mem_fs: dict[str, str],
    ) -> None:
        """💡 cost_tracking.py:674 — once the file exists, ``open()`` was
        unguarded, so a ``PermissionError`` / ``IsADirectoryError`` (plausible
        under the documented bind-mount permission issues) escaped as a
        traceback, breaking the CLI's documented 'never raises' contract. The
        reviewer's fix guards the open and returns a graceful message, mirroring
        ``read_iteration_usage``'s OSError convention.

        Pre-fix this raises ``PermissionError`` straight to the operator.
        """
        # The file exists (so the ``exists()`` guard passes) but cannot be
        # opened — simulate the permission/directory failure at ``open()``.
        _write_usage(
            mem_fs,
            [_usage_row(
                iteration=1, cost=1.0, started_at="2026-06-14T12:00:00+00:00",
            )],
        )

        def _raise_open(*_args: Any, **_kwargs: Any) -> Any:
            raise PermissionError("denied")

        # Patch ``open`` with a context manager (not ``monkeypatch``) so it is
        # torn down *before* the ``mem_fs`` fixture. ``mem_fs`` installs its own
        # fake ``open`` via ``with patch(...)``; layering a ``monkeypatch``
        # override on top inverts the teardown order, leaving ``mem_fs``'s fake
        # ``open`` reinstalled globally after this test and silently routing
        # every later test's file *writes* into an orphaned in-memory dict.
        with patch("builtins.open", side_effect=_raise_open):
            report = generate_cost_report(USAGE_PATH)

        assert report == "Usage log exists but could not be read"

    def test_corrupted_bytes_returns_unreadable(
        self, mem_fs: dict[str, str],
    ) -> None:
        """#4116 — text-mode ``open`` decodes lazily, so an invalid UTF-8 byte
        raises ``UnicodeDecodeError`` (a ``ValueError``, not ``OSError``) from
        the ``for raw in fh`` iteration, escaping the ``except OSError`` guard
        and crashing ``cost-report``. It must map to ``"unreadable"`` instead.
        """
        _write_usage(
            mem_fs,
            [_usage_row(
                iteration=1, cost=1.0, started_at="2026-06-14T12:00:00+00:00",
            )],
        )

        class _DecodeErrorHandle:
            """File-like whose lazy UTF-8 decode fails during iteration."""

            def __enter__(self) -> _DecodeErrorHandle:
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def __iter__(self) -> Any:
                raise UnicodeDecodeError(
                    "utf-8", b"\xff", 0, 1, "invalid start byte",
                )

        def _decode_error_open(*_args: Any, **_kwargs: Any) -> Any:
            return _DecodeErrorHandle()

        # Context-manager patch (not ``monkeypatch``) so it tears down before
        # ``mem_fs`` — see the teardown-order note in the test above.
        with patch("builtins.open", side_effect=_decode_error_open):
            report = generate_cost_report(USAGE_PATH)
            data = generate_cost_report_data(USAGE_PATH)

        assert report == "Usage log exists but could not be read"
        assert data["status"] == "unreadable"


# Burn-rate baseline corpora paths (proposal 084 Phase 1).
BASELINE_USAGE_PATH = FAKE_DIR / "usage.jsonl"
BASELINE_TASKS_PATH = FAKE_DIR / "task_history.jsonl"


def _write_jsonl(
    mem_fs: dict[str, str], path: Path, rows: list[dict[str, Any]],
) -> None:
    """Serialise *rows* as JSONL into the in-memory fs at *path*."""
    mem_fs[str(path)] = "\n".join(json.dumps(r) for r in rows) + "\n"


class TestCostPerTaskShape:
    """Tests for :func:`cost_per_task_shape`."""

    def test_buckets_by_shape(self) -> None:
        # "remediate" -> remediation; "investigate" -> investigation.
        usage = [
            {"task_id": "a", "total_cost_usd": 2.0},
            {"task_id": "b", "total_cost_usd": 1.0},
        ]
        tasks = [
            {"task_id": "a", "goal": "remediate the broken merge gate"},
            {"task_id": "b", "goal": "investigate the cache hit rate"},
        ]
        result = cost_per_task_shape(usage, tasks)
        assert result["remediation"].task_count == 1
        assert result["remediation"].total_cost_usd == pytest.approx(2.0)
        assert result["remediation"].avg_cost_per_task_usd == pytest.approx(2.0)
        assert result["investigation"].task_count == 1

    def test_sums_and_averages_within_shape(self) -> None:
        usage = [
            {"task_id": "a", "total_cost_usd": 3.0},
            {"task_id": "b", "total_cost_usd": 1.0},
        ]
        tasks = [
            {"task_id": "a", "goal": "remediate the gate"},
            {"task_id": "b", "goal": "remediate the other gate"},
        ]
        result = cost_per_task_shape(usage, tasks)
        bucket = result["remediation"]
        assert bucket.task_count == 2
        assert bucket.total_cost_usd == pytest.approx(4.0)
        assert bucket.avg_cost_per_task_usd == pytest.approx(2.0)

    def test_none_task_id_excluded(self) -> None:
        usage = [{"task_id": None, "total_cost_usd": 5.0}]
        tasks = [{"task_id": "a", "goal": "fix it"}]
        assert cost_per_task_shape(usage, tasks) == {}

    def test_task_without_history_goal_excluded(self) -> None:
        usage = [{"task_id": "orphan", "total_cost_usd": 5.0}]
        tasks: list[dict[str, Any]] = []
        assert cost_per_task_shape(usage, tasks) == {}

    def test_non_string_task_id_in_history_skipped(self) -> None:
        usage = [{"task_id": "a", "total_cost_usd": 1.0}]
        # A non-string and an empty task_id row are both skipped when building
        # the goal map, so "a" stays unmatched and is excluded.
        tasks = [
            {"task_id": 42, "goal": "fix it"},
            {"task_id": "", "goal": "fix it"},
        ]
        assert cost_per_task_shape(usage, tasks) == {}

    def test_non_string_goal_treated_as_other(self) -> None:
        usage = [{"task_id": "a", "total_cost_usd": 1.0}]
        tasks = [{"task_id": "a", "goal": None}]
        result = cost_per_task_shape(usage, tasks)
        assert "other" in result
        assert result["other"].task_count == 1

    def test_later_goal_wins(self) -> None:
        usage = [{"task_id": "a", "total_cost_usd": 1.0}]
        # Two history rows for the same task; the later goal classifies it.
        tasks = [
            {"task_id": "a", "goal": "investigate something"},
            {"task_id": "a", "goal": "remediate the regression"},
        ]
        result = cost_per_task_shape(usage, tasks)
        assert "remediation" in result
        assert "investigation" not in result

    def test_unmetered_only_task_buckets_at_zero(self) -> None:
        # cost_per_task registers an unmetered task_id at 0.0, so it still
        # forms a bucket (count 1, total 0.0).
        usage = [{"task_id": "a", "total_cost_usd": 9.0, "unmetered": True}]
        tasks = [{"task_id": "a", "goal": "remediate the gate"}]
        result = cost_per_task_shape(usage, tasks)
        assert result["remediation"].task_count == 1
        assert result["remediation"].total_cost_usd == pytest.approx(0.0)

    def test_empty_inputs(self) -> None:
        assert cost_per_task_shape([], []) == {}


class TestAggregateCacheHitRate:
    """Tests for :func:`aggregate_cache_hit_rate`."""

    def test_basic_rate(self) -> None:
        rows = [{"cache_read_tokens": 75, "input_tokens": 25}]
        assert aggregate_cache_hit_rate(rows) == pytest.approx(0.75)

    def test_sums_multiple_rows(self) -> None:
        rows = [
            {"cache_read_tokens": 50, "input_tokens": 50},
            {"cache_read_tokens": 50, "input_tokens": 0},
        ]
        # (50 + 50) / (100 + 50) = 0.6667
        assert aggregate_cache_hit_rate(rows) == pytest.approx(100 / 150)

    def test_empty_returns_zero(self) -> None:
        assert aggregate_cache_hit_rate([]) == 0.0

    def test_zero_denominator_returns_zero(self) -> None:
        rows = [{"cache_read_tokens": 0, "input_tokens": 0}]
        assert aggregate_cache_hit_rate(rows) == 0.0

    def test_non_numeric_tokens_count_zero(self) -> None:
        rows = [
            {"cache_read_tokens": "x", "input_tokens": None},
            {"cache_read_tokens": 30, "input_tokens": 10},
        ]
        assert aggregate_cache_hit_rate(rows) == pytest.approx(0.75)


class TestAggregateCallerCounts:
    """Tests for :func:`aggregate_caller_counts`."""

    def test_sums_requested_callers_across_rows(self) -> None:
        rows = [
            {"per_caller": {"auto_fix": 2, "self_review": 1, "introspect": 9}},
            {"per_caller": {"auto_fix": 3, "self_review": 4}},
        ]
        result = aggregate_caller_counts(rows, ("auto_fix", "self_review"))
        # introspect is not requested, so it is not summed.
        assert result == {"auto_fix": 5, "self_review": 5}

    def test_requested_caller_absent_from_map_totals_zero(self) -> None:
        rows = [{"per_caller": {"auto_fix": 2}}]
        result = aggregate_caller_counts(rows, ("auto_fix", "self_review"))
        assert result == {"auto_fix": 2, "self_review": 0}

    def test_rows_without_per_caller_contribute_nothing(self) -> None:
        # Missing, None, and non-dict per_caller are all skipped defensively.
        rows: list[dict[str, Any]] = [
            {},
            {"per_caller": None},
            {"per_caller": [("auto_fix", 5)]},
            {"per_caller": {"auto_fix": 4}},
        ]
        result = aggregate_caller_counts(rows, ("auto_fix", "self_review"))
        assert result == {"auto_fix": 4, "self_review": 0}

    def test_non_int_counts_coerced_to_zero(self) -> None:
        rows = [
            {"per_caller": {"auto_fix": "x", "self_review": None}},
            {"per_caller": {"auto_fix": 3, "self_review": 2}},
        ]
        result = aggregate_caller_counts(rows, ("auto_fix", "self_review"))
        assert result == {"auto_fix": 3, "self_review": 2}

    def test_empty_rows_returns_zeroed_callers(self) -> None:
        assert aggregate_caller_counts([], ("auto_fix", "self_review")) == {
            "auto_fix": 0,
            "self_review": 0,
        }


class TestAggregateTaskQuality:
    """Tests for :func:`aggregate_task_quality`."""

    def test_sums_outcomes_and_replans_over_window(self) -> None:
        # Three attributable tasks; outcomes and replans sum across the window.
        usage = [
            {"task_id": "a", "total_cost_usd": 1.0},
            {"task_id": "b", "total_cost_usd": 2.0},
            {"task_id": "c", "total_cost_usd": 3.0},
        ]
        tasks = [
            {"task_id": "a", "outcome": "success", "replans": 0},
            {"task_id": "b", "outcome": "partial", "replans": 2},
            {"task_id": "c", "outcome": "success", "replans": 1},
        ]
        result = aggregate_task_quality(usage, tasks)
        assert result.task_count == 3
        assert result.outcome_counts == {
            "success": 2, "partial": 1, "failure": 0, "unknown": 0,
        }
        assert result.total_replans == 3
        assert result.avg_replans_per_task == 1.0

    def test_buckets_missing_and_unrecognised_outcome_as_unknown(self) -> None:
        usage = [
            {"task_id": "a", "total_cost_usd": 1.0},
            {"task_id": "b", "total_cost_usd": 1.0},
            {"task_id": "c", "total_cost_usd": 1.0},
        ]
        tasks = [
            {"task_id": "a"},  # no outcome key
            {"task_id": "b", "outcome": "investigation"},  # not a valid literal
            {"task_id": "c", "outcome": "failure"},
        ]
        result = aggregate_task_quality(usage, tasks)
        assert result.outcome_counts == {
            "success": 0, "partial": 0, "failure": 1, "unknown": 2,
        }

    def test_coerces_non_int_replans_to_zero(self) -> None:
        usage = [{"task_id": "a", "total_cost_usd": 1.0}]
        tasks = [{"task_id": "a", "outcome": "success", "replans": "oops"}]
        result = aggregate_task_quality(usage, tasks)
        assert result.total_replans == 0
        assert result.avg_replans_per_task == 0.0

    def test_empty_input_returns_zeros(self) -> None:
        result = aggregate_task_quality([], [])
        assert result.task_count == 0
        assert result.outcome_counts == {
            "success": 0, "partial": 0, "failure": 0, "unknown": 0,
        }
        assert result.total_replans == 0
        assert result.avg_replans_per_task == 0.0

    def test_excludes_none_task_id_and_tasks_absent_from_history(self) -> None:
        # The ``None`` overhead key (a usage row with no task_id) and a costed
        # task with no matching task record are both excluded from the count.
        usage = [
            {"total_cost_usd": 5.0},  # -> None task_id (loop overhead)
            {"task_id": "a", "total_cost_usd": 1.0},
            {"task_id": "orphan", "total_cost_usd": 2.0},  # no task record
        ]
        tasks = [
            {"task_id": "a", "outcome": "success", "replans": 1},
            {"task_id": "", "outcome": "partial"},  # empty tid guard-excluded
            {"task_id": 123, "outcome": "failure"},  # non-str tid guard-excluded
        ]
        result = aggregate_task_quality(usage, tasks)
        assert result.task_count == 1
        assert result.outcome_counts["success"] == 1
        assert result.total_replans == 1

    def test_task_count_equals_shape_table_all_rollup(self) -> None:
        # The quality window is the same ``usage ∩ task_history`` population as
        # the shape table, so its task_count matches the ``all`` rollup count.
        usage = [
            {"task_id": "a", "total_cost_usd": 1.0},
            {"task_id": "b", "total_cost_usd": 2.0},
            {"total_cost_usd": 9.0},  # None overhead — excluded from both
        ]
        tasks = [
            {"task_id": "a", "goal": "remediate the gate", "outcome": "success"},
            {"task_id": "b", "goal": "investigate the loop", "outcome": "partial"},
        ]
        shapes = cost_per_task_shape(usage, tasks)
        rollup = sum(sc.task_count for sc in shapes.values())
        quality = aggregate_task_quality(usage, tasks)
        assert quality.task_count == rollup

    def test_last_record_wins_for_duplicate_task_id(self) -> None:
        # A later task_history row for the same task_id overrides the earlier
        # one, mirroring cost_per_task_shape's last-wins join.
        usage = [{"task_id": "a", "total_cost_usd": 1.0}]
        tasks = [
            {"task_id": "a", "outcome": "partial", "replans": 5},
            {"task_id": "a", "outcome": "success", "replans": 1},
        ]
        result = aggregate_task_quality(usage, tasks)
        assert result.task_count == 1
        assert result.outcome_counts["success"] == 1
        assert result.outcome_counts["partial"] == 0
        assert result.total_replans == 1


class TestTaskQualityFrozen:
    """TaskQuality is an immutable value object."""

    def test_is_frozen(self) -> None:
        tq = TaskQuality(
            task_count=1,
            outcome_counts={"success": 1, "partial": 0, "failure": 0,
                            "unknown": 0},
            total_replans=0,
            avg_replans_per_task=0.0,
        )
        with pytest.raises(AttributeError):
            tq.task_count = 2  # type: ignore[misc]


class TestFormatBurnBaseline:
    """Tests for :func:`format_burn_baseline`."""

    def test_with_data_renders_rows_rollup_and_cache(self) -> None:
        usage = [
            {
                "task_id": "a", "total_cost_usd": 3.0,
                "cache_read_tokens": 90, "input_tokens": 10,
                "per_caller": {"auto_fix": 2, "self_review": 1},
            },
            {
                "task_id": "b", "total_cost_usd": 1.0,
                "cache_read_tokens": 0, "input_tokens": 0,
                "per_caller": {"auto_fix": 1, "self_review": 3},
            },
        ]
        tasks = [
            {"task_id": "a", "goal": "remediate the gate",
             "outcome": "success", "replans": 1},
            {"task_id": "b", "goal": "investigate the loop",
             "outcome": "partial", "replans": 3},
        ]
        out = format_burn_baseline(usage, tasks, captured_at="2026-06-20")
        assert "## Burn-rate baseline (captured 2026-06-20)" in out
        # Highest-spend shape (remediation, $3) sorts before investigation.
        assert out.index("| remediation |") < out.index("| investigation |")
        # Rollup row sums both tasks: total $4 over 2 tasks -> $2.0000.
        assert "| **all** | 2 | $4.0000 | $2.0000 |" in out
        assert "Prompt-cache hit rate: 90.0%" in out
        # Churn line sums per_caller across both rows: auto_fix 3, self_review 4.
        assert (
            "Implementation churn (window): "
            "auto_fix calls = 3, self_review calls = 4"
        ) in out
        # Task-quality line: 2 tasks, 1 success + 1 partial, 4 replans total
        # (avg 2.00/task); no ``unknown`` segment since every outcome is valid.
        assert (
            "Task quality (window): 2 tasks — "
            "success 1, partial 1, failure 0; "
            "replans total 4 (avg 2.00/task)"
        ) in out
        assert "unknown" not in out

    def test_empty_renders_placeholder(self) -> None:
        out = format_burn_baseline([], [], captured_at="2026-06-20")
        assert "_(no task-attributable spend)_" in out
        assert "Prompt-cache hit rate: 0.0%" in out
        # With no rows, the churn line renders zeros rather than being omitted.
        assert (
            "Implementation churn (window): "
            "auto_fix calls = 0, self_review calls = 0"
        ) in out
        # The Task-quality line likewise renders zeros rather than being omitted.
        assert (
            "Task quality (window): 0 tasks — "
            "success 0, partial 0, failure 0; "
            "replans total 0 (avg 0.00/task)"
        ) in out

    def test_unknown_segment_shown_only_when_nonzero(self) -> None:
        # A task with an unrecognised outcome surfaces the ``unknown`` segment,
        # inserted between ``failure`` and the ``;`` replans clause.
        usage = [{"task_id": "a", "total_cost_usd": 1.0}]
        tasks = [{"task_id": "a", "goal": "do a thing", "outcome": "weird"}]
        out = format_burn_baseline(usage, tasks, captured_at="2026-06-20")
        assert (
            "Task quality (window): 1 task — "
            "success 0, partial 0, failure 0, unknown 1; "
            "replans total 0 (avg 0.00/task)"
        ) in out

    def test_task_count_is_singularized(self) -> None:
        # The task-count noun agrees with the count: "1 task" (singular), but
        # "0 tasks" and "2 tasks" (plural). Guards the ledger-flagged nit.
        one = [{"task_id": "a", "total_cost_usd": 1.0}]
        one_task = [{"task_id": "a", "goal": "do a thing", "outcome": "success"}]
        out_one = format_burn_baseline(one, one_task, captured_at="2026-06-20")
        assert "Task quality (window): 1 task — " in out_one
        assert "1 tasks" not in out_one

        two = [
            {"task_id": "a", "total_cost_usd": 1.0},
            {"task_id": "b", "total_cost_usd": 1.0},
        ]
        two_tasks = [
            {"task_id": "a", "goal": "do a thing", "outcome": "success"},
            {"task_id": "b", "goal": "do another", "outcome": "partial"},
        ]
        out_two = format_burn_baseline(two, two_tasks, captured_at="2026-06-20")
        assert "Task quality (window): 2 tasks — " in out_two

        out_zero = format_burn_baseline([], [], captured_at="2026-06-20")
        assert "Task quality (window): 0 tasks — " in out_zero


class TestFilterRecordsSince:
    """Tests for :func:`_filter_records_since` (the ``--since`` cutoff)."""

    def test_includes_at_or_after_cutoff(self) -> None:
        # The cutoff is inclusive: a row stamped exactly at *since* survives,
        # one stamped a second earlier is dropped, one after survives.
        cutoff = datetime(2026, 6, 21, tzinfo=UTC)
        records: list[dict[str, object]] = [
            {"task_id": "before", "started_at": "2026-06-20T23:59:59+00:00"},
            {"task_id": "at", "started_at": "2026-06-21T00:00:00+00:00"},
            {"task_id": "after", "started_at": "2026-06-22T00:00:00+00:00"},
        ]
        kept = _filter_records_since(records, cutoff)
        assert [r["task_id"] for r in kept] == ["at", "after"]

    def test_excludes_missing_started_at(self) -> None:
        # Every row that cannot be dated against the cutoff is dropped:
        # missing field, non-string value, unparseable string, and a naive
        # timestamp that cannot be compared to the aware cutoff. Only the
        # well-formed aware row at/after the cutoff survives.
        cutoff = datetime(2026, 6, 21, tzinfo=UTC)
        records: list[dict[str, object]] = [
            {"task_id": "missing"},
            {"task_id": "nonstring", "started_at": 12345},
            {"task_id": "unparseable", "started_at": "not-a-date"},
            {"task_id": "naive", "started_at": "2026-06-22T00:00:00"},
            {"task_id": "good", "started_at": "2026-06-22T00:00:00+00:00"},
        ]
        kept = _filter_records_since(records, cutoff)
        assert [r["task_id"] for r in kept] == ["good"]


class TestSplitRecordsAt:
    """Tests for :func:`_split_records_at` (the ``--compare`` partition)."""

    def test_partitions_about_inclusive_cutoff(self) -> None:
        # The cutoff is inclusive on the after side: a row stamped exactly at
        # *cutoff* joins ``at_or_after``, one a second earlier joins ``before``,
        # one after joins ``at_or_after``.
        cutoff = datetime(2026, 6, 21, tzinfo=UTC)
        records: list[dict[str, object]] = [
            {"task_id": "before", "started_at": "2026-06-20T23:59:59+00:00"},
            {"task_id": "at", "started_at": "2026-06-21T00:00:00+00:00"},
            {"task_id": "after", "started_at": "2026-06-22T00:00:00+00:00"},
        ]
        before, at_or_after = _split_records_at(records, cutoff)
        assert [r["task_id"] for r in before] == ["before"]
        assert [r["task_id"] for r in at_or_after] == ["at", "after"]

    def test_drops_undatable_rows_from_both_windows(self) -> None:
        # Every row that cannot be dated against the cutoff is dropped from both
        # sides: missing field, non-string value, unparseable string, and a
        # naive timestamp that cannot be compared to the aware cutoff. Only the
        # two well-formed aware rows land in their respective windows.
        cutoff = datetime(2026, 6, 21, tzinfo=UTC)
        records: list[dict[str, object]] = [
            {"task_id": "missing"},
            {"task_id": "nonstring", "started_at": 12345},
            {"task_id": "unparseable", "started_at": "not-a-date"},
            {"task_id": "naive", "started_at": "2026-06-22T00:00:00"},
            {"task_id": "old", "started_at": "2026-06-20T00:00:00+00:00"},
            {"task_id": "new", "started_at": "2026-06-22T00:00:00+00:00"},
        ]
        before, at_or_after = _split_records_at(records, cutoff)
        assert [r["task_id"] for r in before] == ["old"]
        assert [r["task_id"] for r in at_or_after] == ["new"]

    def test_empty_input_yields_two_empty_lists(self) -> None:
        before, at_or_after = _split_records_at([], datetime(2026, 6, 21, tzinfo=UTC))
        assert before == []
        assert at_or_after == []


class TestFormatBurnComparison:
    """Tests for :func:`format_burn_comparison` (the ``--compare`` renderer)."""

    _CUTOFF = datetime(2026, 6, 21, tzinfo=UTC)

    def test_renders_signed_deltas_with_arrows(self) -> None:
        # Before: one remediation task at $4, 50% cache, auto_fix=2/self_review=1,
        # success, 2 replans. After: one remediation task at $1, 90% cache,
        # auto_fix=5/self_review=3, partial, 4 replans. Every signal moves, so
        # the Δ column exercises ↑ (increase), ↓ (decrease), and → (unchanged).
        before_usage = [{
            "task_id": "b1", "total_cost_usd": 4.0,
            "cache_read_tokens": 50, "input_tokens": 50,
            "per_caller": {"auto_fix": 2, "self_review": 1},
        }]
        before_tasks = [{
            "task_id": "b1", "goal": "remediate the gate",
            "outcome": "success", "replans": 2,
        }]
        after_usage = [{
            "task_id": "a1", "total_cost_usd": 1.0,
            "cache_read_tokens": 90, "input_tokens": 10,
            "per_caller": {"auto_fix": 5, "self_review": 3},
        }]
        after_tasks = [{
            "task_id": "a1", "goal": "remediate the other gate",
            "outcome": "partial", "replans": 4,
        }]
        out = format_burn_comparison(
            before_usage, before_tasks, after_usage, after_tasks,
            cutoff=self._CUTOFF,
        )
        assert "## Burn-rate before/after comparison (cutoff 2026-06-21T00:00:00+00:00)" in out
        assert "| Signal | Before | After | Δ |" in out
        # Decreasing cost -> ↓; an absent shape reads $0.0000 with a → delta.
        # Each $/task cell carries its window sample size as ``(n=C)``.
        assert "| remediation $/task | $4.0000 (n=1) | $1.0000 (n=1) | ↓ -$3.0000 |" in out
        assert "| construction $/task | $0.0000 (n=0) | $0.0000 (n=0) | → +$0.0000 |" in out
        assert "| module_extension $/task | $0.0000 (n=0) | $0.0000 (n=0) | → +$0.0000 |" in out
        assert "| **all** $/task | $4.0000 (n=1) | $1.0000 (n=1) | ↓ -$3.0000 |" in out
        # Increasing cache -> ↑ in percentage points.
        assert "| Prompt-cache hit rate | 50.0% | 90.0% | ↑ +40.0pp |" in out
        assert "| auto_fix calls | 2 | 5 | ↑ +3 |" in out
        assert "| self_review calls | 1 | 3 | ↑ +2 |" in out
        assert "| Tasks (window) | 1 | 1 | → +0 |" in out
        assert "| success | 1 | 0 | ↓ -1 |" in out
        assert "| partial | 0 | 1 | ↑ +1 |" in out
        assert "| failure | 0 | 0 | → +0 |" in out
        assert "| avg replans/task | 2.00 | 4.00 | ↑ +2.00 |" in out
        # No side carries an unrecognised outcome, so no unknown row appears.
        assert "| unknown |" not in out

    def test_empty_windows_render_zeros(self) -> None:
        # Both windows empty: every signal reads zero and the call never raises.
        out = format_burn_comparison([], [], [], [], cutoff=self._CUTOFF)
        assert "| **all** $/task | $0.0000 (n=0) | $0.0000 (n=0) | → +$0.0000 |" in out
        assert "| Prompt-cache hit rate | 0.0% | 0.0% | → +0.0pp |" in out
        assert "| auto_fix calls | 0 | 0 | → +0 |" in out
        assert "| Tasks (window) | 0 | 0 | → +0 |" in out
        assert "| avg replans/task | 0.00 | 0.00 | → +0.00 |" in out
        assert "| unknown |" not in out

    def test_unknown_row_and_negative_deltas(self) -> None:
        # Before: 90% cache, 3 replans. After: 50% cache, 1 replan, and an
        # unrecognised outcome. This exercises the ↓ cache/replan branches and
        # the conditional unknown row (present because the after side has one).
        before_usage = [{
            "task_id": "b1", "total_cost_usd": 2.0,
            "cache_read_tokens": 90, "input_tokens": 10,
        }]
        before_tasks = [{
            "task_id": "b1", "goal": "remediate it",
            "outcome": "success", "replans": 3,
        }]
        after_usage = [{
            "task_id": "a1", "total_cost_usd": 1.0,
            "cache_read_tokens": 50, "input_tokens": 50,
        }]
        after_tasks = [{
            "task_id": "a1", "goal": "remediate it again",
            "outcome": "weird", "replans": 1,
        }]
        out = format_burn_comparison(
            before_usage, before_tasks, after_usage, after_tasks,
            cutoff=self._CUTOFF,
        )
        assert "| Prompt-cache hit rate | 90.0% | 50.0% | ↓ -40.0pp |" in out
        assert "| avg replans/task | 3.00 | 1.00 | ↓ -2.00 |" in out
        assert "| unknown | 0 | 1 | ↑ +1 |" in out

    def test_cost_rows_carry_per_window_sample_sizes(self) -> None:
        # A $/task delta is only meaningful weighted by sample size, so every
        # cost cell carries its own window task count as ``(n=C)`` — read
        # independently per side. Before: 1 remediation task; after: 3
        # remediation tasks. The remediation row must show n=1 before, n=3
        # after, and the ``all`` rollup the same (both windows are single-shape).
        before_usage = [{"task_id": "b1", "total_cost_usd": 4.0}]
        before_tasks = [{"task_id": "b1", "goal": "remediate one", "outcome": "success"}]
        after_usage = [
            {"task_id": f"a{i}", "total_cost_usd": 1.0} for i in range(3)
        ]
        after_tasks = [
            {"task_id": f"a{i}", "goal": f"remediate {i}", "outcome": "success"}
            for i in range(3)
        ]
        out = format_burn_comparison(
            before_usage, before_tasks, after_usage, after_tasks,
            cutoff=self._CUTOFF,
        )
        # remediation avg is $4/1 = $4 before, $3/3 = $1 after; the counts
        # differ per side, which is exactly the sample-size context r6 adds.
        assert "| remediation $/task | $4.0000 (n=1) | $1.0000 (n=3) | ↓ -$3.0000 |" in out
        assert "| **all** $/task | $4.0000 (n=1) | $1.0000 (n=3) | ↓ -$3.0000 |" in out
        # A shape with no task in either window still reads n=0 both sides.
        assert "| construction $/task | $0.0000 (n=0) | $0.0000 (n=0) | → +$0.0000 |" in out


class TestGenerateBurnBaseline:
    """Tests for :func:`generate_burn_baseline` and ``_read_jsonl_dicts``."""

    def test_since_filters_usage_records(self, mem_fs: dict[str, str]) -> None:
        """``since=`` drops pre-cutoff usage rows before the table is built.

        With an empty ``task_history.jsonl`` the shape table is the
        placeholder, so the observable signal of the filter is the aggregate
        prompt-cache hit rate: the pre-cutoff row (0/100) and post-cutoff row
        (90/10) yield 45.0% together but 90.0% when only the post-cutoff row
        survives the ``since`` cutoff.
        """
        _write_jsonl(mem_fs, BASELINE_USAGE_PATH, [
            {
                "task_id": "before",
                "started_at": "2026-06-20T00:00:00+00:00",
                "total_cost_usd": 1.0,
                "cache_read_tokens": 0, "input_tokens": 100,
            },
            {
                "task_id": "after",
                "started_at": "2026-06-22T00:00:00+00:00",
                "total_cost_usd": 2.0,
                "cache_read_tokens": 90, "input_tokens": 10,
            },
        ])
        _write_jsonl(mem_fs, BASELINE_TASKS_PATH, [])
        out = generate_burn_baseline(
            BASELINE_USAGE_PATH, BASELINE_TASKS_PATH,
            now=datetime(2026, 6, 23, tzinfo=UTC),
            since=datetime(2026, 6, 21, tzinfo=UTC),
        )
        assert "Prompt-cache hit rate: 90.0%" in out
        assert "45.0%" not in out

    def test_missing_files_render_empty_baseline(
        self, mem_fs: dict[str, str],
    ) -> None:
        now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
        out = generate_burn_baseline(
            BASELINE_USAGE_PATH, BASELINE_TASKS_PATH, now=now,
        )
        assert "captured 2026-06-20T12:00:00+00:00" in out
        assert "_(no task-attributable spend)_" in out

    def test_default_now_used_when_omitted(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Omitting ``now`` exercises the ``datetime.now(UTC)`` default; the
        # header renders and the call does not raise.
        out = generate_burn_baseline(BASELINE_USAGE_PATH, BASELINE_TASKS_PATH)
        assert "## Burn-rate baseline (captured " in out

    def test_reads_and_joins_corpora(self, mem_fs: dict[str, str]) -> None:
        _write_jsonl(mem_fs, BASELINE_USAGE_PATH, [
            {
                "task_id": "a", "total_cost_usd": 2.0,
                "cache_read_tokens": 80, "input_tokens": 20,
                "per_caller": {"auto_fix": 6, "self_review": 2},
            },
        ])
        _write_jsonl(mem_fs, BASELINE_TASKS_PATH, [
            {"task_id": "a", "goal": "remediate the broken gate"},
        ])
        now = datetime(2026, 6, 20, tzinfo=UTC)
        out = generate_burn_baseline(
            BASELINE_USAGE_PATH, BASELINE_TASKS_PATH, now=now,
        )
        assert "| remediation | 1 | $2.0000 | $2.0000 |" in out
        assert "Prompt-cache hit rate: 80.0%" in out
        # The churn line reads the persisted per_caller map end-to-end.
        assert (
            "Implementation churn (window): "
            "auto_fix calls = 6, self_review calls = 2"
        ) in out

    def test_since_window_narrows_churn_counts(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The churn line inherits the ``--since`` cutoff for free: the pre-cutoff
        # row's per_caller counts are dropped, leaving only the post-cutoff row.
        _write_jsonl(mem_fs, BASELINE_USAGE_PATH, [
            {
                "task_id": "before",
                "started_at": "2026-06-20T00:00:00+00:00",
                "total_cost_usd": 1.0,
                "per_caller": {"auto_fix": 9, "self_review": 9},
            },
            {
                "task_id": "after",
                "started_at": "2026-06-22T00:00:00+00:00",
                "total_cost_usd": 2.0,
                "per_caller": {"auto_fix": 3, "self_review": 1},
            },
        ])
        _write_jsonl(mem_fs, BASELINE_TASKS_PATH, [])
        out = generate_burn_baseline(
            BASELINE_USAGE_PATH, BASELINE_TASKS_PATH,
            now=datetime(2026, 6, 23, tzinfo=UTC),
            since=datetime(2026, 6, 21, tzinfo=UTC),
        )
        assert (
            "Implementation churn (window): "
            "auto_fix calls = 3, self_review calls = 1"
        ) in out

    def test_task_quality_line_reads_persisted_fields(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The Task-quality line reads the persisted ``outcome``/``replans``
        # fields off task_history.jsonl end-to-end.
        _write_jsonl(mem_fs, BASELINE_USAGE_PATH, [
            {"task_id": "a", "total_cost_usd": 2.0},
            {"task_id": "b", "total_cost_usd": 1.0},
        ])
        _write_jsonl(mem_fs, BASELINE_TASKS_PATH, [
            {"task_id": "a", "goal": "remediate the gate",
             "outcome": "success", "replans": 2},
            {"task_id": "b", "goal": "investigate it",
             "outcome": "failure", "replans": 0},
        ])
        out = generate_burn_baseline(
            BASELINE_USAGE_PATH, BASELINE_TASKS_PATH,
            now=datetime(2026, 6, 20, tzinfo=UTC),
        )
        assert (
            "Task quality (window): 2 tasks — "
            "success 1, partial 0, failure 1; "
            "replans total 2 (avg 1.00/task)"
        ) in out

    def test_since_window_narrows_task_quality(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The Task-quality line inherits the ``--since`` cutoff via the usage
        # join: the pre-cutoff task's cost row drops out, so only the
        # post-cutoff task is counted (its 9 replans never enter the total).
        _write_jsonl(mem_fs, BASELINE_USAGE_PATH, [
            {"task_id": "before", "started_at": "2026-06-20T00:00:00+00:00",
             "total_cost_usd": 1.0},
            {"task_id": "after", "started_at": "2026-06-22T00:00:00+00:00",
             "total_cost_usd": 2.0},
        ])
        _write_jsonl(mem_fs, BASELINE_TASKS_PATH, [
            {"task_id": "before", "goal": "old work",
             "outcome": "failure", "replans": 9},
            {"task_id": "after", "goal": "new work",
             "outcome": "success", "replans": 1},
        ])
        out = generate_burn_baseline(
            BASELINE_USAGE_PATH, BASELINE_TASKS_PATH,
            now=datetime(2026, 6, 23, tzinfo=UTC),
            since=datetime(2026, 6, 21, tzinfo=UTC),
        )
        assert (
            "Task quality (window): 1 task — "
            "success 1, partial 0, failure 0; "
            "replans total 1 (avg 1.00/task)"
        ) in out
        assert "replans total 10" not in out

    def test_skips_blank_malformed_and_non_dict_lines(
        self, mem_fs: dict[str, str],
    ) -> None:
        good = json.dumps({
            "task_id": "a", "total_cost_usd": 1.0,
            "cache_read_tokens": 1, "input_tokens": 1,
        })
        # Blank line, malformed JSON, and a valid-JSON non-dict (a list) are
        # each skipped; only the good row survives.
        mem_fs[str(BASELINE_USAGE_PATH)] = f"\n{{bad json\n[1, 2, 3]\n{good}\n"
        _write_jsonl(mem_fs, BASELINE_TASKS_PATH, [
            {"task_id": "a", "goal": "remediate it"},
        ])
        out = generate_burn_baseline(
            BASELINE_USAGE_PATH, BASELINE_TASKS_PATH,
            now=datetime(2026, 6, 20, tzinfo=UTC),
        )
        assert "| remediation | 1 |" in out

    def test_unreadable_file_treated_as_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Both corpora exist so ``exists()`` passes, but ``open()`` raises —
        # _read_jsonl_dicts swallows the OSError and returns [].
        _write_jsonl(mem_fs, BASELINE_USAGE_PATH, [
            {"task_id": "a", "total_cost_usd": 1.0},
        ])
        _write_jsonl(mem_fs, BASELINE_TASKS_PATH, [
            {"task_id": "a", "goal": "fix it"},
        ])

        def _raise_open(*_args: Any, **_kwargs: Any) -> Any:
            raise PermissionError("denied")

        with patch("builtins.open", side_effect=_raise_open):
            out = generate_burn_baseline(
                BASELINE_USAGE_PATH, BASELINE_TASKS_PATH,
                now=datetime(2026, 6, 20, tzinfo=UTC),
            )
        assert "_(no task-attributable spend)_" in out

    def test_corrupted_bytes_treated_as_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Invalid UTF-8 bytes degrade to an empty baseline, not a crash.

        ``open(..., encoding="utf-8")`` defers decoding to iteration, so a bad
        byte raises ``UnicodeDecodeError`` (a ``ValueError`` subclass, *not*
        ``OSError``) from ``for raw in fh`` — escaping a bare ``except
        OSError``. ``_read_jsonl_dicts`` documents that an unreadable file
        "yields ``[]`` rather than raising", so the broadened
        ``except (OSError, ValueError)`` must swallow the decode error too.
        """
        _write_jsonl(mem_fs, BASELINE_USAGE_PATH, [
            {"task_id": "a", "total_cost_usd": 1.0},
        ])
        _write_jsonl(mem_fs, BASELINE_TASKS_PATH, [
            {"task_id": "a", "goal": "fix it"},
        ])

        class _DecodeErrorHandle:
            """File-like whose lazy UTF-8 decode fails during iteration."""

            def __enter__(self) -> _DecodeErrorHandle:
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def __iter__(self) -> Any:
                raise UnicodeDecodeError(
                    "utf-8", b"\xff", 0, 1, "invalid start byte",
                )

        def _decode_error_open(*_args: Any, **_kwargs: Any) -> Any:
            return _DecodeErrorHandle()

        with patch("builtins.open", side_effect=_decode_error_open):
            out = generate_burn_baseline(
                BASELINE_USAGE_PATH, BASELINE_TASKS_PATH,
                now=datetime(2026, 6, 20, tzinfo=UTC),
            )
        assert "_(no task-attributable spend)_" in out


class TestGenerateBurnComparison:
    """Tests for :func:`generate_burn_comparison` (the ``--compare`` end-to-end)."""

    def test_partitions_usage_and_joins_both_sides(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Usage splits at the cutoff; the task list feeds both sides.

        ``old`` (pre-cutoff, remediation, $4) lands in the before window;
        ``atcut`` (exactly at the cutoff, construction, $2) and ``new`` (after,
        construction, $1) land in the after window — so the after construction
        rollup is $3/2 = $1.5000, which only holds if the at-cutoff row joined
        the *after* side. ``ghost`` carries no ``started_at`` and is dropped
        from both windows, so its $99 never surfaces.
        """
        _write_jsonl(mem_fs, BASELINE_USAGE_PATH, [
            {"task_id": "old", "started_at": "2026-06-20T00:00:00+00:00",
             "total_cost_usd": 4.0},
            {"task_id": "atcut", "started_at": "2026-06-21T00:00:00+00:00",
             "total_cost_usd": 2.0},
            {"task_id": "new", "started_at": "2026-06-22T00:00:00+00:00",
             "total_cost_usd": 1.0},
            {"task_id": "ghost", "total_cost_usd": 99.0},
        ])
        _write_jsonl(mem_fs, BASELINE_TASKS_PATH, [
            {"task_id": "old", "goal": "remediate the gate",
             "outcome": "success", "replans": 1},
            {"task_id": "atcut", "goal": "create a new widget",
             "outcome": "success", "replans": 0},
            {"task_id": "new", "goal": "create another widget",
             "outcome": "success", "replans": 0},
            {"task_id": "ghost", "goal": "remediate the ghost",
             "outcome": "success", "replans": 0},
        ])
        out = generate_burn_comparison(
            BASELINE_USAGE_PATH, BASELINE_TASKS_PATH,
            cutoff=datetime(2026, 6, 21, tzinfo=UTC),
        )
        assert "cutoff 2026-06-21T00:00:00+00:00" in out
        # The after window holds two construction tasks (n=2) vs one remediation
        # before (n=1); the sample sizes ride each $/task cell.
        assert "| remediation $/task | $4.0000 (n=1) | $0.0000 (n=0) | ↓ -$4.0000 |" in out
        assert "| construction $/task | $0.0000 (n=0) | $1.5000 (n=2) | ↑ +$1.5000 |" in out
        assert "| **all** $/task | $4.0000 (n=1) | $1.5000 (n=2) | ↓ -$2.5000 |" in out
        # The undatable ``ghost`` row is dropped from both windows.
        assert "$99" not in out

    def test_missing_files_render_zero_comparison(
        self, mem_fs: dict[str, str],
    ) -> None:
        # Neither corpus exists, so both windows are empty and every signal
        # reads zero rather than raising.
        out = generate_burn_comparison(
            BASELINE_USAGE_PATH, BASELINE_TASKS_PATH,
            cutoff=datetime(2026, 6, 21, tzinfo=UTC),
        )
        assert "## Burn-rate before/after comparison (cutoff " in out
        assert "| **all** $/task | $0.0000 (n=0) | $0.0000 (n=0) | → +$0.0000 |" in out


class TestShapeCostFrozen:
    """ShapeCost is an immutable value object."""

    def test_is_frozen(self) -> None:
        sc = ShapeCost(
            shape="remediation", task_count=1,
            total_cost_usd=1.0, avg_cost_per_task_usd=1.0,
        )
        with pytest.raises(AttributeError):
            sc.shape = "other"  # type: ignore[misc]


# ── CostSampleBackend seam (proposal 106 Phase B Step 3) ────────────────────


def _sqlite_backend() -> SqliteCostSampleStore:
    """An in-memory SQLite cost-sample backend (never touches a real file)."""
    return SqliteCostSampleStore(StateStore(":memory:"))


class TestJsonlCostSampleStore:
    """The default flag-off backend wrapping ``cost_samples.jsonl``."""

    def test_append_delegates_to_module_writer(
        self, mem_fs: dict[str, str],
    ) -> None:
        backend = _backend()
        backend.append(_make_sample(cost=0.42, caller="introspect"))
        data = json.loads(mem_fs[str(SAMPLES_PATH)].strip())
        assert data["cost_usd"] == 0.42
        assert data["caller"] == "introspect"

    def test_samples_in_window_honours_now(self, mem_fs: dict[str, str]) -> None:
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        # Appended oldest-first (the real write order) so the JSONL reverse-walk
        # break at the cutoff is sound.
        append_cost_sample(SAMPLES_PATH, _make_sample(
            ts=now - timedelta(hours=5), cost=9.0,
        ))
        append_cost_sample(SAMPLES_PATH, _make_sample(
            ts=now - timedelta(minutes=30), cost=1.0,
        ))
        got = _backend().samples_in_window(1.0, now=now)
        assert [s.cost_usd for s in got] == [1.0]

    def test_window_cutoff_normalizes_naive_stamp(
        self, mem_fs: dict[str, str],
    ) -> None:
        # A naive stored stamp's window membership must be decided in UTC, not
        # host-local time. The two epochs coincide on a UTC host, so no value
        # assertion can distinguish the normalization there — the spy on the
        # delegation can, and trips if this site reverts to a bare
        # ``fromisoformat`` on any host.
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        stale = (now - timedelta(hours=5)).replace(tzinfo=None)
        fresh = (now - timedelta(minutes=30)).replace(tzinfo=None)
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=stale, cost=9.0))
        append_cost_sample(SAMPLES_PATH, _make_sample(ts=fresh, cost=1.0))

        with patch.object(cost_tracking, "_as_utc", wraps=_as_utc) as spy:
            got = _backend().samples_in_window(1.0, now=now)

        assert [s.cost_usd for s in got] == [1.0]
        # Reverse-walk order: the in-window row, then the row that breaks it.
        assert spy.call_args_list == [
            call(fresh.isoformat()),
            call(stale.isoformat()),
        ]

    def test_recent_samples_returns_tail_oldest_first(
        self, mem_fs: dict[str, str],
    ) -> None:
        for i in range(5):
            append_cost_sample(SAMPLES_PATH, _make_sample(cost=float(i)))
        got = _backend().recent_samples(3)
        assert [s.cost_usd for s in got] == [2.0, 3.0, 4.0]

    def test_recent_samples_non_positive_limit(
        self, mem_fs: dict[str, str],
    ) -> None:
        append_cost_sample(SAMPLES_PATH, _make_sample())
        assert _backend().recent_samples(0) == []

    def test_recent_samples_missing_file(self, mem_fs: dict[str, str]) -> None:
        assert JsonlCostSampleStore(Path("/fake/gone.jsonl")).recent_samples(5) == []

    def test_recent_samples_skips_blank_and_malformed(
        self, mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
    ) -> None:
        valid = json.dumps(asdict(_make_sample(cost=0.7, caller="dream")))
        mem_fs[str(SAMPLES_PATH)] = (
            f"{valid}\n"
            "\n"                      # blank → skipped
            "not json\n"             # bad JSON → skipped + warned
            "[1, 2, 3]\n"            # non-dict → skipped + warned
            '{"caller": "x"}\n'      # dict missing fields → skipped + warned
        )
        got = _backend().recent_samples(10)
        assert [s.cost_usd for s in got] == [0.7]

    def test_recent_samples_oserror_returns_empty(
        self, mem_fs: dict[str, str],
    ) -> None:
        import builtins

        mem_fs[str(SAMPLES_PATH)] = json.dumps(asdict(_make_sample())) + "\n"
        real_open = builtins.open

        def fail_open(file: object, mode: str = "r", **kwargs: object) -> object:
            if str(file) == str(SAMPLES_PATH) and "r" in mode:
                raise OSError("permission denied")
            return real_open(file, mode, **kwargs)  # type: ignore[call-overload]

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(builtins, "open", fail_open)
            assert _backend().recent_samples(10) == []

    def test_cache_key_is_path_and_mtime(self, mem_fs: dict[str, str]) -> None:
        append_cost_sample(SAMPLES_PATH, _make_sample())
        identity, version = _backend().cache_key()
        assert identity == str(SAMPLES_PATH)
        assert version == cost_tracking._stat_mtime_ns(SAMPLES_PATH)


class TestBackendParity:
    """JSONL and SQLite backends must be interchangeable over a shared fixture.

    Proves the flag-flip is behaviour-preserving before the JSONL path is ever
    conceded: identical windowed aggregation, identical recent-N output, and
    equal ``build_cost_rates`` / ``build_budget_breakdown`` snapshots from either
    substrate.
    """

    @staticmethod
    def _shared_samples(now: datetime) -> list[CostSample]:
        # Strictly ascending by timestamp (the real append order): the oldest,
        # out-of-window sample comes first so the JSONL reverse-walk's cutoff
        # break stays valid.
        return [
            # Out of a 1h window — both backends must drop it.
            _make_sample(ts=now - timedelta(hours=5), cost=9.0, caller="dream"),
            _make_sample(ts=now - timedelta(minutes=40), cost=1.0, caller="reflect"),
            _make_sample(ts=now - timedelta(minutes=30), cost=2.0, caller="introspect",
                         estimated=True, provider="anthropic"),
            _make_sample(ts=now - timedelta(minutes=20), cost=0.0, caller="cli_phase",
                         unmetered=True, provider="cli_paced"),
            _make_sample(ts=now - timedelta(minutes=10), cost=3.0, caller="reflect"),
        ]

    def _both(
        self, mem_fs: dict[str, str], now: datetime,
    ) -> tuple[JsonlCostSampleStore, SqliteCostSampleStore]:
        samples = self._shared_samples(now)
        jsonl = _backend()
        sqlite = _sqlite_backend()
        for s in samples:
            jsonl.append(s)
            sqlite.append(s)
        return jsonl, sqlite

    def test_samples_in_window_parity(self, mem_fs: dict[str, str]) -> None:
        now = datetime(2026, 3, 1, 9, 0, 0, tzinfo=UTC)
        jsonl, sqlite = self._both(mem_fs, now)
        assert jsonl.samples_in_window(1.0, now=now) == sqlite.samples_in_window(
            1.0, now=now,
        )

    def test_recent_samples_parity(self, mem_fs: dict[str, str]) -> None:
        now = datetime(2026, 3, 1, 9, 0, 0, tzinfo=UTC)
        jsonl, sqlite = self._both(mem_fs, now)
        assert jsonl.recent_samples(3) == sqlite.recent_samples(3)

    def test_build_cost_rates_parity(self, mem_fs: dict[str, str]) -> None:
        # A real-now window (build_cost_rates takes no ``now``); samples sit well
        # inside it so microsecond timing between the two calls cannot diverge.
        now = datetime.now(UTC)
        jsonl, sqlite = self._both(mem_fs, now)
        assert build_cost_rates(jsonl, 1.0) == build_cost_rates(sqlite, 1.0)

    def test_build_budget_breakdown_parity(self, mem_fs: dict[str, str]) -> None:
        now = datetime.now(UTC)
        jsonl, sqlite = self._both(mem_fs, now)
        assert build_budget_breakdown(jsonl, 1.0) == build_budget_breakdown(
            sqlite, 1.0,
        )

    def test_naive_timestamp_windowed_and_aggregated_identically(
        self, mem_fs: dict[str, str],
    ) -> None:
        # The SQLite backend derives ``ts_epoch`` at write time and never runs
        # the JSONL reader, so a reader-only normalization would leave a naive
        # row in a host-local epoch here — landing it in the wrong window on
        # any non-UTC host. Both substrates must agree.
        now = datetime.now(UTC)
        naive = _make_sample(
            ts=(now - timedelta(minutes=30)).replace(tzinfo=None),
            cost=1.0,
            caller="reflect",
        )
        aware = _make_sample(ts=now - timedelta(minutes=10), cost=2.0, caller="dream")

        jsonl = _backend()
        sqlite = _sqlite_backend()
        for s in (naive, aware):
            jsonl.append(s)
            sqlite.append(s)

        assert jsonl.samples_in_window(1.0, now=now) == sqlite.samples_in_window(
            1.0, now=now,
        )
        assert len(sqlite.samples_in_window(1.0, now=now)) == 2
        assert build_cost_rates(jsonl, 1.0) == build_cost_rates(sqlite, 1.0)
        assert build_budget_breakdown(jsonl, 1.0) == build_budget_breakdown(
            sqlite, 1.0,
        )


def test_count_metered_iterations_since_windows_and_drops_unmetered(
    mem_fs: dict[str, str],
) -> None:
    """Only dated, in-window, metered usage rows count; a missing log is 0."""
    path = Path("/tmp/net-burn-usage.jsonl")
    since = datetime(2026, 9, 1, tzinfo=UTC)
    assert cost_tracking.count_metered_iterations_since(path, since) == 0

    rows = [
        {"started_at": "2026-08-31T23:59:59+00:00"},
        {"started_at": "2026-09-01T00:00:00+00:00"},
        {"started_at": "2026-09-02T00:00:00+00:00", "unmetered": True},
        {"started_at": "2026-09-03T00:00:00"},
        {},
    ]
    mem_fs[str(path)] = "".join(json.dumps(r) + "\n" for r in rows)

    assert cost_tracking.count_metered_iterations_since(path, since) == 1
