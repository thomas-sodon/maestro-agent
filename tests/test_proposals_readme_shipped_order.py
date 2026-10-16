"""Regression: the **Shipped** table in ``docs/proposals/README.md`` must list
proposals in strictly ascending proposal-number order.

The Shipped table is the landing record, maintained in ascending
proposal-number order (…, 112, 117, 119). PR #3590 review flagged a row placed
out of sequence (…112, 119, 117 — 119 rendered before 117), inconsistent with
the table's own ordering convention. This deterministic, zero-LLM-cost
doc-lint — the same family as ``tests/test_no_line_citations_in_docs.py`` and
``tests/test_proposals_readme_order_numbering.py`` — converts that
previously-silent ordering drift into a loud CI failure, so a future misplaced
row fails the gate instead of slipping in unnoticed.
"""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

_README = (
    Path(__file__).resolve().parent.parent / "docs" / "proposals" / "README.md"
)
_SHIPPED_HEADING = "## Shipped"
_NEXT_HEADING = "## Open Proposals"

# A Shipped-table data row: ``| 119 | Title | Landed as |``. The (zero-padded)
# proposal number is the first column. ``re.MULTILINE`` ``^`` anchors to line
# start, so prose citing ``#NNN`` mid-line never matches — only table rows do.
_SHIPPED_ROW = re.compile(r"^\| (\d+) \|", re.MULTILINE)


def _shipped_section(text: str) -> str:
    """Slice the Shipped-table region, from its heading to the next section.

    ``## Shipped`` is immediately followed (after the table) by
    ``## Open Proposals``, so the slice between the two headings is exactly the
    Shipped table plus its trailing note.
    """
    start = text.index(_SHIPPED_HEADING)
    end = text.index(_NEXT_HEADING, start)
    return text[start:end]


def _shipped_numbers(text: str) -> list[int]:
    """Return the Shipped-table proposal numbers in document order."""
    return [int(m) for m in _SHIPPED_ROW.findall(text)]


def test_shipped_table_is_strictly_ascending() -> None:
    """The Shipped table's proposal numbers must strictly ascend (no holes are
    required, but each row must be greater than the one above it).

    Against the pre-fix tree the sequence ended ``…, 112, 119, 117`` and failed,
    pinning the misordering the PR #3590 review flagged.
    """
    section = _shipped_section(_README.read_text(encoding="utf-8"))
    nums = _shipped_numbers(section)
    # Non-vacuous guard: if the table or its row format ever disappears the
    # ordering assertion below would pass on an empty list, validating nothing.
    # The landing record has dozens of rows, so an empty collection is a bug.
    assert nums, (
        f"Found no '| NNN |' rows in the {_SHIPPED_HEADING!r} section of "
        f"{_README}; the table or its row format changed, so this test would "
        "otherwise pass vacuously."
    )
    ascending = all(earlier < later for earlier, later in pairwise(nums))
    assert ascending, (
        "The Shipped table is not in strictly ascending proposal-number order: "
        f"got {nums}. The table is the landing record and is maintained in "
        "ascending order; move the out-of-order row so each number is greater "
        "than the one above it."
    )


def test_shipped_row_detector_ignores_prose_and_fires_on_disorder() -> None:
    """Negative control: a broken collector (e.g. a regex matching nothing, or
    one that swept up prose ``#NNN`` refs) would make the ordering assertion
    pass vacuously or spuriously, so pin the detector directly."""
    prose = "See proposal #119 and #117 for details.\n"
    assert _shipped_numbers(prose) == []
    disordered = "| 112 | a | b |\n| 119 | c | d |\n| 117 | e | f |\n"
    nums = _shipped_numbers(disordered)
    assert nums == [112, 119, 117]
    ascending = all(earlier < later for earlier, later in pairwise(nums))
    assert ascending is False
