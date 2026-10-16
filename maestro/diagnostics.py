"""Operational diagnostics — aggregate log signals into a snapshot for contemplation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from maestro.host_capacity import HostCapacityReading
from maestro.logs import parse_app_log_tail

# Keywords that indicate quota exhaustion or rate-limiting issues. Shared with
# ``maestro.sdk`` (rate-limit ghost-completion detection in ``_consume_messages``),
# so this is a deliberately public name rather than a module-private constant.
QUOTA_KEYWORDS = (
    "quota",
    "you've hit your limit",
    "hit your limit",
    "hit our limit",
    "overloaded",
)
# Tokens mirror the structured rate-limit log formats in ``maestro.sdk``
# (``_log_rate_limit``'s ``resets_in_s=`` and the ``RATE LIMIT: … resets_at=…``
# session-log text) — renaming those log fields requires updating this tuple.
_RATE_LIMIT_KEYWORDS = (
    "rate limit",
    "rate-limited",
    "resets_in_s",
    "resets_at",
)

_SDK_CALL_PREFIX = "SDK call:"
_SDK_RATE_LIMIT_PREFIX = "SDK rate-limited:"

# Compiled once: matches ``key=value`` pairs in our structured log lines.
# Values are non-whitespace and may include ``$`` / ``.`` / ``-`` so floats and
# negative numbers are captured.  Caller labels are alphanumeric + ``_``.
_KV_RE = re.compile(r"(\w+)=([^\s]+)")

# Threshold below which the cache hit rate is considered concerning.
_CACHE_HIT_RATE_FLOOR = 0.3

# Absolute recent-iteration cost (USD) above which a *sustained* "rising" cost
# trend is still worth surfacing.  A sustained climb reads as "rising" (not the
# transient "spiking") — see :func:`_compute_cost_trend` — but an expensive one
# should not be silently dropped.  Single source of truth so ``is_noteworthy``
# and ``health_check.summarize_health`` cannot drift apart on a retune.
_RISING_HIGH_COST_FLOOR = 0.05


@dataclass
class SDKUsageStats:
    """Aggregated SDK telemetry parsed from structured app-log lines.

    Populated by :func:`build_sdk_usage_stats` from log entries emitted by
    :mod:`maestro.sdk` (``"SDK call: ..."`` and ``"SDK rate-limited: ..."``).
    All values are *observation only* — they do not influence retry or
    backoff behavior.
    """

    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    cache_hit_rate: float = 0.0
    by_caller: dict[str, int] = field(default_factory=dict)
    cost_by_caller: dict[str, float] = field(default_factory=dict)
    rate_limit_hits: int = 0


@dataclass(frozen=True)
class ContextWindowSnapshot:
    """Realized context-window composition for the ``cli_paced`` provider.

    Built by the ``cli_paced`` probe consumer from a ``/context`` reading
    (proposal 062 Step 4) and folded onto
    :attr:`DiagnosticSnapshot.context_window`. Mirrors ``maestro_sdk``'s
    ``ContextReport`` with plain built-in types so this module stays free of the
    ``maestro_sdk`` import — the default ``anthropic`` path must never pay for it.
    Every field tolerates a layout miss: ``used_tokens`` / ``total_tokens`` are
    ``| None`` and ``buckets`` defaults to the empty tuple. The consumer returns
    ``None`` (not an all-empty snapshot) for a missing reading, so a snapshot's
    presence already means "something was measured"; its absence is what
    :meth:`DiagnosticSnapshot.blind_spots` names.
    """

    used_tokens: int | None = None
    total_tokens: int | None = None
    buckets: tuple[tuple[str, int], ...] = ()


@dataclass
class DiagnosticSnapshot:
    """Aggregated operational health signals from recent log entries."""

    recent_error_count: int
    has_quota_warning: bool
    has_rate_limit: bool
    consecutive_failures: int
    summary: str
    sdk_usage: SDKUsageStats | None = None
    recent_iteration_costs: list[float] = field(default_factory=list)
    cost_trend: Literal["flat", "rising", "spiking"] | None = None
    context_window: ContextWindowSnapshot | None = None
    host_capacity: HostCapacityReading | None = None

    def blind_spots(self) -> list[str]:
        """Return structural measurement gaps this snapshot cannot detect.

        Each entry declares a specific limitation derived from the snapshot's
        data sources and field values.  Always-present spots reflect inherent
        limitations of log-tail analysis; conditional spots appear only when
        specific field values indicate the gap is operationally relevant.
        """
        spots: list[str] = []

        # -- always-present ----------------------------------------------------
        spots.append(
            "Error count only sees ERROR-level log entries — swallowed "
            "exceptions and warnings are invisible to this count."
        )
        spots.append(
            "Log window is bounded by the analysed tail — events older "
            "than the window are invisible to this snapshot."
        )

        # -- conditional on sdk_usage ------------------------------------------
        if self.sdk_usage is not None:
            spots.append(
                "Cache hit rate is a window-average estimate — per-call "
                "cache misses are not individually visible."
            )
            if self.sdk_usage.total_calls > 0:
                spots.append(
                    "Cost figures mix reported and estimated values — "
                    "actual billed amounts may differ."
                )

        # -- conditional on has_rate_limit -------------------------------------
        if self.has_rate_limit:
            spots.append(
                "Rate-limit detection only covers WARNING/ERROR entries — "
                "INFO-level rate-limit events are intentionally excluded."
            )

        # -- conditional on cost_trend None ------------------------------------
        if self.cost_trend is None:
            spots.append(
                "Cost trend is None — cannot distinguish insufficient "
                "data from missing usage log."
            )

        # -- conditional on context_window None --------------------------------
        if self.context_window is None:
            spots.append(
                "Context-window composition is unmeasured — the cli_paced "
                "/context probe is the only runtime source of realized "
                "context-bloat, and it is absent (disabled, a non-cli_paced "
                "provider, or a parse miss)."
            )

        # -- conditional on host_capacity None or all-None-empty ---------------
        # An all-fields-None reading is produced-but-empty (every source failed
        # at once): it is not a measurement, so name the gap here rather than
        # letting a non-None-but-empty reading read as measured-and-healthy.
        if self.host_capacity is None or self.host_capacity.is_empty():
            spots.append(
                "Host capacity is unmeasured — load per core, memory and swap "
                "residency, and free disk on the Docker root have no runtime "
                "source here (the host-capacity sensor is disabled or produced "
                "no reading)."
            )

        return spots


def _parse_kv(message: str) -> dict[str, str]:
    """Extract ``key=value`` pairs from a structured log message."""
    return dict(_KV_RE.findall(message))


def _to_int(s: str | None) -> int:
    if not s:
        return 0
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _to_float(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def build_sdk_usage_stats(
    entries: list[dict[str, Any]],
) -> SDKUsageStats:
    """Aggregate SDK call telemetry from a list of parsed log entries.

    The caller is responsible for providing entries from
    :func:`maestro.logs.parse_app_log_tail`; this function only walks the
    given list so the caller controls the time window and avoids re-reading
    the log file.
    """
    stats = SDKUsageStats()
    for entry in entries:
        msg = entry.get("message") or ""
        if msg.startswith(_SDK_CALL_PREFIX):
            kv = _parse_kv(msg[len(_SDK_CALL_PREFIX):])
            caller = kv.get("caller", "unknown")
            stats.total_calls += 1
            stats.by_caller[caller] = stats.by_caller.get(caller, 0) + 1
            in_tok = _to_int(kv.get("tokens_in"))
            out_tok = _to_int(kv.get("tokens_out"))
            cache_read = _to_int(kv.get("cache_read"))
            cache_create = _to_int(kv.get("cache_create"))
            cost = _to_float(kv.get("cost"))
            stats.total_input_tokens += in_tok
            stats.total_output_tokens += out_tok
            stats.total_cache_read_tokens += cache_read
            stats.total_cache_creation_tokens += cache_create
            stats.total_cost_usd += cost
            stats.cost_by_caller[caller] = (
                stats.cost_by_caller.get(caller, 0.0) + cost
            )
        elif msg.startswith(_SDK_RATE_LIMIT_PREFIX):
            stats.rate_limit_hits += 1

    # cache_hit_rate = cache_read / (cache_read + uncached_input).
    # Anthropic reports input_tokens as the *uncached* portion that was
    # actually billed at the input rate, so the denominator is the sum of
    # cache_read and input.
    denominator = stats.total_cache_read_tokens + stats.total_input_tokens
    stats.cache_hit_rate = (
        stats.total_cache_read_tokens / denominator if denominator > 0 else 0.0
    )
    return stats


def _format_tokens(n: int) -> str:
    """Compact human-friendly token count: 1234 -> '1.2k'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _summarise_sdk_usage(stats: SDKUsageStats) -> str | None:
    """Return a one-line summary of SDK usage, or ``None`` if empty.

    Internal segments are joined with ``" · "`` so the whole summary survives
    as a single fragment when the status formatter splits on ``", "`` to build
    its top-level bullet list — the SDK breakdown is one logical group.
    """
    if stats.total_calls == 0 and stats.rate_limit_hits == 0:
        return None
    call_word = "call" if stats.total_calls == 1 else "calls"
    parts = [
        f"{stats.total_calls} {call_word}",
        f"{_format_tokens(stats.total_input_tokens)} in",
        f"{_format_tokens(stats.total_output_tokens)} out",
        f"cache {stats.cache_hit_rate * 100:.0f}%",
    ]
    if stats.rate_limit_hits:
        hit_word = "hit" if stats.rate_limit_hits == 1 else "hits"
        parts.append(f"{stats.rate_limit_hits} rate-limit {hit_word}")
    return "sdk: " + " · ".join(parts)


def _compute_cost_trend(
    costs: list[float],
) -> Literal["flat", "rising", "spiking"] | None:
    """Classify recent iteration costs as flat / rising / spiking.

    Requires at least 4 data points (3 previous + 1 current) for a
    meaningful comparison.  Returns ``None`` when there is insufficient data.
    """
    if len(costs) < 4:
        return None
    last = costs[-1]
    prev = costs[:-3][-5:]  # up to 5 values before the last 3 (always >= 1)
    mean_prev = sum(prev) / len(prev)
    if mean_prev > 0 and last > mean_prev * 3 and last > 0.10:
        # Only keep "spiking" for a *transient* — a spike confined to the
        # final iteration.  When the two iterations preceding the last are
        # themselves already elevated above baseline, the climb is *sustained*,
        # so fall through to the window-based "rising" check rather than
        # mislabel a cost regression as a self-resolving blip.  Discriminate on
        # the preceding window (not the whole last-3 mean, which the single high
        # final value would inflate enough to hide a true transient).
        preceding = costs[-3:-1]  # the two values before the last (len >= 4 => always 2)
        mean_preceding = sum(preceding) / len(preceding)
        if mean_preceding <= mean_prev * 1.5:
            return "spiking"
    last_3 = costs[-3:]
    mean_last_3 = sum(last_3) / len(last_3)
    if mean_prev > 0 and mean_last_3 > mean_prev * 1.5:
        return "rising"
    return "flat"


def build_diagnostic_snapshot(
    log_path: Path | None,
    n_entries: int = 100,
    memory_dir: Path | None = None,
    context_window: ContextWindowSnapshot | None = None,
    host_capacity: HostCapacityReading | None = None,
) -> DiagnosticSnapshot:
    """Build a DiagnosticSnapshot from the tail of the app log.

    Args:
        log_path: Path to the application log file.  If ``None`` or the file
            does not exist, returns a safe "no log data" snapshot.
        n_entries: Number of recent log entries to analyse.
        memory_dir: If provided, reads per-iteration usage from
            ``usage.jsonl`` to compute cost trends.
        context_window: Optional realized context-window composition for the
            current iteration (proposal 062 Step 4).  Threaded through verbatim
            so a probe-carrying ``cli_paced`` iteration exposes it; ``None`` (the
            default, and every non-``cli_paced`` path) leaves the gap for
            :meth:`DiagnosticSnapshot.blind_spots` to name.
        host_capacity: Optional host-capacity reading for the current iteration
            (proposal 125).  Threaded through verbatim so a reading-carrying
            iteration exposes host load/memory/swap/disk; ``None`` (the default,
            and every path with the sensor off) leaves the gap for
            :meth:`DiagnosticSnapshot.blind_spots` to name.

    Returns:
        A :class:`DiagnosticSnapshot` summarising operational health.
    """
    if log_path is None or not log_path.exists():
        return DiagnosticSnapshot(
            recent_error_count=0,
            has_quota_warning=False,
            has_rate_limit=False,
            consecutive_failures=0,
            summary="No log data available",
            context_window=context_window,
            host_capacity=host_capacity,
        )

    entries = parse_app_log_tail(log_path, n=n_entries)

    if not entries:
        return DiagnosticSnapshot(
            recent_error_count=0,
            has_quota_warning=False,
            has_rate_limit=False,
            consecutive_failures=0,
            summary="All clear",
            context_window=context_window,
            host_capacity=host_capacity,
        )

    # Count ERROR-level entries
    recent_error_count = sum(
        1 for e in entries if e.get("level") == "ERROR"
    )

    # Scan WARNING/ERROR messages for quota / rate-limit keywords
    # (case-insensitive).  Benign INFO/DEBUG mentions (e.g. the SDK's
    # ``"SDK rate limit: allowed"`` event) must not trip the detector.
    has_quota_warning = False
    has_rate_limit = False
    for entry in entries:
        if entry.get("level") not in ("WARNING", "ERROR"):
            continue
        msg_lower = (entry.get("message") or "").lower()
        if not has_quota_warning and any(kw in msg_lower for kw in QUOTA_KEYWORDS):
            has_quota_warning = True
        if not has_rate_limit and any(kw in msg_lower for kw in _RATE_LIMIT_KEYWORDS):
            has_rate_limit = True
        if has_quota_warning and has_rate_limit:
            break

    # Count consecutive ERROR entries at the tail
    consecutive_failures = 0
    for entry in reversed(entries):
        if entry.get("level") == "ERROR":
            consecutive_failures += 1
        else:
            break

    sdk_usage = build_sdk_usage_stats(entries)

    # Build human-readable summary
    parts: list[str] = []
    if recent_error_count:
        parts.append(f"{recent_error_count} recent error(s)")
    if has_quota_warning:
        parts.append("quota warning detected")
    if has_rate_limit:
        parts.append("rate limit detected")
    if consecutive_failures:
        parts.append(f"{consecutive_failures} consecutive failure(s) at log tail")

    sdk_summary = _summarise_sdk_usage(sdk_usage)
    if sdk_summary:
        parts.append(sdk_summary)

    # Cost trend from per-iteration usage log
    recent_iteration_costs: list[float] = []
    cost_trend: Literal["flat", "rising", "spiking"] | None = None
    if memory_dir is not None:
        try:
            from maestro.sdk import read_iteration_usage

            records = read_iteration_usage(memory_dir / "usage.jsonl", n=10)
            recent_iteration_costs = [r.total_cost_usd for r in records]
            cost_trend = _compute_cost_trend(recent_iteration_costs)
        except Exception:
            pass

    summary = ", ".join(parts) if parts else "All clear"

    return DiagnosticSnapshot(
        recent_error_count=recent_error_count,
        has_quota_warning=has_quota_warning,
        has_rate_limit=has_rate_limit,
        consecutive_failures=consecutive_failures,
        summary=summary,
        sdk_usage=sdk_usage,
        recent_iteration_costs=recent_iteration_costs,
        cost_trend=cost_trend,
        context_window=context_window,
        host_capacity=host_capacity,
    )


def is_rising_high_cost(snapshot: DiagnosticSnapshot) -> bool:
    """Return True when a "rising" cost trend is expensive enough to surface.

    A sustained cost climb reads as ``"rising"`` (not the transient
    ``"spiking"``); it is still worth flagging when the most recent iteration
    cost clears :data:`_RISING_HIGH_COST_FLOOR`.  Shared by :func:`is_noteworthy`
    and :func:`~maestro.health_check.summarize_health` so the two noteworthy
    gates stay in parity.
    """
    return (
        snapshot.cost_trend == "rising"
        and bool(snapshot.recent_iteration_costs)
        and snapshot.recent_iteration_costs[-1] > _RISING_HIGH_COST_FLOOR
    )


def is_noteworthy(snapshot: DiagnosticSnapshot) -> bool:
    """Return True if the snapshot has anything worth reporting."""
    if (
        snapshot.recent_error_count
        or snapshot.has_quota_warning
        or snapshot.has_rate_limit
        or snapshot.consecutive_failures
    ):
        return True
    if snapshot.cost_trend == "spiking":
        return True
    if is_rising_high_cost(snapshot):
        return True
    sdk = snapshot.sdk_usage
    if sdk is None:
        return False
    if sdk.rate_limit_hits > 0:
        return True
    return sdk.total_calls >= 5 and sdk.cache_hit_rate < _CACHE_HIT_RATE_FLOOR
