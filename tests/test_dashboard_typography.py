"""Guard the typographic token pair, the text-size scale, and the no-webfont
contract for the operator console.

The dashboard now carries a two-face hierarchy: ``--font-ui`` (a ``system-ui``
sans stack) for headings, labels, navigation, prose, and controls, and
``--font-data`` (the existing monospace stack) reserved for the data it is good
at. Both are system stacks, so the surface loads *no* webfont — nothing blocks
first paint and there is no font-swap layout shift. This suite asserts, with a
regex source-scan (no subprocess, no network, no writes):

* ``tokens.css`` defines the ``--font-data`` / ``--font-ui`` pair and the full
  ``--text-*`` size scale.
* No page stylesheet or template loads a render-blocking external font. Because
  this increment ships only system stacks, the assertion reduces to "no
  external font is loaded," failing loudly if a later change links a webfont
  without ``font-display: swap``.

Reuses ``_defines_token`` and the static-dir constants from
``tests/test_dashboard_tokens.py`` so the definition detector has one owner.
"""

from __future__ import annotations

import re

from tests.test_dashboard_tokens import _STATIC_DIR, _TOKENS_CSS, _defines_token

_TEMPLATES_DIR = _STATIC_DIR.parent / "templates"
# tokens.css is linked first by every page, so a render-blocking @import or
# @font-face remote src added there would affect all three pages — the proposal
# names the token file as the isolated home of any future self-hosted face, so
# the guard must scan it, not just the per-page stylesheets.
_PAGE_STYLESHEETS = ("tokens.css", "index.css", "public.css", "login.css")
_TEMPLATES = ("index.html", "public.html", "login.html")

# The two typographic faces tokens.css must define.
_FONT_TOKENS = ("--font-data", "--font-ui")

# The full text-size scale seeded for the later type-hierarchy increment.
_TEXT_SCALE = (
    "--text-xs",
    "--text-sm",
    "--text-base",
    "--text-lg",
    "--text-xl",
    "--text-2xl",
)

# A remote URL: absolute (``https://``) or protocol-relative (``//host``).
_REMOTE_URL = re.compile(r"(?:https?:)?//[^\s\"')>]+")

# A font signal in a URL: a font host, or a webfont file extension.
_FONT_URL_SIGNAL = re.compile(
    r"fonts\.googleapis\.com|fonts\.gstatic\.com|use\.typekit|fontawesome"
    r"|\.woff2?(?:[?#]|$)|\.ttf(?:[?#]|$)|\.otf(?:[?#]|$)|\.eot(?:[?#]|$)",
    re.IGNORECASE,
)

# An @font-face rule block (rules do not nest, so a non-greedy brace pair is
# enough). Used to scope the swap exemption to the block it lives in.
_FONT_FACE_BLOCK = re.compile(r"@font-face\s*\{[^}]*\}", re.IGNORECASE)


def _swap_exempt_spans(source: str) -> list[tuple[int, int]]:
    """Char spans of ``@font-face`` blocks that declare ``font-display: swap``.

    A ``font-display: swap`` descriptor governs *only* the ``@font-face`` rule it
    lives in; it cannot make an unrelated ``<link>``/``@import`` font
    non-render-blocking. So the swap exemption applies only to a URL that sits
    inside such a block, never to the whole file.
    """
    spans: list[tuple[int, int]] = []
    for block in _FONT_FACE_BLOCK.finditer(source):
        if "font-display:swap" in re.sub(r"\s+", "", block.group(0)).lower():
            spans.append((block.start(), block.end()))
    return spans


def _render_blocking_font_urls(source: str) -> list[str]:
    """Return remote font URLs in ``source`` loaded without ``font-display: swap``.

    A URL is an external-font reference when it is remote (absolute or
    protocol-relative) *and* names a known font host or a webfont file. Such a
    reference is render-blocking unless it opts into swap — either
    ``display=swap`` in the URL's own query (the Google Fonts idiom) or the URL
    is the ``src`` of an ``@font-face`` block that itself declares
    ``font-display: swap`` (the self-hosted idiom). The swap exemption is scoped
    to the reference: a ``font-display: swap`` descriptor elsewhere in the file
    does *not* clear an unrelated blocking ``<link>``/``@import`` in the same
    source. System-stack ``font-family`` declarations reference no URL, so they
    never match; a local ``/static/*.css`` link is not remote (no ``//``), so it
    never matches either.
    """
    swap_spans = _swap_exempt_spans(source)
    hits: list[str] = []
    for match in _REMOTE_URL.finditer(source):
        url = match.group(0)
        if _FONT_URL_SIGNAL.search(url) is None:
            continue
        if "display=swap" in url.lower():
            continue
        if any(start <= match.start() < end for start, end in swap_spans):
            continue
        hits.append(url)
    return hits


def test_tokens_css_defines_font_face_pair() -> None:
    """``tokens.css`` defines both typographic faces."""
    css = _TOKENS_CSS.read_text(encoding="utf-8")
    missing = [name for name in _FONT_TOKENS if not _defines_token(css, name)]
    assert not missing, (
        "tokens.css must define the typographic face pair but is missing: "
        f"{missing}. The mono/sans hierarchy resolves --font-ui / --font-data "
        "off :root, so both must live in the shared token file."
    )


def test_tokens_css_defines_full_text_scale() -> None:
    """``tokens.css`` seeds the complete ``--text-*`` size scale."""
    css = _TOKENS_CSS.read_text(encoding="utf-8")
    missing = [name for name in _TEXT_SCALE if not _defines_token(css, name)]
    assert not missing, (
        "tokens.css must seed the full text-size scale but is missing: "
        f"{missing}. The scale is seeded here for the later type-hierarchy "
        "increment to consume."
    )


def test_font_data_reserves_the_monospace_stack() -> None:
    """``--font-data`` keeps the existing monospace face (a ``monospace`` stack)."""
    css = _TOKENS_CSS.read_text(encoding="utf-8")
    match = re.search(r"--font-data\s*:\s*([^;]+);", css)
    assert match is not None, "tokens.css must define --font-data with a value"
    assert "monospace" in match.group(1), (
        "--font-data must reserve the monospace face (its value should end in "
        f"the monospace generic family), got: {match.group(1)!r}"
    )


def test_font_ui_names_a_sans_system_stack() -> None:
    """``--font-ui`` is a ``system-ui`` sans stack, distinct from the data face."""
    css = _TOKENS_CSS.read_text(encoding="utf-8")
    match = re.search(r"--font-ui\s*:\s*([^;]+);", css)
    assert match is not None, "tokens.css must define --font-ui with a value"
    value = match.group(1)
    assert "system-ui" in value, (
        f"--font-ui must be a system-ui stack (no webfont), got: {value!r}"
    )
    assert "sans-serif" in value, (
        f"--font-ui must end in the sans-serif generic family, got: {value!r}"
    )
    assert "monospace" not in value, (
        "--font-ui must be distinct from the monospace data face"
    )


def test_no_page_loads_a_render_blocking_external_font() -> None:
    """No page stylesheet or template links a webfont without swap.

    This increment ships only system stacks, so the whole surface must load no
    external font. A later change that carelessly adds a Google Fonts link or an
    ``@font-face`` remote ``src`` without ``font-display: swap`` trips here.
    """
    offenders: dict[str, list[str]] = {}
    for name in _PAGE_STYLESHEETS:
        source = (_STATIC_DIR / name).read_text(encoding="utf-8")
        urls = _render_blocking_font_urls(source)
        if urls:
            offenders[name] = urls
    for name in _TEMPLATES:
        source = (_TEMPLATES_DIR / name).read_text(encoding="utf-8")
        urls = _render_blocking_font_urls(source)
        if urls:
            offenders[name] = urls
    assert not offenders, (
        "No page may load a render-blocking external font, but found: "
        f"{offenders}. Ship a system stack, or add font-display: swap if a "
        "bespoke face is ever wanted (a separate increment)."
    )


def test_render_blocking_font_detector_flags_external_fonts() -> None:
    """The detector catches the webfont idioms it is meant to guard against."""
    assert _render_blocking_font_urls(
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">'
    ) == ["https://fonts.googleapis.com/css2?family=Inter"]
    assert _render_blocking_font_urls(
        '@import url("https://cdn.example.com/inter.woff2");'
    ) == ["https://cdn.example.com/inter.woff2"]
    assert _render_blocking_font_urls(
        '<link rel="preconnect" href="//fonts.gstatic.com">'
    ) == ["//fonts.gstatic.com"]
    # An @font-face with a remote src but NO font-display: swap descriptor is
    # render-blocking — the swap exemption must not apply to it.
    assert _render_blocking_font_urls(
        "@font-face { src: url(https://cdn.example.com/inter.woff2); }"
    ) == ["https://cdn.example.com/inter.woff2"]


def test_render_blocking_font_detector_ignores_safe_references() -> None:
    """Local links, system stacks, and swap-guarded fonts are not flagged."""
    # A local stylesheet link is not remote (no ``//``).
    assert _render_blocking_font_urls('<link href="/static/tokens.css">') == []
    # A system-stack font-family declaration references no URL.
    assert (
        _render_blocking_font_urls(
            "body { font-family: system-ui, sans-serif; }"
        )
        == []
    )
    # A non-font remote asset (a script CDN) is not a font.
    assert _render_blocking_font_urls('<script src="//cdn.example.com/app.js">') == []
    # A Google Fonts link that opts into swap via the URL query is allowed.
    assert (
        _render_blocking_font_urls(
            '<link href="https://fonts.googleapis.com/css2?family=Inter&display=swap">'
        )
        == []
    )
    # A self-hosted @font-face carrying a font-display: swap descriptor is allowed.
    assert (
        _render_blocking_font_urls(
            "@font-face { src: url(https://cdn.example.com/inter.woff2);"
            " font-display: swap; }"
        )
        == []
    )


def test_swap_descriptor_scoped_to_its_font_face_block() -> None:
    """A file-global swap descriptor must not exempt an unrelated blocking link.

    A ``font-display: swap`` descriptor governs only the ``@font-face`` rule it
    lives in. A source that self-hosts one face *with* swap and *also* adds a
    blocking third-party font ``<link>`` must still flag that link — the swap
    exemption is scoped to the reference, not the whole file. Before the scope
    fix a file-global descriptor cleared every URL, so this exact regression
    slipped through.
    """
    source = (
        "@font-face { font-family: Local;"
        " src: url(https://cdn.example.com/local.woff2);"
        " font-display: swap; }\n"
        '<link rel="stylesheet"'
        ' href="https://fonts.googleapis.com/css2?family=Inter">'
    )
    # The swap-guarded @font-face src is exempt; the unrelated Google Fonts
    # <link>, which carries no swap of its own, is still flagged.
    assert _render_blocking_font_urls(source) == [
        "https://fonts.googleapis.com/css2?family=Inter"
    ]


def test_guard_scans_every_stylesheet_the_pages_link() -> None:
    """The webfont guard scans every ``/static/*.css`` the page templates link.

    ``tokens.css`` is linked first by index/public/login, so a render-blocking
    ``@import`` or ``@font-face`` remote ``src`` added there would affect all
    three pages — yet the guard would miss it unless tokens.css is scanned. The
    proposal names the token file as the isolated home of any future
    self-hosted face, so the guard's file set must be a superset of the
    stylesheets the templates actually load.
    """
    linked: set[str] = set()
    for name in _TEMPLATES:
        source = (_TEMPLATES_DIR / name).read_text(encoding="utf-8")
        linked.update(re.findall(r'href="/static/([^"]+\.css)"', source))
    missing = linked - set(_PAGE_STYLESHEETS)
    assert not missing, (
        "Every stylesheet the page templates link must be scanned by the "
        "no-render-blocking-font guard, but these are linked yet unscanned: "
        f"{sorted(missing)}."
    )
