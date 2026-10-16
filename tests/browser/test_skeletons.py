"""Proof that the account dashboard's cold state renders accessible skeletons.

The seventeen data panels used to paint the bare string ``Loading...`` on first
paint — no accessibility semantics and a layout jump when content snapped in.
Each ``Loading...`` node is now a ``<div class="skeleton" aria-busy="true">``
block of shimmer bars that reserves layout height. This test proves three
things in a real browser:

* the cold state (data fetches stalled) shows visible ``aria-busy`` skeletons and
  never the literal ``Loading...``;
* a normal render removes every skeleton and an empty list still shows its
  labelled ``.empty`` block (the purposeful empty state is untouched);
* under ``prefers-reduced-motion: reduce`` the shimmer animation is suppressed to
  a flat placeholder, while it is live otherwise.

The cold state is held by routing ``**/api/**`` to a no-op handler: it borrows
the ``page.route("**/api/**", ...)`` interception shape from
``test_error_handling.py``, but where that file resolves each route immediately
(``route.fulfill`` / ``route.abort``), this one deliberately leaves the request
unresolved so the data fetches hang and the loading state stays observable. The
reduced-motion assertion uses the ``getComputedStyle(...)`` idiom shared with
``test_typography.py`` / ``test_token_parity.py``.
"""

from __future__ import annotations

from typing import Any


def _stall_api(page: Any) -> None:
    """Route every ``/api/**`` request to a handler that never resolves.

    The data fetches hang, so the JS render never replaces the skeleton markup
    and the cold state stays observable for the assertions.
    """
    page.route("**/api/**", lambda route: None)


def _computed_animation_name(page: Any, selector: str) -> str:
    """Return the computed ``animation-name`` of the first ``selector`` node."""
    name: str = page.evaluate(
        "sel => getComputedStyle(document.querySelector(sel)).animationName",
        selector,
    )
    return name


def test_cold_state_shows_accessible_skeletons(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A cold panel shows visible ``aria-busy`` skeletons, never ``Loading...``."""
    url, *_ = dashboard_server
    _stall_api(page)

    page.goto(f"{url}/account", wait_until="domcontentloaded")
    # Let refresh()/refreshLogs() fire against the stalled API — the skeletons
    # must persist rather than being wiped by an in-flight (never-resolving) fetch.
    page.wait_for_timeout(300)

    skeletons = page.locator('[aria-busy="true"]')
    assert skeletons.count() == 17, (
        "every one of the seventeen cold panels must render an aria-busy "
        f"skeleton block, got {skeletons.count()}"
    )
    assert skeletons.first.is_visible(), "the cold skeleton must be visible"
    assert page.locator(".skeleton-line").first.is_visible(), (
        "the skeleton shimmer bars must be visible in the cold state"
    )
    assert "Loading..." not in page.content(), (
        "the literal Loading... placeholder must be gone from the cold state"
    )


def test_render_removes_skeletons_and_keeps_empty_states(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A normal render clears every skeleton and empty lists show labelled blocks."""
    page, *_ = dashboard_page

    assert page.locator('[aria-busy="true"]').count() == 0, (
        "once real content (or an empty state) renders, no aria-busy skeleton "
        "may remain"
    )
    assert page.locator(".skeleton").count() == 0, (
        "the skeleton markup must be replaced on first render"
    )
    # The purposeful empty-state blocks the render functions already emit are
    # untouched by this change: an empty list still shows its labelled .empty.
    assert page.locator("#pending-plans .empty", has_text="No pending plans.").is_visible()
    assert page.locator("#journal .empty", has_text="Journal is empty").is_visible()


def test_reduced_motion_suppresses_skeleton_shimmer(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The shimmer animation runs normally but stills under reduced-motion."""
    url, *_ = dashboard_server
    _stall_api(page)

    # Motion allowed: the shimmer keyframes are live on the skeleton bars.
    page.emulate_media(reduced_motion="no-preference")
    page.goto(f"{url}/account", wait_until="domcontentloaded")
    page.wait_for_selector('[aria-busy="true"] .skeleton-line')
    assert _computed_animation_name(page, ".skeleton-line") == "skeleton-shimmer", (
        "with motion allowed the skeleton bar must run the skeleton-shimmer "
        "animation"
    )

    # Reduced motion: the scoped @media guard stills the shimmer to a flat bar.
    page.emulate_media(reduced_motion="reduce")
    assert _computed_animation_name(page, ".skeleton-line") == "none", (
        "under prefers-reduced-motion: reduce the shimmer animation must be "
        "suppressed to a flat placeholder"
    )
