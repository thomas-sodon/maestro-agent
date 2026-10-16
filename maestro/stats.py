"""Iteration health summary — backward-compatible shim re-exporting health helpers.

The canonical implementations of ``success_rate``, ``failure_streak``, and
``outcome_trend`` now live in :mod:`maestro.health_check`.  This module
re-exports them under their original private names (``_success_rate``, etc.)
so existing call-sites and tests continue to work.

``is_health_noteworthy`` and ``summarize_iteration_health`` delegate to
:func:`~maestro.health_check.summarize_health`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from maestro.diagnostics import DiagnosticSnapshot
from maestro.fileutil import file_lock
from maestro.health_check import failure_streak as _failure_streak  # noqa: F401
from maestro.health_check import outcome_trend as _outcome_trend  # noqa: F401
from maestro.health_check import success_rate as _success_rate  # noqa: F401
from maestro.health_check import summarize_health
from maestro.memory import (
    EvolutionPlan,
    EvolutionProposal,
    SensorReading,
    TaskRecord,
)
from maestro.remediation_tracker import (
    measure_confirmed_landing_rate,
    measure_forced_vs_chosen_quiet,
    pr_number_from_remediation_goal,
)

# Append-only ledger recording the size of the owned ``pending_review`` backlog
# at each contemplation. Its cycle-over-cycle *signed delta* is the exact metric
# the backlog sensor tracks ("cycle-end backlog >= cycle-start" is the
# falsification clause), so this ledger is the instrument that makes that number
# visible instead of inferred.
_OWNED_BACKLOG_LEDGER_FILE = "owned_backlog_ledger.jsonl"

# Confirmed-landing rate above which :func:`format_confirmed_landing_summary`
# surfaces a CONFIRMED-LANDING-RATIO line. Below this share of attributable
# landings being mere confirmations of an already-landed cure the pattern is
# noise; above it, confirming another's land dominates my "successful
# resolutions" and is worth naming before I count the next merge as my own.
_CONFIRMED_LANDING_RATE_THRESHOLD = 0.5

# Count of ``pending_review`` evolution plans at or above which
# :func:`format_pending_plan_backlog` surfaces a PENDING-PLAN BACKLOG line.
# Below this a couple of unreviewed plans is normal in-flight work; at or above
# it the backlog is large enough that minting another sibling almost certainly
# restates one I already hold — the moment to stop proposing and start landing.
_PENDING_PLAN_BACKLOG_THRESHOLD = 3

# Cap on corrupted ``evolution_plans.jsonl`` lines tolerated while counting
# pending plans before the scan stops with a partial count. Mirrors the bounded
# skip in :meth:`maestro.memory.Memory.get_recent_tasks` so a corrupted file
# tail never forces a full-file walk on this zero-SDK-cost read path.
_MAX_CORRUPT_PLAN_LINES = 100


def is_health_noteworthy(
    tasks: list[TaskRecord],
    snapshot: DiagnosticSnapshot,
) -> bool:
    """Return True when iteration health crosses a threshold worth reporting.

    Delegates to :func:`~maestro.health_check.summarize_health` and returns
    the ``is_noteworthy`` flag.  Kept for backward compatibility.

    Triggers on any of:
    - Task success rate below 70% (with at least 3 tasks)
    - Failure streak of 3 or more consecutive non-success outcomes
    - Recent error count of 5 or more in the diagnostic snapshot
    - Cost trend classified as 'spiking'
    - Cost trend 'rising' with a recent iteration cost above $0.05
    """
    return summarize_health(tasks, snapshot).is_noteworthy


def summarize_iteration_health(
    tasks: list[TaskRecord],
    snapshot: DiagnosticSnapshot,
) -> str:
    """Build a compact pipe-delimited health summary string.

    Delegates to :func:`~maestro.health_check.summarize_health` and formats
    the result.  Kept for backward compatibility.
    """
    s = summarize_health(tasks, snapshot)
    cost = s.cost_trend or "unknown"

    base = (
        f"success_rate={s.success_rate:.0%} | "
        f"failure_streak={s.failure_streak} | "
        f"outcome_trend={s.outcome_trend} | "
        f"errors={s.error_count} | "
        f"cost_trend={cost}"
    )
    for condition in s.conditions:
        base = f"{base}\n{condition}"
    return base


def format_confirmed_landing_summary(
    data_dir: Path,
    n: int = 0,
    threshold: float = _CONFIRMED_LANDING_RATE_THRESHOLD,
) -> str:
    """Render a CONFIRMED-LANDING-RATIO line when confirmations dominate.

    Reads the recent landing attribution via
    :func:`maestro.remediation_tracker.measure_confirmed_landing_rate` (trailing
    *n* tasks; ``n`` ≤ 0 reads effectively all) and returns a single line naming
    the share of attributable landings that were mere confirmations of an
    already-landed cure — but only when at least one attributable landing exists
    and the confirmed rate strictly exceeds *threshold*. Otherwise returns ``""``
    so the caller injects nothing, suppressing the line below threshold exactly
    like the sibling SETTLED-RELAP / FORCED-QUIET sensors.

    This is the awareness mirror for 'Confirmed-isn't-caused': it names how
    often a cure I stamped ``success`` was one I merely re-verified already
    landed rather than one I caused to land. Reads from disk; otherwise pure —
    zero SDK cost, measurement-only.
    """
    rate = measure_confirmed_landing_rate(data_dir, n)
    caused: int = rate["caused"]  # type: ignore[assignment]
    confirmed: int = rate["confirmed"]  # type: ignore[assignment]
    total: int = rate["total"]  # type: ignore[assignment]
    confirmed_rate: float = rate["confirmed_rate"]  # type: ignore[assignment]
    if total <= 0 or confirmed_rate <= threshold:
        return ""
    return (
        f"CONFIRMED-LANDING-RATIO: {confirmed}/{total} ({confirmed_rate:.0%}) "
        f"of attributable landings merely confirmed a cure already landed "
        f"before the task ({caused} caused) — confirming another's merge is "
        f"not a release I caused; do not count it as my own landing."
    )


def format_pending_plan_backlog(
    data_dir: Path,
    threshold: int = _PENDING_PLAN_BACKLOG_THRESHOLD,
) -> str:
    """Render a PENDING-PLAN BACKLOG line when unreviewed plans pile up.

    Reads ``evolution_plans.jsonl`` under *data_dir*, counts rows whose
    ``status`` is ``"pending_review"``, and returns the exact line::

        PENDING-PLAN BACKLOG: <N> unreviewed plans — proposing is not your
        bottleneck; landing is.

    when that count is at or above *threshold*, else ``""`` so the caller
    injects nothing below threshold — exactly like the sibling
    CONFIRMED-LANDING / SETTLED-RELAP sensors.

    Corrupted JSONL lines are skipped, but the skip is bounded at
    :data:`_MAX_CORRUPT_PLAN_LINES`: once that many lines fail to parse the
    scan stops with a partial count, so a corrupted file tail never forces a
    full-file walk (mirroring :meth:`maestro.memory.Memory.get_recent_tasks`).
    A line that parses to a non-mapping is not corruption — it is simply not a
    plan row — so it is ignored without consuming the corruption budget.

    This is the anti-treadmill awareness mirror: at task-generation time I
    cannot otherwise see my own unlanded backlog, so I keep minting redundant
    siblings that re-diagnose what five pending plans already say. Surfacing
    the count makes the backlog impossible to not-see *before* I propose the
    next one. Reads from disk; otherwise pure — zero SDK cost, measurement-only.
    """
    path = Path(data_dir) / "evolution_plans.jsonl"
    if not path.exists():
        return ""
    count = 0
    skipped = 0
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                if skipped >= _MAX_CORRUPT_PLAN_LINES:
                    break
                continue
            if isinstance(row, dict) and row.get("status") == "pending_review":
                count += 1
    if count < threshold:
        return ""
    return (
        f"PENDING-PLAN BACKLOG: {count} unreviewed plans — proposing is "
        f"not your bottleneck; landing is."
    )


@dataclass(frozen=True)
class BacklogSnapshot:
    """One snapshot of the owned ``pending_review`` backlog size.

    Recorded once per contemplation cycle. ``timestamp`` is an ISO-8601 UTC
    string, so lexical order equals chronological order — mirroring
    :class:`~maestro.deed_ledger.Deed`. The signed difference between two
    consecutive snapshots' ``count`` is the metric the backlog sensor tracks.
    """

    iteration: int
    count: int
    timestamp: str


def build_backlog_snapshot(
    iteration: int,
    count: int,
    *,
    now: str | None = None,
) -> BacklogSnapshot:
    """Construct a :class:`BacklogSnapshot`, stamping the UTC time when *now* is None.

    The shared construction path for both the JSONL
    :class:`JsonlOwnedBacklogLedger` and the SQLite
    :class:`~maestro.state_store.SqliteOwnedBacklogLedger` adapter, so the
    timestamp default can never drift between substrates — mirroring
    :func:`~maestro.deed_ledger.build_deed`. *now* is an injection seam for
    deterministic tests; it defaults to the current UTC time in ISO-8601.
    """
    stamp = now if now is not None else datetime.now(UTC).isoformat()
    return BacklogSnapshot(iteration=iteration, count=count, timestamp=stamp)


@runtime_checkable
class OwnedBacklogLedgerBackend(Protocol):
    """The owned-backlog ledger persistence surface (proposal 106).

    One writer (the ``contemplate`` snapshot) and one reader (the recent-2 delta)
    go through this seam, so the ledger can live in ``owned_backlog_ledger.jsonl``
    or in ``state.db`` behind ``[paths] sqlite_state_enabled`` with no call-site
    branching. Mirrors the :class:`~maestro.deed_ledger.DeedLedgerBackend` idiom;
    ``@runtime_checkable`` enables a structural ``isinstance`` check in the
    agent's backend-selection tests.
    """

    def record(
        self,
        iteration: int,
        count: int,
        *,
        now: str | None = None,
    ) -> BacklogSnapshot:
        """Append exactly one snapshot and return it."""
        ...

    def recent(self, n: int) -> list[BacklogSnapshot]:
        """Return the *n* most-recent snapshots, newest-first."""
        ...


def record_owned_backlog_snapshot(
    data_dir: Path,
    iteration: int,
    count: int,
    now: str | None = None,
) -> None:
    """Append one snapshot of the owned ``pending_review`` backlog size.

    Writes a single JSONL row ``{iteration, count, timestamp}`` to
    ``owned_backlog_ledger.jsonl`` under *data_dir* and never rewrites prior
    rows — the ledger is append-only so the full cycle-over-cycle history of the
    backlog size is preserved verbatim. *now* stamps the current UTC time when
    ``None`` (injectable for deterministic tests), mirroring the sibling
    append-only writers in :mod:`maestro.remediation_tracker`.

    This is the missing measurement behind the backlog sensor, whose metric is
    the *net movement* of this backlog per cycle: without a persisted
    snapshot I can only count the fire (a ``mark_plan_obsolete`` returning True)
    and never the delta. Recording the count each cycle is the precondition for
    :func:`measure_owned_net_delta` to compute that signed number honestly.
    """
    timestamp = now if now is not None else datetime.now(UTC).isoformat()
    path = Path(data_dir) / _OWNED_BACKLOG_LEDGER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps(
        {"iteration": iteration, "count": count, "timestamp": timestamp},
    )
    with file_lock(path), open(path, "a", encoding="utf-8") as handle:
        handle.write(record + "\n")


def _load_owned_backlog_snapshots(data_dir: Path) -> list[dict[str, object]]:
    """Return every parseable snapshot row from the owned-backlog ledger.

    Rows that fail to parse or are not mappings are skipped. Order is preserved
    (append order == chronological), so the last two entries are the two most
    recent snapshots.
    """
    path = Path(data_dir) / _OWNED_BACKLOG_LEDGER_FILE
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _snapshot_count(row: dict[str, object]) -> int:
    """Return the integer ``count`` field of a snapshot row (0 if absent/bad)."""
    raw = row.get("count", 0)
    return raw if isinstance(raw, int) else 0


def _row_to_snapshot(row: dict[str, object]) -> BacklogSnapshot:
    """Convert a decoded JSONL row into a :class:`BacklogSnapshot`.

    A missing / ill-typed ``iteration`` coerces to 0 and ``timestamp`` to ``""``,
    while ``count`` reuses :func:`_snapshot_count`'s defensive coercion — so a
    corrupted row degrades to a zeroed snapshot rather than crashing the
    zero-SDK-cost read path, exactly as the delta reader has always tolerated.
    """
    raw_iteration = row.get("iteration", 0)
    iteration = raw_iteration if isinstance(raw_iteration, int) else 0
    raw_timestamp = row.get("timestamp", "")
    timestamp = raw_timestamp if isinstance(raw_timestamp, str) else ""
    return BacklogSnapshot(
        iteration=iteration,
        count=_snapshot_count(row),
        timestamp=timestamp,
    )


def measure_owned_net_delta(
    snapshots: Sequence[BacklogSnapshot],
) -> int | None:
    """Return the signed cycle-over-cycle delta of the owned backlog, or None.

    *snapshots* is the two most recent backlog snapshots, newest-first (as
    returned by :meth:`OwnedBacklogLedgerBackend.recent`). Returns the latest
    ``count`` minus the previous ``count`` — a positive number means the owned
    ``pending_review`` backlog *grew* (the direction the backlog sensor's
    falsification clause names), negative means it shrank. Returns ``None`` when fewer than two
    snapshots exist, since a delta is undefined without a prior point to subtract
    from.

    Substrate-agnostic and pure — the caller reads the snapshots through
    whichever backend is live; zero SDK cost, measurement-only.
    """
    if len(snapshots) < 2:
        return None
    latest = snapshots[0]
    previous = snapshots[1]
    return latest.count - previous.count


def build_sensor_readings(
    unlanded_pr_count: int,
    owned_backlog_snapshots: Sequence[BacklogSnapshot],
    *,
    forced_quiet_tasks: list[dict[str, object]] | None = None,
    now: str | None = None,
) -> list[SensorReading]:
    """Build this cycle's scalar behavioral-sensor readings for persistence.

    The write half of the SENSOR_THRESHOLD calibration loop: ``contemplate``
    computes these scalars for its measured-data block, and this pure helper
    stamps them into :class:`~maestro.memory.SensorReading` rows keyed by the
    uppercase tag the reflection sees, so a ``sensor <TAG> goes above/below n``
    bet later resolves against the same number the block showed
    (``_collect_calibration_evidence`` reads them back). ``UNLANDED-REMEDIATION``
    carries the count of PRs the line surfaces; ``OWNED-NET-DELTA`` is recorded
    only when a signed delta exists (two snapshots) — its absence is not a
    ``0`` reading. ``FORCED-QUIET-RATIO`` — the grammar's own teaching example,
    so the tag priced bets are most likely to name — is recorded as the
    ``forced / total`` fraction over the remediation quiets in *forced_quiet_tasks*
    (task-history dicts, as ``contemplate`` already passes to
    :func:`~maestro.remediation_tracker.format_forced_vs_chosen_quiet`), and only
    when at least one remediation quiet exists (``total > 0``) — matching the
    line's own "emit only when there is a quiet to account for" rule, so its
    absence is not a ``0.0`` reading. Persisting the fraction (not the integer
    ``forced`` count) is what lets a ``goes above 0.5`` bet resolve against the
    same number the grammar teaches. ``crossed`` marks a non-baseline (non-zero)
    value and is informational only; the scorer recomputes crossing from
    ``value``. *now* defaults to the current UTC ISO-8601 timestamp (lexical ==
    chronological, so it compares against a reflection bet's ``timestamp``).
    Pure — zero SDK cost.
    """
    ts = now if now is not None else datetime.now(UTC).isoformat()
    readings = [
        SensorReading(
            timestamp=ts,
            tag="UNLANDED-REMEDIATION",
            value=float(unlanded_pr_count),
            crossed=unlanded_pr_count > 0,
        ),
    ]
    owned_net = measure_owned_net_delta(owned_backlog_snapshots)
    if owned_net is not None:
        readings.append(
            SensorReading(
                timestamp=ts,
                tag="OWNED-NET-DELTA",
                value=float(owned_net),
                crossed=owned_net != 0,
            ),
        )
    if forced_quiet_tasks is not None:
        ratio = measure_forced_vs_chosen_quiet(forced_quiet_tasks)
        if ratio.total > 0:
            readings.append(
                SensorReading(
                    timestamp=ts,
                    tag="FORCED-QUIET-RATIO",
                    value=ratio.forced / ratio.total,
                    crossed=ratio.forced > 0,
                ),
            )
    return readings


def format_owned_net_delta_summary(
    snapshots: Sequence[BacklogSnapshot],
) -> str:
    """Render an OWNED-NET-DELTA line from the two most recent snapshots.

    Returns::

        OWNED-NET-DELTA: prev=X now=Y net=±Z

    where ``prev``/``now`` are the two most recent backlog counts (from
    *snapshots*, newest-first) and ``net`` is their signed difference — but only
    when at least two snapshots exist. Below that returns ``""`` so the caller
    injects nothing, exactly like the sibling PENDING-PLAN / CONFIRMED-LANDING
    sensors.

    This makes the invisible number visible: the net movement the backlog
    sensor tracks, surfaced in contemplation so I can honestly confront my
    falsification condition (cycle-end backlog ≥ cycle-start) instead of
    inferring it. Substrate-agnostic and pure — zero SDK cost.
    """
    net = measure_owned_net_delta(snapshots)
    if net is None:
        return ""
    latest = snapshots[0]
    previous = snapshots[1]
    return f"OWNED-NET-DELTA: prev={previous.count} now={latest.count} net={net:+d}"


# The hard ceiling on the multi-PR rollout summary line — a context-bloat cap
# (every string entering a prompt has a load-bearing bound). The proposal's own
# spec is "a single ≤200-char line".
_ROLLOUT_LINE_MAX = 200


def format_multi_pr_rollout_summary(
    proposals: Sequence[EvolutionProposal],
    slices_by_proposal: Mapping[str, Sequence[EvolutionPlan]],
) -> str:
    """Render a MULTI-PR-ROLLOUT line for an in-flight multi-slice proposal.

    Names the first proposal whose ``status`` is ``rolling_out`` — its id and
    title, how many of its slices have landed (persisted plan ``status`` ==
    ``merged``) out of the total, and the ``phase_index``/``status`` of the next
    slice not yet landed. When more than one proposal is ``rolling_out`` the
    line also notes ``(1 of N rolling out)`` so a concurrent second initiative
    is not silently invisible. Returns ``""`` when no proposal is mid-rollout,
    so the caller injects nothing — mirroring
    :func:`format_owned_net_delta_summary`'s self-suppressing contract. The line
    is bounded to :data:`_ROLLOUT_LINE_MAX` chars. Pure — zero SDK cost.
    """
    rolling_out = [p for p in proposals if p.status == "rolling_out"]
    if not rolling_out:
        return ""
    rolling = rolling_out[0]
    slices = list(slices_by_proposal.get(rolling.proposal_id, ()))
    total = len(slices)
    landed = sum(1 for s in slices if s.status == "merged")
    next_open = next((s for s in slices if s.status != "merged"), None)
    if next_open is None:
        next_desc = "next=none(all landed)"
    else:
        next_desc = f"next=phase{next_open.phase_index}({next_open.status})"
    concurrent = (
        f" (1 of {len(rolling_out)} rolling out)"
        if len(rolling_out) > 1
        else ""
    )
    body = (
        f"MULTI-PR-ROLLOUT: {rolling.proposal_id} "
        f'{landed}/{total} slices landed; {next_desc} — "{rolling.title}"'
    )
    # Order the line so the unbounded, LLM-generated title sits *last*: right-
    # truncation of a long title then clips the title, never the
    # ``{landed}/{total} slices landed; {next_desc}`` progress payload the
    # sensor exists to surface. Room for the concurrent suffix is likewise
    # reserved so the trailing ``(1 of N rolling out)`` note — which keeps a
    # concurrent second initiative visible — also survives the bound. Truncate
    # the body alone, then re-append the intact suffix.
    body_budget = _ROLLOUT_LINE_MAX - len(concurrent)
    if len(body) > body_budget:
        body = body[: body_budget - 1] + "…"
    return body + concurrent


def load_all_backlog_snapshots(data_dir: Path) -> list[BacklogSnapshot]:
    """Return every recorded snapshot oldest-first — the SQLite seed source.

    :func:`_load_owned_backlog_snapshots` reads the ledger in append (==
    chronological) order, so the result is already oldest-first. Seeding the
    SQLite store in chronological order lets its ``AUTOINCREMENT`` ``id``
    reproduce the append order, so a later ``ORDER BY id DESC`` read matches the
    JSONL newest-first read. A real call site
    (:class:`~maestro.state_store.SqliteOwnedBacklogLedger`'s one-time seed), so
    it does not trip the self-review uncalled-symbol gate.
    """
    return [
        _row_to_snapshot(row) for row in _load_owned_backlog_snapshots(data_dir)
    ]


class JsonlOwnedBacklogLedger:
    """JSONL-backed :class:`OwnedBacklogLedgerBackend` — the default, flag-off store.

    Binds a ``data_dir`` and delegates to the existing module-level
    :func:`record_owned_backlog_snapshot` write plus a newest-first read over
    :func:`_load_owned_backlog_snapshots`, which remain the JSONL implementation
    core so ``tests/test_stats.py`` and this adapter share one code path
    (behaviour-preserving: the ledger stays on ``owned_backlog_ledger.jsonl``).
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def record(
        self,
        iteration: int,
        count: int,
        *,
        now: str | None = None,
    ) -> BacklogSnapshot:
        snapshot = build_backlog_snapshot(iteration, count, now=now)
        record_owned_backlog_snapshot(
            self._data_dir, iteration, count, now=snapshot.timestamp,
        )
        return snapshot

    def recent(self, n: int) -> list[BacklogSnapshot]:
        if n <= 0:
            return []
        # ``_load_owned_backlog_snapshots`` returns oldest-first (append order);
        # reverse to newest-first to match the SQLite ``ORDER BY id DESC`` read,
        # then take the *n* most recent.
        snapshots = load_all_backlog_snapshots(self._data_dir)
        snapshots.reverse()
        return snapshots[:n]


def record_owned_backlog_once(
    ledger: OwnedBacklogLedgerBackend, iteration: int, count: int,
) -> bool:
    """Record one owned-backlog snapshot for *iteration*, at most once.

    Returns ``True`` when a snapshot was appended, ``False`` when the newest
    snapshot already carries *iteration* — a phase resumed in place at the same
    iteration re-reads the first attempt's snapshot instead of appending a
    duplicate that would make ``OWNED-NET-DELTA`` compare the cycle with itself
    (#4106). Iteration 0 (the non-loop CLI ``maestro contemplate``) always
    records, matching the bet path's non-loop exemption. The backends'
    append-only ``record`` contract is unchanged.
    """
    newest = ledger.recent(1) if iteration else None
    if newest and newest[0].iteration == iteration:
        return False
    ledger.record(iteration, count)
    return True


# ---------------------------------------------------------------------------
# Per-PR remediation-lap ledger — a read-only instrument that quantifies the
# treadmill per owned PR lineage. ``cost-report`` is global and ``cost-per-task``
# is per-task; neither aggregates every remediation lap against the *one PR* the
# laps re-remediate. This ledger groups the ``Remediate PR #<n>:`` lineage so a
# per-PR empty target (many laps, much time, OWNED-NET-DELTA still zero) is
# visible in numbers before the next expensive lap is staked. It measures only —
# no gate, no disposal, no stand-down; structurally incapable of "giving up".
# ---------------------------------------------------------------------------

# Matches a leading bracketed state tag in a remediation ``result_summary``
# (e.g. ``[stood-down]``, ``[merged]``, ``[ci-pending-hold]``, ``[fetch-missing]``)
# — the honest per-lap blocker label when the row carries no explicit ``blocker``
# field.
_BRACKET_TAG_PATTERN = re.compile(r"\[([^\]]+)\]")


@dataclass(frozen=True)
class PRLapLedgerRow:
    """One PR lineage's aggregated remediation-lap waste.

    Attributes
    ----------
    pr_number:
        The PR the grouped laps re-remediated.
    lap_count:
        Sum of the grouped rows' ``lap_count`` (each row defaults to one lap).
    total_duration_seconds:
        Sum of the grouped rows' durations (``duration_seconds`` /
        ``duration_secs``).
    blocker_labels:
        The distinct blocker labels seen across the lineage, sorted.
    latest_outcome:
        The outcome of the chronologically latest grouped row (input order is
        append order == chronological), ``""`` when none carried an outcome.
    """

    pr_number: int
    lap_count: int
    total_duration_seconds: float
    blocker_labels: tuple[str, ...]
    latest_outcome: str


def _ledger_pr_number(row: Mapping[str, object]) -> int | None:
    """Extract the PR number from a task row's goal/title/description.

    Tries each field in turn, matching the unified ``Remediate PR #<n>:`` header
    via :func:`~maestro.remediation_tracker.pr_number_from_remediation_goal`
    (start-anchored, so a stray ``#1234`` in a summary never misattributes).
    Returns ``None`` when no field yields a remediation-lineage PR number.
    """
    for key in ("goal", "title", "description"):
        value = row.get(key)
        if isinstance(value, str) and value:
            pr = pr_number_from_remediation_goal(value)
            if pr is not None:
                return pr
    return None


def _ledger_lap_count(row: Mapping[str, object]) -> int:
    """Return the row's ``lap_count`` (positive int), defaulting to one lap.

    A remediation task row is itself one lap, so a row lacking an explicit
    ``lap_count`` — the norm for real ``TaskRecord`` data — contributes exactly
    one lap. A non-positive or non-int value also falls back to one.
    """
    raw = row.get("lap_count", 1)
    if isinstance(raw, bool):
        return 1
    if isinstance(raw, int) and raw > 0:
        return raw
    return 1


def _ledger_duration_seconds(row: Mapping[str, object]) -> float:
    """Return the row's duration in seconds from either duration field.

    Prefers ``duration_seconds`` (the plan's field name) then falls back to
    ``duration_secs`` (the real :class:`~maestro.memory.TaskRecord` field);
    a missing or non-numeric value contributes zero.
    """
    for key in ("duration_seconds", "duration_secs"):
        raw = row.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
    return 0.0


def _ledger_blocker_label(row: Mapping[str, object]) -> str | None:
    """Return the row's blocker label, or ``None`` when none is present.

    Prefers an explicit non-empty ``blocker`` field; otherwise falls back to
    the leading bracketed state tag in ``result_summary`` (e.g. ``[stood-down]``
    → ``stood-down``), the honest per-lap label real remediation rows carry.
    """
    raw = row.get("blocker")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    summary = row.get("result_summary")
    if isinstance(summary, str):
        match = _BRACKET_TAG_PATTERN.search(summary)
        if match:
            return match.group(1).strip()
    return None


def aggregate_pr_lap_ledger(
    tasks: list[dict[str, object]],
) -> list[PRLapLedgerRow]:
    """Aggregate remediation-lap waste per PR lineage from task-history rows.

    Groups *tasks* by the PR number extracted from each row's
    ``Remediate PR #<n>:`` goal/title/description (see :func:`_ledger_pr_number`)
    and, per PR, sums ``lap_count`` (each row defaults to one lap) and duration
    seconds, collects the set of distinct blocker labels, and keeps the latest
    row's outcome. Rows with no extractable PR number are **dropped** — never
    attributed to another PR. Returns one :class:`PRLapLedgerRow` per distinct
    PR, sorted ascending by ``pr_number``.

    Pure over its input, zero SDK cost; **measurement-only** — no gate,
    actuator, disposal, or stand-down. It reads a narrative and returns a ledger
    so a per-PR empty target is visible in numbers before the next lap is staked.
    """
    lap_counts: dict[int, int] = {}
    durations: dict[int, float] = {}
    blockers: dict[int, set[str]] = {}
    outcomes: dict[int, str] = {}
    for row in tasks:
        if not isinstance(row, Mapping):
            continue
        pr = _ledger_pr_number(row)
        if pr is None:
            continue
        if pr not in lap_counts:
            lap_counts[pr] = 0
            durations[pr] = 0.0
            blockers[pr] = set()
            outcomes[pr] = ""
        lap_counts[pr] += _ledger_lap_count(row)
        durations[pr] += _ledger_duration_seconds(row)
        label = _ledger_blocker_label(row)
        if label:
            blockers[pr].add(label)
        outcome = row.get("outcome")
        if isinstance(outcome, str) and outcome:
            outcomes[pr] = outcome
    return [
        PRLapLedgerRow(
            pr_number=pr,
            lap_count=lap_counts[pr],
            total_duration_seconds=durations[pr],
            blocker_labels=tuple(sorted(blockers[pr])),
            latest_outcome=outcomes[pr],
        )
        for pr in sorted(lap_counts)
    ]
