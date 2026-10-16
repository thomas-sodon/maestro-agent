"""Regression: the "Recommended Implementation Order" priority key in
``docs/proposals/README.md`` must run ``1..N`` with no gaps.

The list uses explicit leading numbers as a *priority key* across the main
list and the per-track sub-lists that follow. Because each track sits under
its own ``###`` heading, GitHub-flavored Markdown renders each sub-list
starting at its explicit leading number rather than auto-collapsing the
sequence, so a hole in the numbering (e.g. ``14.`` -> ``16.``, skipping 15) is
visibly rendered in the priority key. Issue #1662 flagged exactly this: after
the artifact-review / publication track was collapsed the key skipped 15 and
27-29.

This doc-lint test converts that previously-silent numbering drift into a loud
CI failure — the same deterministic zero-LLM-cost family as
``tests/test_no_line_citations_in_docs.py`` — so any future appended track that
reintroduces a hole fails the gate instead of slipping in unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

_README = (
    Path(__file__).resolve().parent.parent / "docs" / "proposals" / "README.md"
)
_HEADING = "## Recommended Implementation Order"

# A top-level ordered-list marker at column 0: ``1. ``, ``16. ``, etc. The
# ``re.MULTILINE`` ``^`` anchors to line start, so indented markers and prose
# beginning mid-line do not match; ``###`` sub-headings start with ``#`` and so
# never match either.
_ORDER_MARKER = re.compile(r"^(\d+)\. ", re.MULTILINE)


def _order_numbers(text: str) -> list[int]:
    """Return the leading ordered-list numbers in document order."""
    return [int(m) for m in _ORDER_MARKER.findall(text)]


def _recommended_order_section(text: str) -> str:
    """Slice from the heading to end-of-file.

    ``## Recommended Implementation Order`` is the last ``##`` section in the
    file, so everything from the heading onward is the priority key.
    """
    return text[text.index(_HEADING) :]


def test_recommended_implementation_order_numbering_is_contiguous() -> None:
    """The priority key must be a contiguous run starting at 1.

    Against the pre-fix tree this collected ``[1..14, 16..26, 30..39]`` (holes
    at 15 and 27-29) and failed, pinning the bad state before the renumber.
    """
    section = _recommended_order_section(_README.read_text(encoding="utf-8"))
    nums = _order_numbers(section)
    # Non-vacuous guard: if the section or its markers ever disappear the
    # contiguity assertion below would pass on an empty list, validating
    # nothing. The list has dozens of items, so an empty collection is a bug.
    assert nums, (
        "Found no ordered-list markers under "
        f"{_HEADING!r} in {_README}; the section or its numbering was removed "
        "or restructured, so this test would otherwise pass vacuously."
    )
    assert nums == list(range(1, len(nums) + 1)), (
        "The Recommended Implementation Order priority key is not a contiguous "
        f"1..{len(nums)} run: got {nums}. A hole means a track sub-list's "
        "leading NN. markers skip a number (GitHub renders each ### sub-list "
        "from its explicit start), so the key no longer reads as one ordering. "
        "Renumber the leading NN. markers to close the gap (the #0NN proposal "
        "IDs and prose are unaffected)."
    )


def test_order_number_detector_fires_on_seeded_gap() -> None:
    """Negative control: a seeded gap must be collected as a gap.

    Without this, a collector regression (e.g. a broken regex matching
    nothing) would make the contiguity assertion above pass vacuously.
    """
    text = "1. first\n2. second\n4. fourth\n"
    nums = _order_numbers(text)
    assert nums == [1, 2, 4]
    assert nums != list(range(1, len(nums) + 1))


def test_order_number_detector_ignores_indented_and_prose() -> None:
    """Indented markers and prose beginning mid-line must not be collected."""
    text = "For maximum impact:\n  3. indented\nsee 9. inline\n### 5. heading\n"
    assert _order_numbers(text) == []
