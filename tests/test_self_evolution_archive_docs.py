"""Tests for the tiered-memory archive paragraph in docs/self-evolution.md.

Issue #4081: the "Dreaming for Context Compression" deep-dive still said
``evolution_plans.jsonl`` "is rotated by timestamp regardless of plan status"
long after ``Memory.archive`` became status-scoped (``_plan_row_archivable``), so
an operator judging whether enabling ``archive_threshold_days`` is safe was told
the opposite of what the code does. These guards bind the paragraph to
``ARCHIVABLE_PLAN_STATUSES`` so a widened status set forces a doc update.

Like ``test_deployment_gate_lease_docs.py``, these are content assertions over
committed repo source (no subprocess, network, or sleep), so the
``mem_fs``/mock-externals rule does not apply.
"""

from __future__ import annotations

from pathlib import Path

from maestro.memory import ARCHIVABLE_PLAN_STATUSES

_ROOT = Path(__file__).resolve().parent.parent
_SELF_EVOLUTION_DOC = _ROOT / "docs" / "self-evolution.md"


def _archive_paragraph() -> str:
    text = _SELF_EVOLUTION_DOC.read_text(encoding="utf-8")
    paragraphs = [p for p in text.split("\n\n") if "Memory.archive(" in p]
    assert len(paragraphs) == 1
    return paragraphs[0]


def test_archive_paragraph_does_not_claim_status_blind_rotation() -> None:
    assert "regardless of plan status" not in _archive_paragraph()


def test_archive_paragraph_names_status_scope_owners() -> None:
    paragraph = _archive_paragraph()
    assert "ARCHIVABLE_PLAN_STATUSES" in paragraph
    assert "FINISHED_PROPOSAL_STATUSES" in paragraph
    assert "retry_budget" in paragraph


def test_archive_paragraph_lists_every_archivable_status() -> None:
    paragraph = _archive_paragraph()
    missing = sorted(s for s in ARCHIVABLE_PLAN_STATUSES if f"`{s}`" not in paragraph)
    assert missing == []
