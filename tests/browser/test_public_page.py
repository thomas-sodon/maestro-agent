"""Browser tests for the public intro page served at ``/``."""

from __future__ import annotations

from typing import Any

import pytest

from maestro.prompts.defaults import PUBLIC_FRAMING


def test_page_loads_without_auth(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """Navigating to / returns 200 and the public intro page."""
    url, _data = public_page_server
    page.goto(url)
    page.wait_for_load_state("networkidle")
    assert page.title() == "Maestro"


def test_hero_heading_and_subtitle(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """The hero heading is Maestro and the subtitle is the served framing.

    ``renderSubtitle`` is a single stage-independent projection of the served
    ``framing`` (PUBLIC_FRAMING) — the objective vocabulary — so the page neither claims Maestro has arrived nor re-projects
    the retired "seeking" telos, and it renders the same at every internal
    state. Exact equality subsumes the vocabulary/no-arrival checks: the served
    framing's objective words and absence of "Arrived" are golden-tested over
    PUBLIC_FRAMING in tests/test_public_surface_fidelity.py.
    """
    url, _data = public_page_server
    page.goto(url)
    page.wait_for_load_state("networkidle")
    h1 = page.locator("h1")
    assert h1.text_content() == "Maestro"
    # Wait on a JS-only signal (status-text becomes "Awake" once loadIdentity
    # sees the awakened fixture; the static default is "Waking up…") so the wait
    # proves loadIdentity()'s synchronous render pass — the same pass that sets
    # the subtitle — ran before asserting on it.
    page.wait_for_function(
        "() => document.getElementById('status-text').textContent === 'Awake'",
        timeout=5000,
    )
    subtitle = (page.locator(".hero-subtitle").text_content() or "")
    assert subtitle == PUBLIC_FRAMING


def test_account_link_points_to_account(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """The top-bar Account link sends the visitor to /account."""
    url, _data = public_page_server
    page.goto(url)
    page.wait_for_load_state("networkidle")
    link = page.locator("a.account-link")
    assert link.is_visible()
    assert link.get_attribute("href") == "/account"


def test_live_uptime_renders_from_awakened_at(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """The uptime counter is populated from the awakened_at timestamp."""
    url, data = public_page_server
    data["public_identity"]["awakened_at"] = "2024-01-01T00:00:00Z"
    page.goto(url)
    page.wait_for_load_state("networkidle")
    uptime = page.locator("#uptime")
    # Poll until text appears — JS fetch completes after network idle in rare cases.
    page.wait_for_function(
        "() => (document.getElementById('uptime').textContent || '').length > 0",
        timeout=5000,
    )
    text = (uptime.text_content() or "").strip()
    assert text
    assert "Alive for" in text


def test_stats_reflect_identity_payload(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    url, data = public_page_server
    data["public_identity"]["evolution_count"] = 7
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => document.getElementById('stat-evolutions').textContent === '7'",
        timeout=5000,
    )
    assert page.locator("#stat-evolutions").text_content() == "7"
    # The single merge-backed number lives inside a ratio line that names it.
    ratio_text = (page.locator(".ratio-line").text_content() or "")
    assert "shipped" in ratio_text


def test_self_portrait_renders_capabilities_and_limitations(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """Capabilities and limitations render as a single self-portrait section.

    The earlier two-card layout implied parity; the merged self-portrait
    keeps both lists but lets CSS encode the asymmetry.
    """
    url, data = public_page_server
    data["public_identity"]["capabilities_known"] = ["draft plans", "merge prs"]
    data["public_identity"]["limitations_known"] = ["context loss", "loop bias"]
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => document.querySelectorAll('#capabilities-list li').length === 2"
        " && document.querySelectorAll('#limitations-list li').length === 2",
        timeout=5000,
    )
    portrait = page.locator(".self-portrait")
    assert portrait.count() == 1
    groups = page.locator(".self-portrait .portrait-group")
    assert groups.count() == 2
    headings = [h.strip() for h in page.locator(".self-portrait .portrait-group h4").all_text_contents()]
    assert headings == ["What I can do", "What I can't do"]


@pytest.mark.browser_context_args(timezone_id="UTC")
@pytest.mark.parametrize("awakened_at", ["2025-06-01T00:00:00Z", "2025-06-01T12:00:00Z"])
def test_awakened_since_renders_from_awakened_at(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
    awakened_at: str,
) -> None:
    """The journey strip footer renders the full human-readable awakened-since date.

    ``formatAwakenedSince`` extracts the date parts in UTC, so the day/month/year
    render deterministically across hosts; the context timezone is pinned to UTC
    to match.  Both a midnight and a non-midnight timestamp on the same UTC day
    must render identically, proving the assertion does not silently depend on
    midnight-boundary edge behaviour.
    """
    url, data = public_page_server
    data["public_identity"]["awakened_at"] = awakened_at
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => (document.getElementById('awakened-since').textContent || '').length > 0",
        timeout=5000,
    )
    text = (page.locator("#awakened-since").text_content() or "").strip()
    assert text == "Awakened Jun 1, 2025"


@pytest.mark.browser_context_args(timezone_id="America/New_York")
def test_awakened_since_stable_across_viewer_timezone(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """The awakened-since date renders in UTC regardless of the viewer's timezone.

    ``formatAwakenedSince`` derives the awakening date from the canonical UTC
    ``awakened_at`` instant, so a viewer west of UTC must see the same calendar
    day as a UTC viewer.  Here the context timezone is ``America/New_York``
    (UTC-04:00 in June), so a midnight-UTC ``awakened_at`` falls on the previous
    local day.  If the render read the local date parts it would show
    ``Awakened May 31, 2025``; reading them in UTC keeps it ``Awakened Jun 1,
    2025`` for every viewer.
    """
    url, data = public_page_server
    data["public_identity"]["awakened_at"] = "2025-06-01T00:00:00Z"
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => (document.getElementById('awakened-since').textContent || '').length > 0",
        timeout=5000,
    )
    text = (page.locator("#awakened-since").text_content() or "").strip()
    assert text == "Awakened Jun 1, 2025"


def test_phase_orb_defaults_to_idle(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """With a null current_phase the orb settles on the idle visual."""
    url, _data = public_page_server
    page.goto(url)
    page.wait_for_load_state("networkidle")
    orb = page.locator("#phase-orb")
    orb.wait_for(timeout=5000)
    page.wait_for_function(
        "() => document.getElementById('phase-orb').dataset.phase === 'idle'",
        timeout=5000,
    )
    assert orb.get_attribute("data-phase") == "idle"
    assert (page.locator("#phase-caption").text_content() or "").strip() == "Idle"


def test_phase_orb_reflects_known_phase(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """A known loop phase drives the orb's data-phase and caption."""
    url, data = public_page_server
    data["public_identity"]["current_phase"] = "dream"
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => document.getElementById('phase-orb').dataset.phase === 'dream'",
        timeout=5000,
    )
    orb = page.locator("#phase-orb")
    assert orb.get_attribute("data-phase") == "dream"
    assert "pattern-drift" in (orb.get_attribute("class") or "")
    assert (page.locator("#phase-caption").text_content() or "").strip() == "Dreaming"


def test_phase_orb_reports_budget_throttle(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """Budget throttle is surfaced as a distinct visual + caption."""
    url, data = public_page_server
    data["public_identity"]["current_phase"] = "budget_throttle"
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => document.getElementById('phase-orb').dataset.phase === 'budget_throttle'",
        timeout=5000,
    )
    orb = page.locator("#phase-orb")
    assert orb.get_attribute("data-phase") == "budget_throttle"
    assert "Throttling" in (page.locator("#phase-caption").text_content() or "")


def test_phase_orb_falls_back_to_idle_for_unknown_phase(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """An unrecognised phase string degrades gracefully to idle."""
    url, data = public_page_server
    data["public_identity"]["current_phase"] = "not_a_real_phase"
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => document.getElementById('phase-orb').dataset.phase === 'idle'",
        timeout=5000,
    )
    assert page.locator("#phase-orb").get_attribute("data-phase") == "idle"


def test_partner_section_names_thomas_without_contact_email(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """The Human partner block names Thomas and exposes no mailto link."""
    url, _data = public_page_server
    page.goto(url)
    page.wait_for_load_state("networkidle")
    section = page.locator("#partner-section")
    assert section.is_visible()
    assert "Human partner" in (section.locator(".section-title").text_content() or "")
    assert "Thomas Sodon" in (section.locator(".partner-line").text_content() or "")
    assert page.locator('a[href^="mailto:"]').count() == 0


def test_evolve_shimmer_preserves_orb_blur(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """During the evolve phase the orb-shimmer animation must not erase the
    orb blur on .orb-core.  Regression: the previous keyframes set
    ``filter: hue-rotate(...) saturate(...)`` with no blur, so CSS animation
    semantics replaced the static ``filter: blur(var(--orb-blur))`` for the
    entire animation, dropping the blur while shimmer ran. Independent of the
    (now-retired) stage ladder: blur is a fixed orb property.
    """
    url, data = public_page_server
    data["public_identity"]["current_phase"] = "evolve"
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => {"
        "  const orb = document.getElementById('phase-orb');"
        "  return orb.dataset.phase === 'evolve'"
        "      && orb.classList.contains('pattern-shimmer');"
        "}",
        timeout=5000,
    )
    # Sample the animated filter across one full shimmer cycle.  With the
    # bug, the filter never contains "blur" because the shimmer keyframes
    # replace the static filter property entirely.  With the fix, blur is
    # preserved in every keyframe so it appears at every sampled timepoint.
    samples = page.evaluate(
        "async () => {"
        "  const core = document.querySelector('#phase-orb .orb-core');"
        "  const out = [];"
        "  for (let i = 0; i < 6; i++) {"
        "    out.push(getComputedStyle(core).filter);"
        "    await new Promise(r => setTimeout(r, 120));"
        "  }"
        "  return out;"
        "}",
    )
    assert all("blur" in s for s in samples), (
        f"orb-shimmer animation erased the orb blur. Samples: {samples}"
    )
