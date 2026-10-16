"""Append-only cost sample JSONL and run-rate computation."""

from __future__ import annotations

import collections
import copy
import json
import logging
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast, get_args, runtime_checkable

from maestro.fileutil import file_lock, iter_jsonl_reverse

logger = logging.getLogger(__name__)

# Cache TTL for ``build_cost_rates`` / ``build_budget_breakdown`` results.
# Keeps the throttle decision close to real-time while collapsing the three
# dashboard endpoints + internal agent callers into one rebuild per window.
_CACHE_TTL_SECONDS = 5.0

# Hard cap on live entries per cache.  The dashboard accepts any float
# ``window_hours``, so without a bound memory would grow with request
# diversity; expired entries are swept first, then the oldest-inserted go.
_CACHE_MAX_ENTRIES = 32


CostBilling = Literal["subscription", "metered"]
COST_BILLINGS: frozenset[str] = frozenset(get_args(CostBilling))


@dataclass
class CostSample:
    """One SDK call's cost and token data."""

    timestamp: str  # ISO 8601 UTC
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    caller: str
    duration_ms: int
    # ``True`` when ``cost_usd`` was derived from token counts via the
    # SDK pricing table rather than read from a ``ResultMessage``. Lets
    # post-hoc analysis distinguish reported-vs-estimated dollars while
    # keeping :func:`build_cost_rates` arithmetic simple (an estimated
    # dollar still counts toward the burn-rate gate).
    estimated: bool = False
    # ``True`` when the call's usage could not be measured (e.g. a
    # ``cli_paced`` turn whose transcript sidecar was unreadable). The fields
    # are then *unknown*, not measured-as-zero: :func:`build_cost_rates`
    # excludes the sample from rate/burn-rate aggregation entirely, and the
    # Budget breakdown flags the owning caller/group so the dashboard renders
    # ``—`` rather than ``$0.00`` (proposal 058 Step 13).
    unmetered: bool = False
    # Registry name of the provider that served the call, from
    # ``QueryUsage.provider`` (proposal 067 Step 2) — lets the Budget
    # breakdown attribute spend (and unknown spend in particular) to the
    # producing provider. ``None`` on rows written before attribution landed.
    provider: str | None = None
    # Effort that actually reached the provider (``"default"`` when none was
    # sent) and the resolved model that served the call (proposal 140).
    # ``None`` on rows written before attribution landed.
    effort: str | None = None
    model: str | None = None
    # Whether the call is cash outlay (``metered``: API-key or OpenAI billed)
    # or covered by the subscription (``subscription``). A legacy row written
    # before the stamp landed reads as ``subscription``.
    billing: CostBilling = "subscription"


def resolve_billing(provider: str | None, auth_method: str | None) -> CostBilling:
    """Return the billing basis of a call served by *provider* under *auth_method*.

    ``metered`` for an OpenAI call or an API-key-authenticated call; otherwise
    ``subscription`` (the OAuth subscription serves ``anthropic`` and
    ``cli_paced`` alike). Pure.
    """
    if provider == "openai" or auth_method == "api_key":
        return "metered"
    return "subscription"


@runtime_checkable
class CostSampleBackend(Protocol):
    """The cost-sample persistence surface (proposal 106 Phase B Step 3).

    One writer (``sdk._log_sdk_call``) and a handful of readers — the burn-rate
    throttle, the Budget dashboard tab, the dream-cycle phase report — go
    through this seam, so cost samples can live in ``cost_samples.jsonl`` or in
    ``state.db`` behind ``[paths] sqlite_state_enabled`` with no call-site
    branching. Mirrors the :class:`~maestro.task_queue.TaskQueueBackend` idiom;
    ``@runtime_checkable`` enables a structural ``isinstance`` check in the
    agent's backend-selection tests.
    """

    def append(self, sample: CostSample) -> None:
        """Persist one cost sample."""
        ...

    def samples_in_window(
        self, window_hours: float, *, now: datetime | None = None,
    ) -> list[CostSample]:
        """Return samples within the trailing window, oldest-first.

        Includes unmetered samples (callers filter them as needed). *now*
        overrides the window's reference instant for deterministic tests.
        """
        ...

    def recent_samples(self, limit: int) -> list[CostSample]:
        """Return the most recent *limit* samples, oldest-first."""
        ...

    def cache_key(self) -> tuple[str, int | None]:
        """Return ``(identity, version)`` for the 5-second TTL cache.

        *identity* names the backing store; *version* advances on every append
        (JSONL: the file ``mtime_ns``; SQLite: the max sample id) so a fresh
        sample invalidates the cached snapshot within the TTL window.
        """
        ...


@dataclass(frozen=True)
class CallerRate:
    """Per-caller cost and call count within a rate window.

    Mirrors :class:`CallerSpend`'s ``calls: int`` so the count stays a cardinal
    all the way out through ``asdict`` into the ``/api/cost-rates`` response —
    the aggregation loop accumulates in floats and narrows here, at
    construction, exactly as :func:`build_budget_breakdown` does.
    """

    cost_usd: float
    calls: int


@dataclass(frozen=True)
class CostRateSnapshot:
    """Aggregated cost rates over a time window."""

    window_start: str  # ISO 8601
    window_end: str  # ISO 8601
    window_seconds: float
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    # Metered calls only: :func:`_build_cost_rates_uncached` drops unmetered
    # samples before aggregating, so this counts spend-bearing calls — unlike
    # :attr:`BudgetBreakdown.total_calls`, which counts every call in the window
    # (metered and unmetered) for the dashboard's activity total.
    total_calls: int
    cost_per_hour: float
    cost_per_day: float
    cost_per_month: float
    tokens_in_per_hour: float
    tokens_out_per_hour: float
    by_caller: dict[str, CallerRate]


# Maps each SDK ``caller`` to a logical phase bucket rendered on the
# dashboard Budget tab and the dream-cycle phase cost report.  The key set
# must cover every ``call_policy.CALL_POLICIES`` tag, and any key outside
# that registry must be listed in ``RETIRED_CALLERS`` (both pinned by
# ``tests/test_cost_tracking.py``), so ``"Other"`` is reachable only for a
# tag that bypasses the call-policy registry.
CALLER_GROUPS: dict[str, str] = {
    # Contemplation
    "introspect": "Contemplation",
    "evolution_introspect": "Contemplation",
    "update_self_model": "Contemplation",
    "respond_to_messages": "Contemplation",
    "contemplation_session": "Contemplation",
    # Evolution
    "reflect": "Evolution",
    "propose_plan": "Evolution",
    "evolution_session": "Evolution",
    "propose_plan_chain": "Evolution",
    "curate_pending_plans": "Evolution",
    "curate_session": "Evolution",
    "rejection_classifier": "Evolution",
    "rejection_session": "Evolution",
    "rejection_self_model_session": "Evolution",
    # Implementation
    "implement_plan": "Implementation",
    "self_review": "Implementation",
    "auto_fix": "Implementation",
    "implementation_session": "Implementation",
    "replan_for_scope": "Implementation",
    # Planning & Execution
    "planner": "Planning & Execution",
    "replan": "Planning & Execution",
    "execute_step": "Planning & Execution",
    "distill_findings": "Planning & Execution",
    "run_session": "Planning & Execution",
    "assess_criteria_outcome": "Planning & Execution",
    # Dreaming
    "dream": "Dreaming",
    "dream_validate": "Dreaming",
    # Task Generation
    "generate_task": "Task Generation",
    "task_generation_session": "Task Generation",
    # Probes
    "startup_probe": "Probes",
    "rate_limit_idle_probe": "Probes",
    "warning_hold_probe": "Probes",
    "workspace_counterfactual": "Probes",
    # Artifacts & Docs
    "artifact_review": "Artifacts & Docs",
    "artifact_review_session": "Artifacts & Docs",
    "doc_proposals": "Artifacts & Docs",
    "doc_proposals_session": "Artifacts & Docs",
    # Scouting
    "scout_opportunities": "Scouting",
    # Engagements
    "quote_intake": "Engagements",
}

# Callers retired from ``CALL_POLICIES`` whose ``CALLER_GROUPS`` entry is
# kept so historical usage rows still roll up to their phase, not "Other".
RETIRED_CALLERS: frozenset[str] = frozenset({
    # Proposal 149 Step 4 retired ``propose_task_docs``.
    "doc_proposals",
    "doc_proposals_session",
    # Proposal 149 Step 1 retired the unpriced ``evolution_introspect`` arm of
    # ``Consciousness.introspect``.
    "evolution_introspect",
    # Proposal 149 Step 5 deleted the priced dream digest.
    "dream",
    "dream_validate",
    # Proposal 155 Step 4 deleted the planning reflection: evolve plans a gap.
    "reflect",
    # The revenue lineage (scouting and service quotes) was retired.
    "scout_opportunities",
    "quote_intake",
})

_OTHER_GROUP = "Other"

# Returned verbatim by :func:`generate_cost_report` when the usage log is
# missing or carries no parseable rows — the operator-facing CLI never raises
# on a fresh deployment whose ``usage.jsonl`` does not exist yet.
_NO_USAGE_DATA = "No usage data available"

# Returned when ``usage.jsonl`` exists but cannot be opened — a permission
# error, an ``IsADirectoryError`` (plausible under the documented bind-mount
# permission issues), or a path that vanished between the ``exists()`` check and
# the ``open()``. Mirrors ``read_iteration_usage``'s OSError convention so the
# operator-facing CLI honours its "never raises" contract and surfaces the
# unreadable file rather than masking it as "no data".
_UNREADABLE_USAGE = "Usage log exists but could not be read"

# Stand-in provider key for a usage row that carried no ``providers`` stamp
# (pre-attribution rows, hand-rolled usages). Keeps the per-provider sum equal
# to the total cost rather than silently dropping such rows' spend.
_UNKNOWN_PROVIDER = "unknown"

# Relative token-count gap past which the cli_paced ``/cost`` probe reading and
# the transcript-sidecar count are considered divergent (proposal 062 Step 5).
# Both readings are session-cumulative token totals for the same session, so a
# small gap is rounding/lag; a gap past this fraction of the sidecar count means
# one of the two parsers has fallen behind a CLI layout change and deserves a
# WARNING. The sidecar stays authoritative either way.
METERING_DIVERGENCE_THRESHOLD = 0.1


@dataclass(frozen=True)
class PlanHeadroom:
    """Subscription plan rate-limit headroom from the cli_paced ``/usage`` probe.

    The one session reading neither the API response nor the transcript sidecar
    exposes (proposal 062 Step 5). Mirrors ``maestro_sdk``'s ``UsageReport`` with
    plain built-in types so this module stays free of the ``maestro_sdk`` import.
    Read-only: the burn-rate throttle reports it on transitions but never feeds
    it into gating math (that wiring is explicitly deferred). ``percent_used``
    is ``None`` on a non-subscription login or a layout miss — never a
    fabricated percentage.
    """

    percent_used: float | None = None
    label: str | None = None


def log_metering_divergence(
    probe_total_tokens: int | None,
    sidecar_total_tokens: int | None,
    *,
    caller: str | None = None,
    threshold: float = METERING_DIVERGENCE_THRESHOLD,
) -> bool:
    """Log one ``metering_divergence`` WARNING when the two readings disagree.

    Cross-checks the cli_paced ``/cost`` probe's session token total against the
    transcript sidecar's (proposal 062 Step 5). Returns ``True`` when a WARNING
    was emitted, ``False`` otherwise. Either reading being ``None`` (probe parse
    miss, or no metered sidecar count) compares nothing — an honest missing
    reading is not a divergence. A zero sidecar total diverges on any non-zero
    probe reading (the relative gap is unbounded); otherwise the gap is relative
    to the sidecar count, which stays the authoritative figure — this helper
    only ever logs, never overrides.
    """
    if probe_total_tokens is None or sidecar_total_tokens is None:
        return False
    if sidecar_total_tokens == 0:
        diverged = probe_total_tokens != 0
    else:
        gap = abs(probe_total_tokens - sidecar_total_tokens) / sidecar_total_tokens
        diverged = gap > threshold
    if not diverged:
        return False
    logger.warning(
        "metering_divergence: /cost probe read %d session tokens but the "
        "transcript sidecar metered %d (threshold %.0f%%, caller=%s); the "
        "sidecar count stays authoritative — a parser has likely fallen "
        "behind a CLI layout change",
        probe_total_tokens,
        sidecar_total_tokens,
        threshold * 100,
        caller or "unknown",
    )
    return True


@dataclass(frozen=True)
class CallerSpend:
    """Per-caller spend within a budget window."""

    caller: str
    group: str
    cost_usd: float
    calls: int
    reported_cost_usd: float
    estimated_cost_usd: float
    share_of_total: float  # 0.0-1.0
    # ``True`` when *every* call from this caller in the window came from a
    # non-metering provider, so the cost figures are unknown rather than zero
    # (proposal 058 Step 13). The dashboard renders ``—`` for such rows. A
    # caller with even one metered sample stays ``False`` (its cost is a real,
    # if partial, figure).
    unmetered: bool = False
    # Sorted distinct provider names over this caller's calls in the window
    # (proposal 067 Step 2); pre-attribution rows (``provider=None``) are
    # dropped, so an empty list means "no attributed call" and the dashboard
    # renders ``—`` for the provider cell.
    providers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GroupSpend:
    """Per-phase-group spend within a budget window."""

    group: str
    cost_usd: float
    calls: int
    share_of_total: float  # 0.0-1.0
    # ``True`` when every caller rolled up into this group is unmetered, so
    # the group's cost is unknown rather than zero (see :class:`CallerSpend`).
    unmetered: bool = False


@dataclass(frozen=True)
class BudgetBreakdown:
    """Caller- and group-level spend breakdown for the Budget dashboard tab."""

    window_start: str
    window_end: str
    window_hours: float
    total_cost_usd: float
    reported_cost_usd: float
    estimated_cost_usd: float
    total_calls: int
    by_caller: list[CallerSpend]  # sorted descending by cost_usd
    by_group: list[GroupSpend]  # sorted descending by cost_usd


def _zeroed_budget(window_hours: float) -> BudgetBreakdown:
    now = datetime.now(UTC).isoformat()
    return BudgetBreakdown(
        window_start=now,
        window_end=now,
        window_hours=window_hours,
        total_cost_usd=0.0,
        reported_cost_usd=0.0,
        estimated_cost_usd=0.0,
        total_calls=0,
        by_caller=[],
        by_group=[],
    )


def _zeroed_snapshot() -> CostRateSnapshot:
    now = datetime.now(UTC).isoformat()
    return CostRateSnapshot(
        window_start=now,
        window_end=now,
        window_seconds=0.0,
        total_cost_usd=0.0,
        total_input_tokens=0,
        total_output_tokens=0,
        total_cache_read_tokens=0,
        total_calls=0,
        cost_per_hour=0.0,
        cost_per_day=0.0,
        cost_per_month=0.0,
        tokens_in_per_hour=0.0,
        tokens_out_per_hour=0.0,
        by_caller={},
    )


def append_cost_sample(path: Path, sample: CostSample) -> None:
    """Append one JSONL line to the cost samples file.

    The append takes ``file_lock`` because ``Memory.archive`` rewrites
    ``cost_samples.jsonl`` under that lock (``read_text`` ->
    ``atomic_write``).  An advisory ``flock`` only excludes writers who also
    take it, so an unlocked append landing inside a rotation is destroyed by
    the rename.  This is the highest-frequency appender on a rotated file —
    every metered SDK call writes a sample — so it has the widest window to
    land inside one.  The ``mkdir`` stays above the lock: ``file_lock``
    creates its ``.lock`` file adjacent to *path* and needs the parent to
    exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path), open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(sample)) + "\n")


def _stat_mtime_ns(path: Path) -> int | None:
    """Return ``path.stat().st_mtime_ns`` or ``None`` if stat fails.

    Used as part of the cache key so a fresh sample appearing mid-poll
    surfaces immediately even within the TTL window.
    """
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


@dataclass
class _CostRateCacheEntry:
    expires_at: float
    version: int | None
    snapshot: CostRateSnapshot


@dataclass
class _BudgetCacheEntry:
    expires_at: float
    version: int | None
    breakdown: BudgetBreakdown


_cost_rate_cache: dict[tuple[str, float], _CostRateCacheEntry] = {}
_budget_cache: dict[tuple[str, float], _BudgetCacheEntry] = {}


def _store_cache_entry[E: (_CostRateCacheEntry, _BudgetCacheEntry)](
    cache: dict[tuple[str, float], E], key: tuple[str, float], entry: E, now: float,
) -> None:
    """Insert *entry* under *key*, evicting expired and over-capacity entries.

    Expired entries are dropped first; then the oldest-inserted keys are
    popped until there is room.  *key* is popped before re-insertion so a
    refreshed entry moves to the back and dict order stays a truthful FIFO.
    """
    cache.pop(key, None)
    for stale in [k for k, v in cache.items() if v.expires_at <= now]:
        del cache[stale]
    while len(cache) >= _CACHE_MAX_ENTRIES:
        del cache[next(iter(cache))]
    cache[key] = entry


def _clear_cost_rate_cache() -> None:
    """Test-only: drop both module-level caches.

    Production code never calls this; ``mem_fs`` does not move ``mtime``
    forward when files are rewritten in-memory, so tests that re-run
    ``build_cost_rates`` against a mutated fixture explicitly clear the
    cache between calls.
    """
    _cost_rate_cache.clear()
    _budget_cache.clear()


def _as_utc(timestamp: str) -> datetime:
    """Parse an ISO *timestamp* into a UTC-aware datetime.

    The single owner of the naive→UTC coercion for stored **cost-sample**
    stamps — both aggregators, the JSONL window cutoff, and the SQLite
    ``ts_epoch`` write-path derivation — plus the usage row ``started_at``
    the cost report buckets by date through :func:`_parse_utc_date`. A naive
    value is *read as* UTC (both write paths emit UTC), an aware value is
    converted to UTC, so every consumer compares within one homogeneous
    domain — mixing the two raises ``TypeError`` on ``min()`` / ``max()`` /
    subtraction, which is what the aggregators used to do.

    Deliberately **not** the owner for :func:`_filter_records_since` and
    :func:`_split_records_at`, which *drop* a naive ``started_at`` rather than
    coerce it: an A/B cutoff must not silently admit a row whose instant is
    genuinely unknown. Neither may be folded onto this rule.

    Reading a naive stamp as UTC is a codebase-wide convention rather than this
    helper's invention — ``audit_checks._parse_iso_utc``,
    ``task_cost_gate._parse_task_timestamp``, ``publication._parse_iso`` and
    ``remediation_tracker._parse_iso_timestamp`` each keep a local tolerant
    parser applying it to their own records — so what is owned here is the
    *cost-sample domain* named above, not the rule itself. Two neighbours sit
    close enough that a change to the rule must change them too:

    - ``Memory._record_timestamp`` (``maestro/memory.py``) dates the very same
      rows: archive rotation splits ``cost_samples.jsonl`` and ``usage.jsonl``
      (plus task and plan rows) by this reading, under a contract this helper
      does not have — three candidate keys tried in order, and ``None`` rather
      than a raised ``ValueError`` so an undatable row stays hot. Diverging
      would age one naive row two different ways — deciding its hot/cold tier
      here and its window membership there — over the same bytes.
    - ``journal._ensure_utc`` (``maestro/journal.py``, shared with
      ``health_check``) is the closest-shaped sibling: a ``datetime`` →
      ``datetime`` normalizer that stamps a naive value UTC but leaves an aware
      one at its own offset. That buys mutual comparability, which is all its
      callers need; it stops short of the single *representation* these callers
      need — ``window_start`` / ``window_end`` are rendered as ISO strings —
      hence the ``astimezone`` below.

    Neither is folded onto this one: a rule this small should not add a
    ``memory`` → ``cost_tracking`` import edge, nor pull a state-and-memory
    module into this infra one. Each site is pinned by its own test.

    An unparseable value propagates its ``ValueError`` (and a non-string its
    ``TypeError``) rather than being absorbed here: callers that tolerate a
    malformed row own that decision at their own skip site.
    """
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cost_sample_from_dict(
    data: dict[str, Any], path: Path,
) -> CostSample | None:
    """Parse one raw JSONL record into a :class:`CostSample`, or ``None``.

    A record with valid JSON but a missing or non-coercible semantic field is
    skipped with a warning (returning ``None``), matching the historical
    forward-scan behaviour. *path* is named only for the warning message.
    """
    raw_provider = data.get("provider")
    raw_effort = data.get("effort")
    raw_model = data.get("model")
    raw_billing = data.get("billing")
    billing: CostBilling = (
        cast(CostBilling, raw_billing) if raw_billing in COST_BILLINGS
        else "subscription"
    )
    try:
        return CostSample(
            timestamp=str(data["timestamp"]),
            cost_usd=float(data["cost_usd"]),
            input_tokens=int(data["input_tokens"]),
            output_tokens=int(data["output_tokens"]),
            cache_read_tokens=int(data["cache_read_tokens"]),
            cache_creation_tokens=int(data["cache_creation_tokens"]),
            caller=str(data["caller"]),
            duration_ms=int(data["duration_ms"]),
            estimated=bool(data.get("estimated", False)),
            unmetered=bool(data.get("unmetered", False)),
            provider=raw_provider if isinstance(raw_provider, str) else None,
            effort=raw_effort if isinstance(raw_effort, str) else None,
            model=raw_model if isinstance(raw_model, str) else None,
            billing=billing,
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("Skipping malformed cost sample line in %s", path)
        return None


def _iter_samples_in_window(
    path: Path, window_hours: float, *, now: datetime | None = None,
) -> list[CostSample]:
    """Reverse-walk *path* and collect samples within *window_hours*.

    Cost samples are append-only with monotonically advancing timestamps,
    so the first sample older than the cutoff terminates the walk — every
    earlier line stays unparsed.  Lines with valid JSON but missing
    semantic fields are skipped with a warning, matching the previous
    forward-scan behaviour. *now* overrides the window's reference instant
    for deterministic tests (default: the current UTC time).
    """
    if not path.exists():
        return []

    cutoff = (now or datetime.now(UTC)).timestamp() - window_hours * 3600
    samples: list[CostSample] = []

    for data in iter_jsonl_reverse(path):
        try:
            ts_dt = _as_utc(data["timestamp"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed cost sample line in %s", path)
            continue
        if ts_dt.timestamp() < cutoff:
            break
        sample = _cost_sample_from_dict(data, path)
        if sample is not None:
            samples.append(sample)

    samples.reverse()  # restore oldest-first order for stable aggregation.
    return samples


def _tail_cost_samples(path: Path, limit: int) -> list[CostSample]:
    """Return the last *limit* samples from *path*, oldest-first.

    Reads only the file tail via a bounded :class:`~collections.deque`, so a
    large log is never fully materialised, then parses each line into a
    :class:`CostSample` (malformed lines skipped with a warning, as in
    :func:`_iter_samples_in_window`). A non-positive *limit*, a missing file,
    or an unreadable file returns an empty list.
    """
    if limit <= 0 or not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            tail: deque[str] = deque(fh, maxlen=limit)
    except OSError:
        logger.warning("Cannot read %s", path)
        return []

    samples: list[CostSample] = []
    for raw in tail:
        line = raw.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            logger.warning("Skipping malformed cost sample line in %s", path)
            continue
        if not isinstance(data, dict):
            logger.warning("Skipping malformed cost sample line in %s", path)
            continue
        sample = _cost_sample_from_dict(data, path)
        if sample is not None:
            samples.append(sample)
    return samples


def build_cost_rates(
    backend: CostSampleBackend, window_hours: float = 1.0,
) -> CostRateSnapshot:
    """Read cost samples within *window_hours* from *backend*, compute rates.

    Returns a zeroed snapshot if the backing store is empty or has no data in
    the window.  Results are cached for ``_CACHE_TTL_SECONDS`` keyed on the
    backend's ``(identity, window_hours)`` with its ``version`` (file mtime for
    JSONL, max sample id for SQLite) as the freshness check, so the three
    dashboard endpoints plus the agent's internal burn-rate / throttle / status
    callers share one rebuild per window.
    """
    now = time.monotonic()
    key_id, version = backend.cache_key()
    cache_key = (key_id, window_hours)
    cached = _cost_rate_cache.get(cache_key)
    if (
        cached is not None
        and cached.version == version
        and now < cached.expires_at
    ):
        return copy.deepcopy(cached.snapshot)

    snapshot = _build_cost_rates_uncached(backend, window_hours)
    _store_cache_entry(_cost_rate_cache, cache_key, _CostRateCacheEntry(
        expires_at=now + _CACHE_TTL_SECONDS,
        version=version,
        snapshot=snapshot,
    ), now)
    return snapshot


def _build_cost_rates_uncached(
    backend: CostSampleBackend, window_hours: float,
) -> CostRateSnapshot:
    # Unmetered samples carry no real token/cost data, so they are excluded
    # from rate aggregation entirely — the burn-rate gate (which divides
    # ``cost_per_hour`` by the budget limit) must never read their placeholder
    # zeros as genuine spend.  A window of only unmetered calls therefore
    # yields a zeroed snapshot, the same as an empty window.
    samples = [s for s in backend.samples_in_window(window_hours) if not s.unmetered]
    if not samples:
        return _zeroed_snapshot()

    timestamps = [_as_utc(s.timestamp) for s in samples]
    window_start = min(timestamps)
    window_end = max(timestamps)
    window_seconds = max((window_end - window_start).total_seconds(), window_hours * 3600)

    total_cost = sum(s.cost_usd for s in samples)
    total_in = sum(s.input_tokens for s in samples)
    total_out = sum(s.output_tokens for s in samples)
    total_cache_read = sum(s.cache_read_tokens for s in samples)

    hours = window_seconds / 3600

    caller_data: dict[str, dict[str, float]] = {}
    for s in samples:
        entry = caller_data.setdefault(s.caller, {"cost_usd": 0.0, "calls": 0.0})
        entry["cost_usd"] += s.cost_usd
        entry["calls"] += 1.0

    by_caller: dict[str, CallerRate] = {
        caller: CallerRate(cost_usd=vals["cost_usd"], calls=int(vals["calls"]))
        for caller, vals in caller_data.items()
    }

    return CostRateSnapshot(
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        window_seconds=window_seconds,
        total_cost_usd=total_cost,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_cache_read_tokens=total_cache_read,
        total_calls=len(samples),
        cost_per_hour=total_cost / hours,
        cost_per_day=(total_cost / hours) * 24,
        cost_per_month=(total_cost / hours) * 24 * 30,
        tokens_in_per_hour=total_in / hours,
        tokens_out_per_hour=total_out / hours,
        by_caller=by_caller,
    )


def build_budget_breakdown(
    backend: CostSampleBackend, window_hours: float = 1.0,
) -> BudgetBreakdown:
    """Read cost samples within *window_hours* from *backend*, group by caller.

    Returns a zeroed :class:`BudgetBreakdown` if the backing store is empty or
    has no data in the window.  Callers not in :data:`CALLER_GROUPS` fall
    through to ``"Other"``.  Shares the same TTL + version cache shape as
    :func:`build_cost_rates`.
    """
    now = time.monotonic()
    key_id, version = backend.cache_key()
    cache_key = (key_id, window_hours)
    cached = _budget_cache.get(cache_key)
    if (
        cached is not None
        and cached.version == version
        and now < cached.expires_at
    ):
        return copy.deepcopy(cached.breakdown)

    breakdown = _build_budget_breakdown_uncached(backend, window_hours)
    _store_cache_entry(_budget_cache, cache_key, _BudgetCacheEntry(
        expires_at=now + _CACHE_TTL_SECONDS,
        version=version,
        breakdown=breakdown,
    ), now)
    return breakdown


def _build_budget_breakdown_uncached(
    backend: CostSampleBackend, window_hours: float,
) -> BudgetBreakdown:
    samples = backend.samples_in_window(window_hours)
    if not samples:
        return _zeroed_budget(window_hours)

    timestamps = [_as_utc(s.timestamp) for s in samples]
    window_start = min(timestamps)
    window_end = max(timestamps)

    # Unmetered samples are kept here (so their caller/group rows still render,
    # flagged ``unmetered`` → ``—``), but they always carry ``cost_usd=0.0``:
    # ``_log_sdk_call`` coerces an unmetered call's ``total_cost_usd=None`` to
    # ``0.0`` at the single write site, so they contribute nothing to these
    # sums and need no filtering. A nonzero unmetered cost would be a bug, and
    # is deliberately left to surface rather than be silently filtered.
    total_cost = sum(s.cost_usd for s in samples)
    reported_cost = sum(s.cost_usd for s in samples if not s.estimated)
    estimated_cost = sum(s.cost_usd for s in samples if s.estimated)

    # Per-caller aggregation. ``unmetered_calls`` counts the rows whose
    # provider could not meter cost; a caller is flagged unmetered only when
    # *all* of its calls were unmetered (one metered sample makes the figure
    # real, if partial).
    caller_data: dict[str, dict[str, float]] = {}
    # Distinct producing providers per caller (proposal 067 Step 2); kept
    # beside the float-valued aggregation dict rather than inside it.
    caller_providers: dict[str, set[str]] = {}
    for s in samples:
        entry = caller_data.setdefault(s.caller, {
            "cost_usd": 0.0, "calls": 0.0,
            "reported_cost_usd": 0.0, "estimated_cost_usd": 0.0,
            "unmetered_calls": 0.0,
        })
        entry["cost_usd"] += s.cost_usd
        entry["calls"] += 1.0
        if s.provider is not None:
            caller_providers.setdefault(s.caller, set()).add(s.provider)
        if s.unmetered:
            entry["unmetered_calls"] += 1.0
        elif s.estimated:
            entry["estimated_cost_usd"] += s.cost_usd
        else:
            entry["reported_cost_usd"] += s.cost_usd

    by_caller: list[CallerSpend] = [
        CallerSpend(
            caller=caller,
            group=CALLER_GROUPS.get(caller, _OTHER_GROUP),
            cost_usd=vals["cost_usd"],
            calls=int(vals["calls"]),
            reported_cost_usd=vals["reported_cost_usd"],
            estimated_cost_usd=vals["estimated_cost_usd"],
            share_of_total=(vals["cost_usd"] / total_cost) if total_cost > 0 else 0.0,
            unmetered=vals["unmetered_calls"] == vals["calls"] and vals["calls"] > 0,
            providers=sorted(caller_providers.get(caller, set())),
        )
        for caller, vals in caller_data.items()
    ]
    by_caller.sort(key=lambda c: c.cost_usd, reverse=True)

    # Per-group aggregation (roll up caller rows by their group). A group is
    # unmetered only when every caller rolled into it is unmetered.
    group_data: dict[str, dict[str, float]] = {}
    for c in by_caller:
        entry = group_data.setdefault(c.group, {
            "cost_usd": 0.0, "calls": 0.0, "metered_callers": 0.0,
        })
        entry["cost_usd"] += c.cost_usd
        entry["calls"] += c.calls
        if not c.unmetered:
            entry["metered_callers"] += 1.0

    by_group: list[GroupSpend] = [
        GroupSpend(
            group=group,
            cost_usd=vals["cost_usd"],
            calls=int(vals["calls"]),
            share_of_total=(vals["cost_usd"] / total_cost) if total_cost > 0 else 0.0,
            unmetered=vals["metered_callers"] == 0.0,
        )
        for group, vals in group_data.items()
    ]
    by_group.sort(key=lambda g: g.cost_usd, reverse=True)

    return BudgetBreakdown(
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        window_hours=window_hours,
        total_cost_usd=total_cost,
        reported_cost_usd=reported_cost,
        estimated_cost_usd=estimated_cost,
        total_calls=len(samples),
        by_caller=by_caller,
        by_group=by_group,
    )


class JsonlCostSampleStore:
    """JSONL-backed :class:`CostSampleBackend` — the default, flag-off store.

    Wraps the append-only ``cost_samples.jsonl`` primitives so the read/write
    surface is identical whether samples live in the file or in ``state.db``.
    Behaviour-preserving: every method delegates to the existing module
    functions, and ``cache_key`` reproduces the former ``(str(path), mtime_ns)``
    freshness key exactly (a fresh sample bumps the file mtime, so a mid-poll
    append still surfaces within the TTL window).
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, sample: CostSample) -> None:
        append_cost_sample(self._path, sample)

    def samples_in_window(
        self, window_hours: float, *, now: datetime | None = None,
    ) -> list[CostSample]:
        return _iter_samples_in_window(self._path, window_hours, now=now)

    def recent_samples(self, limit: int) -> list[CostSample]:
        return _tail_cost_samples(self._path, limit)

    def cache_key(self) -> tuple[str, int | None]:
        return (str(self._path), _stat_mtime_ns(self._path))


def cost_per_task(
    records: collections.abc.Sequence[dict[str, object]],
) -> dict[str | None, float]:
    """Group usage records by ``task_id`` and sum ``total_cost_usd`` per group.

    *records* is a sequence of dicts as produced by
    :meth:`~maestro.sdk.IterationUsage.to_dict` (typically read from
    ``usage.jsonl``).  Each record is expected to carry:

    * ``task_id`` — ``str | None``; records missing the key are grouped
      under ``None``.
    * ``total_cost_usd`` — ``float``; records missing or non-numeric are
      treated as ``0.0``.
    * ``unmetered`` — ``bool``; a truthy value means the provider could not
      meter the iteration, so its (placeholder-zero) cost is unknown rather
      than measured.  Such records contribute ``0.0`` to their task group
      but still register the ``task_id`` key (proposal 058 Step 13).
    * ``task_cost_usd`` / ``task_unmetered`` — the task-scoped slice of a
      tagged row (#4066): only the spend made while the task run was open.
      A tagged row carrying ``task_cost_usd`` contributes that figure (or
      ``0.0`` when ``task_unmetered``) rather than the whole iteration's
      ``total_cost_usd``, so other phases' spend is not billed to the task.
      Legacy tagged rows written before the field existed fall back to
      ``total_cost_usd`` / ``unmetered`` — an upper bound on the task's cost.
      Untagged (``None``) rows always sum ``total_cost_usd``.

    Returns a mapping ``{task_id: summed_cost_usd}``.  An empty input
    produces an empty dict.
    """
    if not records:
        return {}

    totals: dict[str | None, float] = {}
    for rec in records:
        raw_tid = rec.get("task_id")
        tid: str | None = raw_tid if isinstance(raw_tid, str) else None
        task_scoped = tid is not None and "task_cost_usd" in rec
        unmetered_key = "task_unmetered" if task_scoped else "unmetered"
        cost_key = "task_cost_usd" if task_scoped else "total_cost_usd"
        if rec.get(unmetered_key):
            cost = 0.0
        else:
            try:
                cost = float(rec.get(cost_key, 0.0))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                cost = 0.0
        totals[tid] = totals.get(tid, 0.0) + cost
    return totals


def cost_for_task(usage_path: Path, task_id: str) -> float | None:
    """Return the summed USD cost for *task_id* from ``usage.jsonl``, or ``None``.

    Reads *usage_path* (a missing or unreadable file yields no rows), groups the
    rows by ``task_id`` via :func:`cost_per_task`, and returns that task's
    summed measured spend — the spend made while the task run was open, not
    the whole iteration's (legacy rows are an upper bound). Returns ``None``
    when the task has no usage rows at all, letting the operator-facing CLI
    distinguish a genuine ``$0.00`` task (e.g. an all-unmetered task) from an
    unknown one. Never raises.
    """
    totals = cost_per_task(_read_jsonl_dicts(usage_path))
    return totals.get(task_id)


def _coerce_int(value: object) -> int:
    """A usage-row token field as ``int``; a missing or non-int value -> ``0``.

    Token fields are serialised as integers in ``usage.jsonl``; anything else
    (a missing key read as ``None``, a corrupted string) counts as ``0``.
    """
    return value if isinstance(value, int) else 0


@dataclass(frozen=True)
class ShapeCost:
    """Per-task-shape cost aggregate for the burn-rate baseline.

    *shape* is a :data:`~maestro.audit_checks.TaskShape` value; *task_count* the
    number of distinct task-attributable cost rows in the bucket;
    *total_cost_usd* their summed measured spend; *avg_cost_per_task_usd* the
    per-task mean (``total_cost_usd / task_count``).
    """

    shape: str
    task_count: int
    total_cost_usd: float
    avg_cost_per_task_usd: float


def cost_per_task_shape(
    usage_records: collections.abc.Sequence[dict[str, object]],
    task_records: collections.abc.Sequence[dict[str, object]],
) -> dict[str, ShapeCost]:
    """Bucket per-task cost by :func:`classify_task_shape` for the baseline.

    Joins :func:`cost_per_task` output (``{task_id: cost}``) with task goals
    read from ``task_history.jsonl`` rows, classifies each task via
    :func:`~maestro.audit_checks.classify_task_shape`, and aggregates measured
    spend per shape bucket (proposal 084 Phase 1).

    * *usage_records* — ``usage.jsonl`` rows (``IterationUsage.to_dict`` shape);
      unmetered rows contribute ``0.0`` exactly as in :func:`cost_per_task`.
    * *task_records* — ``task_history.jsonl`` rows carrying ``task_id`` and
      ``goal``; a later row for the same ``task_id`` wins, matching
      :meth:`~maestro.memory.Memory.get_task_record`'s last-match semantics.

    Costs whose ``task_id`` is ``None`` (non-task loop overhead) or absent from
    *task_records* (no goal to classify) are excluded — the baseline counts
    only task-attributable spend with a classifiable goal. An empty join
    produces an empty dict.
    """
    from maestro.audit_checks import classify_task_shape

    per_task = cost_per_task(usage_records)

    goals: dict[str, str] = {}
    for rec in task_records:
        raw_tid = rec.get("task_id")
        if not isinstance(raw_tid, str) or not raw_tid:
            continue
        raw_goal = rec.get("goal")
        goals[raw_tid] = raw_goal if isinstance(raw_goal, str) else ""

    buckets: dict[str, dict[str, float]] = {}
    for tid, cost in per_task.items():
        if tid is None or tid not in goals:
            continue
        shape = classify_task_shape(goals[tid])
        entry = buckets.setdefault(shape, {"total": 0.0, "count": 0.0})
        entry["total"] += cost
        entry["count"] += 1.0

    # A bucket exists only after at least one increment, so ``count`` is always
    # >= 1 here and the per-task mean never divides by zero.
    return {
        shape: ShapeCost(
            shape=shape,
            task_count=int(vals["count"]),
            total_cost_usd=vals["total"],
            avg_cost_per_task_usd=vals["total"] / vals["count"],
        )
        for shape, vals in buckets.items()
    }


@dataclass(frozen=True)
class TaskQuality:
    """Windowed task-outcome quality aggregate for the burn-rate baseline.

    Computed over exactly the same ``usage ∩ task_history`` population as the
    :class:`ShapeCost` table (proposal 084): *task_count* the number of
    in-window tasks that also carry a task record; *outcome_counts* their
    outcome mix bucketed into ``success`` / ``partial`` / ``failure`` (a missing
    or unrecognised value → an ``unknown`` bucket), always carrying all four
    keys so the renderer never key-errors; *total_replans* their summed
    ``replans``; *avg_replans_per_task* the per-task mean
    (``total_replans / task_count``, ``0.0`` on an empty window).
    """

    task_count: int
    outcome_counts: dict[str, int]
    total_replans: int
    avg_replans_per_task: float


def aggregate_task_quality(
    usage_records: collections.abc.Sequence[dict[str, object]],
    task_records: collections.abc.Sequence[dict[str, object]],
) -> TaskQuality:
    """Aggregate task-outcome quality over the windowed cost population.

    Cost alone hides an edit-quality regression: a retrieval tool can shave
    ``$/task`` while producing more ``partial`` / ``failure`` outcomes and more
    re-plans. This pairs that task-attributed signal with the cost table by
    restricting to the *same* windowed task set (proposal 084).

    Derives the in-window task set from :func:`cost_per_task` (dropping the
    ``None`` overhead key), intersects it with a last-wins ``{task_id: record}``
    map over *task_records* built with the same
    ``isinstance(raw_tid, str) and raw_tid`` guard :func:`cost_per_task_shape`
    uses, then for each qualifying task buckets its ``outcome`` into
    ``success`` / ``partial`` / ``failure`` (a missing or unrecognised value →
    ``unknown``) and sums its ``replans`` via :func:`_coerce_int`.

    By construction *task_count* equals the shape table's ``all`` rollup count,
    since both restrict to the same ``usage ∩ task_history`` population. An
    empty window yields all-zero counts and ``avg_replans_per_task == 0.0``.
    """
    per_task = cost_per_task(usage_records)

    records: dict[str, dict[str, object]] = {}
    for rec in task_records:
        raw_tid = rec.get("task_id")
        if not isinstance(raw_tid, str) or not raw_tid:
            continue
        records[raw_tid] = rec

    outcome_counts = {"success": 0, "partial": 0, "failure": 0, "unknown": 0}
    total_replans = 0
    task_count = 0
    for tid in per_task:
        if tid is None or tid not in records:
            continue
        task_count += 1
        rec = records[tid]
        raw_outcome = rec.get("outcome")
        if isinstance(raw_outcome, str) and raw_outcome in (
            "success", "partial", "failure",
        ):
            outcome_counts[raw_outcome] += 1
        else:
            outcome_counts["unknown"] += 1
        total_replans += _coerce_int(rec.get("replans"))

    avg = total_replans / task_count if task_count else 0.0
    return TaskQuality(
        task_count=task_count,
        outcome_counts=outcome_counts,
        total_replans=total_replans,
        avg_replans_per_task=avg,
    )


def aggregate_cache_hit_rate(
    usage_records: collections.abc.Sequence[dict[str, object]],
) -> float:
    """Aggregate prompt-cache hit rate across usage rows.

    Sums ``cache_read_tokens`` and ``input_tokens`` over *usage_records* and
    returns ``cache_read / (cache_read + input)`` — the same definition as
    :attr:`~maestro.diagnostics.DiagnosticSnapshot.cache_hit_rate` (Anthropic
    reports ``input_tokens`` as the uncached, billed portion). Non-numeric
    token fields count as ``0``; an empty input or zero denominator returns
    ``0.0``.
    """
    total_cache_read = 0
    total_input = 0
    for rec in usage_records:
        total_cache_read += _coerce_int(rec.get("cache_read_tokens"))
        total_input += _coerce_int(rec.get("input_tokens"))
    denominator = total_cache_read + total_input
    if denominator <= 0:
        return 0.0
    return total_cache_read / denominator


def aggregate_caller_counts(
    usage_records: collections.abc.Sequence[dict[str, object]],
    callers: collections.abc.Sequence[str],
) -> dict[str, int]:
    """Sum per-caller call counts across usage rows for the requested callers.

    Each ``usage.jsonl`` row carries ``per_caller`` — a ``{caller: call_count}``
    map (:attr:`~maestro.sdk.IterationUsage.per_caller`) with keys such as
    ``auto_fix`` and ``self_review``. Returns ``{caller: total_calls}`` for every
    name in *callers*, summing ``per_caller.get(name, 0)`` over the rows; a
    caller absent from every row totals ``0``. Rows whose ``per_caller`` is
    missing, ``None``, or not a dict contribute nothing, and non-int counts are
    coerced via :func:`_coerce_int` — the same defensiveness as
    :func:`aggregate_cache_hit_rate` (proposal 084).
    """
    totals = {name: 0 for name in callers}
    for rec in usage_records:
        per_caller = rec.get("per_caller")
        if not isinstance(per_caller, dict):
            continue
        for name in totals:
            totals[name] += _coerce_int(per_caller.get(name))
    return totals


def format_burn_baseline(
    usage_records: collections.abc.Sequence[dict[str, object]],
    task_records: collections.abc.Sequence[dict[str, object]],
    *,
    captured_at: str,
) -> str:
    """Render the date-stamped burn-rate baseline as Markdown.

    Combines :func:`cost_per_task_shape` and :func:`aggregate_cache_hit_rate`
    into the operations-note baseline table (proposal 084 Phase 1): ``$/task``
    by task-shape (highest total spend first, plus an ``all`` rollup row) and
    the measured prompt-cache hit rate. *captured_at* is the ISO timestamp
    stamped into the header — passed in so tests stay deterministic.
    """
    shapes = cost_per_task_shape(usage_records, task_records)
    cache_rate = aggregate_cache_hit_rate(usage_records)

    lines = [
        f"## Burn-rate baseline (captured {captured_at})",
        "",
        "| Task shape | Tasks | Total $ | $/task |",
        "|------------|------:|--------:|-------:|",
    ]
    if shapes:
        for sc in sorted(
            shapes.values(), key=lambda s: s.total_cost_usd, reverse=True,
        ):
            lines.append(
                f"| {sc.shape} | {sc.task_count} | "
                f"${sc.total_cost_usd:.4f} | ${sc.avg_cost_per_task_usd:.4f} |"
            )
        total_cost = sum(s.total_cost_usd for s in shapes.values())
        total_tasks = sum(s.task_count for s in shapes.values())
        # ``shapes`` non-empty implies every bucket has >= 1 task, so
        # ``total_tasks`` >= 1 and the rollup mean never divides by zero.
        lines.append(
            f"| **all** | {total_tasks} | ${total_cost:.4f} | "
            f"${total_cost / total_tasks:.4f} |"
        )
    else:
        lines.append("| _(no task-attributable spend)_ | 0 | $0.0000 | $0.0000 |")

    lines.append("")
    lines.append(f"Prompt-cache hit rate: {cache_rate * 100:.1f}%")

    # Implementation-churn proxy: auto-fix / self-review call counts over the
    # same window (proposal 084). A retrieval tool that shaves $/task while
    # raising these is adding churn, not removing it, so the operator reads the
    # count alongside the cost delta. Inherits the caller's --since window for
    # free — no separate write-path instrumentation.
    churn = aggregate_caller_counts(usage_records, ("auto_fix", "self_review"))
    lines.append(
        "Implementation churn (window): "
        f"auto_fix calls = {churn['auto_fix']}, "
        f"self_review calls = {churn['self_review']}"
    )

    # Task-quality cross-check: cost alone hides an edit-quality regression, so
    # pair the $/task table with the outcome mix and re-plan count over exactly
    # the same windowed task population (proposal 084). Inherits the caller's
    # --since window for free — no separate write-path instrumentation.
    quality = aggregate_task_quality(usage_records, task_records)
    counts = quality.outcome_counts
    task_word = "task" if quality.task_count == 1 else "tasks"
    quality_line = (
        f"Task quality (window): {quality.task_count} {task_word} — "
        f"success {counts['success']}, partial {counts['partial']}, "
        f"failure {counts['failure']}"
    )
    if counts["unknown"] > 0:
        quality_line += f", unknown {counts['unknown']}"
    quality_line += (
        f"; replans total {quality.total_replans} "
        f"(avg {quality.avg_replans_per_task:.2f}/task)"
    )
    lines.append(quality_line)
    return "\n".join(lines)


#: Implementation-heavy task shapes a retrieval seam is expected to help, shown
#: as explicit ``$/task`` rows in the before/after comparison (proposal 084).
_COMPARISON_SHAPES: tuple[str, ...] = (
    "construction", "module_extension", "remediation",
)


def _shape_cost_per_task(
    shapes: dict[str, ShapeCost], shape: str,
) -> float:
    """Return a shape's ``$/task``, or ``0.0`` when the window has no such task."""
    sc = shapes.get(shape)
    return sc.avg_cost_per_task_usd if sc is not None else 0.0


def _rollup_cost_per_task(shapes: dict[str, ShapeCost]) -> float:
    """Return the ``all``-rollup ``$/task`` over *shapes* (``0.0`` when empty)."""
    total_cost = sum(s.total_cost_usd for s in shapes.values())
    total_tasks = sum(s.task_count for s in shapes.values())
    return total_cost / total_tasks if total_tasks else 0.0


def _shape_task_count(shapes: dict[str, ShapeCost], shape: str) -> int:
    """Return a shape's window task count, or ``0`` when the window lacks it."""
    sc = shapes.get(shape)
    return sc.task_count if sc is not None else 0


def _rollup_task_count(shapes: dict[str, ShapeCost]) -> int:
    """Return the ``all``-rollup task count over *shapes* (``0`` when empty)."""
    return sum(s.task_count for s in shapes.values())


def format_burn_comparison(
    before_usage: collections.abc.Sequence[dict[str, object]],
    before_tasks: collections.abc.Sequence[dict[str, object]],
    after_usage: collections.abc.Sequence[dict[str, object]],
    after_tasks: collections.abc.Sequence[dict[str, object]],
    *,
    cutoff: datetime,
) -> str:
    """Render a before/after/Δ burn-rate comparison as Markdown.

    Given the two windows an A/B split produces about a flag-flip *cutoff*,
    computes the same four signals :func:`format_burn_baseline` reports —
    ``$/task`` by shape (:func:`cost_per_task_shape`), prompt-cache hit rate
    (:func:`aggregate_cache_hit_rate`), the implementation-churn counts
    (:func:`aggregate_caller_counts`), and the task-quality mix
    (:func:`aggregate_task_quality`) — for each side, and renders one
    ``| Signal | Before | After | Δ |`` table (proposal 084).

    The ``$/task`` block covers the implementation-heavy shapes a retrieval
    seam is meant to help (:data:`_COMPARISON_SHAPES`) plus the ``all`` rollup;
    a shape absent from a window reads ``$0.0000`` there. Each ``$/task`` cell
    carries its window task count as ``(n=C)`` so a large delta on a one-task
    shape reads as the small sample it is, not a signal. Deltas are signed
    ``after - before`` and carry an ↑/↓/→ glyph mirroring
    :func:`~maestro.audit_checks._trend_arrow`. An empty window renders zeros
    rather than raising. *cutoff* is stamped into the header; it is an
    aware instant so the header reads the same in any process timezone.
    """
    from maestro.audit_checks import _trend_arrow

    shapes_before = cost_per_task_shape(before_usage, before_tasks)
    shapes_after = cost_per_task_shape(after_usage, after_tasks)
    cache_before = aggregate_cache_hit_rate(before_usage)
    cache_after = aggregate_cache_hit_rate(after_usage)
    churn_before = aggregate_caller_counts(before_usage, ("auto_fix", "self_review"))
    churn_after = aggregate_caller_counts(after_usage, ("auto_fix", "self_review"))
    quality_before = aggregate_task_quality(before_usage, before_tasks)
    quality_after = aggregate_task_quality(after_usage, after_tasks)

    def _cost_row(
        label: str, before_val: float, after_val: float,
        before_n: int, after_n: int,
    ) -> str:
        delta = after_val - before_val
        sign = "+" if delta >= 0 else "-"
        return (
            f"| {label} | ${before_val:.4f} (n={before_n}) | "
            f"${after_val:.4f} (n={after_n}) | "
            f"{_trend_arrow(delta)} {sign}${abs(delta):.4f} |"
        )

    def _int_row(label: str, before_val: int, after_val: int) -> str:
        delta = after_val - before_val
        return (
            f"| {label} | {before_val} | {after_val} | "
            f"{_trend_arrow(delta)} {delta:+d} |"
        )

    lines = [
        f"## Burn-rate before/after comparison (cutoff {cutoff.isoformat()})",
        "",
        "Before = usage rows before the cutoff; After = rows at or after it.",
        "",
        "| Signal | Before | After | Δ |",
        "|--------|-------:|------:|--:|",
    ]

    for shape in _COMPARISON_SHAPES:
        lines.append(_cost_row(
            f"{shape} $/task",
            _shape_cost_per_task(shapes_before, shape),
            _shape_cost_per_task(shapes_after, shape),
            _shape_task_count(shapes_before, shape),
            _shape_task_count(shapes_after, shape),
        ))
    lines.append(_cost_row(
        "**all** $/task",
        _rollup_cost_per_task(shapes_before),
        _rollup_cost_per_task(shapes_after),
        _rollup_task_count(shapes_before),
        _rollup_task_count(shapes_after),
    ))

    cache_delta = cache_after - cache_before
    cache_sign = "+" if cache_delta >= 0 else "-"
    lines.append(
        f"| Prompt-cache hit rate | {cache_before * 100:.1f}% | "
        f"{cache_after * 100:.1f}% | "
        f"{_trend_arrow(cache_delta)} {cache_sign}{abs(cache_delta) * 100:.1f}pp |"
    )

    lines.append(_int_row(
        "auto_fix calls", churn_before["auto_fix"], churn_after["auto_fix"],
    ))
    lines.append(_int_row(
        "self_review calls",
        churn_before["self_review"], churn_after["self_review"],
    ))

    lines.append(_int_row(
        "Tasks (window)", quality_before.task_count, quality_after.task_count,
    ))
    for outcome in ("success", "partial", "failure"):
        lines.append(_int_row(
            outcome,
            quality_before.outcome_counts[outcome],
            quality_after.outcome_counts[outcome],
        ))
    # An ``unknown`` row appears only when a side carries an unrecognised
    # outcome, mirroring format_burn_baseline's conditional ``unknown`` segment.
    if quality_before.outcome_counts["unknown"] or quality_after.outcome_counts["unknown"]:
        lines.append(_int_row(
            "unknown",
            quality_before.outcome_counts["unknown"],
            quality_after.outcome_counts["unknown"],
        ))

    replan_delta = quality_after.avg_replans_per_task - quality_before.avg_replans_per_task
    replan_sign = "+" if replan_delta >= 0 else "-"
    lines.append(
        f"| avg replans/task | {quality_before.avg_replans_per_task:.2f} | "
        f"{quality_after.avg_replans_per_task:.2f} | "
        f"{_trend_arrow(replan_delta)} {replan_sign}{abs(replan_delta):.2f} |"
    )
    return "\n".join(lines)


def _read_jsonl_dicts(path: Path) -> list[dict[str, object]]:
    """Read a JSONL file into a list of dict rows, skipping unreadable lines.

    A missing or unreadable file (permission error, a directory, a TOCTOU
    removal, or corrupted non-UTF-8 bytes) yields ``[]`` rather than raising;
    blank lines and rows that are not a JSON object are silently skipped.
    """
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    rows.append(data)
    # ``open(encoding="utf-8")`` defers decoding to iteration, so a corrupted
    # byte raises ``UnicodeDecodeError`` (a ``ValueError`` subclass, not
    # ``OSError``) from ``for raw in fh``. Catch ``ValueError`` too so the
    # documented "yields [] rather than raising" contract holds for a
    # corrupted-byte file, not just an unopenable one.
    except (OSError, ValueError):
        return []
    return rows


def _filter_records_since(
    records: list[dict[str, object]], since: datetime,
) -> list[dict[str, object]]:
    """Keep only usage rows whose ``started_at`` is at or after *since*.

    *since* is a timezone-aware cutoff. Each row's ``started_at`` is parsed
    with :func:`datetime.fromisoformat`; a row missing the field, holding a
    non-string, an unparseable value, or a timestamp that cannot be compared
    against *since* (a naive value against an aware cutoff) is **excluded** —
    treated as before the cutoff so the with-Context7 A/B window never absorbs
    an undatable row. Returns a new list, preserving order.

    This deliberately diverges from :func:`_parse_utc_date`, which assumes UTC
    for a naive ``started_at``: the cost-report per-date section tolerates that
    assumed-UTC guess, but an A/B cutoff must not silently admit a row whose
    instant is genuinely unknown, so naive rows are dropped here rather than
    coerced.
    """
    kept: list[dict[str, object]] = []
    for record in records:
        raw = record.get("started_at")
        if not isinstance(raw, str):
            continue
        try:
            if datetime.fromisoformat(raw) >= since:
                kept.append(record)
        except (ValueError, TypeError):
            continue
    return kept


def count_metered_iterations_since(usage_path: Path, since: datetime) -> int:
    """Count ``usage.jsonl`` iterations started at or after *since* and metered.

    The per-iteration spend denominator: windowed with :func:`_filter_records_since` (an
    undatable row is excluded) so it shares the cost-sample window, and an
    ``unmetered`` row is excluded because its spend never reaches the
    numerator — counting it would read unknown spend as cheap iterations.
    A missing or unreadable log counts ``0``.
    """
    rows = _filter_records_since(_read_jsonl_dicts(usage_path), since)
    return sum(1 for row in rows if not row.get("unmetered"))


def _split_records_at(
    records: collections.abc.Sequence[dict[str, object]], cutoff: datetime,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Partition usage rows into ``(before, at_or_after)`` about *cutoff*.

    The complement of :func:`_filter_records_since`: rather than keeping one
    side, it splits *records* into the two windows an A/B comparison needs.
    *cutoff* is a timezone-aware instant (the date a flag was flipped). Each
    row's ``started_at`` is parsed with :func:`datetime.fromisoformat`; a row
    stamped exactly at *cutoff* goes to *at_or_after* (the cutoff is inclusive
    on the after side, matching ``_filter_records_since``'s ``>= since``).

    Mirroring ``_filter_records_since``'s stance, a row that cannot be dated
    against *cutoff* — a missing field, a non-string value, an unparseable
    string, or a naive timestamp that cannot be compared to the aware cutoff —
    is **dropped from both windows**, so a genuinely-unknown instant never
    biases either side. Both returned lists are new and preserve input order.
    """
    before: list[dict[str, object]] = []
    at_or_after: list[dict[str, object]] = []
    for record in records:
        raw = record.get("started_at")
        if not isinstance(raw, str):
            continue
        try:
            when = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            continue
        try:
            is_after = when >= cutoff
        except TypeError:
            # A naive ``started_at`` cannot be compared to the aware cutoff;
            # drop it rather than guess an assumed-UTC instant.
            continue
        if is_after:
            at_or_after.append(record)
        else:
            before.append(record)
    return before, at_or_after


def generate_burn_baseline(
    usage_path: Path,
    task_history_path: Path,
    *,
    now: datetime | None = None,
    since: datetime | None = None,
) -> str:
    """Render the proposal-084 burn-rate baseline from the on-disk corpora.

    Reads ``usage.jsonl`` (*usage_path*) and ``task_history.jsonl``
    (*task_history_path*), joins per-task cost to task shape, and returns the
    date-stamped Markdown baseline via :func:`format_burn_baseline`. Missing or
    unreadable corpora are treated as empty, so the report degrades to a
    "no task-attributable spend" table rather than raising. *now* defaults to
    the current UTC time and exists for deterministic tests.

    *since*, when given, filters ``usage.jsonl`` rows to those whose
    ``started_at`` is at or after the cutoff (:func:`_filter_records_since`),
    isolating the before/after window for the Context7 A/B comparison; rows
    that cannot be dated are dropped.
    """
    reference = now or datetime.now(UTC)
    usage_records = _read_jsonl_dicts(usage_path)
    if since is not None:
        usage_records = _filter_records_since(usage_records, since)
    task_records = _read_jsonl_dicts(task_history_path)
    return format_burn_baseline(
        usage_records, task_records, captured_at=reference.isoformat(),
    )


def generate_burn_comparison(
    usage_path: Path,
    task_history_path: Path,
    *,
    cutoff: datetime,
) -> str:
    """Render the proposal-084 before/after burn-rate comparison from disk.

    The single-command complement of running :func:`generate_burn_baseline`
    twice with different ``--since`` windows: reads ``usage.jsonl``
    (*usage_path*) and ``task_history.jsonl`` (*task_history_path*) via
    :func:`_read_jsonl_dicts` (missing/unreadable → empty), partitions the
    usage rows about the flag-flip *cutoff* with :func:`_split_records_at`, and
    passes the full task-record list to **both** sides — the per-task cost
    meters intersect tasks with each side's usage by ``task_id``, so no
    task-side split is needed. Returns the before/after/Δ table via
    :func:`format_burn_comparison`. *cutoff* is a timezone-aware instant.
    """
    usage_records = _read_jsonl_dicts(usage_path)
    before_usage, after_usage = _split_records_at(usage_records, cutoff)
    task_records = _read_jsonl_dicts(task_history_path)
    return format_burn_comparison(
        before_usage, task_records, after_usage, task_records, cutoff=cutoff,
    )


def _collect_cost_aggregates(
    usage_path: Path,
    days: int,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Aggregate ``usage.jsonl`` cost data — the shared core of the cost report.

    Returns a dict with a ``status`` of ``"ok"``, ``"no_data"``, or
    ``"unreadable"`` and the numeric aggregates both the text renderer
    (:func:`generate_cost_report`) and the machine-readable renderer
    (:func:`generate_cost_report_data`) present. Keeping the parse loop in one
    place means the two surfaces can never drift: they always sum, split
    per-provider cost, and window per-date spend identically. See
    :func:`generate_cost_report` for the semantics of each aggregate.
    """
    from maestro.sdk import IterationUsage

    empty: dict[str, object] = {
        "iterations": 0,
        "total_cost_usd": 0.0,
        "by_date": {},
        "by_provider": {},
        "malformed_rows_skipped": 0,
    }
    if not usage_path.exists():
        return {"status": "no_data", **empty}

    reference = now or datetime.now(UTC)
    # Floor to midnight of the earliest in-window day so the per-date section
    # lists exactly N calendar dates (not N+1) and the earliest date reflects
    # its full-day spend rather than only the portion after the current
    # time-of-day, matching the "last N days" header.
    window_cutoff = (reference - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    total_cost = 0.0
    iterations = 0
    skip_count = 0
    by_date: dict[str, float] = {}
    by_provider: dict[str, float] = {}

    # The ``exists()`` guard above only rules out a missing file; an existing
    # file can still fail to open (permission denied, a directory, or a TOCTOU
    # removal). Guard the open so the CLI returns a graceful message instead of
    # letting the OSError escape as a traceback — matching read_iteration_usage.
    # Text-mode ``open`` decodes lazily, so a byte that is not valid UTF-8
    # raises ``UnicodeDecodeError`` (a ``ValueError``, not an ``OSError``) from
    # the ``for raw in fh`` iteration itself; that case also maps to
    # ``"unreadable"`` rather than escaping as a traceback.
    try:
        with open(usage_path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    skip_count += 1
                    continue
                if not isinstance(data, dict):
                    skip_count += 1
                    continue

                usage = IterationUsage.from_dict(data)
                # An unmetered row's ``total_cost_usd`` is a lower bound, not a
                # measured cost (see the IterationUsage comment in sdk.py), so
                # it contributes unknown — i.e. 0.0 — spend, matching
                # cost_per_task and the dashboard rather than counting
                # placeholder cost as real.
                cost = 0.0 if usage.unmetered else usage.total_cost_usd
                total_cost += cost
                iterations += 1

                providers = usage.providers or [_UNKNOWN_PROVIDER]
                # A single iteration can legitimately name several providers
                # (phase-routed providers, model fallback). Split the cost
                # evenly across them so the per-provider section still sums to
                # the total rather than counting the full cost once per provider.
                share = cost / len(providers)
                for provider in providers:
                    by_provider[provider] = by_provider.get(provider, 0.0) + share

                started = _parse_utc_date(usage.started_at)
                # Only dates with nonzero spend belong in the per-date section
                # (acceptance criterion #1). Unmetered rows force ``cost`` to
                # 0.0 above, so without this guard the section would render
                # misleading ``$0.0000`` lines that present unknown/unmetered
                # spend as measured-zero spend.
                if cost > 0 and started is not None and started >= window_cutoff:
                    key = started.date().isoformat()
                    by_date[key] = by_date.get(key, 0.0) + cost
    except (OSError, UnicodeDecodeError):
        return {"status": "unreadable", **empty}

    if iterations == 0:
        return {"status": "no_data", **empty,
                "malformed_rows_skipped": skip_count}

    return {
        "status": "ok",
        "iterations": iterations,
        "total_cost_usd": total_cost,
        "by_date": by_date,
        "by_provider": by_provider,
        "malformed_rows_skipped": skip_count,
    }


def generate_cost_report_data(
    usage_path: Path,
    days: int = 7,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Machine-readable twin of :func:`generate_cost_report`.

    Returns a JSON-serializable dict carrying the same aggregates the text
    report renders — per-UTC-date and per-provider cost breakdowns plus the
    grand ``total_cost_usd`` — so callers can query Maestro's economics without
    parsing the human table. Shape (always present, zero-valued when there is
    no data):

    * ``status`` — ``"ok"``, ``"no_data"``, or ``"unreadable"``,
    * ``days`` — the trailing-window size echoed back,
    * ``iterations`` — rows counted,
    * ``total_cost_usd`` — the grand total (unmetered rows contribute 0.0),
    * ``average_cost_usd`` — total over iterations (0.0 when no iterations),
    * ``by_date`` — ``{ISO date: cost}`` over the last *days* days,
    * ``by_provider`` — ``{provider: cost}`` (multi-provider rows split evenly),
    * ``malformed_rows_skipped`` — unparseable rows counted but not summed.

    Never raises on a missing/unreadable/corrupt log — the ``status`` field
    distinguishes those cases so the CLI can always exit 0.
    """
    aggregates = _collect_cost_aggregates(usage_path, days, now=now)
    iterations = aggregates["iterations"]
    total_cost = aggregates["total_cost_usd"]
    assert isinstance(iterations, int)
    assert isinstance(total_cost, float)
    average = total_cost / iterations if iterations else 0.0
    return {
        "status": aggregates["status"],
        "days": days,
        "iterations": iterations,
        "total_cost_usd": total_cost,
        "average_cost_usd": average,
        "by_date": aggregates["by_date"],
        "by_provider": aggregates["by_provider"],
        "malformed_rows_skipped": aggregates["malformed_rows_skipped"],
    }


def generate_cost_report(
    usage_path: Path,
    days: int = 7,
    *,
    now: datetime | None = None,
) -> str:
    """Summarize per-iteration cost from ``usage.jsonl`` for the operator.

    Reads *usage_path* line by line, parsing each row via
    :meth:`~maestro.sdk.IterationUsage.from_dict`, and aggregates over every
    parseable row:

    * total cost — unmetered rows contribute 0.0 (their ``total_cost_usd`` is
      a lower bound, not measured spend), matching :func:`cost_per_task`,
    * cost per UTC date over the last *days* days (rows older than the
      window, or lacking a parseable ``started_at``, are absent from this
      section but still counted everywhere else),
    * cost per provider name — the served-model dimension a usage row
      actually carries (:class:`~maestro.sdk.IterationUsage` records
      ``providers``, not a bare model string); rows with no provider stamp
      roll up under ``"unknown"`` and a multi-provider row's cost is split
      evenly across its providers, so the per-provider sum equals the total,
    * average cost per iteration, and
    * the count of iterations processed.

    Lines that are not a valid JSON object are caught individually and
    counted; the skip count is reported in the returned string. A missing or
    empty file returns :data:`_NO_USAGE_DATA` without raising; a non-empty
    file whose every row is unreadable appends the skip count to that
    sentinel so corruption is surfaced. An existing file that cannot be opened
    (permission error, a directory, or a TOCTOU removal), or contains bytes
    that are not valid UTF-8, returns
    :data:`_UNREADABLE_USAGE` without raising. *now* defaults to the current UTC
    time and exists for deterministic tests.
    """
    aggregates = _collect_cost_aggregates(usage_path, days, now=now)
    status = aggregates["status"]
    if status == "unreadable":
        return _UNREADABLE_USAGE
    if status == "no_data":
        # Distinguish a genuinely empty/missing log from a corrupted one whose
        # rows were all unreadable — surfacing the skip count so corruption is
        # reported rather than masked as "no data".
        skip_count = aggregates["malformed_rows_skipped"]
        if skip_count:
            return f"{_NO_USAGE_DATA} ({skip_count} malformed rows skipped)"
        return _NO_USAGE_DATA

    iterations = aggregates["iterations"]
    total_cost = aggregates["total_cost_usd"]
    by_date = aggregates["by_date"]
    by_provider = aggregates["by_provider"]
    skip_count = aggregates["malformed_rows_skipped"]
    assert isinstance(iterations, int)
    assert isinstance(total_cost, float)
    assert isinstance(by_date, dict)
    assert isinstance(by_provider, dict)
    average = total_cost / iterations

    lines = [
        f"Cost Report (last {days} day{'s' if days != 1 else ''})",
        "=" * 40,
        f"Iterations processed: {iterations}",
        f"Total cost: ${total_cost:.4f}",
        f"Average cost per iteration: ${average:.4f}",
        "",
        f"By UTC date (last {days} day{'s' if days != 1 else ''}):",
    ]
    if by_date:
        for date_key in sorted(by_date):
            lines.append(f"  {date_key}: ${by_date[date_key]:.4f}")
    else:
        lines.append("  (no iterations in window)")

    lines.append("")
    lines.append("By provider:")
    for provider, cost in sorted(
        by_provider.items(), key=lambda kv: kv[1], reverse=True,
    ):
        lines.append(f"  {provider}: ${cost:.4f}")

    lines.append("")
    lines.append(f"Malformed rows skipped: {skip_count}")

    return "\n".join(lines)


def _parse_utc_date(started_at: str) -> datetime | None:
    """Parse an ISO ``started_at`` into a UTC-aware datetime, or ``None``.

    A thin tolerant wrapper over :func:`_as_utc` — one owner for how these
    stamps read a naive value — adding only the ``None``-on-unparseable stance
    this call site wants, so an empty or malformed string leaves the row simply
    absent from the per-date section rather than failing the report.
    """
    try:
        return _as_utc(started_at)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class LeakFence:
    """Test-fixture cost rows that leaked into the hot ledger before the fence.

    A row matches when its timestamp parses and is at or before :attr:`cutoff`,
    and either its caller is in :attr:`callers` or (with
    :attr:`match_provider_none`) its ``provider`` is absent. An unparseable
    timestamp never matches.
    """

    cutoff: str
    callers: frozenset[str]
    match_provider_none: bool = True

    def cutoff_epoch(self) -> float:
        return _as_utc(self.cutoff).timestamp()

    def matches(self, row: Mapping[str, object]) -> bool:
        raw_ts = row.get("timestamp")
        if not isinstance(raw_ts, str):
            return False
        try:
            epoch = _as_utc(raw_ts).timestamp()
        except (ValueError, OverflowError):
            return False
        if epoch > self.cutoff_epoch():
            return False
        caller = row.get("caller")
        if isinstance(caller, str) and caller in self.callers:
            return True
        return self.match_provider_none and row.get("provider") is None


LEAK_FENCE = LeakFence(
    cutoff="2026-09-25T11:29:00+00:00",
    callers=frozenset({
        "unknown", "test", "t", "phase-a", "a", "b", "c1", "c2",
        "non_probe", "test_caller", "tracker", "contemplate",
    }),
)

LEAK_COLD_FILENAME = "cost_samples.cold.leak.jsonl"
