"""Pin the costume-signature gate doc section to the live marker set (#4084)."""

from pathlib import Path

import maestro.remediation_tracker as remediation_tracker

_DOC = Path(__file__).resolve().parent.parent / "docs" / "loop-lifecycle.md"
_SECTION_HEADER = "### Costume-Signature Gate (`_gate_pr_remediation_branch_resolved`)"
_CITED_TRACKER_SYMBOLS = (
    "_UNDELIVERED_PARTIAL_MARKERS",
    "_BRANCH_RESOLVED_MARKER",
    "_REMEDIATION_GOAL_PATTERN",
    "count_branch_resolved_partials",
)


def _section_body(doc: str) -> str:
    """Return the section's body text, from its header to the next heading."""
    start = doc.index(_SECTION_HEADER) + len(_SECTION_HEADER)
    end = doc.find("\n#", start)
    if end == -1:
        return doc[start:]
    return doc[start:end]


def test_section_body_runs_to_end_without_following_heading() -> None:
    assert _section_body(f"{_SECTION_HEADER}\nbody") == "\nbody"


def test_costume_gate_cites_live_marker_set() -> None:
    section = _section_body(_DOC.read_text(encoding="utf-8"))
    assert "_TEND_CLEARABLE_PARTIAL_MARKERS" not in section
    assert "AWAITING_PUSH/BEHIND_MAIN are skipped" not in section
    for symbol in _CITED_TRACKER_SYMBOLS:
        assert symbol in section
        assert hasattr(remediation_tracker, symbol)
