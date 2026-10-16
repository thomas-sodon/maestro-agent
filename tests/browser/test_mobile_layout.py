"""Mobile dashboard layout invariants: chrome shrinks, nav stays reachable."""

from __future__ import annotations

from typing import Any


def _evaluate(page: Any, script: str) -> Any:
    """Return a fresh page.evaluate() result. Helper to keep tests readable."""
    return page.evaluate(script)


def test_mobile_layout_dissolves_banner_and_pins_nav(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """At a phone viewport (375x812), the sticky banner is dissolved (display:
    contents) so the nav-bar's sticky containing block becomes <body>; the
    subtitle is hidden, the nav strip is a single-row horizontal scroller, the
    metrics row wraps instead of overflowing, and the nav stays pinned at the
    top of the viewport after the user scrolls past the banner.
    """
    page, _, data, _ = dashboard_page

    # Populate metrics with wide values so an unwrapped flex row would overflow
    # at the 375px viewport — proves .metrics-row { flex-wrap: wrap } actually
    # fires rather than passing trivially against the empty-default fixture.
    data["metrics"]["success_rate"] = 0.85
    data["metrics"]["total_tasks"] = 12345
    data["metrics"]["evolution_count"] = 678
    data["metrics"]["pending_plans"] = 99
    data["cost_rates"]["total_calls"] = 1
    data["cost_rates"]["cost_per_hour"] = 12.34

    page.set_viewport_size({"width": 375, "height": 812})
    page.reload()
    page.wait_for_load_state("networkidle")

    sticky_display = _evaluate(
        page,
        "getComputedStyle(document.querySelector('.sticky-header')).display",
    )
    assert sticky_display == "contents", (
        "sticky-header must be dissolved (display: contents) so the nav-bar's "
        "sticky-positioning containing block becomes <body> and the nav can "
        f"stay stuck across the full page; got display={sticky_display}"
    )

    nav_position = _evaluate(
        page,
        "getComputedStyle(document.querySelector('.nav-bar')).position",
    )
    assert nav_position == "sticky"

    subtitle_display = _evaluate(
        page,
        "getComputedStyle(document.querySelector('.subtitle')).display",
    )
    assert subtitle_display == "none"

    nav_metrics = _evaluate(
        page,
        """
        (() => {
          const el = document.querySelector('.nav-bar');
          const rect = el.getBoundingClientRect();
          return {
            scrollWidth: el.scrollWidth,
            clientWidth: el.clientWidth,
            height: rect.height,
          };
        })()
        """,
    )
    assert nav_metrics["scrollWidth"] > nav_metrics["clientWidth"], (
        "nav-bar should overflow horizontally on a phone viewport (single-row "
        f"scroll); got scrollWidth={nav_metrics['scrollWidth']} "
        f"clientWidth={nav_metrics['clientWidth']}"
    )
    assert nav_metrics["height"] < 60, (
        "nav-bar should be a single row on phone viewport; got "
        f"height={nav_metrics['height']}"
    )

    metrics_overflow = _evaluate(
        page,
        """
        (() => {
          const el = document.querySelector('#metrics');
          return {scrollWidth: el.scrollWidth, clientWidth: el.clientWidth};
        })()
        """,
    )
    assert (
        metrics_overflow["scrollWidth"] <= metrics_overflow["clientWidth"] + 1
    ), (
        "metrics row should not overflow horizontally on a phone viewport "
        f"(needs flex-wrap); got scrollWidth={metrics_overflow['scrollWidth']} "
        f"clientWidth={metrics_overflow['clientWidth']}"
    )

    # The behavioural proof of the fix: scroll well past the natural position
    # of the banner+nav and confirm the nav remains glued to the top. Without
    # display: contents on .sticky-header, sticky positioning would be capped
    # to the parent's bounding box and the nav would slide out with the cards.
    page.evaluate("window.scrollTo(0, 800)")
    nav_top_after_scroll = _evaluate(
        page,
        "document.querySelector('.nav-bar').getBoundingClientRect().top",
    )
    assert abs(nav_top_after_scroll) < 1, (
        "nav-bar should remain pinned at viewport top after scrolling past "
        f"the banner; got top={nav_top_after_scroll}"
    )


def test_desktop_layout_unchanged_at_default_viewport(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """At the default desktop viewport (1280x720), the banner stays sticky and
    the subtitle remains visible — the mobile @media block must not bleed up.
    """
    page, *_ = dashboard_page

    sticky_position = _evaluate(
        page,
        "getComputedStyle(document.querySelector('.sticky-header')).position",
    )
    assert sticky_position == "sticky"

    sticky_display = _evaluate(
        page,
        "getComputedStyle(document.querySelector('.sticky-header')).display",
    )
    assert sticky_display != "contents", (
        "sticky-header must keep its box on desktop so its position: sticky "
        f"still applies; got display={sticky_display}"
    )

    subtitle_display = _evaluate(
        page,
        "getComputedStyle(document.querySelector('.subtitle')).display",
    )
    assert subtitle_display != "none"
