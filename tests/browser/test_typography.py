"""Runtime proof that the mono/sans hierarchy lands on the right nodes.

The token guard (``tests/test_dashboard_typography.py``) proves ``tokens.css``
*defines* the ``--font-ui`` / ``--font-data`` pair; this browser test proves the
two faces actually *resolve* onto the correct nodes in a real browser: a heading
and a card label compute to the sans UI face, while a log row computes to the
monospace data face. Mirrors the ``getComputedStyle(...)`` idiom used by
``test_token_parity.py`` and ``test_mobile_layout.py``.
"""

from __future__ import annotations

from typing import Any

from tests.browser.conftest import make_log_entry


def _root_font_tokens(page: Any) -> dict[str, str]:
    """Return the resolved ``--font-ui`` / ``--font-data`` values off :root."""
    return page.evaluate(
        "(() => {"
        " const cs = getComputedStyle(document.documentElement);"
        " return {"
        "  ui: cs.getPropertyValue('--font-ui').trim(),"
        "  data: cs.getPropertyValue('--font-data').trim(),"
        " }; })()"
    )


def _computed_family(page: Any, selector: str) -> str:
    """Return the computed ``font-family`` string of the first ``selector`` node."""
    family: str = page.evaluate(
        "sel => getComputedStyle(document.querySelector(sel)).fontFamily",
        selector,
    )
    return family


def test_font_tokens_resolve_to_distinct_faces(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """Both faces resolve off :root, are distinct, and name a sans vs a mono."""
    page, *_ = dashboard_page
    tokens = _root_font_tokens(page)
    assert tokens["ui"], "--font-ui must resolve to a non-empty stack"
    assert tokens["data"], "--font-data must resolve to a non-empty stack"
    assert tokens["ui"] != tokens["data"], (
        "the UI and data faces must be distinct so the hierarchy reads; got "
        f"identical value {tokens['ui']!r}"
    )
    assert "system-ui" in tokens["ui"], (
        f"--font-ui must be a system-ui sans stack, got {tokens['ui']!r}"
    )
    assert "monospace" in tokens["data"], (
        f"--font-data must name the monospace family, got {tokens['data']!r}"
    )


def test_heading_and_label_compute_to_the_sans_face(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A heading (h1) and a card label (.card-title) resolve to the sans face."""
    page, *_ = dashboard_page
    for selector in ("h1", ".card-title"):
        family = _computed_family(page, selector).lower()
        assert "sans-serif" in family, (
            f"{selector} must compute to the sans UI face (--font-ui), got "
            f"{family!r}"
        )
        assert "monospace" not in family, (
            f"{selector} must not fall back to the monospace data face, got "
            f"{family!r}"
        )


def test_log_entry_computes_to_the_mono_face(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """A data node (.log-entry) stays on the monospace data face."""
    page, _, data, _ = dashboard_page
    data["logs"].append(make_log_entry("a data row that must stay monospace"))
    page.evaluate("refresh()")
    page.wait_for_selector("#logs .log-entry")

    family = _computed_family(page, "#logs .log-entry").lower()
    assert "monospace" in family, (
        "a .log-entry is data and must compute to the monospace data face "
        f"(--font-data), got {family!r}"
    )
    assert "sans-serif" not in family, (
        f".log-entry must not resolve to the sans UI face, got {family!r}"
    )
