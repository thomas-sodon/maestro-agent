"""Tests for the Budget tab on the dashboard."""

from __future__ import annotations

from typing import Any


def test_budget_nav_link_present(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    page, *_ = dashboard_page
    link = page.locator('.nav-link[data-nav="budget"]')
    assert link.count() == 1
    assert (link.text_content() or "").strip() == "Budget"


def test_budget_window_buttons_present(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    page, *_ = dashboard_page
    buttons = page.locator("[data-budget-window]")
    texts = buttons.all_text_contents()
    assert "1h" in texts
    assert "24h" in texts
    assert "7d" in texts


def test_budget_empty_state(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    page, *_ = dashboard_page
    # Empty mock data produces empty-state messages in both tables.
    assert page.locator("#budget-by-group .empty").is_visible()
    assert page.locator("#budget-by-caller .empty").is_visible()
    summary = page.locator("#budget-summary").text_content() or ""
    assert "Total" in summary
    assert "$0.00" in summary


def test_budget_renders_populated(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["budget"] = {
        "window_start": "2025-06-01T11:00:00+00:00",
        "window_end": "2025-06-01T12:00:00+00:00",
        "window_hours": 1.0,
        "total_cost_usd": 3.0,
        "reported_cost_usd": 2.0,
        "estimated_cost_usd": 1.0,
        "total_calls": 2,
        "by_caller": [
            {
                "caller": "reflect", "group": "Evolution",
                "cost_usd": 2.0, "calls": 1,
                "reported_cost_usd": 2.0, "estimated_cost_usd": 0.0,
                "share_of_total": 0.6667, "providers": ["anthropic"],
            },
            {
                "caller": "introspect", "group": "Contemplation",
                "cost_usd": 1.0, "calls": 1,
                "reported_cost_usd": 0.0, "estimated_cost_usd": 1.0,
                "share_of_total": 0.3333,
            },
        ],
        "by_group": [
            {"group": "Evolution", "cost_usd": 2.0, "calls": 1, "share_of_total": 0.6667},
            {"group": "Contemplation", "cost_usd": 1.0, "calls": 1, "share_of_total": 0.3333},
        ],
    }
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")

    summary = page.locator("#budget-summary").text_content() or ""
    assert "$3.00" in summary  # Total
    assert "$2.00" in summary  # Reported
    assert "$1.00" in summary  # Estimated
    assert "2" in summary      # Calls

    group_text = page.locator("#budget-by-group").text_content() or ""
    assert "Evolution" in group_text
    assert "Contemplation" in group_text

    caller_text = page.locator("#budget-by-caller").text_content() or ""
    assert "reflect" in caller_text
    assert "introspect" in caller_text

    # Provider attribution column (067 Step 2): an attributed caller names
    # its provider; a row without attribution renders the honest em-dash.
    assert "Provider" in caller_text
    assert "anthropic" in caller_text

    # Share bars rendered in both tables.
    assert page.locator(".tab-panel[data-tab='budget'] .bar-fill").count() >= 2


def test_budget_renders_unmetered_as_dash(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A window of only unmetered spend (a usage_metering=False provider)
    renders ``—`` instead of misleading ``$0.00`` figures (058 Step 13)."""
    url, data, _ = dashboard_server
    data["budget"] = {
        "window_start": "2025-06-01T11:00:00+00:00",
        "window_end": "2025-06-01T12:00:00+00:00",
        "window_hours": 1.0,
        "total_cost_usd": 0.0,
        "reported_cost_usd": 0.0,
        "estimated_cost_usd": 0.0,
        "total_calls": 1,
        "by_caller": [
            {
                "caller": "reflect", "group": "Evolution",
                "cost_usd": 0.0, "calls": 1,
                "reported_cost_usd": 0.0, "estimated_cost_usd": 0.0,
                "share_of_total": 0.0, "unmetered": True,
                "providers": ["cli_paced"],
            },
        ],
        "by_group": [
            {
                "group": "Evolution", "cost_usd": 0.0, "calls": 1,
                "share_of_total": 0.0, "unmetered": True,
            },
        ],
    }
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")

    # All callers unmetered → top-line totals render as em-dashes, not $0.00.
    summary = page.locator("#budget-summary").text_content() or ""
    assert "—" in summary
    assert "$0.00" not in summary

    group_text = page.locator("#budget-by-group").text_content() or ""
    assert "—" in group_text
    assert "$0.000" not in group_text

    caller_text = page.locator("#budget-by-caller").text_content() or ""
    assert "—" in caller_text
    assert "$0.000" not in caller_text
    # The unknown spend is attributed to its producing provider (067 Step 2),
    # so the operator reads it as unknown-by-design under cli_paced.
    assert "cli_paced" in caller_text
