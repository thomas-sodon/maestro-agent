"""Tests for log level and source filtering on the dashboard."""

from __future__ import annotations

from typing import Any

from tests.browser.conftest import make_log_entry


def _setup_logs(data: dict[str, Any]) -> None:
    """Pre-populate logs with entries at various levels and sources."""
    data["logs"] = [
        make_log_entry("debug app msg", level="DEBUG", source="app"),
        make_log_entry("info app msg", level="INFO", source="app"),
        make_log_entry("warning app msg", level="WARNING", source="app"),
        make_log_entry("error app msg", level="ERROR", source="app"),
        make_log_entry("SDK debug msg", level="DEBUG", source="sdk"),
        make_log_entry("SDK info msg", level="INFO", source="sdk"),
        make_log_entry("SDK warning msg", level="WARNING", source="sdk"),
    ]


def test_all_logs_displayed_by_default(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    _setup_logs(data)
    page.goto(url)
    page.wait_for_load_state("networkidle")

    entries = page.locator("#logs .log-entry")
    assert entries.count() == 7


def test_level_filter_warning(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    _setup_logs(data)
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Click Warning level filter
    page.locator('[data-log-level="WARNING"]').click()
    page.wait_for_timeout(500)

    entries = page.locator("#logs .log-entry")
    assert entries.count() == 2  # app warning + SDK warning
    for i in range(entries.count()):
        assert "WARNING" in (entries.nth(i).text_content() or "")


def test_level_filter_all_levels(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    _setup_logs(data)
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # First filter to WARNING
    page.locator('[data-log-level="WARNING"]').click()
    page.wait_for_timeout(500)
    assert page.locator("#logs .log-entry").count() == 2

    # Then click "All Levels" to reset
    page.locator('[data-log-level=""]').click()
    page.wait_for_timeout(500)
    assert page.locator("#logs .log-entry").count() == 7


def test_source_filter_sdk(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    _setup_logs(data)
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Click SDK source filter
    page.locator('[data-log-source="sdk"]').click()
    page.wait_for_timeout(500)

    entries = page.locator("#logs .log-entry")
    assert entries.count() == 3  # 3 SDK entries


def test_source_filter_app(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    _setup_logs(data)
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-log-source="app"]').click()
    page.wait_for_timeout(500)

    entries = page.locator("#logs .log-entry")
    assert entries.count() == 4  # 4 app entries


def test_source_filter_all(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    _setup_logs(data)
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Filter to SDK first
    page.locator('[data-log-source="sdk"]').click()
    page.wait_for_timeout(500)
    assert page.locator("#logs .log-entry").count() == 3

    # Reset to All
    page.locator('[data-log-source=""]').click()
    page.wait_for_timeout(500)
    assert page.locator("#logs .log-entry").count() == 7


def test_combined_source_and_level_filter(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    _setup_logs(data)
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Filter to SDK source + WARNING level
    page.locator('[data-log-source="sdk"]').click()
    page.wait_for_timeout(300)
    page.locator('[data-log-level="WARNING"]').click()
    page.wait_for_timeout(500)

    entries = page.locator("#logs .log-entry")
    assert entries.count() == 1  # Only SDK warning
    assert "SDK warning msg" in (entries.first.text_content() or "")


def test_level_filter_active_class(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    _setup_logs(data)
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Initially "All Levels" has active class
    all_btn = page.locator('[data-log-level=""]')
    assert "active" in (all_btn.get_attribute("class") or "")

    # Click Warning
    warn_btn = page.locator('[data-log-level="WARNING"]')
    warn_btn.click()
    page.wait_for_timeout(300)

    assert "active" in (warn_btn.get_attribute("class") or "")
    assert "active" not in (all_btn.get_attribute("class") or "")


def test_source_filter_active_class(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    _setup_logs(data)
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Initially "All" source has active class
    all_btn = page.locator('[data-log-source=""]')
    assert "active" in (all_btn.get_attribute("class") or "")

    # Click SDK
    sdk_btn = page.locator('[data-log-source="sdk"]')
    sdk_btn.click()
    page.wait_for_timeout(300)

    assert "active" in (sdk_btn.get_attribute("class") or "")
    assert "active" not in (all_btn.get_attribute("class") or "")


def test_sdk_log_entries_have_sdk_class(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["logs"] = [
        make_log_entry("SDK query started", level="INFO", source="sdk"),
        make_log_entry("normal log", level="INFO", source="app"),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # SDK log messages should have the .SDK class
    sdk_msgs = page.locator(".log-msg.SDK")
    assert sdk_msgs.count() == 1
    assert "SDK query started" in (sdk_msgs.text_content() or "")


def test_logs_render_under_lowered_log_buffer_size(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """With ``log_buffer_size`` below the client's 200 page, logs still render (#4593)."""
    url, data, _ = dashboard_server
    data["status"]["limits"] = {"max_page_size": 500, "log_buffer_size": 100}
    data["logs"] = [make_log_entry("boom", level="ERROR", source="app")]
    page.goto(url)
    page.wait_for_load_state("networkidle")
    # The bootstrap poll omits ``n``; once ``/api/status`` has reported the
    # limits, the next poll must carry the clamped ``n=100`` and succeed.
    with page.expect_response(lambda r: "/api/logs?n=100" in r.url) as resp_info:
        page.evaluate("refreshLogs()")
    assert resp_info.value.status == 200
    page.wait_for_load_state("networkidle")

    entries = page.locator("#logs .log-entry")
    assert entries.count() == 1
    assert page.locator("#nav-badge-logs").text_content() == "1"
