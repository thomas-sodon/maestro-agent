"""Tests for the plan approve/reject interactive workflows.

The confirmation surface is the in-page ``#confirm-dialog`` (proposal 122), not the
browser's native ``confirm()`` / ``prompt()``: each test opens the dialog by clicking
the action button, then drives Confirm / Cancel in-page.
"""

from __future__ import annotations

from typing import Any

from tests.browser.conftest import make_pending_plan


def test_approve_flow(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="ap1", title="Approve Me"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Open the confirmation dialog, then confirm in-page.
    page.locator('[data-action="approve"][data-plan-id="ap1"]').click()
    page.locator("#confirm-dialog-confirm").click()

    # Wait for toast to appear in the live region.
    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)
    assert "Plan approved" in (toast.text_content() or "")

    # Verify POST was sent, echoing the displayed content hash (proposal 021)
    assert len(post_log) == 1
    assert post_log[0]["action"] == "approve"
    assert post_log[0]["plan_id"] == "ap1"
    assert post_log[0]["content_hash"] == "a1b2c3d4e5f6" + "0" * 52


def test_approve_moves_plan_from_pending(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="ap2", title="Will Move"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="approve"][data-plan-id="ap2"]').click()
    page.locator("#confirm-dialog-confirm").click()

    # Wait for the refresh() call that happens after approve
    page.wait_for_timeout(1000)

    # After refresh, plan should be in all-plans (approved), not pending
    assert page.locator("#pending-plans .plan-card").count() == 0
    assert page.locator("#all-plans .plan-card.approved").count() == 1


def test_reject_flow(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="rj1", title="Reject Me"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Reject shows the reason textarea; fill it, then confirm.
    page.locator('[data-action="reject"][data-plan-id="rj1"]').click()
    page.locator("#confirm-dialog-reason").fill("Too risky")
    page.locator("#confirm-dialog-confirm").click()

    # Wait for toast
    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)
    assert "Plan rejected" in (toast.text_content() or "")

    # Verify POST was sent with reason and the displayed content hash
    assert len(post_log) == 1
    assert post_log[0]["action"] == "reject"
    assert post_log[0]["plan_id"] == "rj1"
    assert post_log[0]["reason"] == "Too risky"
    assert post_log[0]["content_hash"] == "a1b2c3d4e5f6" + "0" * 52


def test_reject_shows_rejection_reason_after_refresh(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="rj2", title="Reject And Check"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="reject"][data-plan-id="rj2"]').click()
    page.locator("#confirm-dialog-reason").fill("Not ready yet")
    page.locator("#confirm-dialog-confirm").click()

    # Wait for the refresh() call
    page.wait_for_timeout(1000)

    # After refresh, plan should show rejection reason
    card_text = page.locator("#all-plans .plan-card.rejected").text_content() or ""
    assert "Not ready yet" in card_text


def test_reject_empty_reason_defaults(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Confirming a reject with a blank textarea sends the default reason."""
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="rj3", title="No Reason"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Leave the reason textarea empty and confirm.
    page.locator('[data-action="reject"][data-plan-id="rj3"]').click()
    page.locator("#confirm-dialog-confirm").click()

    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)

    assert len(post_log) == 1
    assert post_log[0]["action"] == "reject"
    assert post_log[0]["reason"] == "No reason provided"


def test_approve_attaches_csrf_header_from_cookie(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The approve fetch echoes the maestro_csrf cookie as X-CSRF-Token (022)."""
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="csrf1", title="CSRF Wiring"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # This mock server has no auth, so no real CSRF cookie exists — plant one
    # and assert the dashboard JS picks it up.
    page.evaluate("document.cookie = 'maestro_csrf=test-csrf-token-123'")
    page.locator('[data-action="approve"][data-plan-id="csrf1"]').click()
    page.locator("#confirm-dialog-confirm").click()

    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)

    assert len(post_log) == 1
    assert post_log[0]["csrf_header"] == "test-csrf-token-123"


def test_cancel_approve_dialog(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="cancel1", title="Cancel Test"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Open the dialog, then dismiss it via Escape — the native <dialog> aborts
    # with no network request.
    page.locator('[data-action="approve"][data-plan-id="cancel1"]').click()
    page.locator("#confirm-dialog-confirm").wait_for(state="visible", timeout=5000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)

    # No POST should have been sent
    assert len(post_log) == 0

    # Plan should still be in pending
    assert page.locator("#pending-plans .plan-card.pending").count() == 1


def test_cancel_reject_via_button_sends_no_post(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Cancelling a reject with the Cancel button aborts with no POST.

    This is the behaviour change from the native ``prompt()``: a cancelled reject
    no longer submits the default 'No reason provided'.
    """
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="cancel2", title="Cancel Reject"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="reject"][data-plan-id="cancel2"]').click()
    page.locator("#confirm-dialog-reason").fill("typed then cancelled")
    page.locator("#confirm-dialog-cancel").click()
    page.wait_for_timeout(500)

    assert len(post_log) == 0
    assert page.locator("#pending-plans .plan-card.pending").count() == 1


def test_buttons_disabled_during_processing(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The action button is disabled and shows 'Processing...' while the POST is in flight.

    ``setButtonsDisabled(true)`` runs synchronously immediately before ``await fetch``,
    so intercepting the approve request captures the DOM exactly in its in-flight
    state — deterministic, not the former timing-sensitive best-effort check.
    """
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="dis1", title="Disable Test"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    captured: list[dict[str, Any]] = []

    def _capture_then_fulfill(route: Any) -> None:
        # The approve POST fires right after setButtonsDisabled(true), so the
        # trigger button is disabled / "Processing..." at this instant.
        state = page.evaluate(
            """() => {
                const b = document.querySelector(
                    '[data-action="approve"][data-plan-id="dis1"]');
                return b ? { disabled: b.disabled, text: b.textContent } : null;
            }""",
        )
        captured.append(state)
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"status": "approved", "plan_id": "dis1"}',
        )

    page.route("**/api/plans/dis1/approve", _capture_then_fulfill)

    page.locator('[data-action="approve"][data-plan-id="dis1"]').click()
    page.locator("#confirm-dialog-confirm").click()

    # The success toast only lands after the route fulfilled, proving the handler
    # ran and captured the in-flight state.
    page.locator("#toast-region .toast").wait_for(state="attached", timeout=5000)

    assert len(captured) == 1
    assert captured[0] is not None
    assert captured[0]["disabled"] is True
    assert "Processing" in captured[0]["text"]
