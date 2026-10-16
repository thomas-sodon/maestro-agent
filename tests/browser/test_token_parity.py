"""Runtime parity: all three pages resolve the design tokens identically.

The palette used to be triplicated in a private ``:root`` block in each of
``index.css`` / ``public.css`` / ``login.css``; it now lives once in
``tokens.css``, linked first by every page. This test proves the consolidation
holds *at runtime* in a real browser: for the authenticated console
(``/account``), the public intro page (``/``), and the login page
(``/login``), the full palette union read off ``document.documentElement``
resolves to the same expected values. A page that failed to link
``tokens.css`` — or re-introduced a per-page token definition that drifted —
would resolve a token to ``""`` or a different value and fail here.
"""

from __future__ import annotations

import json
from typing import Any

# The palette union across the three pages, with values byte-identical to the
# former per-page ``:root`` blocks. Because every page now sources
# ``tokens.css``, the full union resolves identically on all three.
_EXPECTED_PALETTE: dict[str, str] = {
    "--bg": "#0a0a0f",
    "--surface": "#12121a",
    "--surface-2": "#16161f",
    "--border": "#1e1e2e",
    "--text": "#e0e0e8",
    "--text-dim": "#8888a0",
    "--text-mute": "#5a5a72",
    "--accent": "#7c6ff0",
    "--accent-glow": "rgba(124,111,240,0.15)",
    "--accent-soft": "rgba(124,111,240,0.08)",
    "--success": "#4ade80",
    "--warning": "#fbbf24",
    "--danger": "#f87171",
    "--info": "#60a5fa",
}


def _read_palette(page: Any) -> dict[str, str]:
    """Return the resolved value of every palette token off :root.

    Mirrors the ``getComputedStyle(...)`` idiom from ``test_mobile_layout.py``,
    reading each custom property with ``.trim()`` (custom-property values are
    returned as authored, only whitespace-trimmed).
    """
    names = list(_EXPECTED_PALETTE)
    script = (
        "((names) => {"
        " const cs = getComputedStyle(document.documentElement);"
        " const out = {};"
        " for (const n of names) out[n] = cs.getPropertyValue(n).trim();"
        " return out;"
        "})(" + json.dumps(names) + ")"
    )
    result: dict[str, str] = page.evaluate(script)
    return result


def _assert_palette(page: Any) -> None:
    """Assert every palette token resolves to its expected value."""
    actual = _read_palette(page)
    for name, expected in _EXPECTED_PALETTE.items():
        assert actual.get(name) == expected, (
            f"token {name} resolved to {actual.get(name)!r}, expected "
            f"{expected!r} — the page likely failed to link tokens.css or "
            "re-defined the token locally"
        )


def test_account_page_token_parity(
    dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The authenticated console at /account resolves the full palette union."""
    page, *_ = dashboard_page
    _assert_palette(page)


def test_public_page_token_parity(
    page: Any,
    public_page_server: tuple[str, dict[str, Any]],
) -> None:
    """The public intro page at / resolves the full palette union."""
    url, _data = public_page_server
    page.goto(f"{url}/")
    page.wait_for_load_state("networkidle")
    _assert_palette(page)


def test_login_page_token_parity(
    page: Any,
    auth_dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]], str],
) -> None:
    """The login page at /login resolves the full palette union."""
    url, *_ = auth_dashboard_server
    page.goto(f"{url}/login")
    page.wait_for_load_state("networkidle")
    _assert_palette(page)
