"""Tests that the dashboard page loads and renders all sections correctly."""

from __future__ import annotations

from typing import Any


def test_page_title(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    assert page.title() == "Maestro — Account"


def test_main_heading(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    h1 = page.locator("h1")
    assert h1.text_content() == "Maestro"


def test_subtitle(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    subtitle = page.locator(".subtitle")
    assert "Autonomous agent" in (subtitle.text_content() or "")


def test_section_headers_visible(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    expected_headers = [
        "Identity",
        "Pending Evolution Plans",
        "All Evolution Plans",
        "Journal",
        "Task History",
        "Logs",
    ]
    h2_texts = page.locator("h2").all_text_contents()
    for header in expected_headers:
        assert header in h2_texts, f"Section header '{header}' not found"


def test_status_and_metrics_cards(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    assert page.locator(".card-title", has_text="Status").is_visible()
    assert page.locator(".card-title", has_text="Metrics").is_visible()


def test_log_source_filter_buttons(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    source_buttons = page.locator("[data-log-source]")
    texts = source_buttons.all_text_contents()
    assert "All" in texts
    assert "App" in texts
    assert "SDK" in texts


def test_log_level_filter_buttons(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    level_buttons = page.locator("[data-log-level]")
    texts = level_buttons.all_text_contents()
    assert "All Levels" in texts
    assert "Debug" in texts
    assert "Info" in texts
    assert "Warning" in texts
    assert "Error" in texts


def test_sdk_sessions_tab_present(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    """Sessions is its own nav destination with a follow control of its own."""
    page, *_ = dashboard_page
    assert page.locator('.nav-link[data-nav="sessions"]').is_visible()
    assert page.locator("#btn-sdk-follow").is_visible()


def test_auto_refresh_note(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    note = page.locator(".refresh-note")
    assert "Auto-refreshes every 10 seconds" in (note.text_content() or "")


def test_empty_states(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    assert page.locator("#pending-plans .empty", has_text="No pending plans.").is_visible()
    assert page.locator("#all-plans .empty", has_text="No completed plans yet.").is_visible()
    assert page.locator("#identity .empty", has_text="No identity data yet").is_visible()
    assert page.locator("#journal .empty", has_text="Journal is empty").is_visible()
    assert page.locator("#tasks .empty", has_text="No tasks completed yet.").is_visible()
