"""Regression: review-process.md documents the reply-coverage gate.

``docs/review-process.md`` gained a *Reply-Coverage Gate* subsection under
Stage 1 (proposal 083 Step 5) so an operator or CI author can discover the
``--require-comment-replies`` flag, understand its check-only / default-off /
fail-closed / reviewer-findings-only semantics, and enable it. The
``maestro_review`` package guide names the same flag. This test pins that
documentation in place: a future edit cannot silently drop the gate from either
surface.

The acknowledgement sentinel is validated against
``maestro/evolution_tend.py::_OVERFLOW_ACK_SENTINEL`` — the constant the autonomous
loop writes and the reviewer's ``_parse_overflow_ack`` keys on — so the
documented "answered" rule cannot drift from the wording the parser reads.
"""

from __future__ import annotations

from pathlib import Path

from maestro.evolution_tend import _OVERFLOW_ACK_SENTINEL

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REVIEW_PROCESS_DOC = _REPO_ROOT / "docs" / "review-process.md"
_REVIEW_PACKAGE_GUIDE = _REPO_ROOT / "maestro_review" / "CLAUDE.md"

_REPLY_GATE_FLAG = "--require-comment-replies"


def test_review_process_doc_documents_reply_coverage_gate() -> None:
    doc = _REVIEW_PROCESS_DOC.read_text(encoding="utf-8")
    assert _REPLY_GATE_FLAG in doc, (
        "docs/review-process.md must name the --require-comment-replies flag so "
        "an operator can discover the reply-coverage gate."
    )
    assert "find_unreplied_comments" in doc, (
        "docs/review-process.md must cite find_unreplied_comments, the audit "
        "entry point the gate calls."
    )
    assert _OVERFLOW_ACK_SENTINEL in doc, (
        "docs/review-process.md must quote the canonical overflow-ack sentinel "
        f"{_OVERFLOW_ACK_SENTINEL!r} so the documented 'answered' rule matches "
        "the roll-up the parser reads."
    )


def test_review_package_guide_documents_reply_coverage_gate() -> None:
    guide = _REVIEW_PACKAGE_GUIDE.read_text(encoding="utf-8")
    assert _REPLY_GATE_FLAG in guide, (
        "maestro_review/CLAUDE.md must name the --require-comment-replies flag "
        "in its CLI Usage and module rows."
    )
    assert "find_unreplied_comments" in guide, (
        "maestro_review/CLAUDE.md must cite find_unreplied_comments so the "
        "package guide documents the audit entry point."
    )
