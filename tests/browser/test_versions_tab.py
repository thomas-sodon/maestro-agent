"""Tests for the Versions tab on the dashboard."""

from __future__ import annotations

from typing import Any


def test_versions_nav_link_present(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    page, *_ = dashboard_page
    link = page.locator('.nav-link[data-nav="versions"]')
    assert link.count() == 1
    assert (link.text_content() or "").strip() == "Versions"


def test_versions_empty_state(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    page, *_ = dashboard_page
    text = page.locator("#versions").text_content() or ""
    assert "No merged versions" in text


def test_versions_renders_populated(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["versions"] = [
        {
            "version_id": "v20260101120000",
            "plan_id": "ep_1",
            "plan_title": "Add version archive",
            "commit_sha": "deadbeef",
            "merged_at": "2026-01-01T12:00:00+00:00",
            "window_tasks_total": 5,
            "window_tasks_succeeded": 4,
            "window_tasks_failed": 1,
            "window_sdk_cost_usd": 2.50,
            "window_sdk_calls": 12,
            "window_cache_hit_rate": 0.75,
            "window_rate_limit_hits": 0,
            "window_error_count": 0,
            "quality_gate_pass": True,
            "notes": None,
        },
    ]
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")

    text = page.locator("#versions").text_content() or ""
    assert "Add version archive" in text
    assert "$2.50" in text
    assert "4/5" in text
    assert "pass" in text
