"""Tests for the Diagnostics card on the dashboard index page."""

from __future__ import annotations

from typing import Any


def test_diagnostics_empty_state(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """When sdk_usage is None, the card shows the empty-state message."""
    page, *_ = dashboard_page
    card = page.locator("#diagnostics")
    assert card.is_visible()
    text = card.text_content() or ""
    assert "No SDK calls observed" in text


def test_diagnostics_renders_sdk_usage(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """When sdk_usage is populated, the card shows totals, cache rate, and per-caller rows."""
    url, data, _ = dashboard_server
    data["diagnostics"] = {
        "recent_error_count": 0,
        "has_quota_warning": False,
        "has_rate_limit": False,
        "consecutive_failures": 0,
        "summary": "sdk: 12 calls, 8.0k in, 2.0k out, cache 67%, $0.42, 0 rate-limit hits",
        "sdk_usage": {
            "total_calls": 12,
            "total_input_tokens": 8000,
            "total_output_tokens": 2000,
            "total_cache_read_tokens": 16000,
            "total_cache_creation_tokens": 0,
            "total_cost_usd": 0.42,
            "cache_hit_rate": 0.67,
            "by_caller": {"contemplate": 5, "reflect": 4, "dream": 3},
            "cost_by_caller": {"contemplate": 0.20, "reflect": 0.15, "dream": 0.07},
            "rate_limit_hits": 0,
        },
    }
    page.goto(url)
    page.wait_for_load_state("networkidle")

    card = page.locator("#diagnostics")
    text = card.text_content() or ""
    # Summary line
    assert "sdk: 12 calls" in text
    # Stat tiles
    assert "12" in text  # total_calls
    assert "8.0k" in text  # tokens in
    assert "2.0k" in text  # tokens out
    assert "67%" in text  # cache hit
    assert "$0.42" in text  # cost
    # Per-caller rows
    assert "contemplate" in text
    assert "reflect" in text
    assert "dream" in text
    # Cache hit cell uses success class (≥ 60%)
    cache_stat = card.locator(".stat.outcome-success")
    assert cache_stat.count() >= 1


def test_diagnostics_low_cache_hit_flagged_red(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Cache hit rate < 30% gets the failure (red) class."""
    url, data, _ = dashboard_server
    data["diagnostics"] = {
        "recent_error_count": 0,
        "has_quota_warning": False,
        "has_rate_limit": False,
        "consecutive_failures": 0,
        "summary": "sdk: low cache",
        "sdk_usage": {
            "total_calls": 10,
            "total_input_tokens": 10000,
            "total_output_tokens": 1000,
            "total_cache_read_tokens": 1000,
            "total_cache_creation_tokens": 0,
            "total_cost_usd": 0.10,
            "cache_hit_rate": 0.09,
            "by_caller": {"reflect": 10},
            "cost_by_caller": {"reflect": 0.10},
            "rate_limit_hits": 0,
        },
    }
    page.goto(url)
    page.wait_for_load_state("networkidle")

    card = page.locator("#diagnostics")
    failure_stats = card.locator(".stat.outcome-failure")
    assert failure_stats.count() >= 1
    # The 9% cache hit cell should be among them
    text = (card.text_content() or "")
    assert "9%" in text


def test_diagnostics_rate_limit_hits_flagged_red(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Non-zero rate_limit_hits gets the failure (red) class on its tile."""
    url, data, _ = dashboard_server
    data["diagnostics"] = {
        "recent_error_count": 0,
        "has_quota_warning": False,
        "has_rate_limit": True,
        "consecutive_failures": 0,
        "summary": "sdk: rate limited",
        "sdk_usage": {
            "total_calls": 5,
            "total_input_tokens": 1000,
            "total_output_tokens": 500,
            "total_cache_read_tokens": 4000,
            "total_cache_creation_tokens": 0,
            "total_cost_usd": 0.05,
            "cache_hit_rate": 0.80,
            "by_caller": {"contemplate": 5},
            "cost_by_caller": {"contemplate": 0.05},
            "rate_limit_hits": 3,
        },
    }
    page.goto(url)
    page.wait_for_load_state("networkidle")

    card = page.locator("#diagnostics")
    text = card.text_content() or ""
    assert "Rate-Limit Hits" in text
    # The "3" rate-limit cell exists with failure class
    failure_stats = card.locator(".stat.outcome-failure")
    assert failure_stats.count() >= 1
