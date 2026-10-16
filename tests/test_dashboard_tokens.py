"""Static single-source guard for the dashboard design tokens.

The palette (``--bg``, ``--surface`` … ``--info``) used to be triplicated in a
private ``:root`` block in each of ``index.css`` / ``public.css`` /
``login.css``. Those copies had already drifted in *coverage* and would have
drifted in *value* the first time one file was edited without the others.
Proposal 122's first increment moved every palette definition into a single
shared ``tokens.css`` linked first by all three pages.

This fast unit guard locks that consolidation in without needing the browser
suite: it asserts (a) ``tokens.css`` defines every palette token, and (b) none
of the three page stylesheets still *defines* a palette token — so a future
re-introduction of a per-page token definition fails loudly here rather than
only in the slower browser parity test. The check is scoped to the design-token
names, so the legitimate page-local ``--orb-*`` animation vars in ``public.css``
are not swept in.

Mirrors the ``Path.read_text()`` + regex source-scan style of
``tests/test_no_line_citations_in_docs.py`` — no subprocess, no network, no
writes.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC_DIR = (
    Path(__file__).resolve().parent.parent / "maestro" / "dashboard" / "static"
)
_TOKENS_CSS = _STATIC_DIR / "tokens.css"
_PAGE_STYLESHEETS = ("index.css", "public.css", "login.css")

# The palette union that ``tokens.css`` now solely owns.
_PALETTE_TOKENS = (
    "--bg",
    "--surface",
    "--surface-2",
    "--border",
    "--text",
    "--text-dim",
    "--text-mute",
    "--accent",
    "--accent-glow",
    "--accent-soft",
    "--success",
    "--warning",
    "--danger",
    "--info",
)


def _defines_token(css: str, name: str) -> bool:
    """True if ``css`` *defines* custom property ``name`` (``--name: value``).

    A definition is the token name followed by optional whitespace and a colon
    (``--bg:``). A *usage* (``var(--bg)``) has ``)`` after the name, not ``:``,
    so it does not match. The ``\\s*:`` anchor also disambiguates prefix names:
    ``--text`` does not match ``--text-dim:`` (a ``-`` follows ``--text``, not a
    colon), and ``--accent`` does not match ``--accent-glow:``.
    """
    return re.search(rf"{re.escape(name)}\s*:", css) is not None


def test_tokens_css_defines_every_palette_token() -> None:
    """``tokens.css`` is the single definition site for the full palette."""
    assert _TOKENS_CSS.is_file(), (
        f"Expected {_TOKENS_CSS} to exist; tokens.css is the single source of "
        "the design tokens (proposal 122)."
    )
    css = _TOKENS_CSS.read_text(encoding="utf-8")
    missing = [name for name in _PALETTE_TOKENS if not _defines_token(css, name)]
    assert not missing, (
        "tokens.css must define every palette token but is missing: "
        + ", ".join(missing)
    )


def test_page_stylesheets_do_not_redefine_palette_tokens() -> None:
    """No page stylesheet may re-define a palette token — tokens.css owns them.

    A per-page re-definition is exactly the drift this increment removed; if one
    reappears (in a ``:root`` block or anywhere else), this fails loudly in fast
    unit CI rather than only in the browser parity suite.
    """
    offenders: list[tuple[str, list[str]]] = []
    for filename in _PAGE_STYLESHEETS:
        path = _STATIC_DIR / filename
        assert path.is_file(), (
            f"Expected {path} to exist; the page stylesheet was moved or "
            "renamed, so this guard would not validate anything."
        )
        css = path.read_text(encoding="utf-8")
        redefined = [name for name in _PALETTE_TOKENS if _defines_token(css, name)]
        if redefined:
            offenders.append((filename, redefined))
    assert not offenders, (
        "Page stylesheets must not re-define palette tokens (tokens.css is the "
        "single source): "
        + "; ".join(f"{name}: {tokens}" for name, tokens in offenders)
    )


def test_orb_vars_remain_page_local_in_public_css() -> None:
    """The ``--orb-*`` animation vars stay page-local in public.css.

    They are a separate, page-local family — not palette tokens — so the guard
    above must not sweep them into tokens.css, and this increment must leave
    them where they are.
    """
    public_css = (_STATIC_DIR / "public.css").read_text(encoding="utf-8")
    assert _defines_token(public_css, "--orb-hue"), (
        "public.css should still define the page-local --orb-hue animation var; "
        "the palette-single-source move must not touch the --orb-* family."
    )
    tokens_css = _TOKENS_CSS.read_text(encoding="utf-8")
    assert not _defines_token(tokens_css, "--orb-hue"), (
        "tokens.css owns only the palette; the page-local --orb-* vars belong "
        "in public.css, not the shared token file."
    )


def test_definition_detector_distinguishes_definition_from_usage() -> None:
    """Negative control: the detector matches a definition, not a var() usage."""
    assert _defines_token("  --bg: #0a0a0f;", "--bg") is True
    assert _defines_token("background: var(--bg);", "--bg") is False


def test_definition_detector_disambiguates_prefix_tokens() -> None:
    """Negative control: ``--text`` must not match ``--text-dim:`` and vice versa."""
    assert _defines_token("--text-dim: #8888a0;", "--text") is False
    assert _defines_token("--text-dim: #8888a0;", "--text-dim") is True
    assert _defines_token("--accent-glow: rgba(0,0,0,0.1);", "--accent") is False
    assert _defines_token("--accent-glow: rgba(0,0,0,0.1);", "--accent-glow") is True
