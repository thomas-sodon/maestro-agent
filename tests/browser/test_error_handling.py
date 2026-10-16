"""Tests for error handling and resilience when APIs fail."""

from __future__ import annotations

from typing import Any

from tests.browser.conftest import make_log_entry


def test_page_survives_status_api_failure(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["logs"].append(make_log_entry("visible log entry"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Intercept status API to return 500
    page.route("**/api/status", lambda route: route.fulfill(
        status=500,
        content_type="application/json",
        body='{"error": "Internal Server Error"}',
    ))

    # Trigger a refresh — status will fail but other sections should still work
    page.evaluate("refresh()")
    page.wait_for_timeout(500)

    # Logs should still be visible (they use a separate refresh function)
    assert page.locator("#logs .log-entry").count() >= 1


def test_page_survives_all_api_failures(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, *_ = dashboard_server
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Block all API calls
    page.route("**/api/**", lambda route: route.fulfill(
        status=500,
        content_type="application/json",
        body='{"error": "Everything is broken"}',
    ))

    # Trigger refresh — should not crash
    page.evaluate("refresh()")
    page.evaluate("refreshLogs()")
    page.wait_for_timeout(500)

    # Page should still be there (h1 was rendered at load time)
    assert page.locator("h1").text_content() == "Maestro"


def test_console_errors_captured(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, *_ = dashboard_server

    console_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    # Block status API
    page.route("**/api/status", lambda route: route.fulfill(
        status=500,
        content_type="application/json",
        body='{"error": "fail"}',
    ))

    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)

    # The refresh() function catches errors and logs them with console.error
    error_texts = " ".join(console_errors)
    assert "Refresh error" in error_texts or "HTTP 500" in error_texts


def test_network_error_does_not_crash(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, *_ = dashboard_server
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Abort all API requests to simulate network failure
    page.route("**/api/**", lambda route: route.abort())

    # Trigger refresh — should not throw unhandled error
    page.evaluate("refresh()")
    page.evaluate("refreshLogs()")
    page.wait_for_timeout(500)

    # Page structure should still be intact
    assert page.locator("h1").is_visible()
    assert page.locator(".refresh-note").is_visible()
