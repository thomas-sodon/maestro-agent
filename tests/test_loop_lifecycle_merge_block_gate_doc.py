"""Pin the merge-block gate doc section to the live Tend-clearable set (#4169)."""

from pathlib import Path

from maestro.loop import pr_remediation

_DOC = Path(__file__).resolve().parent.parent / "docs" / "loop-lifecycle.md"
_SECTION_HEADER = "### Pre-Execution Merge-Block Gate (`_gate_pr_remediation_merge_block`)"
_STALE_CLAIMS = (
    "`BEHIND_MAIN` or `AWAITING_PUSH`",
    "Two deterministic blockers are Tend-PR-owned",
    "asks the operator to check why Tend PR has not pushed it",
    "`record_outcome(noop=True)`",
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


def test_merge_block_gate_section_matches_tend_clearable_set() -> None:
    section = _section_body(_DOC.read_text(encoding="utf-8"))
    assert "_tend_clearable_blockers" in section
    for blocker in pr_remediation._tend_clearable_blockers(tend_catch_up=True):
        assert blocker in section
    for stale in _STALE_CLAIMS:
        assert stale not in section
    assert "merge_block=True" in section
    assert "carries_comment_review_marker" in section


def test_fall_through_clause_names_no_tend_clearable_blocker() -> None:
    section = _section_body(_DOC.read_text(encoding="utf-8"))
    start = section.index("Every other outcome")
    clause = section[start:section.index("returns `False`", start)]
    assert "AWAITING_PUSH" in clause
    for blocker in pr_remediation._tend_clearable_blockers(tend_catch_up=True):
        assert blocker not in clause
