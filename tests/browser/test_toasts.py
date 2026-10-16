"""Tests for toast notification behavior.

Toasts render into the persistent ``#toast-region`` live region (proposal 122),
so they are located via ``#toast-region .toast`` rather than the former inline
``position: fixed`` style.
"""

from __future__ import annotations

from typing import Any

from tests.browser.conftest import make_pending_plan


def test_approve_shows_success_toast(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="toast1"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="approve"][data-plan-id="toast1"]').click()
    page.locator("#confirm-dialog-confirm").click()

    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)
    assert "Plan approved" in (toast.text_content() or "")


def test_reject_shows_info_toast(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="toast2"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="reject"][data-plan-id="toast2"]').click()
    page.locator("#confirm-dialog-reason").fill("reason")
    page.locator("#confirm-dialog-confirm").click()

    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)
    assert "Plan rejected" in (toast.text_content() or "")


def test_toast_disappears(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="toast3"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="approve"][data-plan-id="toast3"]').click()
    page.locator("#confirm-dialog-confirm").click()

    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)

    # Toast should disappear after ~3.5s (3s delay + 0.5s fade)
    toast.wait_for(state="detached", timeout=5000)


def test_error_toast_on_api_failure(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    # Create a plan that is already approved — approve will return 400
    data["plans"].append(make_pending_plan(plan_id="toast4", status="approved"))
    # But render it as pending in the UI by having another pending plan
    data["plans"].append(make_pending_plan(plan_id="toast5", title="Pending One"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Try to approve the pending plan — but use route interception to force 500
    page.route("**/api/plans/toast5/approve", lambda route: route.fulfill(
        status=500,
        content_type="application/json",
        body='{"error": "Internal Server Error"}',
    ))

    page.locator('[data-action="approve"][data-plan-id="toast5"]').click()
    page.locator("#confirm-dialog-confirm").click()

    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)
    toast_text = toast.text_content() or ""
    assert "failed" in toast_text.lower() or "error" in toast_text.lower()
