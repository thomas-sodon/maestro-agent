"""Tests for XSS protection — verify that user-controlled data is properly escaped."""

from __future__ import annotations

from typing import Any

from tests.browser.conftest import make_journal_entry, make_pending_plan


def test_xss_in_identity_statements(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    payload = "<script>alert('xss')</script>"
    data["consciousness"]["identity_statements"] = [payload]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # The script tag should be escaped, not executed
    script_count = page.evaluate("document.querySelectorAll('script').length")
    assert script_count == 1  # Only the legitimate app script

    # The escaped text should be visible
    identity_html = page.locator("#identity").inner_html()
    assert "<script>" not in identity_html
    assert "&lt;script&gt;" in identity_html


def test_xss_in_journal_content(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    payload = "<img onerror='alert(1)' src='x'>"
    data["journal"].append(make_journal_entry(payload))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    journal_html = page.locator("#journal").inner_html()
    assert "<img" not in journal_html
    assert "&lt;img" in journal_html


def test_xss_in_plan_title(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    payload = '"><script>alert(document.cookie)</script>'
    data["plans"].append(make_pending_plan(title=payload))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    plan_html = page.locator("#pending-plans").inner_html()
    assert "<script>" not in plan_html
    assert "&lt;script&gt;" in plan_html


def test_xss_in_plan_rationale(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    payload = "<iframe src='javascript:alert(1)'></iframe>"
    data["plans"].append(make_pending_plan(rationale=payload))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    plan_html = page.locator("#pending-plans").inner_html()
    assert "<iframe" not in plan_html
    assert "&lt;iframe" in plan_html


def test_safe_url_rejects_javascript_protocol(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(
        plan_id="xss-url",
        title="XSS URL Test",
        status="implemented",
        pr_url="javascript:alert(1)",
    ))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # The PR link should have href="#" not the javascript: URL
    link = page.locator("#all-plans a")
    if link.count() > 0:
        href = link.get_attribute("href") or ""
        assert href == "#"


def test_safe_url_allows_https(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(
        plan_id="safe-url",
        title="Safe URL Test",
        status="implemented",
        pr_url="https://github.com/owner/repo/pull/1",
    ))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    link = page.locator("#all-plans a", has_text="PR")
    assert link.get_attribute("href") == "https://github.com/owner/repo/pull/1"


def test_no_script_injection_across_all_sections(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Inject XSS payloads in all data sections and verify none execute."""
    url, data, _ = dashboard_server
    xss = "<script>window.__xss_fired=true</script>"

    data["consciousness"]["identity_statements"] = [xss]
    data["consciousness"]["values"] = [xss]
    data["consciousness"]["open_questions"] = [xss]
    data["journal"].append(make_journal_entry(xss))
    data["plans"].append(make_pending_plan(title=xss, rationale=xss))
    data["status"]["current_task"] = xss

    page.goto(url)
    page.wait_for_load_state("networkidle")

    # The XSS should NOT have executed
    xss_fired = page.evaluate("window.__xss_fired")
    assert xss_fired is None or xss_fired is False

    # Only the legitimate script tag should exist
    total_scripts = page.evaluate("document.querySelectorAll('script').length")
    assert total_scripts == 1


