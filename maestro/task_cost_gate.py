"""Task cost risk gate — pure-logic analysis of recent task cost patterns.

Analyses a list of :class:`~maestro.memory.TaskRecord` for patterns that
indicate high cost risk: consecutive expensive failures, escalating failure
durations, and high failure-cost ratios.  Returns an optional short verdict
string when risk is detected, or ``None`` when safe to proceed.

Pure functions only.  No I/O, no LLM/SDK calls.  Deterministic.

``duration_secs`` is the cost proxy — longer-running tasks consume more
SDK tokens/dollars.  The gate operates on task-level records, not raw
iteration-level cost samples, so it complements (not duplicates) the
burn-rate throttle in :mod:`maestro.agent`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from maestro.memory import TaskRecord

# ---------------------------------------------------------------------------
# Thresholds (module-level constants for testability)
# ---------------------------------------------------------------------------

#: Minimum number of tasks required before any rule fires.
#: Below this, the history is too short to draw cost conclusions.
MIN_TASKS_FOR_COST_CHECK = 3

#: Recency horizon for the *optional* time-bounded evaluation. When a caller
#: supplies ``reference_time``, records whose ``timestamp`` is older than
#: ``reference_time - COST_RISK_DECAY_HORIZON`` are dropped before the three
#: rules run. This kills the frozen-window livelock (#2566): a static task
#: history that has aged past the horizon can no longer block ``generate_task``
#: forever, because the gate is the only thing that curates the queue and an
#: idle loop appends no new record to refresh the window. A *fresh* expensive-
#: failure window (records within the horizon) still trips the gate.
COST_RISK_DECAY_HORIZON = timedelta(hours=24)

#: Number of consecutive trailing failures required for the
#: "consecutive expensive failures" rule.
CONSECUTIVE_FAILURE_THRESHOLD = 3

#: Minimum number of failure records required for escalation detection.
ESCALATION_MIN_FAILURES = 3

#: Fraction of total duration spent on non-success outcomes (failure +
#: partial) above which the cost-ratio rule fires (0.70 = 70%).
FAILURE_COST_RATIO_THRESHOLD = 0.70

#: Minimum average duration (seconds) for consecutive failures to be
#: considered "expensive".  Cheap failures (e.g. immediate parse errors)
#: don't represent real cost risk.
EXPENSIVE_FAILURE_DURATION_FLOOR = 120.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_task_timestamp(raw: str) -> datetime | None:
    """Parse a :class:`TaskRecord` ISO timestamp, coercing naive to UTC.

    Returns ``None`` for empty or unparseable input so a damaged timestamp is
    treated as "cannot confirm recency" rather than crashing the gate. Mirrors
    the ``_parse_iso_utc`` convention used elsewhere; kept local so this module
    stays pure and free of cross-module dependencies.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _within_horizon(task: TaskRecord, cutoff: datetime) -> bool:
    """Return ``True`` when *task*'s timestamp parses and is at or after *cutoff*.

    A record with an unparseable timestamp is treated as *outside* the horizon
    (dropped): the whole point of the horizon filter is that a static window can
    no longer wedge the gate, so an undatable record must not be able to keep it
    blocked either.
    """
    parsed = _parse_task_timestamp(task.timestamp)
    return parsed is not None and parsed >= cutoff


def _consecutive_failure_streak(tasks: list[TaskRecord]) -> int:
    """Count consecutive ``failure`` outcomes from the end of *tasks*.

    Only ``outcome == "failure"`` counts — ``partial`` and ``success``
    both break the streak.
    """
    count = 0
    for task in reversed(tasks):
        if task.outcome == "failure":
            count += 1
        else:
            break
    return count


def _repeated_expensive_failures(tasks: list[TaskRecord]) -> str | None:
    """Rule 1 — consecutive expensive failures at the tail.

    Fires when the trailing failure streak is ≥
    :data:`CONSECUTIVE_FAILURE_THRESHOLD` AND the average ``duration_secs``
    of those failures exceeds :data:`EXPENSIVE_FAILURE_DURATION_FLOOR`.

    Returns a short verdict string or ``None``.
    """
    streak = _consecutive_failure_streak(tasks)
    if streak < CONSECUTIVE_FAILURE_THRESHOLD:
        return None

    tail_failures = tasks[-streak:]
    avg_duration = sum(
        max(0.0, t.duration_secs) for t in tail_failures
    ) / len(tail_failures)

    if avg_duration < EXPENSIVE_FAILURE_DURATION_FLOOR:
        return None

    return (
        f"{streak} consecutive failures averaging {avg_duration:.0f}s; "
        f"cost risk elevated"
    )


def _escalating_failure_durations(tasks: list[TaskRecord]) -> str | None:
    """Rule 2 — strictly increasing durations among recent failures.

    Extracts the failure subsequence (preserving order), then checks
    whether the last :data:`ESCALATION_MIN_FAILURES` failures form a
    strictly increasing sequence of ``duration_secs``.  Successes between
    failures are ignored — only the failure subsequence matters.

    Returns a short verdict string or ``None``.
    """
    failure_durations = [
        max(0.0, t.duration_secs)
        for t in tasks
        if t.outcome == "failure"
    ]

    if len(failure_durations) < ESCALATION_MIN_FAILURES:
        return None

    # Check the last ESCALATION_MIN_FAILURES entries for strict monotonic
    # increase.
    tail = failure_durations[-ESCALATION_MIN_FAILURES:]
    is_escalating = all(
        tail[i] < tail[i + 1] for i in range(len(tail) - 1)
    )

    if not is_escalating:
        return None

    return (
        f"failure durations escalating "
        f"({' → '.join(f'{d:.0f}s' for d in tail)})"
    )


def _failure_cost_ratio(tasks: list[TaskRecord]) -> str | None:
    """Rule 3 — high fraction of total duration spent on non-success tasks.

    Unlike rules 1 and 2 (which detect pathological *failure* spirals where
    a ``partial`` rightly breaks the pattern), this rule measures total cost
    waste.  Partials are empirically the larger cost driver (~2.2x the cost
    of successes), so both ``failure`` and ``partial`` outcomes count in the
    numerator.

    Fires when the sum of ``duration_secs`` for non-success tasks divided
    by total ``duration_secs`` exceeds :data:`FAILURE_COST_RATIO_THRESHOLD`.

    Returns a short verdict string or ``None``.
    """
    if not tasks:
        return None

    total_duration = sum(max(0.0, t.duration_secs) for t in tasks)
    if total_duration <= 0.0:
        return None

    non_success_duration = sum(
        max(0.0, t.duration_secs)
        for t in tasks
        if t.outcome in ("failure", "partial")
    )
    ratio = non_success_duration / total_duration

    if ratio < FAILURE_COST_RATIO_THRESHOLD:
        return None

    pct = round(ratio * 100)
    return (
        f"{pct}% of recent task duration spent on non-success outcomes "
        f"({non_success_duration:.0f}s / {total_duration:.0f}s)"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_task_cost_risk(
    tasks: list[TaskRecord],
    reference_time: datetime | None = None,
) -> str | None:
    """Analyse recent task history for cost-risk patterns.

    Returns a short reason string when risk is detected, or ``None``
    when safe to proceed.  Rules are evaluated in priority order and the
    first hit wins — callers receive at most one verdict per invocation.

    Pure logic, zero SDK cost, deterministic.

    When *reference_time* is ``None`` (the default) the full window is
    evaluated with no time component, exactly as before — preserving
    determinism, the existing tests, and the ``task_cost_gate.check_task_cost_risk``
    patch target. When a *reference_time* is supplied (production passes
    ``datetime.now(UTC)``), records whose ``timestamp`` is older than
    ``reference_time - COST_RISK_DECAY_HORIZON`` are dropped before the rules
    run, and the gate returns ``None`` when the surviving window falls below
    :data:`MIN_TASKS_FOR_COST_CHECK`. The clock is always passed in, never read
    internally, so the function stays pure and deterministic.

    Rules:

    1. **Consecutive expensive failures** — trailing failure streak ≥ 3
       with average duration above the floor.
    2. **Escalating failure durations** — the last 3+ failures show
       strictly increasing ``duration_secs``.
    3. **High non-success cost ratio** — >70% of total recent duration is
       spent on ``failure`` and ``partial`` tasks (measures total cost waste,
       not failure streaks).
    """
    if reference_time is not None:
        ref = (
            reference_time
            if reference_time.tzinfo is not None
            else reference_time.replace(tzinfo=UTC)
        )
        cutoff = ref - COST_RISK_DECAY_HORIZON
        tasks = [t for t in tasks if _within_horizon(t, cutoff)]

    if len(tasks) < MIN_TASKS_FOR_COST_CHECK:
        return None

    # Rule 1: consecutive expensive failures (most actionable)
    verdict = _repeated_expensive_failures(tasks)
    if verdict is not None:
        return verdict

    # Rule 2: escalating failure durations
    verdict = _escalating_failure_durations(tasks)
    if verdict is not None:
        return verdict

    # Rule 3: high failure cost ratio
    verdict = _failure_cost_ratio(tasks)
    if verdict is not None:
        return verdict

    return None
