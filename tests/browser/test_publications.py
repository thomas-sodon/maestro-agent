"""Browser tests for the Publications tab (proposal 032).

Covers the pending card render, Approve / Reject flows, the all-publications
filter, the narrative-redaction warning chip, and the status-class fallback
for unknown proposal statuses.
"""

from __future__ import annotations

from typing import Any

from tests.browser.conftest import make_pending_publication


def test_pending_publication_card_renders_with_readme_preview(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["publications"].append(make_pending_publication(
        proposal_id="p-render",
        proposed_name="demo-tool",
        description="A small demo tool",
        proposed_readme="# demo-tool\n\nBody paragraph one.",
    ))
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")

    card = page.locator(
        '#pending-publications .plan-card[data-action-scope], '
        '#pending-publications .plan-card.pending',
    ).first
    card.wait_for(state="attached", timeout=5000)

    # Name + description are present.
    body = page.locator("#pending-publications").text_content() or ""
    assert "demo-tool" in body
    assert "A small demo tool" in body

    # Approve / Reject buttons are wired with the proposal id.
    assert page.locator(
        '[data-action="pub-approve"][data-proposal-id="p-render"]',
    ).count() == 1
    assert page.locator(
        '[data-action="pub-reject"][data-proposal-id="p-render"]',
    ).count() == 1

    # README preview lives inside a <details><pre> so arbitrary markdown
    # cannot leak HTML.
    details = page.locator("#pending-publications details")
    assert details.count() >= 1
    pre_text = page.locator(
        "#pending-publications details pre",
    ).first.text_content() or ""
    assert "# demo-tool" in pre_text


def test_approve_publication_flow(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, post_log = dashboard_server
    data["publications"].append(make_pending_publication(
        proposal_id="p-approve", proposed_name="going-public",
    ))
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")

    page.locator(
        '[data-action="pub-approve"][data-proposal-id="p-approve"]',
    ).click()
    page.locator("#confirm-dialog-confirm").click()

    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)
    assert "approved" in (toast.text_content() or "").lower()

    # Exactly one POST recorded against the publication endpoint.
    pub_calls = [
        entry for entry in post_log if entry.get("action") == "pub_approve"
    ]
    assert pub_calls == [{"action": "pub_approve", "proposal_id": "p-approve"}]


def test_reject_publication_with_reason(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, post_log = dashboard_server
    data["publications"].append(make_pending_publication(
        proposal_id="p-reject", proposed_name="needs-work",
    ))
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")

    page.locator(
        '[data-action="pub-reject"][data-proposal-id="p-reject"]',
    ).click()
    page.locator("#confirm-dialog-reason").fill("not yet polished")
    page.locator("#confirm-dialog-confirm").click()

    toast = page.locator("#toast-region .toast")
    toast.wait_for(state="attached", timeout=5000)
    assert "rejected" in (toast.text_content() or "").lower()

    pub_calls = [
        entry for entry in post_log if entry.get("action") == "pub_reject"
    ]
    assert len(pub_calls) == 1
    assert pub_calls[0]["proposal_id"] == "p-reject"
    assert pub_calls[0]["reason"] == "not yet polished"


def test_all_publications_status_filter(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["publications"].extend([
        make_pending_publication(
            proposal_id="p-live",
            proposed_name="live-one",
            status="pending_approval",
        ),
        make_pending_publication(
            proposal_id="p-done",
            proposed_name="done-one",
            status="approved",
        ),
        make_pending_publication(
            proposal_id="p-nope",
            proposed_name="nope-one",
            status="rejected",
            rejection_reason="not ready",
        ),
    ])
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")

    # Click "Rejected" filter chip.
    page.locator(
        '[data-publication-status="rejected"]',
    ).click()
    page.wait_for_timeout(200)

    all_body = page.locator("#all-publications").text_content() or ""
    assert "nope-one" in all_body
    assert "done-one" not in all_body
    assert "live-one" not in all_body


def test_narrative_redaction_warning_chip(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Scrub findings tagged with ``narrative_redactions:*`` render with a warning chip."""
    url, data, _ = dashboard_server
    data["publications"].append(make_pending_publication(
        proposal_id="p-chip",
        proposed_name="scrubbed",
        scrub_findings=[
            "redacted-path: 2",
            "narrative_redactions:name_maestro: 4",
        ],
    ))
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")

    # At least one warning-class chip must exist.
    assert page.locator("#pending-publications .chip.chip-warn").count() >= 1
    # And the plain chip for redacted-path is also present.
    assert page.locator("#pending-publications .chip").count() >= 2


def test_all_publications_unknown_status_falls_back_to_unknown_class(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A bogus status value exercises the fallback in ``publicationStatusClass``."""
    url, data, _ = dashboard_server
    data["publications"].append(make_pending_publication(
        proposal_id="p-weird",
        proposed_name="mystery",
        status="totally-made-up-status",
    ))
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")

    # The renderer must apply the fallback class so the card still
    # shows up rather than silently vanishing.
    card = page.locator("#all-publications .plan-card.unknown")
    assert card.count() == 1
    assert "mystery" in (card.text_content() or "")


def test_publications_nav_badge_shows_pending_count(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["publications"].extend([
        make_pending_publication(proposal_id=f"p-badge-{i}")
        for i in range(3)
    ])
    page.goto(f"{url}/account")
    page.wait_for_load_state("networkidle")

    badge = page.locator("#nav-badge-publications")
    badge.wait_for(state="visible", timeout=5000)
    assert (badge.text_content() or "").strip() == "3"



