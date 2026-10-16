"""Regression: process-comments.md lists every terminal marker.

The *Filter to eligible threads* step in `.claude/commands/process-comments.md`
tells an LLM to skip a thread whose reply already carries a terminal marker,
and enumerates the markers inline as examples. The doc itself names
``maestro/evolution_tend.py::_BUG_COMMENT_TERMINAL_MARKERS`` as the canonical set, so
this test validates against that source. If the inline list drifts from it the
doc omits a marker the autonomous path treats as terminal — most importantly
"Fixed locally with implementation-plan context.", the resolution reply written
on a successful fix. An LLM following the truncated inline examples (rather than
the canonical set) would fail to recognise an already-fixed thread as terminal
and re-process it.

This test guards that the inline marker list stays faithful to the canonical
source: every entry of ``_BUG_COMMENT_TERMINAL_MARKERS`` must appear in the
filter bullet.
"""

from __future__ import annotations

from pathlib import Path

from maestro.evolution_tend import (
    _BUG_COMMENT_TERMINAL_MARKERS,
    _OVERFLOW_ACK_SENTINEL,
    _OVERFLOW_LEGACY_HEADER,
    _OVERFLOW_SENTINEL_CLOSE,
    _OVERFLOW_SENTINEL_OPEN,
)

_PROCESS_COMMENTS_DOC = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "commands"
    / "process-comments.md"
)


def _line_containing(text: str, needle: str) -> str:
    matches = [line for line in text.splitlines() if needle in line]
    assert len(matches) == 1, (
        f"Expected exactly one line containing {needle!r}, found {len(matches)}."
    )
    return matches[0]


def test_filter_bullet_lists_every_terminal_marker() -> None:
    doc = _PROCESS_COMMENTS_DOC.read_text(encoding="utf-8")

    bullet = _line_containing(doc, "carries a terminal marker")
    for marker in _BUG_COMMENT_TERMINAL_MARKERS:
        assert marker in bullet, (
            f"The eligible-thread filter bullet must list the terminal marker "
            f"{marker!r} so an implementer skips already-processed threads "
            f"without consulting the code. Bullet:\n{bullet}"
        )


def test_push_step_requires_rerunning_gates_after_upstream_merge() -> None:
    """The step-3 merge must re-run quality gates before pushing.

    The *Commit, push, and reply* step fetches and merges upstream commits
    (possibly a merge commit or a conflict resolution) before pushing. If the
    instructions push without re-validating the merged tree, an upstream change
    that interacts with the fixes — or a bad conflict resolution — lands on the
    PR branch and then receives a ``Fixed locally …`` terminal reply, leaving a
    broken-but-marked-fixed thread. The CI processor's ``_push_with_rebase``
    closes exactly this gap (it re-runs full quality gates whenever the rebase
    changed the workspace), and ``/merge-main`` does the same. Guard that the
    command keeps the same re-run-after-merge instruction so it cannot drift
    back to push-without-revalidation.
    """
    doc = _PROCESS_COMMENTS_DOC.read_text(encoding="utf-8")

    para = _line_containing(doc, "the merge brought in new upstream commits")
    lowered = para.lower()
    assert "re-run the quality gates" in lowered, (
        "The step-3 merge instruction must tell the implementer to re-run the "
        f"quality gates over the merged tree before pushing. Paragraph:\n{para}"
    )
    assert "before pushing" in lowered, (
        "The re-run must happen before the push so a broken merge aborts the "
        f"push instead of landing on the branch. Paragraph:\n{para}"
    )
    assert "push failure" in lowered or "abort" in lowered, (
        "The step-3 merge instruction must tell the implementer to treat "
        "post-merge gate failures as a push failure and abort the step-4 "
        f"replies, not merely warn. Paragraph:\n{para}"
    )


# Tokens proposal 063 retires from the command: the standalone comment
# processor's module, prompt module, CLI script, "CI agent" framing, and the
# processor-only symbols the command used to cite. Once these are gone the
# command must repoint at surviving symbols (``maestro_review/markers.py``,
# ``maestro/evolution.py`` classifiers, the inlined GraphQL shape). Guard that
# the prose does not drift back to citing internals that will cease to exist.
_RETIRED_PROCESSOR_REFERENCES = (
    "processor.py",
    "processor_prompt",
    "maestro-process-comments",
    "the CI agent",
    "CommentProcessor",
    "_push_with_rebase",
    "VALIDATION_PROMPT",
    "FIX_PROMPT",
    "FINAL_GATE_FIX_PROMPT",
    "_AI_COMMENT_RE",
    "_EMOJI_SEVERITY",
)


def test_command_doc_cites_no_retired_processor_internals() -> None:
    doc = _PROCESS_COMMENTS_DOC.read_text(encoding="utf-8")
    lowered = doc.lower()
    for token in _RETIRED_PROCESSOR_REFERENCES:
        assert token.lower() not in lowered, (
            f"`process-comments.md` must not reference the retired standalone "
            f"comment processor; found {token!r}. Repoint it at a surviving "
            f"symbol (markers.py, evolution.py classifiers, or the inlined "
            f"GraphQL query) instead."
        )


def test_overflow_step_cites_canonical_sentinels() -> None:
    """The overflow instructions must quote the exact sentinel strings the
    reviewer emits and the parser keys on (proposal 064 Step 4). An LLM
    following a paraphrased sentinel would fail to locate the block or would
    write an acknowledgement comment the autonomous parser cannot read."""
    doc = _PROCESS_COMMENTS_DOC.read_text(encoding="utf-8")
    for sentinel in (
        _OVERFLOW_SENTINEL_OPEN,
        _OVERFLOW_SENTINEL_CLOSE,
        _OVERFLOW_ACK_SENTINEL,
        _OVERFLOW_LEGACY_HEADER,
    ):
        assert sentinel in doc, (
            f"`process-comments.md` must quote the canonical overflow "
            f"sentinel {sentinel!r} so its output stays parseable by "
            f"`maestro/evolution_tend.py::_eligible_overflow_bug_findings`."
        )


def test_overflow_step_documents_fingerprint_keyed_acknowledgement() -> None:
    """The command must document the body-fetch, the fingerprint-keyed
    acknowledgement upsert, and the report line (proposal 064 Step 4
    acceptance)."""
    doc = _PROCESS_COMMENTS_DOC.read_text(encoding="utf-8")
    # Body fetch for the overflow block.
    assert "--json reviews" in doc
    # Fingerprint handle shape on the parsed line and the acknowledgement.
    assert "of-<hash8>" in doc
    assert "_eligible_overflow_bug_findings" in doc
    # Acknowledgement lines reuse the canonical marker wording, append-only.
    assert "of-<hash> — Fixed locally with implementation-plan context." in doc
    assert "of-<hash> — Could not prove this defect" in doc
    assert "only append" in doc
    # Report section gained the overflow line.
    assert "**Overflow findings**" in doc
