"""Tests for evolution plan rendering on the dashboard."""

from __future__ import annotations

from typing import Any

from tests.browser.conftest import make_pending_plan


def test_pending_plan_rendered(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan())
    page.goto(url)
    page.wait_for_load_state("networkidle")

    card = page.locator("#pending-plans .plan-card.pending")
    assert card.count() == 1
    assert "Test Evolution Plan" in (card.text_content() or "")
    assert "Impact: Improved testing" in (card.text_content() or "")


def test_pending_plan_has_buttons(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="btn-test"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    approve = page.locator('[data-action="approve"][data-plan-id="btn-test"]')
    reject = page.locator('[data-action="reject"][data-plan-id="btn-test"]')
    assert approve.is_visible()
    assert reject.is_visible()
    assert approve.text_content() == "Approve"
    assert reject.text_content() == "Reject"


def test_plan_metadata(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(
        category="infrastructure",
        priority="resilience",
    ))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    meta = page.locator("#pending-plans .plan-meta").text_content() or ""
    assert "infrastructure" in meta
    assert "resilience" in meta
    assert "Stage:" not in meta
    assert "2025-06-01" in meta


def test_refined_plan_shows_refined_date(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A refined pending plan surfaces its refined date in the meta line."""
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(
        refined_at="2025-06-15T08:30:00+00:00",
    ))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    meta = page.locator("#pending-plans .plan-meta").text_content() or ""
    assert "Refined: 2025-06-15" in meta


def test_pending_plan_shows_content_hash_chip(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The meta line surfaces the first 12 chars of the plan content hash and
    the action buttons carry it for the approve/reject echo (proposal 021)."""
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="hash-test"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    meta = page.locator("#pending-plans .plan-meta").text_content() or ""
    assert "Hash: a1b2c3d4e5f6" in meta

    approve = page.locator('[data-action="approve"][data-plan-id="hash-test"]')
    assert approve.get_attribute("data-content-hash") == "a1b2c3d4e5f6" + "0" * 52


def test_unrefined_plan_omits_refined_date(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A never-refined plan shows no refined-date chip."""
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan())
    page.goto(url)
    page.wait_for_load_state("networkidle")

    meta = page.locator("#pending-plans .plan-meta").text_content() or ""
    assert "Refined:" not in meta


def test_proposed_changes_list(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(
        proposed_changes=["Add logging", "Fix race condition"],
    ))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    card_text = page.locator("#pending-plans .plan-card").text_content() or ""
    assert "Add logging" in card_text
    assert "Fix race condition" in card_text


def test_rationale_displayed(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(rationale="We need better error handling"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    card_text = page.locator("#pending-plans .plan-card").text_content() or ""
    assert "We need better error handling" in card_text


def test_approved_plan_in_all_plans(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(
        plan_id="approved-1",
        title="Approved Plan",
        status="approved",
    ))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    card = page.locator("#all-plans .plan-card.approved")
    assert card.count() == 1
    assert "Approved Plan" in (card.text_content() or "")
    # No buttons on non-pending plans
    assert page.locator('#all-plans [data-action="approve"]').count() == 0


def test_rejected_plan_shows_reason(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(
        plan_id="rejected-1",
        title="Rejected Plan",
        status="rejected",
        rejection_reason="Too risky for now",
    ))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    card = page.locator("#all-plans .plan-card.rejected")
    assert card.count() == 1
    card_text = card.text_content() or ""
    assert "Too risky for now" in card_text


def test_obsolete_plan_shows_reason(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Self-pruned plans render with the obsolete border + ``obsolete_reason``."""
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(
        plan_id="obsolete-1",
        title="Obsolete Plan",
        status="obsolete",
        obsolete_reason="superseded by later thinking",
    ))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    card = page.locator("#all-plans .plan-card.obsolete")
    assert card.count() == 1
    card_text = card.text_content() or ""
    assert "Obsolete because: superseded by later thinking" in card_text
    # Rejection-reason channel must stay untouched on obsolete plans.
    assert "Reason:" not in card_text


def test_obsolete_filter_chip_narrows_all_plans(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The Obsolete filter chip narrows the All-plans list to obsolete rows."""
    url, data, _ = dashboard_server
    data["plans"].extend([
        make_pending_plan(
            plan_id="merged-1",
            title="Already Merged",
            status="merged",
        ),
        make_pending_plan(
            plan_id="obsolete-filter-1",
            title="First Obsolete",
            status="obsolete",
            obsolete_reason="superseded",
        ),
        make_pending_plan(
            plan_id="obsolete-filter-2",
            title="Second Obsolete",
            status="obsolete",
            obsolete_reason="stale",
        ),
    ])
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Without filtering, all three non-pending rows render.
    assert page.locator("#all-plans .plan-card").count() == 3

    page.locator('[data-plan-status="obsolete"]').click()
    obsolete_cards = page.locator("#all-plans .plan-card.obsolete")
    assert obsolete_cards.count() == 2
    # No non-obsolete card leaks through the filter.
    assert page.locator("#all-plans .plan-card.merged").count() == 0


def test_implemented_plan_shows_pr_link(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(
        plan_id="impl-1",
        title="Implemented Plan",
        status="implemented",
        pr_url="https://github.com/test/repo/pull/42",
    ))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    card = page.locator("#all-plans .plan-card.implemented")
    assert card.count() == 1
    link = card.locator("a", has_text="PR")
    assert link.is_visible()
    assert link.get_attribute("href") == "https://github.com/test/repo/pull/42"


def test_multiple_plan_statuses(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].extend([
        make_pending_plan(plan_id="p1", title="Pending One"),
        make_pending_plan(plan_id="p2", title="Approved One", status="approved"),
        make_pending_plan(plan_id="p3", title="Rejected One", status="rejected"),
        make_pending_plan(plan_id="p4", title="Done One", status="implemented"),
    ])
    page.goto(url)
    page.wait_for_load_state("networkidle")

    assert page.locator("#pending-plans .plan-card.pending").count() == 1
    assert page.locator("#all-plans .plan-card").count() == 3


def test_empty_pending_plans(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    assert page.locator("#pending-plans .empty", has_text="No pending plans.").is_visible()


def test_empty_all_plans(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    assert page.locator("#all-plans .empty", has_text="No completed plans yet.").is_visible()


def test_request_changes_flow(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="fb1"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="feedback"][data-plan-id="fb1"]').click()
    page.locator("#confirm-dialog-reason").fill("Narrow the scope")
    page.locator("#confirm-dialog-confirm").click()

    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)
    assert "Feedback queued" in (toast.text_content() or "")
    assert len(post_log) == 1
    assert post_log[0]["action"] == "feedback"
    assert post_log[0]["text"] == "Narrow the scope"
    assert post_log[0]["content_hash"] == "a1b2c3d4e5f6" + "0" * 52


def test_request_changes_empty_text_cancels(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="fb2"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="feedback"][data-plan-id="fb2"]').click()
    page.locator("#confirm-dialog-confirm").click()
    page.wait_for_timeout(200)
    assert post_log == []


def test_pending_feedback_badge_and_response(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="fb3", reviewer_feedback=[
        {"text": "a", "addressed_at": "2026-10-01T00:00:00Z", "response": "Scoped down"},
        {"text": "b", "addressed_at": "2026-10-02T00:00:00Z", "response": "<b>Newest</b>"},
        {"text": "c", "addressed_at": None, "response": ""},
    ]))
    data["plans"].append(make_pending_plan(plan_id="fb4"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    cards = page.locator("#pending-plans .plan-card.pending")
    first = cards.nth(0)
    assert first.locator(".plan-feedback-pending").text_content() == "Feedback pending"
    assert first.locator(".plan-feedback-response").text_content() == "Maestro: <b>Newest</b>"
    assert cards.nth(1).locator(".plan-feedback-pending").count() == 0
    assert cards.nth(1).locator(".plan-feedback-response").count() == 0
