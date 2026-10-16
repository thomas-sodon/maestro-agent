"""Static-text guard for the reviewer-feedback routing invariant (proposal 145).

The **Bidirectional messaging** line in ``CLAUDE.md`` states that a Thomas
``plan_feedback`` message is folded into a plan's ``reviewer_feedback`` ledger
and never enqueued as a user request. A change that misses this routing could
re-enqueue feedback as a context-free task, or let it pile up in a ledger
nothing services. This guard pins the line, its anchor into
``docs/self-evolution.md``, and the symbols the subsection cites — each checked
importable so a rename breaks the test rather than leaving the doc stale.

Static-text only: reads ``CLAUDE.md`` and ``docs/self-evolution.md``, no
``mem_fs``/subprocess/network/sleep.
"""

from __future__ import annotations

from pathlib import Path

from maestro import plan_feedback, plan_feedback_fold
from maestro.memory import Memory

_ROOT = Path(__file__).resolve().parent.parent
_CLAUDE_MD = _ROOT / "CLAUDE.md"
_SELF_EVOLUTION = _ROOT / "docs" / "self-evolution.md"
_ANCHOR = "reviewer-feedback-on-pending-plans"
_HEADING = "### Reviewer feedback on pending plans"


def _messaging_line() -> str:
    for line in _CLAUDE_MD.read_text(encoding="utf-8").splitlines():
        if "**Bidirectional messaging**" in line:
            return line
    raise AssertionError("No '**Bidirectional messaging**' invariant line in CLAUDE.md")


def _subsection() -> str:
    text = _SELF_EVOLUTION.read_text(encoding="utf-8")
    assert _HEADING in text, f"Missing heading {_HEADING!r} in docs/self-evolution.md"
    body = text.split(_HEADING, 1)[1]
    return body.split("\n### ", 1)[0]


def test_messaging_line_states_feedback_routing() -> None:
    line = _messaging_line()
    assert "`plan_feedback`" in line
    assert "`reviewer_feedback`" in line
    assert "never enqueued" in line
    assert f"docs/self-evolution.md#{_ANCHOR}" in line


def test_anchor_resolves_to_subsection_heading() -> None:
    text = _SELF_EVOLUTION.read_text(encoding="utf-8")
    headings = [ln for ln in text.splitlines() if ln.startswith("### ")]
    slugs = [h[4:].strip().lower().replace(" ", "-") for h in headings]
    assert _ANCHOR in slugs


def test_subsection_cites_live_symbols() -> None:
    body = _subsection()
    assert "fold_feedback_messages" in body
    assert "add_plan_feedback" in body
    assert "MAX_FEEDBACK_CURATE_ATTEMPTS" in body
    assert callable(plan_feedback_fold.fold_feedback_messages)
    assert callable(Memory.add_plan_feedback)
    assert plan_feedback.MAX_FEEDBACK_CURATE_ATTEMPTS > 0
