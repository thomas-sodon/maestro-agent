"""Structured health summary — consolidates task-level and diagnostic signals.

Canonical home for health-metric helpers (``success_rate``, ``failure_streak``,
``outcome_trend``) and the :class:`HealthSummary` dataclass that bundles them
into a single object with a computed ``is_noteworthy`` flag.

These helpers were originally private functions in :mod:`maestro.stats`; they
now live here to eliminate a circular dependency (health_check → stats → health_check).
:mod:`maestro.stats` re-exports them under their old private names for backward
compatibility.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from maestro import host_capacity
from maestro.audit_checks import is_no_op_task
from maestro.diagnostics import DiagnosticSnapshot, is_rising_high_cost
from maestro.journal import _ensure_utc
from maestro.memory import TaskRecord

# ``_ensure_utc`` (UTC-normalizing a datetime) is imported from
# :mod:`maestro.journal` rather than redefined here — the two were
# byte-identical. ``journal`` is a leaf dependency (it imports only
# ``search``/``types``), so importing it introduces no cycle.

# ---------------------------------------------------------------------------
# Public health-metric helpers
# ---------------------------------------------------------------------------


def success_rate(tasks: list[TaskRecord]) -> float:
    """Return the fraction of tasks with outcome 'success', or 1.0 if empty."""
    if not tasks:
        return 1.0
    successes = sum(1 for t in tasks if t.outcome == "success")
    return successes / len(tasks)


def failure_streak(tasks: list[TaskRecord]) -> int:
    """Count consecutive non-success outcomes from the most recent task backward."""
    streak = 0
    for task in reversed(tasks):
        if task.outcome != "success":
            streak += 1
        else:
            break
    return streak


def outcome_trend(tasks: list[TaskRecord]) -> str:
    """Classify the recent outcome pattern as 'improving', 'declining', or 'stable'.

    Compares the success rate of the newer half against the older half.
    Requires at least 4 tasks for a meaningful split; returns 'stable' otherwise.
    """
    if len(tasks) < 4:
        return "stable"
    mid = len(tasks) // 2
    older = tasks[:mid]
    newer = tasks[mid:]
    rate_old = success_rate(older)
    rate_new = success_rate(newer)
    delta = rate_new - rate_old
    if delta >= 0.2:
        return "improving"
    if delta <= -0.2:
        return "declining"
    return "stable"


# The journal-event ``triggered_by`` tags marking burn-rate throttle
# transitions, recorded by ``LifecycleController.maybe_notify_throttle_transition``.
# The residency reader is a read-only consumer of these milestone events.
_THROTTLE_ENTER_TAG = "budget_throttle_enter"
_THROTTLE_EXIT_TAG = "budget_throttle_exit"


def _clamped_overlap(
    start: datetime,
    end: datetime,
    window_start: datetime,
    window_end: datetime,
) -> float:
    """Seconds of ``[start, end]`` that fall within ``[window_start, window_end]``."""
    lo = max(start, window_start)
    hi = min(end, window_end)
    return max(0.0, (hi - lo).total_seconds())


def throttle_residency(
    entries: Sequence[tuple[str, str | None]],
    *,
    window_start: datetime,
    now: datetime,
    open_spell_since: str | None = None,
) -> float:
    """Fraction of ``[window_start, now]`` spent in burn-rate throttle.

    ``entries`` is a sequence of ``(iso_timestamp, triggered_by)`` pairs — the
    throttle-transition milestones the loop records as journal events
    (``budget_throttle_enter`` / ``budget_throttle_exit``). Non-throttle tags
    and unparseable timestamps are ignored, so the full set of in-window
    journal events may be passed unfiltered.

    ``open_spell_since`` is the ISO start of a currently-active throttle spell
    (the loop's ``_budget_throttle_entered_at``). When set it seeds an open
    ``enter`` at that time — which may predate ``window_start``. This is what
    makes a throttle that opened *before* the window and is *still* active
    countable: its ``enter`` milestone falls outside the in-window ``entries``
    the caller collects, so without this seed a >24h-continuous pin would
    under-report to ``0.0`` (the exact "pinned at the ceiling" case). Seeding
    a duplicate of an already-present in-window ``enter`` is harmless — the
    earliest-open rule below coalesces them.

    Intervals are formed by pairing each ``enter`` with the next ``exit``:

    - A closed ``enter`` → ``exit`` interval contributes its clamped overlap
      with the window.
    - A trailing ``enter`` with no matching ``exit`` is treated as open through
      ``now`` (still throttled).
    - An ``exit`` whose ``enter`` predates ``window_start`` — so the ``enter``
      milestone falls outside the window and is absent — is clamped to
      ``window_start`` (a pre-window-open interval).

    Returns ``total_throttled_seconds / window_seconds`` clamped to
    ``[0.0, 1.0]``; ``0.0`` for an empty window or no throttle entries.
    """
    window_start = _ensure_utc(window_start)
    now = _ensure_utc(now)
    window_seconds = (now - window_start).total_seconds()
    if window_seconds <= 0:
        return 0.0

    # Parse and sort the throttle-transition milestones ascending by timestamp.
    transitions: list[tuple[datetime, str]] = []
    for iso_ts, tag in entries:
        if tag not in (_THROTTLE_ENTER_TAG, _THROTTLE_EXIT_TAG):
            continue
        try:
            ts = _ensure_utc(datetime.fromisoformat(iso_ts))
        except (ValueError, TypeError):
            continue
        transitions.append((ts, tag))
    # Seed the currently-open spell as an ``enter`` (may predate the window).
    if open_spell_since is not None:
        with contextlib.suppress(ValueError, TypeError):
            transitions.append(
                (
                    _ensure_utc(datetime.fromisoformat(open_spell_since)),
                    _THROTTLE_ENTER_TAG,
                )
            )
    transitions.sort(key=lambda pair: pair[0])

    throttled_seconds = 0.0
    open_since: datetime | None = None
    for ts, tag in transitions:
        if tag == _THROTTLE_ENTER_TAG:
            # A fresh enter opens an interval. If one is already open (a
            # missing exit between two enters), keep the earliest open point.
            if open_since is None:
                open_since = ts
        elif open_since is not None:
            throttled_seconds += _clamped_overlap(
                open_since, ts, window_start, now
            )
            open_since = None
        else:
            # An exit with no preceding enter in the window — its enter
            # predates window_start. Treat the interval as open from
            # window_start (clamped to the window).
            throttled_seconds += _clamped_overlap(
                window_start, ts, window_start, now
            )
    # A trailing enter with no matching exit is still throttled at ``now``.
    if open_since is not None:
        throttled_seconds += _clamped_overlap(open_since, now, window_start, now)

    return max(0.0, min(1.0, throttled_seconds / window_seconds))


# ---------------------------------------------------------------------------
# HealthSummary
# ---------------------------------------------------------------------------

# No-op task rate threshold — when more than 30% of the recent-task window
# are classified as no-op (confirmed existing state without changes), a
# health condition is surfaced. Zero SDK cost, deterministic substring
# classification via :func:`~maestro.audit_checks.is_no_op_task`.
_NO_OP_RATE_THRESHOLD = 0.3

# Window size for the no-op rate check (number of most-recent tasks).
_NO_OP_RATE_WINDOW = 10

# Burn-rate throttle-residency threshold — when more than half of the trailing
# 24h was spent in budget throttle, a health condition is surfaced (and
# ``is_noteworthy`` flips) so a loop pinned at the budget boundary is visible
# without grepping logs. Deterministic, zero SDK cost — the residency is
# derived from the throttle-transition milestones the loop already writes.
_THROTTLE_RESIDENCY_THRESHOLD = 0.5


@dataclass
class HealthSummary:
    """Snapshot of iteration health signals with a noteworthy gate."""

    success_rate: float
    failure_streak: int
    outcome_trend: str
    error_count: int
    cost_trend: str | None
    is_noteworthy: bool
    conditions: list[str] = field(default_factory=list)


def summarize_health(
    tasks: list[TaskRecord],
    snapshot: DiagnosticSnapshot,
    throttle_residency_fraction: float | None = None,
) -> HealthSummary:
    """Build a :class:`HealthSummary` from recent tasks and a diagnostic snapshot.

    The ``is_noteworthy`` flag is the single, canonical definition of the
    iteration-health threshold rule
    (:func:`~maestro.stats.is_health_noteworthy` is a backward-compatible
    shim that delegates here). It fires when any of:

    - Task success rate below 70 % (with at least 3 tasks)
    - Failure streak of 3 or more consecutive non-success outcomes
    - Recent error count of 5 or more
    - Cost trend classified as ``'spiking'``
    - Cost trend ``'rising'`` with a recent iteration cost above $0.05

    Additionally, ``conditions`` collects human-readable strings for
    structural health signals:

    - **No-op task rate** — when > 30 % of the last 10 tasks confirmed
      existing state without producing changes (deterministic substring
      classification, zero SDK cost).
    - **Budget-throttle residency** — when the loop spent > 50 % of the
      trailing 24h in burn-rate throttle (``throttle_residency_fraction``,
      supplied by the caller from the journal's throttle-transition
      milestones).
    - **Host-capacity saturation** — one entry per saturated resource (load
      per core, memory, swap, free disk on the Docker root) in the snapshot's
      optional host-capacity reading (proposal 125); nothing when the sensor is
      off or the snapshot carries no reading.
    """
    rate = success_rate(tasks)
    streak = failure_streak(tasks)
    trend = outcome_trend(tasks)
    error_count = snapshot.recent_error_count
    cost_trend = snapshot.cost_trend

    conditions: list[str] = []

    # No-op task rate condition — single-pass scan avoids the duplicate
    # classification that previously called compute_no_op_rate() and then
    # re-walked the same window to count no-op tasks.
    window_slice = tasks[-_NO_OP_RATE_WINDOW:]
    if window_slice:
        no_op_count = sum(
            1 for t in window_slice if is_no_op_task(t.result_summary)
        )
        no_op_rate = no_op_count / len(window_slice)
        if no_op_rate > _NO_OP_RATE_THRESHOLD:
            conditions.append(
                f"No-op task rate {round(no_op_rate * 100)}% "
                f"({no_op_count}/{len(window_slice)} recent tasks confirmed "
                f"existing state without producing changes)"
            )

    # Budget-throttle residency condition — when the loop spent more than half
    # the trailing 24h in burn-rate throttle, surface it so a loop pinned at
    # the budget boundary is visible in contemplation. Appending it also flips
    # is_noteworthy via the existing bool(conditions) OR below.
    if (
        throttle_residency_fraction is not None
        and throttle_residency_fraction > _THROTTLE_RESIDENCY_THRESHOLD
    ):
        conditions.append(
            f"Budget-throttle residency "
            f"{round(throttle_residency_fraction * 100)}% of the last 24h"
        )

    # Host-capacity saturation (proposal 125) — each saturated resource in the
    # snapshot's reading (load per core, memory, swap, free disk on the Docker
    # root) adds one condition, flipping is_noteworthy via the bool(conditions)
    # OR below. The reading rides the snapshot the function already takes, so a
    # None reading — the sensor off, or a snapshot carrying none — extends
    # ``conditions`` by nothing and leaves the output byte-identical to today.
    conditions.extend(host_capacity.saturation_conditions(snapshot.host_capacity))

    # A sustained cost climb now reads as "rising" (not "spiking"), so mirror
    # the diagnostics.is_noteworthy parity via the shared predicate: a "rising"
    # trend whose most recent iteration cost is high in absolute terms is still
    # worth surfacing.  Without this, reclassifying sustained increases would
    # silently drop the health-summary alert the old "spiking" label provided.
    noteworthy = (
        (len(tasks) >= 3 and rate < 0.70)
        or streak >= 3
        or error_count >= 5
        or cost_trend == "spiking"
        or is_rising_high_cost(snapshot)
        or bool(conditions)
    )

    return HealthSummary(
        success_rate=rate,
        failure_streak=streak,
        outcome_trend=trend,
        error_count=error_count,
        cost_trend=cost_trend,
        is_noteworthy=noteworthy,
        conditions=conditions,
    )
