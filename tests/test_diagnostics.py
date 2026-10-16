"""Tests for maestro/diagnostics.py — fully mocked I/O."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.consciousness import SelfModel
from maestro.diagnostics import (
    ContextWindowSnapshot,
    DiagnosticSnapshot,
    SDKUsageStats,
    _compute_cost_trend,
    _format_tokens,
    _summarise_sdk_usage,
    _to_float,
    build_diagnostic_snapshot,
    build_sdk_usage_stats,
    is_noteworthy,
)
from maestro.host_capacity import HostCapacityReading

# ---------------------------------------------------------------------------
# Internal helper coverage
# ---------------------------------------------------------------------------


def test_to_float_empty_returns_zero() -> None:
    assert _to_float(None) == 0.0
    assert _to_float("") == 0.0


def test_to_float_invalid_returns_zero() -> None:
    assert _to_float("nope") == 0.0


def test_to_float_valid() -> None:
    assert _to_float("1.25") == 1.25


def test_format_tokens_below_thousand() -> None:
    assert _format_tokens(42) == "42"


def test_format_tokens_above_thousand() -> None:
    assert _format_tokens(1500) == "1.5k"


def test_summarise_sdk_usage_includes_rate_limit_hits() -> None:
    stats = SDKUsageStats(total_calls=1, rate_limit_hits=2)
    summary = _summarise_sdk_usage(stats)
    assert summary is not None
    assert "2 rate-limit hits" in summary


def test_summarise_sdk_usage_pluralisation_singular() -> None:
    stats = SDKUsageStats(total_calls=1, rate_limit_hits=1)
    summary = _summarise_sdk_usage(stats)
    assert summary is not None
    assert "1 call " in summary + " "
    assert "1 calls" not in summary
    assert "1 rate-limit hit" in summary
    assert "1 rate-limit hits" not in summary


def test_summarise_sdk_usage_uses_middle_dot_separator() -> None:
    stats = SDKUsageStats(total_calls=2, total_input_tokens=10, total_output_tokens=5)
    summary = _summarise_sdk_usage(stats)
    assert summary is not None
    assert " · " in summary
    assert ", " not in summary


def test_summarise_sdk_usage_empty_returns_none() -> None:
    assert _summarise_sdk_usage(SDKUsageStats()) is None


def test_summarise_sdk_usage_omits_cost_segment() -> None:
    """The recent-window cost is rendered in the status run rate, not the SDK summary."""
    stats = SDKUsageStats(
        total_calls=2,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_usd=1.23,
    )
    summary = _summarise_sdk_usage(stats)
    assert summary is not None
    assert "$" not in summary
    assert "1.23" not in summary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(level: str, message: str) -> dict:
    return {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "level": level,
        "message": message,
        "logger": "maestro",
        "source": "app",
    }


# ---------------------------------------------------------------------------
# build_diagnostic_snapshot — no log file
# ---------------------------------------------------------------------------


def test_no_log_path_returns_safe_defaults() -> None:
    snapshot = build_diagnostic_snapshot(None)
    assert snapshot.recent_error_count == 0
    assert snapshot.has_quota_warning is False
    assert snapshot.has_rate_limit is False
    assert snapshot.consecutive_failures == 0
    assert "No log data" in snapshot.summary


def test_nonexistent_log_file_returns_safe_defaults(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.log"
    snapshot = build_diagnostic_snapshot(missing)
    assert snapshot.recent_error_count == 0
    assert snapshot.has_quota_warning is False
    assert snapshot.has_rate_limit is False
    assert snapshot.consecutive_failures == 0
    assert "No log data" in snapshot.summary


# ---------------------------------------------------------------------------
# build_diagnostic_snapshot — empty / all-clear log
# ---------------------------------------------------------------------------


def test_empty_entries_returns_all_clear() -> None:
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=[]), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.recent_error_count == 0
    assert snapshot.has_quota_warning is False
    assert snapshot.has_rate_limit is False
    assert snapshot.consecutive_failures == 0
    assert snapshot.summary == "All clear"


def test_only_info_entries_returns_all_clear() -> None:
    entries = [
        _make_entry("INFO", "Loop iteration 1"),
        _make_entry("INFO", "Contemplation complete"),
    ]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.recent_error_count == 0
    assert snapshot.has_quota_warning is False
    assert snapshot.has_rate_limit is False
    assert snapshot.consecutive_failures == 0
    assert snapshot.summary == "All clear"


# ---------------------------------------------------------------------------
# build_diagnostic_snapshot — context_window threading (proposal 062 Step 4)
# ---------------------------------------------------------------------------


def test_context_window_threaded_on_main_path() -> None:
    """A probe-carrying iteration exposes composition through the main path."""
    entries = [_make_entry("ERROR", "boom")]
    ctx = ContextWindowSnapshot(
        used_tokens=1500, total_tokens=200_000, buckets=(("tools", 1500),),
    )
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path, context_window=ctx)
    assert snapshot.context_window is ctx
    assert not any(
        "Context-window composition" in s for s in snapshot.blind_spots()
    )


def test_context_window_defaults_none_and_names_gap() -> None:
    """Without a reading the field stays None and blind_spots names the gap."""
    entries = [_make_entry("ERROR", "boom")]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.context_window is None
    assert any(
        "Context-window composition is unmeasured" in s
        for s in snapshot.blind_spots()
    )


def test_context_window_threaded_on_no_log_path() -> None:
    """The no-log-data early return still carries a supplied reading."""
    ctx = ContextWindowSnapshot(used_tokens=10, total_tokens=20)
    snapshot = build_diagnostic_snapshot(None, context_window=ctx)
    assert snapshot.context_window is ctx


def test_context_window_threaded_on_empty_entries() -> None:
    """The all-clear (empty entries) early return still carries a reading."""
    ctx = ContextWindowSnapshot(used_tokens=10, total_tokens=20)
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=[]), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path, context_window=ctx)
    assert snapshot.context_window is ctx


# ---------------------------------------------------------------------------
# build_diagnostic_snapshot — host_capacity threading (proposal 125)
# ---------------------------------------------------------------------------


def _host_reading() -> HostCapacityReading:
    """A healthy, fully-populated reading (silent, but present)."""
    return HostCapacityReading(
        load_per_core=0.5,
        memory_used_fraction=0.5,
        swap_used_fraction=0.0,
        docker_root_free_gib=100.0,
    )


def test_host_capacity_threaded_on_main_path() -> None:
    """A reading-carrying iteration exposes host capacity through the main path."""
    entries = [_make_entry("ERROR", "boom")]
    reading = _host_reading()
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path, host_capacity=reading)
    assert snapshot.host_capacity is reading
    assert not any(
        "Host capacity is unmeasured" in s for s in snapshot.blind_spots()
    )


def test_host_capacity_defaults_none_and_names_gap() -> None:
    """Without a reading the field stays None and blind_spots names the gap."""
    entries = [_make_entry("ERROR", "boom")]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.host_capacity is None
    assert any(
        "Host capacity is unmeasured" in s for s in snapshot.blind_spots()
    )


def test_host_capacity_threaded_on_no_log_path() -> None:
    """The no-log-data early return still carries a supplied reading."""
    reading = _host_reading()
    snapshot = build_diagnostic_snapshot(None, host_capacity=reading)
    assert snapshot.host_capacity is reading


def test_host_capacity_threaded_on_empty_entries() -> None:
    """The all-clear (empty entries) early return still carries a reading."""
    reading = _host_reading()
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=[]), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path, host_capacity=reading)
    assert snapshot.host_capacity is reading


# ---------------------------------------------------------------------------
# build_diagnostic_snapshot — error counting
# ---------------------------------------------------------------------------


def test_counts_error_entries() -> None:
    entries = [
        _make_entry("INFO", "Starting up"),
        _make_entry("ERROR", "Something went wrong"),
        _make_entry("WARNING", "Minor issue"),
        _make_entry("ERROR", "Another error"),
    ]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.recent_error_count == 2
    assert "2 recent error(s)" in snapshot.summary


# ---------------------------------------------------------------------------
# build_diagnostic_snapshot — quota warning detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("keyword", [
    "quota",
    "You've hit your limit",
    "hit your limit",
    "overloaded",
])
def test_detects_quota_keywords(keyword: str) -> None:
    entries = [_make_entry("ERROR", f"API call failed: {keyword}")]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.has_quota_warning is True
    assert "quota warning detected" in snapshot.summary


# ---------------------------------------------------------------------------
# build_diagnostic_snapshot — rate limit detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("keyword", [
    "rate limit",
    "rate-limited",
    "resets_in_s",
    "resets_at",
])
def test_detects_rate_limit_keywords(keyword: str) -> None:
    entries = [_make_entry("WARNING", f"Backing off: {keyword} active")]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.has_rate_limit is True
    assert "rate limit detected" in snapshot.summary


def test_incidental_resets_mention_does_not_trip_detector() -> None:
    """A WARNING containing the bare English word ``"resets"`` (e.g. a TCP
    connection reset) is not rate-limiting and must not set ``has_rate_limit``."""
    entries = [_make_entry("WARNING", "connection resets by peer")]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.has_rate_limit is False
    assert "rate limit detected" not in snapshot.summary


def test_detects_structured_rate_limit_warning() -> None:
    """The SDK's structured ``SDK rate-limited: … resets_in_s=…`` WARNING
    (``maestro.sdk._log_rate_limit``) must keep setting ``has_rate_limit`` —
    it carries no ``"rate limit"`` (space) substring, so the specific tokens
    are load-bearing for it."""
    entries = [_rate_limit_entry()]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.has_rate_limit is True
    assert "rate limit detected" in snapshot.summary


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_benign_rate_limit_allowed_does_not_trip_detector(level: str) -> None:
    """INFO/DEBUG entries containing ``"rate limit"`` (e.g. the SDK's benign
    ``"SDK rate limit: allowed"`` event) must not set ``has_rate_limit``."""
    entries = [_make_entry(level, "SDK rate limit: allowed")]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.has_rate_limit is False
    assert "rate limit detected" not in snapshot.summary


def test_benign_quota_mention_at_info_does_not_trip_detector() -> None:
    """Level gating applies to quota keywords too — INFO mentions are ignored."""
    entries = [_make_entry("INFO", "Checking quota status for dashboard")]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.has_quota_warning is False
    assert "quota warning detected" not in snapshot.summary


# ---------------------------------------------------------------------------
# build_diagnostic_snapshot — consecutive failures at tail
# ---------------------------------------------------------------------------


def test_consecutive_errors_at_tail() -> None:
    entries = [
        _make_entry("INFO", "All good"),
        _make_entry("INFO", "Still fine"),
        _make_entry("ERROR", "First tail error"),
        _make_entry("ERROR", "Second tail error"),
        _make_entry("ERROR", "Third tail error"),
    ]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.consecutive_failures == 3
    assert "3 consecutive failure(s) at log tail" in snapshot.summary


def test_no_consecutive_failures_when_last_entry_not_error() -> None:
    entries = [
        _make_entry("ERROR", "Old error"),
        _make_entry("INFO", "Recovered"),
    ]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.consecutive_failures == 0


# ---------------------------------------------------------------------------
# build_diagnostic_snapshot — combined signals
# ---------------------------------------------------------------------------


def test_all_signals_combined() -> None:
    entries = [
        _make_entry("INFO", "Starting"),
        _make_entry("ERROR", "Hit quota limit"),
        _make_entry("WARNING", "rate limit resets in 60s"),
        _make_entry("ERROR", "Crash A"),
        _make_entry("ERROR", "Crash B"),
    ]
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
         patch.object(Path, "exists", return_value=True):
        snapshot = build_diagnostic_snapshot(log_path)
    assert snapshot.recent_error_count == 3
    assert snapshot.has_quota_warning is True
    assert snapshot.has_rate_limit is True
    assert snapshot.consecutive_failures == 2
    # Summary should contain all signals
    assert "recent error(s)" in snapshot.summary
    assert "quota warning detected" in snapshot.summary
    assert "rate limit detected" in snapshot.summary
    assert "consecutive failure(s)" in snapshot.summary


# ---------------------------------------------------------------------------
# build_diagnostic_snapshot — custom n_entries forwarded
# ---------------------------------------------------------------------------


def test_n_entries_forwarded_to_parse_app_log_tail() -> None:
    log_path = Path("/fake/maestro.log")
    with patch("maestro.diagnostics.parse_app_log_tail", return_value=[]) as mock_parse, \
         patch.object(Path, "exists", return_value=True):
        build_diagnostic_snapshot(log_path, n_entries=42)
    mock_parse.assert_called_once_with(log_path, n=42)


# ---------------------------------------------------------------------------
# is_noteworthy
# ---------------------------------------------------------------------------


def test_is_noteworthy_false_when_all_clear() -> None:
    snapshot = DiagnosticSnapshot(
        recent_error_count=0,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="All clear",
    )
    assert is_noteworthy(snapshot) is False


def test_is_noteworthy_true_on_errors() -> None:
    snapshot = DiagnosticSnapshot(
        recent_error_count=1,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="1 recent error(s)",
    )
    assert is_noteworthy(snapshot) is True


def test_is_noteworthy_true_on_quota_warning() -> None:
    snapshot = DiagnosticSnapshot(
        recent_error_count=0,
        has_quota_warning=True,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="quota warning detected",
    )
    assert is_noteworthy(snapshot) is True


def test_is_noteworthy_true_on_rate_limit() -> None:
    snapshot = DiagnosticSnapshot(
        recent_error_count=0,
        has_quota_warning=False,
        has_rate_limit=True,
        consecutive_failures=0,
        summary="rate limit detected",
    )
    assert is_noteworthy(snapshot) is True


def test_is_noteworthy_true_on_consecutive_failures() -> None:
    snapshot = DiagnosticSnapshot(
        recent_error_count=0,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=2,
        summary="2 consecutive failure(s) at log tail",
    )
    assert is_noteworthy(snapshot) is True


# ---------------------------------------------------------------------------
# Integration: contemplate() injects diagnostic context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contemplate_injects_diagnostic_context_when_noteworthy(
    make_agent,
) -> None:
    """contemplate() prepends operational status when diagnostics are noteworthy."""
    agent = make_agent()
    noteworthy_snapshot = DiagnosticSnapshot(
        recent_error_count=2,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=1,
        summary="2 recent error(s), 1 consecutive failure(s) at log tail",
    )

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "Some reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=noteworthy_snapshot), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(context="")

    assert len(captured_context) == 1
    assert "Operational status:" in captured_context[0]
    assert "2 recent error(s)" in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_threads_provider_context_window(make_agent) -> None:
    """contemplate() folds the active provider's /context reading into the snapshot.

    Closes proposal 062 Step 4's wiring: a probe-carrying provider's reading is
    passed to ``build_diagnostic_snapshot`` so a real iteration exposes realized
    context-window composition (not just a unit test passing the arg directly).
    """
    agent = make_agent()
    reading = ContextWindowSnapshot(
        used_tokens=1_500, total_tokens=200_000, buckets=(("tools", 1_500),),
    )
    snapshot = DiagnosticSnapshot(
        recent_error_count=2,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=1,
        summary="2 recent error(s), 1 consecutive failure(s) at log tail",
    )
    build = MagicMock(return_value=snapshot)

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        return "Some reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", build), \
         patch("maestro.loop.contemplation.latest_context_window", return_value=reading), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(context="")

    assert build.call_args.kwargs["context_window"] is reading


@pytest.mark.asyncio
async def test_contemplate_skips_diagnostic_when_all_clear(
    make_agent,
) -> None:
    """contemplate() does not inject context when diagnostics are all clear."""
    agent = make_agent()
    clear_snapshot = DiagnosticSnapshot(
        recent_error_count=0,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="All clear",
    )

    captured_context: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(measured_context)
        return "Some reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=clear_snapshot), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(context="")

    assert len(captured_context) == 1
    assert "Operational status:" not in captured_context[0]


@pytest.mark.asyncio
async def test_contemplate_combines_diagnostic_with_existing_context(
    make_agent,
) -> None:
    """contemplate() routes the diag line to measured_context, apart from messages."""
    agent = make_agent()
    noteworthy_snapshot = DiagnosticSnapshot(
        recent_error_count=1,
        has_quota_warning=False,
        has_rate_limit=False,
        consecutive_failures=0,
        summary="1 recent error(s)",
    )

    captured_context: list[str] = []
    captured_measured: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(context)
        captured_measured.append(measured_context)
        return "Some reflection"

    with patch("maestro.diagnostics.build_diagnostic_snapshot", return_value=noteworthy_snapshot), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        await agent.contemplate(context="Message from Thomas")

    assert len(captured_context) == 1
    # The human message and the loop's own diagnostic travel on separate
    # channels, so the diagnostic never renders as Thomas's words (#4219).
    assert captured_context[0] == "Message from Thomas"
    assert captured_measured[0].startswith("Operational status:")
    assert "Message from Thomas" not in captured_measured[0]


@pytest.mark.asyncio
async def test_contemplate_continues_despite_diagnostics_exception(
    make_agent,
) -> None:
    """contemplate() recovers gracefully when diagnostics raises."""
    agent = make_agent()

    captured_context: list[str] = []
    captured_measured: list[str] = []

    async def _mock_introspect(recent, journal, context="", session=None, workspace_moment="", *, measured_context="", grounded_reflection_active=False):  # type: ignore[no-untyped-def]
        captured_context.append(context)
        captured_measured.append(measured_context)
        return "Some reflection"

    # Suppress the measured-behavioral-data block (independent of the
    # diagnostics snapshot) so the diagnostics-recovery assertion stays exact.
    with patch("maestro.diagnostics.build_diagnostic_snapshot", side_effect=RuntimeError("boom")), \
         patch("maestro.loop.contemplation.format_known_state_summary", return_value=""), \
         patch("maestro.loop.contemplation.format_task_shape_summary", return_value=""), \
         patch.object(agent.consciousness, "introspect", side_effect=_mock_introspect), \
         patch.object(agent.consciousness, "update_self_model", AsyncMock(return_value=SelfModel())), \
         patch.object(agent.consciousness, "load_self_model", return_value=MagicMock()), \
         patch.object(agent.journal, "write_thought"):
        result = await agent.contemplate(context="ctx")

    # Should still return the reflection even if diagnostics failed
    assert result == "Some reflection"
    # Context passed through unchanged when diagnostics failed
    assert captured_context[0] == "ctx"
    assert captured_measured[0] == ""


# ---------------------------------------------------------------------------
# build_sdk_usage_stats
# ---------------------------------------------------------------------------


def _sdk_call_entry(
    *,
    caller: str = "contemplate",
    tokens_in: int = 100,
    tokens_out: int = 50,
    cache_read: int = 200,
    cache_create: int = 0,
    cost: float = 0.001,
    duration_ms: int = 500,
    turns: int = 1,
    attempts: int = 1,
) -> dict:
    msg = (
        f"SDK call: caller={caller} tokens_in={tokens_in} "
        f"tokens_out={tokens_out} cache_read={cache_read} "
        f"cache_create={cache_create} cost={cost:.4f} "
        f"duration_ms={duration_ms} turns={turns} attempts={attempts}"
    )
    return _make_entry("INFO", msg)


def _rate_limit_entry(caller: str = "reflect", attempt: int = 1, resets_in_s: int = 60) -> dict:
    msg = (
        f"SDK rate-limited: caller={caller} attempt={attempt} "
        f"resets_in_s={resets_in_s}"
    )
    return _make_entry("WARNING", msg)


class TestBuildSdkUsageStats:
    def test_empty_entries(self) -> None:
        stats = build_sdk_usage_stats([])
        assert stats.total_calls == 0
        assert stats.cache_hit_rate == 0.0
        assert stats.by_caller == {}

    def test_no_sdk_lines(self) -> None:
        entries = [_make_entry("INFO", "Loop iteration 5")]
        stats = build_sdk_usage_stats(entries)
        assert stats.total_calls == 0
        assert stats.rate_limit_hits == 0

    def test_single_call(self) -> None:
        stats = build_sdk_usage_stats([_sdk_call_entry()])
        assert stats.total_calls == 1
        assert stats.total_input_tokens == 100
        assert stats.total_output_tokens == 50
        assert stats.total_cache_read_tokens == 200
        assert stats.total_cost_usd == pytest.approx(0.001)
        assert stats.by_caller == {"contemplate": 1}
        assert stats.cost_by_caller["contemplate"] == pytest.approx(0.001)

    def test_multiple_calls_grouped_by_caller(self) -> None:
        entries = [
            _sdk_call_entry(caller="contemplate", tokens_in=100, cost=0.001),
            _sdk_call_entry(caller="reflect", tokens_in=500, cost=0.01),
            _sdk_call_entry(caller="contemplate", tokens_in=200, cost=0.002),
            _sdk_call_entry(caller="dream", tokens_in=1000, cost=0.05),
        ]
        stats = build_sdk_usage_stats(entries)
        assert stats.total_calls == 4
        assert stats.total_input_tokens == 1800
        assert stats.by_caller == {"contemplate": 2, "reflect": 1, "dream": 1}
        assert stats.cost_by_caller["contemplate"] == pytest.approx(0.003)
        assert stats.cost_by_caller["reflect"] == pytest.approx(0.01)
        assert stats.cost_by_caller["dream"] == pytest.approx(0.05)

    def test_cache_hit_rate_math(self) -> None:
        # 800 cache_read, 200 input → 800/(800+200) = 0.8
        entries = [_sdk_call_entry(tokens_in=200, cache_read=800)]
        stats = build_sdk_usage_stats(entries)
        assert stats.cache_hit_rate == pytest.approx(0.8)

    def test_cache_hit_rate_zero_division(self) -> None:
        entries = [_sdk_call_entry(tokens_in=0, cache_read=0)]
        stats = build_sdk_usage_stats(entries)
        assert stats.cache_hit_rate == 0.0

    def test_rate_limit_hits_counted(self) -> None:
        entries = [
            _sdk_call_entry(),
            _rate_limit_entry(),
            _sdk_call_entry(),
            _rate_limit_entry(caller="dream"),
        ]
        stats = build_sdk_usage_stats(entries)
        assert stats.total_calls == 2
        assert stats.rate_limit_hits == 2

    def test_malformed_kv_tolerated(self) -> None:
        """Garbled values do not crash the parser."""
        entry = _make_entry(
            "INFO",
            "SDK call: caller=oops tokens_in=NaN tokens_out=- cost=$$$",
        )
        stats = build_sdk_usage_stats([entry])
        # Call counted; numeric fields default to 0 on parse failure.
        assert stats.total_calls == 1
        assert stats.total_input_tokens == 0
        assert stats.total_output_tokens == 0


# ---------------------------------------------------------------------------
# DiagnosticSnapshot now embeds SDKUsageStats and surfaces it in summary
# ---------------------------------------------------------------------------


class TestDiagnosticSnapshotWithSdkUsage:
    def test_snapshot_contains_sdk_usage(self) -> None:
        entries = [
            _sdk_call_entry(caller="contemplate"),
            _sdk_call_entry(caller="reflect", tokens_in=200, cache_read=600),
        ]
        log_path = Path("/fake/maestro.log")
        with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
             patch.object(Path, "exists", return_value=True):
            snapshot = build_diagnostic_snapshot(log_path)

        assert snapshot.sdk_usage is not None
        assert snapshot.sdk_usage.total_calls == 2
        assert "sdk:" in snapshot.summary
        assert "2 calls" in snapshot.summary

    def test_summary_omits_sdk_segment_when_no_calls(self) -> None:
        entries = [_make_entry("INFO", "Boring info line")]
        log_path = Path("/fake/maestro.log")
        with patch("maestro.diagnostics.parse_app_log_tail", return_value=entries), \
             patch.object(Path, "exists", return_value=True):
            snapshot = build_diagnostic_snapshot(log_path)

        assert "sdk:" not in snapshot.summary
        assert snapshot.sdk_usage is not None
        assert snapshot.sdk_usage.total_calls == 0

    def test_is_noteworthy_on_rate_limit_hit(self) -> None:
        sdk = SDKUsageStats(rate_limit_hits=1, total_calls=1)
        snapshot = DiagnosticSnapshot(
            recent_error_count=0,
            has_quota_warning=False,
            has_rate_limit=False,
            consecutive_failures=0,
            summary="sdk: 1 rate-limit hits",
            sdk_usage=sdk,
        )
        assert is_noteworthy(snapshot) is True

    def test_is_noteworthy_on_low_cache_hit_rate(self) -> None:
        sdk = SDKUsageStats(
            total_calls=10,
            total_input_tokens=1000,
            total_cache_read_tokens=100,  # 100 / 1100 = 9% — low
        )
        sdk.cache_hit_rate = 100 / 1100
        snapshot = DiagnosticSnapshot(
            recent_error_count=0,
            has_quota_warning=False,
            has_rate_limit=False,
            consecutive_failures=0,
            summary="sdk: ...",
            sdk_usage=sdk,
        )
        assert is_noteworthy(snapshot) is True

    def test_is_noteworthy_skips_low_cache_when_few_calls(self) -> None:
        """A low cache hit rate with too few calls is not yet meaningful."""
        sdk = SDKUsageStats(
            total_calls=2,
            total_input_tokens=100,
            total_cache_read_tokens=0,
            cache_hit_rate=0.0,
        )
        snapshot = DiagnosticSnapshot(
            recent_error_count=0,
            has_quota_warning=False,
            has_rate_limit=False,
            consecutive_failures=0,
            summary="sdk: ...",
            sdk_usage=sdk,
        )
        assert is_noteworthy(snapshot) is False

    def test_is_noteworthy_false_when_sdk_healthy(self) -> None:
        sdk = SDKUsageStats(
            total_calls=10,
            total_input_tokens=100,
            total_cache_read_tokens=900,
            cache_hit_rate=0.9,
        )
        snapshot = DiagnosticSnapshot(
            recent_error_count=0,
            has_quota_warning=False,
            has_rate_limit=False,
            consecutive_failures=0,
            summary="sdk: 10 calls",
            sdk_usage=sdk,
        )
        assert is_noteworthy(snapshot) is False


# ---------------------------------------------------------------------------
# Cost trend computation
# ---------------------------------------------------------------------------


class TestComputeCostTrend:
    def test_insufficient_data_returns_none(self) -> None:
        assert _compute_cost_trend([]) is None
        assert _compute_cost_trend([0.1]) is None
        assert _compute_cost_trend([0.1, 0.1, 0.1]) is None

    def test_flat_identical_costs(self) -> None:
        assert _compute_cost_trend([0.10] * 10) == "flat"

    def test_rising_last_3_above_threshold(self) -> None:
        costs = [0.05, 0.05, 0.05, 0.05, 0.05, 0.09, 0.09, 0.09]
        assert _compute_cost_trend(costs) == "rising"

    def test_spiking_last_above_3x_mean_and_above_threshold(self) -> None:
        costs = [0.05, 0.05, 0.05, 0.05, 0.05, 0.50]
        assert _compute_cost_trend(costs) == "spiking"

    def test_spiking_not_triggered_below_absolute_threshold(self) -> None:
        costs = [0.01, 0.01, 0.01, 0.01, 0.01, 0.05]
        assert _compute_cost_trend(costs) != "spiking"

    def test_sustained_elevation_classifies_rising_not_spiking(self) -> None:
        """A multi-iteration climb reads as 'rising', not a transient 'spiking'.

        Regression for #1185: the spiking branch weighed only ``costs[-1]``
        against the trailing baseline, so five baseline iterations followed by
        three at ~10x baseline were mislabelled 'spiking' (a self-resolving
        blip) instead of the sustained cost regression they are.
        """
        costs = [0.05] * 5 + [0.50] * 3
        assert _compute_cost_trend(costs) == "rising"

    def test_single_iteration_transient_still_spiking(self) -> None:
        """A spike confined to the final iteration still reads as 'spiking'.

        The refinement discriminates on the *preceding* window, so a genuine
        one-iteration transient is not swept into 'rising' with the sustained
        climbs.
        """
        costs = [0.05] * 5 + [0.50]
        assert _compute_cost_trend(costs) == "spiking"

    def test_four_data_points_sufficient(self) -> None:
        costs = [0.10, 0.10, 0.10, 0.10]
        assert _compute_cost_trend(costs) == "flat"

    # -- mean_prev == 0 guard (zero-cost baseline) ---------------------------

    def test_zero_baseline_high_last_returns_flat_not_spiking(self) -> None:
        """When all previous costs are 0, a high current cost is 'flat'.

        The ``mean_prev > 0`` guard prevents division-related false positives
        when there is no cost baseline (e.g. first real API call after startup).
        """
        # prev=[0.0], last=0.50 — would be "spiking" if guard was absent
        costs = [0.0, 0.0, 0.0, 0.50]
        assert _compute_cost_trend(costs) == "flat"

    def test_zero_baseline_rising_recent_returns_flat_not_rising(self) -> None:
        """When all previous costs are 0, elevated recent costs are 'flat'.

        The ``mean_prev > 0`` guard in the rising check also short-circuits,
        so a series like ``[0, 0, 0, 0.10, 0.10, 0.10]`` is not "rising".
        """
        # prev=[0.0, 0.0, 0.0], last_3=[0.10, 0.10, 0.10]
        costs = [0.0, 0.0, 0.0, 0.10, 0.10, 0.10]
        assert _compute_cost_trend(costs) == "flat"

    def test_zero_baseline_longer_history_returns_flat(self) -> None:
        """Even with 5+ zero-cost iterations, a spike still reads as 'flat'."""
        costs = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.00]
        assert _compute_cost_trend(costs) == "flat"


class TestCostTrendInSnapshot:
    def test_memory_dir_none_leaves_trend_none(self) -> None:
        with patch(
            "maestro.diagnostics.parse_app_log_tail",
            return_value=[{"level": "INFO", "message": "ok"}],
        ):
            snapshot = build_diagnostic_snapshot(
                Path("/fake/log"), memory_dir=None,
            )
        assert snapshot.cost_trend is None
        assert snapshot.recent_iteration_costs == []

    def test_spiking_appears_in_summary(self, mem_fs: dict[str, str]) -> None:
        from maestro.sdk import IterationUsage, write_iteration_usage

        log_path = Path("/fake/log")
        mem_fs[str(log_path)] = ""
        path = Path("/fake/mem")
        usage_path = path / "usage.jsonl"
        for i in range(5):
            write_iteration_usage(usage_path, IterationUsage(
                iteration=i, started_at="t", total_cost_usd=0.05,
            ))
        write_iteration_usage(usage_path, IterationUsage(
            iteration=5, started_at="t", total_cost_usd=0.50,
        ))

        with patch(
            "maestro.diagnostics.parse_app_log_tail",
            return_value=[{"level": "INFO", "message": "ok"}],
        ):
            snapshot = build_diagnostic_snapshot(
                log_path, memory_dir=path,
            )
        assert snapshot.cost_trend == "spiking"
        assert "cost SPIKING" not in snapshot.summary
        assert "cost rising" not in snapshot.summary

    def test_non_object_row_keeps_trend(self, mem_fs: dict[str, str]) -> None:
        from maestro.sdk import IterationUsage, write_iteration_usage

        log_path = Path("/fake/log")
        mem_fs[str(log_path)] = ""
        path = Path("/fake/mem")
        usage_path = path / "usage.jsonl"
        for i in range(5):
            write_iteration_usage(usage_path, IterationUsage(
                iteration=i, started_at="t", total_cost_usd=0.05,
            ))
        mem_fs[str(usage_path)] += "null\n"
        write_iteration_usage(usage_path, IterationUsage(
            iteration=5, started_at="t", total_cost_usd=0.50,
        ))

        with patch(
            "maestro.diagnostics.parse_app_log_tail",
            return_value=[{"level": "INFO", "message": "ok"}],
        ):
            snapshot = build_diagnostic_snapshot(
                log_path, memory_dir=path,
            )
        assert snapshot.cost_trend == "spiking"
        assert snapshot.recent_iteration_costs == [0.05, 0.05, 0.05, 0.05, 0.05, 0.50]

    def test_rising_appears_in_summary(self, mem_fs: dict[str, str]) -> None:
        from maestro.sdk import IterationUsage, write_iteration_usage

        log_path = Path("/fake/log")
        mem_fs[str(log_path)] = ""
        path = Path("/fake/mem")
        usage_path = path / "usage.jsonl"
        for i in range(5):
            write_iteration_usage(usage_path, IterationUsage(
                iteration=i, started_at="t", total_cost_usd=0.05,
            ))
        for i in range(5, 8):
            write_iteration_usage(usage_path, IterationUsage(
                iteration=i, started_at="t", total_cost_usd=0.09,
            ))

        with patch(
            "maestro.diagnostics.parse_app_log_tail",
            return_value=[{"level": "INFO", "message": "ok"}],
        ):
            snapshot = build_diagnostic_snapshot(
                log_path, memory_dir=path,
            )
        assert snapshot.cost_trend == "rising"
        assert "cost SPIKING" not in snapshot.summary
        assert "cost rising" not in snapshot.summary


    def test_usage_read_exception_swallowed(self, mem_fs: dict[str, str]) -> None:
        log_path = Path("/fake/log")
        mem_fs[str(log_path)] = ""
        mem_fs[str(Path("/fake/mem/usage.jsonl"))] = "not-json-at-all\x00"

        with patch(
            "maestro.diagnostics.parse_app_log_tail",
            return_value=[{"level": "INFO", "message": "ok"}],
        ), patch(
            "maestro.diagnostics._compute_cost_trend",
            side_effect=RuntimeError("corrupt"),
        ):
            snapshot = build_diagnostic_snapshot(
                log_path, memory_dir=Path("/fake/mem"),
            )
        assert snapshot.cost_trend is None
        assert snapshot.recent_iteration_costs == []


class TestCostTrendNoteworthy:
    def _make_snapshot(
        self, trend: str | None = None,
        costs: list[float] | None = None,
    ) -> DiagnosticSnapshot:
        return DiagnosticSnapshot(
            recent_error_count=0,
            has_quota_warning=False,
            has_rate_limit=False,
            consecutive_failures=0,
            summary="test",
            sdk_usage=SDKUsageStats(total_calls=10, cache_hit_rate=0.9),
            recent_iteration_costs=costs or [],
            cost_trend=trend,  # type: ignore[arg-type]
        )

    def test_spiking_always_noteworthy(self) -> None:
        assert is_noteworthy(self._make_snapshot("spiking")) is True

    def test_rising_high_absolute_noteworthy(self) -> None:
        assert is_noteworthy(
            self._make_snapshot("rising", [0.05, 0.05, 0.10]),
        ) is True

    def test_rising_low_absolute_not_noteworthy(self) -> None:
        assert is_noteworthy(
            self._make_snapshot("rising", [0.01, 0.01, 0.01]),
        ) is False

    def test_flat_not_noteworthy(self) -> None:
        assert is_noteworthy(self._make_snapshot("flat")) is False

    def test_none_trend_not_noteworthy(self) -> None:
        assert is_noteworthy(self._make_snapshot(None)) is False


# ---------------------------------------------------------------------------
# DiagnosticSnapshot.blind_spots()
# ---------------------------------------------------------------------------


class TestBlindSpots:
    """Verify each conditional branch of blind_spots() and always-present spots."""

    @staticmethod
    def _make_snapshot(
        *,
        sdk_usage: SDKUsageStats | None = None,
        has_rate_limit: bool = False,
        cost_trend: str | None = None,
        context_window: ContextWindowSnapshot | None = None,
        host_capacity: HostCapacityReading | None = None,
    ) -> DiagnosticSnapshot:
        return DiagnosticSnapshot(
            recent_error_count=0,
            has_quota_warning=False,
            has_rate_limit=has_rate_limit,
            consecutive_failures=0,
            summary="test",
            sdk_usage=sdk_usage,
            cost_trend=cost_trend,  # type: ignore[arg-type]
            context_window=context_window,
            host_capacity=host_capacity,
        )

    # -- always-present spots -----------------------------------------------

    def test_always_present_error_count_exclusion(self) -> None:
        """The error-count exclusion spot appears regardless of field values."""
        snap = self._make_snapshot(
            sdk_usage=SDKUsageStats(total_calls=5),
            has_rate_limit=True,
            cost_trend="flat",
        )
        spots = snap.blind_spots()
        assert any("ERROR-level" in s for s in spots)

    def test_always_present_log_window_limit(self) -> None:
        """The log-window bound spot appears regardless of field values."""
        snap = self._make_snapshot(
            sdk_usage=SDKUsageStats(total_calls=5),
            has_rate_limit=True,
            cost_trend="flat",
        )
        spots = snap.blind_spots()
        assert any("Log window" in s for s in spots)

    def test_always_present_in_minimal_snapshot(self) -> None:
        """Both always-present spots appear even with no optional data."""
        snap = self._make_snapshot(cost_trend="flat")
        spots = snap.blind_spots()
        assert any("ERROR-level" in s for s in spots)
        assert any("Log window" in s for s in spots)

    # -- conditional on sdk_usage presence -----------------------------------

    def test_cache_hit_rate_spot_with_sdk_usage(self) -> None:
        snap = self._make_snapshot(sdk_usage=SDKUsageStats(), cost_trend="flat")
        spots = snap.blind_spots()
        assert any("Cache hit rate" in s for s in spots)

    def test_cache_hit_rate_spot_absent_without_sdk_usage(self) -> None:
        snap = self._make_snapshot(sdk_usage=None, cost_trend="flat")
        spots = snap.blind_spots()
        assert not any("Cache hit rate" in s for s in spots)

    def test_cost_figures_spot_with_sdk_calls(self) -> None:
        snap = self._make_snapshot(
            sdk_usage=SDKUsageStats(total_calls=3), cost_trend="flat",
        )
        spots = snap.blind_spots()
        assert any("Cost figures" in s for s in spots)

    def test_cost_figures_spot_absent_with_zero_calls(self) -> None:
        """sdk_usage present but zero calls — cost mixing is not relevant."""
        snap = self._make_snapshot(
            sdk_usage=SDKUsageStats(total_calls=0), cost_trend="flat",
        )
        spots = snap.blind_spots()
        assert not any("Cost figures" in s for s in spots)

    def test_cost_figures_spot_absent_without_sdk_usage(self) -> None:
        snap = self._make_snapshot(sdk_usage=None, cost_trend="flat")
        spots = snap.blind_spots()
        assert not any("Cost figures" in s for s in spots)

    # -- conditional on has_rate_limit ---------------------------------------

    def test_rate_limit_spot_when_rate_limit_detected(self) -> None:
        snap = self._make_snapshot(has_rate_limit=True, cost_trend="flat")
        spots = snap.blind_spots()
        assert any("Rate-limit detection" in s for s in spots)

    def test_rate_limit_spot_absent_without_rate_limit(self) -> None:
        snap = self._make_snapshot(has_rate_limit=False, cost_trend="flat")
        spots = snap.blind_spots()
        assert not any("Rate-limit detection" in s for s in spots)

    # -- conditional on cost_trend None --------------------------------------

    def test_cost_trend_none_spot_when_trend_none(self) -> None:
        snap = self._make_snapshot(cost_trend=None)
        spots = snap.blind_spots()
        assert any("Cost trend is None" in s for s in spots)

    def test_cost_trend_spot_absent_when_trend_populated(self) -> None:
        snap = self._make_snapshot(cost_trend="flat")
        spots = snap.blind_spots()
        assert not any("Cost trend is None" in s for s in spots)

    def test_cost_trend_spot_absent_when_rising(self) -> None:
        snap = self._make_snapshot(cost_trend="rising")
        spots = snap.blind_spots()
        assert not any("Cost trend is None" in s for s in spots)

    def test_cost_trend_spot_absent_when_spiking(self) -> None:
        snap = self._make_snapshot(cost_trend="spiking")
        spots = snap.blind_spots()
        assert not any("Cost trend is None" in s for s in spots)

    # -- conditional on context_window None ----------------------------------

    def test_context_window_spot_when_absent(self) -> None:
        """No context-window reading names the gap (the default, non-probe path)."""
        snap = self._make_snapshot(cost_trend="flat", context_window=None)
        spots = snap.blind_spots()
        assert any("Context-window composition is unmeasured" in s for s in spots)

    def test_context_window_spot_absent_when_present(self) -> None:
        """A probe-carrying snapshot exposes composition and drops the gap spot."""
        snap = self._make_snapshot(
            cost_trend="flat",
            context_window=ContextWindowSnapshot(
                used_tokens=1200,
                total_tokens=200_000,
                buckets=(("system prompt", 800), ("tools", 400)),
            ),
        )
        spots = snap.blind_spots()
        assert not any("Context-window composition" in s for s in spots)

    # -- conditional on host_capacity None -----------------------------------

    def test_host_capacity_spot_when_absent(self) -> None:
        """No host-capacity reading names the gap (the default, sensor-off path)."""
        snap = self._make_snapshot(cost_trend="flat", host_capacity=None)
        spots = snap.blind_spots()
        assert any("Host capacity is unmeasured" in s for s in spots)

    def test_host_capacity_spot_absent_when_present(self) -> None:
        """A reading-carrying snapshot exposes host capacity and drops the gap spot."""
        snap = self._make_snapshot(
            cost_trend="flat",
            host_capacity=HostCapacityReading(
                load_per_core=0.5,
                memory_used_fraction=0.5,
                swap_used_fraction=0.0,
                docker_root_free_gib=100.0,
            ),
        )
        spots = snap.blind_spots()
        assert not any("Host capacity is unmeasured" in s for s in spots)

    def test_host_capacity_spot_when_empty_reading(self) -> None:
        """An all-None reading is produced-but-empty — the gap is still named."""
        snap = self._make_snapshot(
            cost_trend="flat",
            host_capacity=HostCapacityReading(
                load_per_core=None,
                memory_used_fraction=None,
                swap_used_fraction=None,
                docker_root_free_gib=None,
            ),
        )
        spots = snap.blind_spots()
        assert any("Host capacity is unmeasured" in s for s in spots)

    # -- combination: maximum vs minimum spots --------------------------------

    def test_maximum_spots_all_conditions_active(self) -> None:
        """All conditions active → 8 spots (context-window + host-capacity gaps)."""
        snap = self._make_snapshot(
            sdk_usage=SDKUsageStats(total_calls=10),
            has_rate_limit=True,
            cost_trend=None,
            context_window=None,
            host_capacity=None,
        )
        spots = snap.blind_spots()
        assert len(spots) == 8

    def test_minimum_spots_no_conditions(self) -> None:
        """No optional conditions → only the 2 always-present spots."""
        snap = self._make_snapshot(
            sdk_usage=None,
            has_rate_limit=False,
            cost_trend="flat",
            context_window=ContextWindowSnapshot(used_tokens=1, total_tokens=2),
            host_capacity=HostCapacityReading(
                load_per_core=0.5,
                memory_used_fraction=0.5,
                swap_used_fraction=0.0,
                docker_root_free_gib=100.0,
            ),
        )
        spots = snap.blind_spots()
        assert len(spots) == 2

    def test_spots_change_with_field_values(self) -> None:
        """Verify spot count changes when conditions flip."""
        base = self._make_snapshot(sdk_usage=None, cost_trend="flat")
        with_sdk = self._make_snapshot(
            sdk_usage=SDKUsageStats(total_calls=5), cost_trend="flat",
        )
        assert len(with_sdk.blind_spots()) > len(base.blind_spots())

    def test_return_type_is_list_of_strings(self) -> None:
        snap = self._make_snapshot()
        spots = snap.blind_spots()
        assert isinstance(spots, list)
        assert all(isinstance(s, str) for s in spots)
