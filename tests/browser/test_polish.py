"""Browser proofs for the account dashboard accessibility/responsive polish.

Proposal 122, step 8 closes four gaps on the ``/account`` surface; this file
proves them in a real browser. The sign-out control's *revealed* half needs a
session cookie and so lives in ``test_login.py``, where the auth-enabled fixture
supplies one; its *hidden* half — the branch that keeps the control out of the
no-auth local-dev config, where ``/logout`` is never registered — is proven here
against the auth-less fixture:

* the reduced-motion block stills every motion it claims — the ``pulse``
  animation on the active status dot, the ``.toast`` ``fadeIn``, and the
  ``.nav-link`` / ``.sign-out-btn`` colour transitions — under
  ``prefers-reduced-motion: reduce``, and leaves them running otherwise
  (mirroring ``test_skeletons.py::test_reduced_motion_suppresses_skeleton_shimmer``);
* every control the shared ``:focus-visible`` rule names — the nav links, the
  filter and load-more buttons, the compose controls, and the confirm-dialog
  actions — paints the 2px accent ring instead of the invisible browser default,
  and ``--header-offset`` keeps a focused or anchor-jumped target clear of the
  sticky header rather than behind it;
* the ``--gutter`` widens at the ``768px`` breakpoint and the content is capped
  and centred at ``1440px`` for a comfortable measure on wide monitors.

The reduced-motion assertions use the ``getComputedStyle(...).animationName``
idiom shared with ``test_skeletons.py``; the breakpoint assertions read the
resolved ``body`` padding / ``max-width`` via ``getComputedStyle`` after
``page.set_viewport_size(...)``.
"""

from __future__ import annotations

from typing import Any


def _to_rgb(hex_color: str) -> str:
    """Convert a ``#rrggbb`` token value to the ``rgb(r, g, b)`` computed form.

    ``getComputedStyle`` reports a colour as ``rgb(...)``, but the token is
    authored as hex; converting here keeps the assertion bound to the live
    ``--accent`` value rather than to a hard-coded copy of it that a palette
    change would silently desynchronise.
    """
    raw = hex_color.lstrip("#")
    r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgb({r}, {g}, {b})"


def _computed_animation_name(page: Any, selector: str) -> str:
    """Return the computed ``animation-name`` of the first ``selector`` node."""
    name: str = page.evaluate(
        "sel => getComputedStyle(document.querySelector(sel)).animationName",
        selector,
    )
    return name


def test_status_pulse_respects_reduced_motion(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The active-status pulse runs with motion allowed and stills under reduce."""
    url, data, _log = dashboard_server
    # The mock status defaults to idle (.status-dot.idle); flip it so the render
    # emits .status-dot.active, the node that carries the pulse animation.
    data["status"]["running"] = True

    page.emulate_media(reduced_motion="no-preference")
    page.goto(f"{url}/account", wait_until="domcontentloaded")
    page.wait_for_selector(".status-dot.active")
    assert _computed_animation_name(page, ".status-dot.active") == "pulse", (
        "with motion allowed the active status dot must run the pulse animation"
    )

    page.emulate_media(reduced_motion="reduce")
    assert _computed_animation_name(page, ".status-dot.active") == "none", (
        "under prefers-reduced-motion: reduce the pulse animation must be stilled"
    )


def test_keyboard_focused_nav_link_shows_focus_ring(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A keyboard-focused nav-link exposes a visible focus ring, not the default."""
    url, *_ = dashboard_server
    page.goto(f"{url}/account", wait_until="domcontentloaded")

    # Tab from the freshly-loaded flat-nav page lands on the first focusable
    # control — the first nav-link — and keyboard focus engages :focus-visible.
    page.keyboard.press("Tab")
    focused_class = page.evaluate("() => document.activeElement.className")
    assert "nav-link" in focused_class, (
        f"the first Tab should land on a nav-link, got {focused_class!r}"
    )

    outline = page.evaluate(
        """() => {
            const s = getComputedStyle(document.activeElement);
            return {
                style: s.outlineStyle,
                width: s.outlineWidth,
                color: s.outlineColor,
                accent: getComputedStyle(document.documentElement)
                    .getPropertyValue('--accent').trim(),
            };
        }""",
    )
    # Assert the *shared* ring, not merely "some ring": Chromium already paints
    # its own `outline: auto` default on a keyboard-focused anchor, so a
    # non-none / non-zero-width check would pass with the rule deleted and prove
    # nothing. The 2px solid accent triple is what the change actually adds.
    assert outline["style"] == "solid", (
        f"the nav-link ring must be the shared solid ring, not the UA default "
        f"`auto`, got {outline!r}"
    )
    assert outline["width"] == "2px", (
        f"the focus ring must be the shared 2px ring, got {outline!r}"
    )
    assert _to_rgb(outline["accent"]) == outline["color"], (
        f"the focus ring must be painted in --accent, got {outline!r}"
    )


def _gutter_geometry(page: Any) -> dict[str, str]:
    """Report the resolved --gutter alongside the two boxes it must drive."""
    result: dict[str, str] = page.evaluate(
        """() => {
            const body = getComputedStyle(document.body);
            const nav = getComputedStyle(document.getElementById('nav-bar'));
            const header = getComputedStyle(document.querySelector('.sticky-header'));
            return {
                gutter: body.getPropertyValue('--gutter').trim(),
                padding: body.paddingLeft,
                headerDisplay: header.display,
                headerBleed: header.marginLeft,
                navBleed: nav.marginLeft,
            };
        }"""
    )
    return result


def test_gutter_widens_at_768_breakpoint(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The --gutter-driven body padding steps up at the 768px breakpoint."""
    url, *_ = dashboard_server
    page.goto(f"{url}/account", wait_until="domcontentloaded")

    # Between the 640 and 768 breakpoints the default 24px gutter applies.
    page.set_viewport_size({"width": 700, "height": 800})
    page.wait_for_timeout(50)
    pad_narrow = page.evaluate("() => getComputedStyle(document.body).paddingLeft")
    assert pad_narrow == "24px", f"expected the default 24px gutter, got {pad_narrow!r}"

    # At >=768px the gutter widens to 40px.
    page.set_viewport_size({"width": 900, "height": 800})
    page.wait_for_timeout(50)
    pad_wide = page.evaluate("() => getComputedStyle(document.body).paddingLeft")
    assert pad_wide == "40px", f"expected the widened 40px gutter, got {pad_wide!r}"


def test_gutter_stays_in_lock_step_at_every_breakpoint(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """--gutter, the body padding, and the pinned element's bleed move together.

    The ``body`` comment states the invariant the whole responsive block rests
    on: --gutter drives the horizontal padding *and* the edge-bleed of whichever
    element pins at the top. The <=640px block used to hard-code
    ``padding: 12px`` while --gutter stayed at 24px, so the invariant was already
    false there — inert only because ``.sticky-header`` is ``display: contents``
    at that width and its ``margin-inline`` never applied. This pins all three
    values together at each breakpoint so a future edit cannot re-open the gap.
    """
    url, *_ = dashboard_server
    page.goto(f"{url}/account", wait_until="domcontentloaded")

    for width, expected in ((375, "12px"), (700, "24px"), (900, "40px")):
        page.set_viewport_size({"width": width, "height": 800})
        page.wait_for_timeout(50)
        geom = _gutter_geometry(page)
        assert geom["gutter"] == expected, (
            f"at {width}px --gutter must be {expected}, got {geom!r}"
        )
        assert geom["padding"] == expected, (
            f"at {width}px the body padding must track --gutter, got {geom!r}"
        )
        # Whichever element actually pins takes the negative gutter as its bleed:
        # .sticky-header above 640px, .nav-bar below it where the header is
        # display:contents and generates no box to bleed.
        pinned = "navBleed" if geom["headerDisplay"] == "contents" else "headerBleed"
        bleed = float(geom[pinned].removesuffix("px"))
        assert bleed == -float(expected.removesuffix("px")), (
            f"at {width}px the pinned element's bleed must be -{expected}, got {geom!r}"
        )


def test_content_capped_and_centred_at_1440(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Above 1440px the content is capped by a max-width and centred."""
    url, *_ = dashboard_server
    page.goto(f"{url}/account", wait_until="domcontentloaded")

    # Below 1440px the body is uncapped.
    page.set_viewport_size({"width": 1000, "height": 800})
    page.wait_for_timeout(50)
    max_narrow = page.evaluate("() => getComputedStyle(document.body).maxWidth")
    assert max_narrow == "none", f"below 1440 the body must be uncapped, got {max_narrow!r}"

    # At >=1440px the content is capped and the auto side-margins centre it.
    page.set_viewport_size({"width": 1600, "height": 900})
    page.wait_for_timeout(50)
    max_wide = page.evaluate("() => getComputedStyle(document.body).maxWidth")
    assert max_wide == "1400px", f"at >=1440 the body must cap at 1400px, got {max_wide!r}"
    margin_left = page.evaluate("() => getComputedStyle(document.body).marginLeft")
    assert margin_left != "0px", (
        f"the capped body must be centred by a non-zero auto margin, got {margin_left!r}"
    )


def _focus_ring(page: Any, selector: str) -> dict[str, str]:
    """Keyboard-focus the first ``selector`` node *inside the active panel*.

    The lookup is scoped to ``.tab-panel.active`` — the panel ``_show_tab`` just
    activated — rather than resolved document-wide, so the helper delivers what
    that docstring promises: each control probed on its own *activated* panel. A
    document-wide ``querySelector`` would land on the right node today only by
    accident of DOM order (the first ``.log-filter-btn`` happens to sit in the
    Plans panel, the first ``.load-more-btn`` in Journal); adding either class to
    an earlier panel would silently move the probe onto a panel nobody activated
    while the promise still read as kept.

    A bare ``.focus()`` is not enough on its own: ``:focus-visible`` matches a
    programmatic focus move only when the previously-focused element already
    matched it. Pressing Tab first engages the keyboard modality (it lands on a
    nav-link, which does match), and that state carries into the programmatic
    move — so this reports the ring a keyboard operator would really see.
    """
    page.keyboard.press("Tab")
    result: dict[str, str] = page.evaluate(
        """sel => {
            const panel = document.querySelector('.tab-panel.active');
            if (!panel) return { missing: 'no active .tab-panel' };
            const el = panel.querySelector(sel);
            if (!el) return { missing: sel };
            el.focus();
            const s = getComputedStyle(el);
            return {
                focused: String(document.activeElement === el),
                focusVisible: String(el.matches(':focus-visible')),
                style: s.outlineStyle,
                width: s.outlineWidth,
            };
        }""",
        selector,
    )
    return result


def _show_tab(page: Any, tab: str) -> None:
    """Activate ``tab`` via the hash router and wait for its panel to render.

    In production the inactive ``.tab-panel`` nodes are ``display: none``, so a
    control inside one could not take focus at all. That is *not* what makes the
    helper necessary here: ``conftest._dashboard_html()`` injects
    ``.tab-panel { display: block !important }`` into every ``dashboard_server``
    page, so all panels are visible and any control could be focused without
    activating its tab. What the helper buys is that each control is probed on
    its own *activated* panel — the state a real keyboard operator is in when
    they reach it — and that the hash-driven render has settled before the ring
    is read. That is load-bearing rather than decorative because ``_focus_ring``
    scopes its lookup to ``.tab-panel.active``: the tab this activates is what
    selects the probe target. Do not drop it on the reasoning that the panels
    are already visible.
    """
    page.evaluate("t => { location.hash = `#section-${t}`; }", tab)
    page.wait_for_selector(f'.tab-panel[data-tab="{tab}"].active')


def test_reduced_motion_stills_toast_and_control_transitions(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Reduce stills the toast fade and the nav-link / sign-out transitions.

    ``test_status_pulse_respects_reduced_motion`` covers only the ``pulse``
    animation; the same block also stills the ``.toast`` ``fadeIn`` plus the
    ``.nav-link`` / ``.sign-out-btn`` colour transitions, which are what a
    reduced-motion user actually notices on every hover and every toast.
    """
    url, *_ = dashboard_server
    page.emulate_media(reduced_motion="no-preference")
    page.goto(f"{url}/account", wait_until="domcontentloaded")
    page.wait_for_selector(".nav-link")

    # showToast() is a module-level function on the classic script, so a toast
    # can be raised directly instead of driving a whole approve/reject flow. The
    # probe raises its *own* toast on every read rather than reusing one shown
    # once up front: showToast() removes its node 3000ms + 500ms later, so the
    # second read — taken after an emulate_media() round-trip — could dereference
    # a null node on a loaded runner. Computed style is live, so a freshly-raised
    # toast reports exactly what a surviving one would.
    probe = """() => {
        showToast('reduced-motion probe', 'info');
        const toasts = document.querySelectorAll('.toast');
        const read = el => {
            const s = getComputedStyle(el);
            return { animation: s.animationName, duration: s.transitionDuration };
        };
        return {
            toast: read(toasts[toasts.length - 1]),
            navLink: read(document.querySelector('.nav-link')),
            signOut: read(document.querySelector('.sign-out-btn')),
        };
    }"""

    allowed = page.evaluate(probe)
    assert allowed["toast"]["animation"] == "fadeIn", (
        f"with motion allowed the toast must run fadeIn, got {allowed['toast']!r}"
    )
    assert allowed["toast"]["duration"] != "0s"
    assert allowed["navLink"]["duration"] != "0s"
    assert allowed["signOut"]["duration"] != "0s"

    page.emulate_media(reduced_motion="reduce")
    reduced = page.evaluate(probe)
    assert reduced["toast"]["animation"] == "none", (
        f"under reduce the toast fade must be stilled, got {reduced['toast']!r}"
    )
    assert reduced["toast"]["duration"] == "0s", (
        f"under reduce the toast transition must be stilled, got {reduced['toast']!r}"
    )
    assert reduced["navLink"]["duration"] == "0s", (
        f"under reduce the nav-link transition must be stilled, got {reduced['navLink']!r}"
    )
    assert reduced["signOut"]["duration"] == "0s", (
        f"under reduce the sign-out transition must be stilled, got {reduced['signOut']!r}"
    )


def test_focus_ring_covers_the_secondary_controls(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Every control the shared :focus-visible rule names paints a ring.

    ``test_keyboard_focused_nav_link_shows_focus_ring`` proves only the
    ``.nav-link`` arm. The same rule also covers the plan/journal filter buttons,
    the load-more buttons, and the compose controls — each of which fell back to
    the invisible browser default before this change.
    """
    url, *_ = dashboard_server
    page.goto(f"{url}/account", wait_until="domcontentloaded")
    page.wait_for_selector(".nav-link")

    for tab, selector in (
        ("plans", ".log-filter-btn"),
        ("journal", ".load-more-btn"),
        ("messages", ".compose-form textarea"),
        ("messages", ".compose-form button"),
    ):
        _show_tab(page, tab)
        ring = _focus_ring(page, selector)
        assert "missing" not in ring, (
            f"{selector} was not found on the activated {tab!r} panel: {ring!r}"
        )
        assert ring["focused"] == "true", f"{selector} did not take focus: {ring!r}"
        assert ring["focusVisible"] == "true", (
            f"{selector} must match :focus-visible under keyboard focus, got {ring!r}"
        )
        assert ring["style"] != "none", f"{selector} painted no focus ring: {ring!r}"
        assert ring["width"] == "2px", (
            f"{selector} must carry the shared 2px accent ring, got {ring!r}"
        )


def test_focus_ring_covers_the_confirm_dialog_buttons(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The confirm-dialog actions paint the shared ring under keyboard focus.

    The dialog buttons are the last arm of the shared rule and the one a
    keyboard operator most needs to see, since the modal traps focus.
    """
    url, *_ = dashboard_server
    page.goto(f"{url}/account", wait_until="domcontentloaded")
    page.wait_for_selector(".nav-link")

    # Open the modal directly: the ring is a property of the dialog's buttons,
    # not of the approve/reject flow that normally raises it.
    page.evaluate("() => document.getElementById('confirm-dialog').showModal()")
    # showModal() focuses the first focusable child; Tab keeps focus inside the
    # modal and engages the keyboard modality that :focus-visible keys off.
    page.keyboard.press("Tab")

    ring = page.evaluate(
        """() => {
            const el = document.activeElement;
            const s = getComputedStyle(el);
            return {
                inActions: String(!!el.closest('.confirm-dialog-actions')),
                focusVisible: String(el.matches(':focus-visible')),
                style: s.outlineStyle,
                width: s.outlineWidth,
            };
        }"""
    )
    assert ring["inActions"] == "true", (
        f"Tab inside the modal must land on a dialog action button, got {ring!r}"
    )
    assert ring["focusVisible"] == "true", f"the dialog button must be focus-visible: {ring!r}"
    assert ring["style"] != "none", f"the dialog button painted no focus ring: {ring!r}"
    assert ring["width"] == "2px", (
        f"the dialog button must carry the shared 2px accent ring, got {ring!r}"
    )


def test_header_offset_follows_the_pinned_element_across_breakpoints(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """--header-offset tracks whichever element actually pins to the top.

    Step 8's acceptance asks that a focus target is not obscured by the sticky
    header. The header's height is content-dependent, so ``syncHeaderOffset()``
    measures it rather than approximating: on desktop that is ``.sticky-header``,
    but below ``640px`` the header is ``display: contents`` (no box at all) and
    ``.nav-bar`` pins instead.
    """
    url, *_ = dashboard_server
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{url}/account", wait_until="domcontentloaded")
    # Wait for the first render: the Status/Metrics skeletons are replaced by
    # real content, which changes the header height the ResizeObserver reports.
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
            const h2 = document.getElementById('section-identity');
            return {
                header: header.offsetHeight,
                scrollMargin: getComputedStyle(h2).scrollMarginTop,
            };
        }"""
    )
    # The whole point of measuring: the pinned header is taller than the 6rem
    # (96px) fallback the rule would otherwise have used, so a fixed clearance
    # would leave the heading partly behind it.
    assert desktop["header"] > 96, (
        f"the pinned header must exceed the 6rem fallback for this to bite, got {desktop!r}"
    )
    margin = float(desktop["scrollMargin"].removesuffix("px"))
    assert margin >= desktop["header"], (
        f"scroll-margin-top must fully clear the pinned header, got {desktop!r}"
    )

    # Below 640px the header dissolves to display:contents and .nav-bar pins.
    page.set_viewport_size({"width": 375, "height": 812})
    page.wait_for_function(
        """() => {
            const nav = document.getElementById('nav-bar');
            const v = getComputedStyle(document.documentElement)
                .getPropertyValue('--header-offset').trim();
            return nav.offsetHeight > 0 && v === `${nav.offsetHeight}px`;
        }"""
    )
    mobile_header = page.evaluate(
        "() => document.querySelector('.sticky-header').offsetHeight"
    )
    assert mobile_header == 0, (
        f"below 640px the display:contents header must report no box, got {mobile_header!r}"
    )


def test_header_offset_clears_rather_than_writing_zero(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """With nothing measurable pinned, the CSS 6rem fallback is restored.

    ``measure()`` used to write ``--header-offset: 0px`` whenever neither
    candidate reported a box. A written ``0px`` is a *defined* value, so
    ``var(--header-offset, 6rem)`` would never reach the fallback the CSS
    comment promises and ``scroll-margin-top`` would collapse to the
    ``--space-2`` gap alone — an anchor-jumped heading landing flush against the
    top of the viewport. Clearing the property instead keeps the fallback live.
    """
    url, *_ = dashboard_server
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{url}/account", wait_until="domcontentloaded")
    page.wait_for_selector(".status-dot")
    # Settle on a real measurement first, so the clear below is provably
    # replacing a measured value rather than an unset one.
    page.wait_for_function(
        """() => {
            const header = document.querySelector('.sticky-header');
            const v = getComputedStyle(document.documentElement)
                .getPropertyValue('--header-offset').trim();
            return header.offsetHeight > 0 && v === `${header.offsetHeight}px`;
        }"""
    )

    state = page.evaluate(
        """() => {
            const h2 = document.getElementById('section-identity');
            const header = document.querySelector('.sticky-header');
            const read = () => getComputedStyle(h2).scrollMarginTop;
            const measured = read();
            // Take a box away from *both* candidates syncHeaderOffset() knows
            // about, so measure() resolves a height of 0 whichever it picks: the
            // header is hidden outright, and #nav-bar — captured by reference
            // when the page loaded — is detached. The resize listener re-runs it.
            header.style.display = 'none';
            document.getElementById('nav-bar').remove();
            window.dispatchEvent(new Event('resize'));
            return {
                measured,
                fallback: read(),
                property: getComputedStyle(document.documentElement)
                    .getPropertyValue('--header-offset').trim(),
            };
        }"""
    )
    # The measured clearance exceeds the fallback, so the swap is observable.
    assert float(state["measured"].removesuffix("px")) > 104, (
        f"the measured clearance must exceed the 6rem fallback, got {state!r}"
    )
    assert state["property"] == "", (
        f"--header-offset must be cleared, not written as '0px', got {state!r}"
    )
    # 6rem (96px) + --space-2 (8px) = 104px, not the 8px a '0px' write leaves.
    assert state["fallback"] == "104px", (
        f"clearing the property must restore the 6rem fallback, got {state!r}"
    )


def test_sign_out_control_stays_hidden_without_auth(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Without the maestro_csrf cookie the sign-out control is never revealed.

    ``revealSignOut()``'s guard is what keeps the control out of the no-auth
    local-dev configuration, where ``create_router()`` registers no ``/logout``
    route at all — revealing it there would offer a button that 404s. This
    fixture serves the dashboard with auth disabled (no ``maestro_csrf`` cookie
    is ever set), which is exactly that configuration; the revealed half is
    covered by ``test_login.py::test_sign_out_control_visible_and_clears_cookies``.
    """
    url, *_ = dashboard_server
    page.goto(f"{url}/account", wait_until="domcontentloaded")
    # The control is in the served markup either way — the guard governs only
    # whether index.js clears its `hidden` attribute.
    page.wait_for_selector(".sign-out", state="attached")

    state = page.evaluate(
        """() => {
            const form = document.querySelector('.sign-out');
            return {
                hidden: String(form.hidden),
                display: getComputedStyle(form).display,
                csrfCookie: String(/(?:^|;\\s*)maestro_csrf=/.test(document.cookie)),
            };
        }"""
    )
    assert state["csrfCookie"] == "false", (
        f"the auth-less fixture must set no maestro_csrf cookie, got {state!r}"
    )
    assert state["hidden"] == "true", (
        f"revealSignOut() must leave the control hidden without auth, got {state!r}"
    )
    # The `hidden` attribute only bites if no author rule sets `display` on
    # .sign-out — assert the rendered result, not just the attribute.
    assert state["display"] == "none", (
        f"the hidden sign-out control must not render, got {state!r}"
    )
    assert page.locator(".sign-out-btn").is_visible() is False


def test_toast_region_anchors_to_the_capped_content_column(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Above 1440px the toast region tracks the content column, not the viewport.

    The 1440px rule caps and centres the content, but ``.toast-region`` is
    ``position: fixed``: left viewport-anchored it would announce from the far
    edge of a wide monitor, well outside the column its message refers to.
    """
    url, *_ = dashboard_server
    page.goto(f"{url}/account", wait_until="domcontentloaded")
    page.set_viewport_size({"width": 1600, "height": 900})
    page.wait_for_timeout(50)
    page.evaluate("() => showToast('anchor probe', 'info')")
    page.wait_for_selector(".toast")

    geom = page.evaluate(
        """() => {
            const region = document.getElementById('toast-region');
            const body = document.body.getBoundingClientRect();
            const pad = parseFloat(getComputedStyle(document.body).paddingRight);
            return {
                toastRight: region.getBoundingClientRect().right,
                contentRight: body.right - pad,
                viewport: document.documentElement.clientWidth,
            };
        }"""
    )
    assert abs(geom["toastRight"] - geom["contentRight"]) <= 1, (
        f"the toast region must align with the capped content column, got {geom!r}"
    )
    # And is genuinely off the viewport edge — the pre-fix behaviour parked it a
    # single --space-5 (20px) in, which this margin excludes.
    assert geom["viewport"] - geom["toastRight"] > 40, (
        f"the toast region must not stay pinned to the viewport edge, got {geom!r}"
    )
