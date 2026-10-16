"""Browser tests for the proposal 122 v2 account navigation shell.

The shell is gated on a server-injected ``data-account-ui-v2`` body marker. These
tests serve the real ``index.html`` two ways — marker-injected (flag-on) at
``/account`` and verbatim (flag-off) at ``/flat`` — exactly as the production
``dashboard()`` handler does, and assert the ARIA structure, icons, skip-link,
and keyboard traversal appear only under the marker.

Since the 2026-09-03 graduation the flag ships **on**, so the marker-injected
page is the shipped composition and the verbatim one is the rollback. The rest
of the browser suite still runs against the unmarked template (``conftest.py``'s
mock app serves it raw, and the auth fixture pins the flag off), which leaves
``test_polish.py`` — the step-8 tripwires — validating the flat nav only. The two
``..._under_v2_shell`` tests below re-assert those same two invariants under the
marker, where ``enhanceNavV2()`` has moved ``#nav-bar`` out of ``.sticky-header``
and into the ``.account-shell`` rail: that move changes which element pins and
which element's containing block bounds the sticky nav, so the header-clearance
and gutter lock-step guarantees have to be proven again on this side of the
branch rather than inherited from the flat-nav proofs.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "maestro" / "dashboard"
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"

# The eleven panels in the arrow-traversal order the grouped rail renders:
# Monitor, Govern, Introspect, System (see NAV_GROUPS in index.js).
_NAV_ORDER = [
    "diagnostics", "budget", "sessions", "logs",
    "plans", "publications", "messages",
    "identity", "journal", "tasks",
    "versions",
]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _create_nav_app() -> FastAPI:
    """Serve the real dashboard HTML marker-injected at /account, verbatim at /flat.

    Mirrors the production ``dashboard()`` injection so the flag-on / flag-off
    branches are exercised end-to-end through the same static CSS/JS.
    """
    app = FastAPI()
    template = TEMPLATES_DIR.joinpath("index.html").read_text()

    @app.get("/account", response_class=HTMLResponse)
    async def account() -> HTMLResponse:
        html = template.replace("<body>", '<body data-account-ui-v2="true">', 1)
        return HTMLResponse(html)

    @app.get("/flat", response_class=HTMLResponse)
    async def flat() -> HTMLResponse:
        return HTMLResponse(template)

    empty_payloads: dict[str, Any] = {
        "/api/status": {
            "running": False, "current_task": "", "stage": "awakening",
            "evolution_count": 0, "awakened_at": "2025-01-01T00:00:00Z",
        },
        "/api/metrics": {
            "success_rate": 0.0, "total_tasks": 0, "evolution_count": 0,
            "stage": "awakening", "total_plans": 0, "pending_plans": 0,
        },
        "/api/consciousness": {
            "identity_statements": [], "values": [], "open_questions": [],
            "capabilities_known": [], "limitations_known": [],
            "stage": "awakening", "evolution_count": 0,
            "awakened_at": "2025-01-01T00:00:00Z",
        },
        "/api/diagnostics": {
            "recent_error_count": 0, "has_quota_warning": False,
            "has_rate_limit": False, "consecutive_failures": 0,
            "summary": "", "sdk_usage": None,
        },
    }
    for path, payload in empty_payloads.items():
        app.add_api_route(path, lambda payload=payload: JSONResponse(payload))

    for path in (
        "/api/plans", "/api/journal", "/api/tasks", "/api/logs",
        "/api/messages", "/api/versions", "/api/publications",
        "/api/artifact-reviews",
    ):
        app.add_api_route(path, lambda: JSONResponse([]))
    app.add_api_route("/api/logs/sdk-sessions", lambda: JSONResponse([]))
    app.add_api_route(
        "/api/journal/narrative", lambda: JSONResponse({"narrative": ""}),
    )
    app.add_api_route("/api/cost-rates", lambda: JSONResponse({
        "window_start": "2025-06-01T11:00:00+00:00",
        "window_end": "2025-06-01T12:00:00+00:00",
        "window_seconds": 3600.0, "total_cost_usd": 0.0,
        "total_input_tokens": 0, "total_output_tokens": 0,
        "total_cache_read_tokens": 0, "total_calls": 0, "cost_per_hour": 0.0,
        "cost_per_day": 0.0, "cost_per_month": 0.0, "tokens_in_per_hour": 0.0,
        "tokens_out_per_hour": 0.0, "by_caller": {},
    }))
    app.add_api_route("/api/budget", lambda: JSONResponse({
        "window_start": "2025-06-01T11:00:00+00:00",
        "window_end": "2025-06-01T12:00:00+00:00",
        "window_hours": 1.0, "total_cost_usd": 0.0, "reported_cost_usd": 0.0,
        "estimated_cost_usd": 0.0, "total_calls": 0, "by_caller": [],
        "by_group": [],
    }))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


@pytest.fixture()
def nav_server() -> Generator[str]:
    app = _create_nav_app()
    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _goto_wide(page: Any, url: str, path: str) -> None:
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{url}{path}")
    page.wait_for_load_state("networkidle")


def test_flag_on_exposes_tablist_roles_and_icons(page: Any, nav_server: str) -> None:
    _goto_wide(page, nav_server, "/account")

    # The nav bar is a tablist; each link is a tab carrying an inline icon.
    assert page.locator('.nav-bar[role="tablist"]').count() == 1
    tabs = page.locator('.nav-link[role="tab"]')
    assert tabs.count() == len(_NAV_ORDER)
    for name in _NAV_ORDER:
        tab = page.locator(f'.nav-link[data-nav="{name}"][role="tab"]')
        assert tab.count() == 1
        assert tab.locator("svg.nav-icon").count() == 1

    # Every panel is a tabpanel; the default (identity) tab is aria-current.
    assert page.locator('.tab-panel[role="tabpanel"]').count() == len(_NAV_ORDER)
    current = page.locator('.nav-link[aria-current="page"]')
    assert current.count() == 1
    assert current.get_attribute("data-nav") == "identity"


def test_flag_on_skip_link_is_first_focusable(page: Any, nav_server: str) -> None:
    _goto_wide(page, nav_server, "/account")
    skip = page.locator("a.skip-link")
    assert skip.count() == 1
    assert skip.get_attribute("href") == "#main-content"
    # The first Tab from the freshly-loaded page lands on the skip-link.
    page.keyboard.press("Tab")
    focused_class = page.evaluate("() => document.activeElement.className")
    assert "skip-link" in focused_class


def test_flag_on_skip_link_activation_keeps_tab_and_moves_focus(
    page: Any, nav_server: str,
) -> None:
    """Activating the skip-link from a non-default tab jumps focus to the main
    region without resetting the active tab back to the default.

    Regression for r1: the skip-link's ``#main-content`` href, if followed,
    fired ``hashchange`` and ``currentTabFromHash()`` mapped the foreign hash to
    ``DEFAULT_TAB`` (identity), silently switching the active tab. The handler
    now ``preventDefault``s and focuses ``#main-content`` directly.
    """
    _goto_wide(page, nav_server, "/account")

    # Switch to a non-default tab (plans) via its nav-link.
    page.locator('.nav-link[data-nav="plans"]').focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(60)
    assert page.locator('.tab-panel[data-tab="plans"]').is_visible()

    # Activate the skip-link (focus brings it on-screen, Enter activates it).
    page.locator("a.skip-link").focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(60)

    # The active tab is still plans — not reset to identity — and focus landed
    # on the main content region.
    visible = [
        n for n in _NAV_ORDER
        if page.locator(f'.tab-panel[data-tab="{n}"]').is_visible()
    ]
    assert visible == ["plans"]
    assert page.evaluate("() => document.activeElement.id") == "main-content"
    current = page.locator('.nav-link[aria-current="page"]')
    assert current.count() == 1
    assert current.get_attribute("data-nav") == "plans"


def test_flag_on_aria_orientation_tracks_breakpoint(
    page: Any, nav_server: str,
) -> None:
    """aria-orientation reflects the rendered axis: vertical for the >=1024px
    rail, horizontal for the collapsed sub-breakpoint strip (r3)."""
    _goto_wide(page, nav_server, "/account")
    tablist = page.locator('.nav-bar[role="tablist"]')
    assert tablist.get_attribute("aria-orientation") == "vertical"

    # Cross below the 1024px rail breakpoint; the matchMedia listener updates it.
    page.set_viewport_size({"width": 900, "height": 900})
    page.wait_for_timeout(60)
    assert tablist.get_attribute("aria-orientation") == "horizontal"


def test_flag_on_arrow_keys_traverse_every_tab(page: Any, nav_server: str) -> None:
    _goto_wide(page, nav_server, "/account")
    page.locator('.nav-link[data-nav="diagnostics"]').focus()
    for name in _NAV_ORDER:
        active = page.evaluate(
            "() => document.activeElement.getAttribute('data-nav')",
        )
        assert active == name, f"expected arrow focus on {name}, got {active}"
        page.keyboard.press("ArrowDown")
    # ArrowDown off the last tab wraps back to the first (roving tabindex).
    active = page.evaluate("() => document.activeElement.getAttribute('data-nav')")
    assert active == "diagnostics"


def test_flag_on_enter_activates_and_focuses_each_panel(
    page: Any, nav_server: str,
) -> None:
    _goto_wide(page, nav_server, "/account")
    for name in _NAV_ORDER:
        page.locator(f'.nav-link[data-nav="{name}"]').focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(60)
        panel = page.locator(f'.tab-panel[data-tab="{name}"]')
        assert panel.is_visible(), f"panel {name} should be visible after Enter"
        # Only that panel is visible, and focus landed on it.
        visible = [
            n for n in _NAV_ORDER
            if page.locator(f'.tab-panel[data-tab="{n}"]').is_visible()
        ]
        assert visible == [name]
        focused_id = page.evaluate("() => document.activeElement.id")
        assert focused_id == f"panel-{name}"


def test_flag_on_enhance_survives_zero_panels(page: Any, nav_server: str) -> None:
    """enhanceNavV2 must not abort when there are no .tab-panel elements.

    Regression for the review nit: the two-column shell build ran
    ``main.parentNode.insertBefore(shell, main)`` (and appended nav into the
    shell) unconditionally, but ``main`` is only attached to the DOM when
    ``panels.length`` is truthy. With zero panels ``main`` is detached,
    ``main.parentNode`` is null, and the unguarded dereference threw a TypeError
    that aborted enhanceNavV2/initTabs — tearing down the whole tab system. The
    shell build is now guarded on the main region being attached, so the tablist
    roles still apply, the nav stays in the DOM, and no error is thrown.

    Unreachable with the real 11-panel template, so this exercises enhanceNavV2
    against a hand-built panel-less DOM.
    """
    _goto_wide(page, nav_server, "/flat")
    result = page.evaluate(
        """() => {
            document.body.setAttribute('data-account-ui-v2', 'true');
            document.body.innerHTML =
                '<nav class="nav-bar" id="nav-bar">' +
                '<a class="nav-link" data-nav="diagnostics" href="#s">D</a>' +
                '<a class="nav-link" data-nav="identity" href="#s">I</a>' +
                '</nav>';
            try {
                enhanceNavV2();
            } catch (e) {
                return { threw: true, error: String(e) };
            }
            return {
                threw: false,
                tablists: document.querySelectorAll('.nav-bar[role="tablist"]').length,
                tabs: document.querySelectorAll('.nav-link[role="tab"]').length,
                shells: document.querySelectorAll('.account-shell').length,
                navAttached: !!document.getElementById('nav-bar'),
            };
        }"""
    )
    # No TypeError; the nav still receives its tablist/tab roles and stays in the
    # DOM, while the shell is correctly skipped for want of an attached main.
    assert result["threw"] is False, result.get("error")
    assert result["tablists"] == 1
    assert result["tabs"] == 2
    assert result["shells"] == 0
    assert result["navAttached"] is True


def test_header_offset_clears_the_pinned_header_under_v2_shell(
    page: Any, nav_server: str,
) -> None:
    """--header-offset still clears whatever pins once the nav moves into the rail.

    The v2 posture variant of
    ``test_polish.py::test_header_offset_follows_the_pinned_element_across_breakpoints``,
    which proves the same invariant on the flat nav. Under the marker the shape
    of the problem changes: ``enhanceNavV2()`` lifts ``#nav-bar`` out of
    ``.sticky-header``, so the pinned header is a *different, shorter* box than
    the one the flat-nav proof measures, and below 640px the dissolved
    (``display: contents``) header hands the pin to a ``#nav-bar`` that now lives
    in ``.account-shell`` rather than in ``<body>``. ``syncHeaderOffset()`` must
    still publish the height of whichever element actually obstructs the top of
    the viewport, and ``scroll-margin-top`` must still clear it — otherwise an
    activated tab panel (v2 moves focus onto it) lands behind the header.
    """
    _goto_wide(page, nav_server, "/account")
    # First render replaces the Status/Metrics skeletons, which grows the header;
    # the ResizeObserver republishes --header-offset from the settled height.
    page.wait_for_selector(".status-dot")
    page.wait_for_function(
        """() => {
            const header = document.querySelector('.sticky-header');
            const v = getComputedStyle(document.documentElement)
                .getPropertyValue('--header-offset').trim();
            return header.offsetHeight > 0 && v === `${header.offsetHeight}px`;
        }"""
    )

    desktop = page.evaluate(
        """() => {
            const header = document.querySelector('.sticky-header');
            const panel = document.querySelector('.tab-panel');
            return {
                header: header.offsetHeight,
                navInHeader: header.contains(document.getElementById('nav-bar')),
                navInShell: !!document.getElementById('nav-bar')
                    .closest('.account-shell'),
                scrollMargin: getComputedStyle(panel).scrollMarginTop,
            };
        }"""
    )
    # The premise that makes this a distinct posture: the rail owns the nav now,
    # so the measured header is not the flat-nav header.
    assert desktop["navInHeader"] is False, (
        f"the v2 rail must own #nav-bar, not .sticky-header, got {desktop!r}"
    )
    assert desktop["navInShell"] is True, (
        f"#nav-bar must sit inside .account-shell under the marker, got {desktop!r}"
    )
    margin = float(desktop["scrollMargin"].removesuffix("px"))
    assert margin >= desktop["header"], (
        f"a focused .tab-panel must clear the pinned header, got {desktop!r}"
    )

    # Below 640px the header dissolves to display:contents and the shell-owned
    # #nav-bar is what pins, so the offset must follow it there too.
    page.set_viewport_size({"width": 375, "height": 812})
    page.wait_for_function(
        """() => {
            const nav = document.getElementById('nav-bar');
            const v = getComputedStyle(document.documentElement)
                .getPropertyValue('--header-offset').trim();
            return nav.offsetHeight > 0 && v === `${nav.offsetHeight}px`;
        }"""
    )
    mobile = page.evaluate(
        """() => {
            const panel = document.querySelector('.tab-panel');
            return {
                header: document.querySelector('.sticky-header').offsetHeight,
                nav: document.getElementById('nav-bar').offsetHeight,
                navPosition: getComputedStyle(document.getElementById('nav-bar'))
                    .position,
                scrollMargin: getComputedStyle(panel).scrollMarginTop,
            };
        }"""
    )
    assert mobile["header"] == 0, (
        f"below 640px the display:contents header must report no box, got {mobile!r}"
    )
    # The shell became .nav-bar's containing block when enhanceNavV2 moved it;
    # sticky is bounded by that block, and .account-shell spans nav + all panels,
    # so the nav still pins over the content it labels.
    assert mobile["navPosition"] == "sticky", (
        f"the shell-owned nav must still pin below 640px, got {mobile!r}"
    )
    mobile_margin = float(mobile["scrollMargin"].removesuffix("px"))
    assert mobile_margin >= mobile["nav"], (
        f"a focused .tab-panel must clear the pinned nav, got {mobile!r}"
    )


def test_gutter_stays_in_lock_step_under_v2_shell(
    page: Any, nav_server: str,
) -> None:
    """--gutter, the body padding, and the pinned element's bleed track under v2.

    The v2 posture variant of
    ``test_polish.py::test_gutter_stays_in_lock_step_at_every_breakpoint``. The
    shell rules are scoped under ``body[data-account-ui-v2]`` and deliberately
    leave the 640/768/1440 gutter breakpoints alone, but they *do* re-parent
    ``#nav-bar`` into ``.account-shell`` — and below 640px the nav is the element
    whose negative margin bleeds to the body padding edge. Re-parenting the
    bleeding element is exactly the edit that could break the lock-step silently,
    so pin all three values together on this side of the branch too.
    """
    _goto_wide(page, nav_server, "/account")
    assert page.locator(".account-shell").count() == 1

    for width, expected in ((375, "12px"), (700, "24px"), (900, "40px")):
        page.set_viewport_size({"width": width, "height": 800})
        page.wait_for_timeout(50)
        geom = page.evaluate(
            """() => {
                const body = getComputedStyle(document.body);
                const nav = getComputedStyle(document.getElementById('nav-bar'));
                const header = getComputedStyle(
                    document.querySelector('.sticky-header'));
                return {
                    gutter: body.getPropertyValue('--gutter').trim(),
                    padding: body.paddingLeft,
                    headerDisplay: header.display,
                    headerBleed: header.marginLeft,
                    navBleed: nav.marginLeft,
                };
            }"""
        )
        assert geom["gutter"] == expected, (
            f"at {width}px --gutter must be {expected} under the marker, got {geom!r}"
        )
        assert geom["padding"] == expected, (
            f"at {width}px the body padding must track --gutter, got {geom!r}"
        )
        pinned = "navBleed" if geom["headerDisplay"] == "contents" else "headerBleed"
        bleed = float(geom[pinned].removesuffix("px"))
        assert bleed == -float(expected.removesuffix("px")), (
            f"at {width}px the pinned element's bleed must be -{expected} under "
            f"the marker, got {geom!r}"
        )


def test_flag_off_renders_flat_nav_without_shell(page: Any, nav_server: str) -> None:
    _goto_wide(page, nav_server, "/flat")
    # No tablist roles, no icons, no skip-link — the current flat pill nav.
    assert page.locator('.nav-bar[role="tablist"]').count() == 0
    assert page.locator('.nav-link[role="tab"]').count() == 0
    assert page.locator(".nav-link svg.nav-icon").count() == 0
    assert page.locator("a.skip-link").count() == 0
    # The flat anchors are still present and the default tab still shows.
    assert page.locator(".nav-link[data-nav]").count() == len(_NAV_ORDER)
    assert page.locator('.tab-panel[data-tab="identity"]').is_visible()
