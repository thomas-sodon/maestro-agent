"""Regression: the account dashboard's reduced-motion guard stays exhaustive.

Proposal 122 step 8 replaced the narrow skeleton-only
``prefers-reduced-motion`` guard with one consolidated block that claims to
still *every* non-essential motion in ``index.css``. A prose claim rots: the
first ``transition:``/``animation:`` added to a new rule escapes the guard
silently, and the failure is invisible in review because it only manifests for
a user who has asked the OS to reduce motion — a population that never shows up
in a screenshot.

These tests turn the claim into an enforced invariant by parsing the stylesheet:
every selector that declares motion outside the guard must be named inside it,
and the guard must come *after* those rules in source order (``@media`` adds no
specificity, so at equal specificity only source order decides the override).

The unit of accounting is the ``(selector, kind)`` pair, not the bare selector:
``transition`` and ``animation`` are independent motion families, and the guard
stills them with separate declarations (``.toast`` appears in both). Keying on
the selector alone would let a selector named only in the block's
``transition: none`` rule count as guarded for a newly-added ``animation`` too —
exactly the drift these tests exist to catch.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path

_INDEX_CSS = (
    Path(__file__).resolve().parent.parent
    / "maestro" / "dashboard" / "static" / "index.css"
)

# A ``transition``/``animation`` shorthand or longhand declaration, captured as
# (motion family, value) — the family is what the guard must still per selector.
_MOTION_DECL = re.compile(r"\b(transition|animation)(?:-[a-z-]+)?\s*:\s*([^;}]*)")

# Values that declare no motion, so a rule carrying only these needs no guard —
# and, inside the guard, are what actually *stills* the family.
_INERT_VALUES = frozenset({"none", "0s", "0ms", "initial", "unset"})

# A (selector, motion family) pair, e.g. ('.toast', 'animation').
_Key = tuple[str, str]


def _strip_comments(css: str) -> str:
    """Return ``css`` with every ``/* ... */`` comment blanked out.

    Blanked rather than deleted (each comment becomes spaces of the same
    length) so byte offsets stay aligned with the original file — the
    source-order test compares offsets.
    """
    return re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), css, flags=re.DOTALL)


def _walk(css: str, base: int, in_reduce: bool) -> Iterator[tuple[str, str, int, bool]]:
    """Yield ``(prelude, body, offset, in_reduce)`` for every declaration block.

    At-rules recurse (so rules nested in ``@media`` are reached); ``offset`` is
    the block's start position in the original file.
    """
    i, n = 0, len(css)
    while i < n:
        brace = css.find("{", i)
        if brace == -1:
            return
        prelude = css[i:brace].strip()
        depth, j = 1, brace + 1
        while j < n and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        body = css[brace + 1:j - 1]
        if prelude.startswith("@"):
            reduce_here = in_reduce or "prefers-reduced-motion" in prelude
            yield from _walk(body, base + brace + 1, reduce_here)
        else:
            yield prelude, body, base + i, in_reduce
        i = j


def _selectors(prelude: str) -> list[str]:
    """Split a comma-separated prelude into normalised individual selectors."""
    return [" ".join(part.split()) for part in prelude.split(",") if part.strip()]


def _normalise(value: str) -> str:
    """Normalise a declaration value for the inert-vs-moving comparison.

    ``!important`` is stripped so a guard hardened to ``animation: none
    !important`` still reads as stilling the family rather than as an
    unrecognised — and therefore *moving* — value.
    """
    return value.replace("!important", "").strip().lower()


def _kinds(body: str, *, inert: bool) -> set[str]:
    """Return the motion families ``body`` declares, filtered by inertness.

    ``inert=False`` gives the families that actually move (what needs guarding);
    ``inert=True`` gives the families explicitly stilled (what the guard
    supplies).
    """
    return {
        family
        for family, value in _MOTION_DECL.findall(body)
        if (_normalise(value) in _INERT_VALUES) is inert
    }


def _fmt(keys: Iterable[_Key]) -> list[str]:
    """Render ``(selector, kind)`` pairs for an assertion message."""
    return sorted(f"{selector} {{{kind}}}" for selector, kind in keys)


def _parse_text(css: str) -> tuple[dict[_Key, int], set[_Key], int]:
    """Return (moving (selector, kind) -> offset, guarded pairs, guard offset)."""
    css = _strip_comments(css)
    motion: dict[_Key, int] = {}
    guarded: set[_Key] = set()
    guard_offset = -1
    for prelude, body, offset, in_reduce in _walk(css, 0, False):
        selectors = _selectors(prelude)
        if in_reduce:
            # Only a family the block actually stills counts as guarded: a rule
            # inside the block that re-declared motion would supply nothing.
            guarded.update(
                (sel, kind)
                for sel in selectors
                for kind in _kinds(body, inert=True)
            )
            guard_offset = offset if guard_offset == -1 else min(guard_offset, offset)
            continue
        for kind in _kinds(body, inert=False):
            for sel in selectors:
                # Keep the *latest* offset, not the first. A selector that
                # declares the same family in two places must be judged by its
                # last declaration: a second one placed after the guard is
                # exactly the drift
                # test_reduced_motion_block_follows_every_motion_rule exists to
                # catch, and remembering only the earliest would hide it behind
                # the compliant first declaration.
                motion[(sel, kind)] = max(motion.get((sel, kind), -1), offset)
    return motion, guarded, guard_offset


def _parse() -> tuple[dict[_Key, int], set[_Key], int]:
    """Parse the real ``index.css``."""
    return _parse_text(_INDEX_CSS.read_text(encoding="utf-8"))


def test_index_css_is_parsed_and_a_guard_exists() -> None:
    """Guard the guard: a parse that found nothing would pass vacuously."""
    motion, guarded, guard_offset = _parse()
    assert motion, "found no motion-declaring rules in index.css — parser is broken"
    assert guarded, "found no prefers-reduced-motion block in index.css"
    assert guard_offset > 0


def test_reduced_motion_block_covers_every_motion_selector() -> None:
    """Every selector declaring motion is stilled for the family it declares."""
    motion, guarded, _ = _parse()
    unguarded = _fmt(set(motion) - guarded)
    assert not unguarded, (
        "index.css declares motion the prefers-reduced-motion block does not "
        f"still: {unguarded}. Add each selector to the block's rule for that "
        "family at the end of index.css (proposal 122 step 8), or drop the "
        "transition/animation."
    )


def test_reduced_motion_block_follows_every_motion_rule() -> None:
    """The guard sits after the rules it overrides, so source order decides.

    ``@media`` contributes no specificity, so a reduced-motion rule only beats
    an equal-specificity base rule by appearing later in the file.
    """
    motion, _, guard_offset = _parse()
    too_late = _fmt(key for key, offset in motion.items() if offset > guard_offset)
    assert not too_late, (
        "these motion rules appear after the prefers-reduced-motion block, so "
        f"at equal specificity they win and the guard is inert: {too_late}. "
        "Keep the reduced-motion block last in index.css."
    )


def test_parse_tracks_the_latest_motion_declaration() -> None:
    """A selector declaring a family twice is judged by its *latest* declaration.

    Recording only the first offset would let a second declaration placed after
    the guard slip past ``test_reduced_motion_block_follows_every_motion_rule``,
    masked by a compliant earlier one — the precise drift that test exists to
    catch. The synthetic stylesheet here puts the guard between two ``transition``
    declarations of the same selector, so the offset must land after the guard.
    """
    css = (
        ".a { transition: color 0.2s; }\n"
        "@media (prefers-reduced-motion: reduce) { .a { transition: none; } }\n"
        ".a { transition: background 1s; }\n"
    )
    motion, guarded, guard_offset = _parse_text(css)
    assert guarded == {(".a", "transition")}
    assert motion[(".a", "transition")] > guard_offset


def test_guard_is_tracked_per_motion_kind_not_per_selector() -> None:
    """Stilling one family does not vouch for the other on the same selector.

    Keying ``guarded`` on the bare selector would let ``.a``'s ``animation``
    ride on the ``transition: none`` the block supplies, leaving a real
    unguarded animation invisible to
    ``test_reduced_motion_block_covers_every_motion_selector``.
    """
    css = (
        ".a { transition: color 0.2s; animation: spin 1s; }\n"
        "@media (prefers-reduced-motion: reduce) { .a { transition: none; } }\n"
    )
    motion, guarded, _ = _parse_text(css)
    assert guarded == {(".a", "transition")}
    assert set(motion) == {(".a", "transition"), (".a", "animation")}
    # The animation is the arm that escapes; the transition is genuinely stilled.
    assert set(motion) - guarded == {(".a", "animation")}


def test_important_on_a_guard_declaration_still_counts_as_stilled() -> None:
    """``animation: none !important`` stills the family like a bare ``none``.

    The inert-value check compares against a fixed vocabulary, so an
    ``!important`` appended to a guard declaration would otherwise read as an
    unrecognised — and therefore *moving* — value, failing the coverage test on
    a stylesheet that is in fact correct.
    """
    css = (
        ".a { animation: spin 1s; }\n"
        "@media (prefers-reduced-motion: reduce) "
        "{ .a { animation: none !important; } }\n"
    )
    motion, guarded, _ = _parse_text(css)
    assert guarded == {(".a", "animation")}
    assert not set(motion) - guarded
