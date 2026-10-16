"""Per-caller spend over a trailing window, normalised per day and per iteration.

Folds the already-recorded per-call spend (``cost_samples.jsonl`` via
:class:`~maestro.cost_tracking.CostSampleBackend`) and an injected loop-iteration
count into one :class:`SpendReading`. Normalising per *iteration* (not per
wall-clock day) is deliberate: standby or throttle sleep lowers spend per day
without making any iteration cheaper. Every per-iteration figure is ``None`` —
unknown, never zero — when no iterations were counted.

Pure assembly over :func:`~maestro.cost_tracking.build_cost_rates`: unmetered
samples are excluded exactly as the throttle excludes them. The window never
outreaches the hot files: :func:`effective_window_days` clamps it to the
archive threshold, and the reading carries the requested span alongside it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from maestro.cost_tracking import CALLER_GROUPS, CostSampleBackend, build_cost_rates

#: Provenance of ``spend_usd``: the SDK-reported per-call ``total_cost_usd``
#: recorded in the cost-sample store, not a provider invoice.
SPEND_SOURCE = "sdk_reported_total_cost_usd"

@dataclass(frozen=True)
class CallerWindowSpend:
    """One caller's spend within the reading's window."""

    caller: str
    group: str
    cost_usd: float
    calls: int
    usd_per_iteration: float | None


@dataclass(frozen=True)
class SpendReading:
    """Spend over a trailing window, normalised per day and per iteration."""

    window_days: int
    requested_window_days: int
    iterations: int
    spend_usd: float
    spend_usd_per_day: float
    spend_usd_per_iteration: float | None
    by_caller: tuple[CallerWindowSpend, ...]
    spend_source: str = SPEND_SOURCE

    def to_dict(self) -> dict[str, object]:
        return {
            "window_days": self.window_days,
            "requested_window_days": self.requested_window_days,
            "iterations": self.iterations,
            "spend_usd": self.spend_usd,
            "spend_usd_per_day": self.spend_usd_per_day,
            "spend_usd_per_iteration": self.spend_usd_per_iteration,
            "by_caller": [asdict(spend) for spend in self.by_caller],
            "spend_source": self.spend_source,
        }


def _per_iteration(amount: float, iterations: int) -> float | None:
    return amount / iterations if iterations > 0 else None


def effective_window_days(requested_days: int, archive_threshold_days: int) -> int:
    """Clamp *requested_days* to what the hot files can still cover.

    ``archive_threshold_days`` of ``0`` means archival is disabled, so nothing
    is clamped; otherwise rows older than the threshold have been rotated cold.
    """
    if archive_threshold_days > 0:
        return min(requested_days, archive_threshold_days)
    return requested_days


def read_spend(
    backend: CostSampleBackend,
    *,
    window_days: int,
    iterations: int,
    requested_window_days: int | None = None,
) -> SpendReading:
    """Assemble the spend reading over the trailing *window_days*.

    ``by_caller`` is sorted by descending cost, caller name breaking ties.
    """
    snapshot = build_cost_rates(backend, window_hours=window_days * 24)
    callers = sorted(
        snapshot.by_caller.items(), key=lambda item: (-item[1].cost_usd, item[0]),
    )
    by_caller = tuple(
        CallerWindowSpend(
            caller=caller,
            group=CALLER_GROUPS.get(caller, "Other"),
            cost_usd=rate.cost_usd,
            calls=rate.calls,
            usd_per_iteration=_per_iteration(rate.cost_usd, iterations),
        )
        for caller, rate in callers
    )
    return SpendReading(
        window_days=window_days,
        requested_window_days=(
            window_days if requested_window_days is None else requested_window_days
        ),
        iterations=iterations,
        spend_usd=snapshot.total_cost_usd,
        spend_usd_per_day=snapshot.cost_per_day,
        spend_usd_per_iteration=_per_iteration(snapshot.total_cost_usd, iterations),
        by_caller=by_caller,
    )


def read_window_spend(
    backend: CostSampleBackend,
    *,
    requested_days: int,
    archive_threshold_days: int,
    count_iterations: Callable[[datetime], int],
    now: datetime,
) -> SpendReading:
    """Read the clamped trailing window ending at *now*."""
    days = effective_window_days(requested_days, archive_threshold_days)
    return read_spend(
        backend,
        window_days=days,
        iterations=count_iterations(now - timedelta(days=days)),
        requested_window_days=requested_days,
    )
