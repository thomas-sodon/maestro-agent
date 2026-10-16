"""Accessibility behaviour of the reusable confirmation dialog (proposal 122).

The governance actions confirm through an in-page native ``<dialog>`` rather than
the browser's ``confirm()`` / ``prompt()``. These tests pin the accessibility
contract the native element buys us: an implicit ``role="dialog"`` with a wired
``aria-labelledby``, Escape-to-cancel that sends no request and restores focus to
the trigger, and outcome feedback announced through a ``role="status"`` live region.
"""

from __future__ import annotations

from typing import Any

from tests.browser.conftest import make_pending_plan


def test_dialog_exposes_dialog_role_and_labelledby(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="a11y1", title="Label Me"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="approve"][data-plan-id="a11y1"]').click()
    page.locator("#confirm-dialog-confirm").wait_for(state="visible", timeout=5000)

    # Native <dialog> supplies the implicit role="dialog".
    assert page.get_by_role("dialog").count() >= 1

    # aria-labelledby points at the title element, which carries populated text.
    dialog = page.locator("#confirm-dialog")
    assert dialog.get_attribute("aria-labelledby") == "confirm-dialog-title"
    title_text = page.locator("#confirm-dialog-title").text_content() or ""
    assert title_text.strip() != ""


def test_escape_cancels_with_no_post_and_restores_focus(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="a11y2", title="Escape Me"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="approve"][data-plan-id="a11y2"]').click()
    page.locator("#confirm-dialog-confirm").wait_for(state="visible", timeout=5000)
    assert page.locator("#confirm-dialog[open]").count() == 1

    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # Dialog closed, no request sent.
    assert page.locator("#confirm-dialog[open]").count() == 0
    assert len(post_log) == 0

    # Native <dialog> restores focus to the trigger button.
    active = page.evaluate(
        "() => ({"
        "  action: document.activeElement && document.activeElement.getAttribute('data-action'),"
        "  planId: document.activeElement && document.activeElement.getAttribute('data-plan-id'),"
        "})"
    )
    assert active["action"] == "approve"
    assert active["planId"] == "a11y2"


def test_confirmed_action_announces_through_status_region(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="a11y3", title="Announce Me"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # The live region pre-exists in the DOM with the announcing semantics.
    region = page.locator("#toast-region")
    assert region.get_attribute("role") == "status"
    assert region.get_attribute("aria-live") == "polite"

    page.locator('[data-action="approve"][data-plan-id="a11y3"]').click()
    page.locator("#confirm-dialog-confirm").click()

    # The success toast lands inside the role="status" region so it is announced.
    toast = page.locator("#toast-region[role='status'] .toast")
    toast.wait_for(state="attached", timeout=5000)
    assert "Plan approved" in (toast.text_content() or "")


def test_reject_dialog_shows_reason_textarea(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A reject opens the dialog with the reason textarea visible; an approve hides it."""
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="a11y4", title="Reason Toggle"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Approve: reason textarea stays hidden.
    page.locator('[data-action="approve"][data-plan-id="a11y4"]').click()
    page.locator("#confirm-dialog-confirm").wait_for(state="visible", timeout=5000)
    assert page.locator("#confirm-dialog-reason").is_hidden()
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)

    # Reject: reason textarea is shown.
    page.locator('[data-action="reject"][data-plan-id="a11y4"]').click()
    reason = page.locator("#confirm-dialog-reason")
    reason.wait_for(state="visible", timeout=5000)
    assert reason.is_visible()


def test_backdrop_click_cancels_with_no_post(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Clicking the ::backdrop (outside the dialog box) cancels and sends no POST."""
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="bd1", title="Backdrop"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="approve"][data-plan-id="bd1"]').click()
    page.locator("#confirm-dialog-confirm").wait_for(state="visible", timeout=5000)
    assert page.locator("#confirm-dialog[open]").count() == 1

    # The modal backdrop covers the viewport; the centred dialog leaves the
    # top-left corner on the backdrop, so a click there dismisses it.
    page.mouse.click(5, 5)
    page.wait_for_timeout(300)

    assert page.locator("#confirm-dialog[open]").count() == 0
    assert len(post_log) == 0


def test_click_on_dialog_padding_keeps_dialog_open(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A click on the dialog's own padding must NOT cancel it.

    Regression guard: the backdrop-cancel handler fires for ``e.target === dialog``,
    which also matches clicks on the dialog's padding. The handler narrows this by
    the pointer's position relative to the bounding rect, so a padding click stays
    inside the box and leaves the dialog open.
    """
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="pad1", title="Padding"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="approve"][data-plan-id="pad1"]').click()
    dialog = page.locator("#confirm-dialog")
    dialog.wait_for(state="visible", timeout=5000)
    box = dialog.bounding_box()
    assert box is not None

    # Click the top-of-box padding gap (target === dialog, but inside the rect).
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + 4)
    page.wait_for_timeout(300)

    assert page.locator("#confirm-dialog[open]").count() == 1
    assert len(post_log) == 0


def test_drag_from_inside_ending_on_backdrop_keeps_dialog_open(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A press that begins inside the dialog and releases on the backdrop must NOT cancel.

    Regression guard for the drag-out pitfall: a ``click`` fires on the nearest
    common ancestor of the mousedown and mouseup targets, so drag-selecting text in
    the reason textarea and releasing outside the box dispatches a ``click`` whose
    ``target`` is the dialog and whose release coordinates land on the backdrop.
    Coordinate-only detection would then dismiss the dialog and silently discard the
    reason already typed. The handler additionally requires the press to have
    *started* on the dialog element itself (``pressedOnDialog``), so a press that
    began on a child leaves the dialog — and the typed reason — intact.
    """
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="drag1", title="Drag Out"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Reject flow so the reason textarea is present and pre-filled.
    page.locator('[data-action="reject"][data-plan-id="drag1"]').click()
    reason = page.locator("#confirm-dialog-reason")
    reason.wait_for(state="visible", timeout=5000)
    reason.fill("carefully typed reason")

    box = reason.bounding_box()
    assert box is not None
    # Press inside the reason textarea, drag onto the top-left backdrop, release.
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(5, 5)
    page.mouse.up()
    page.wait_for_timeout(300)

    # Dialog stays open, the typed reason survives, and no request was sent.
    assert page.locator("#confirm-dialog[open]").count() == 1
    assert reason.input_value() == "carefully typed reason"
    assert len(post_log) == 0


def test_reentrant_open_does_not_raise_unhandled_rejection(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A re-entrant openConfirmDialog() (fast double-click) must not crash.

    Regression guard: ``setButtonsDisabled()`` disables the trigger only *after* the
    dialog resolves, so two rapid clicks on an action button can call
    ``openConfirmDialog()`` twice before the first modal opens. The second
    ``dialog.showModal()`` runs against an already-open ``<dialog>`` and throws
    ``InvalidStateError``; without a guard that rejects the awaited promise as an
    unhandled rejection and silently drops the second governance action. The helper
    catches the throw and resolves the duplicate call as a plain cancel, so the first
    dialog stays live and confirming it fires exactly one approve POST.
    """
    url, data, post_log = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="reentrant1", title="Double Click"))

    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Record unhandled promise rejections in-page (Playwright's pageerror does not
    # reliably surface them) and force the *second* showModal() to throw
    # InvalidStateError, reproducing the re-entrant-open crash deterministically
    # regardless of the browser's own already-open showModal() semantics.
    page.evaluate(
        """() => {
            window.__unhandled = [];
            window.addEventListener('unhandledrejection', (e) => {
                window.__unhandled.push(String(e.reason));
            });
            const dialog = document.getElementById('confirm-dialog');
            const orig = dialog.showModal.bind(dialog);
            let calls = 0;
            dialog.showModal = function () {
                calls += 1;
                if (calls >= 2) {
                    throw new DOMException('already open', 'InvalidStateError');
                }
                return orig();
            };
        }"""
    )

    # Two synchronous openConfirmDialog() calls, as a fast double-click dispatches
    # before the first modal has disabled its trigger button.
    page.evaluate(
        "() => { approvePlan('reentrant1', ''); approvePlan('reentrant1', ''); }"
    )
    page.wait_for_timeout(200)

    # The guard swallowed the InvalidStateError: no unhandled rejection, no page error.
    assert page.evaluate("() => window.__unhandled") == []
    assert page_errors == []

    # The first dialog survived; confirming it fires exactly one approve POST — the
    # duplicate call was resolved as a cancel, not dropped mid-flow or double-sent.
    assert page.locator("#confirm-dialog[open]").count() == 1
    page.locator("#confirm-dialog-confirm").click()
    page.wait_for_timeout(200)
    assert len(post_log) == 1
    assert post_log[0]["action"] == "approve"
    assert post_log[0]["plan_id"] == "reentrant1"


def test_dialog_is_centred_not_pinned_top_left(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The modal dialog centres in the viewport rather than pinning to the corner.

    Regression guard: index.css's global ``* { margin: 0 }`` reset overrides the
    UA ``margin: auto`` that centres a modal ``<dialog>``, which would pin it to
    the top-left corner (rect left/top ~0). The explicit ``margin: auto`` on
    ``dialog#confirm-dialog`` restores the centring.
    """
    url, data, _ = dashboard_server
    data["plans"].append(make_pending_plan(plan_id="ctr1", title="Centred"))
    page.goto(url)
    page.wait_for_load_state("networkidle")

    page.locator('[data-action="approve"][data-plan-id="ctr1"]').click()
    dialog = page.locator("#confirm-dialog")
    dialog.wait_for(state="visible", timeout=5000)

    m = page.evaluate(
        """() => {
            const d = document.getElementById('confirm-dialog');
            const r = d.getBoundingClientRect();
            return { left: r.left, right: r.right, top: r.top,
                     vw: document.documentElement.clientWidth, vh: document.documentElement.clientHeight };
        }"""
    )
    # A top-left-pinned dialog has left/top ~0; a centred one clears the edges.
    assert m["left"] > 20
    assert m["top"] > 20
    # Left and right gutters are balanced → horizontally centred.
    right_gap = m["vw"] - m["right"]
    assert abs(m["left"] - right_gap) < 2
