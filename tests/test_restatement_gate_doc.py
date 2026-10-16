"""Static-text guard for the 'Restatement gate' invariant line in ``CLAUDE.md``.

The line used to say ``is_plan_restatement`` "extends it to ``pending_review``
plans", hiding that the plan duplicate / theme-cluster guards already compare
against the whole live-work corpus (proposal 100). The loop read that stale
line as authoritative and kept re-minting the shipped capability (#4581).

Static-text plus source inspection only: no ``mem_fs``/subprocess/network.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from maestro.memory import Memory

_CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"


def _restatement_gate_line() -> str:
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "**Restatement gate**" in line:
            return line
    raise AssertionError("No '**Restatement gate**' invariant line in CLAUDE.md")


def test_restatement_gate_line_names_live_work_corpus() -> None:
    line = _restatement_gate_line()
    assert "find_duplicate_pending_plan" in line
    assert "find_theme_cluster_pending_plans" in line
    assert "get_live_plan_records" in line
    assert "LIVE_STATUSES" in line
    assert "proposal 100" in line


def test_restatement_gate_line_does_not_claim_pending_review_only_corpus() -> None:
    line = _restatement_gate_line()
    assert "`is_plan_restatement` extends it to `pending_review` plans" not in line


def test_duplicate_guards_read_live_corpus() -> None:
    for method in (
        Memory.find_duplicate_pending_plan,
        Memory.find_theme_cluster_pending_plans,
    ):
        source = inspect.getsource(method)
        assert "self.get_live_plan_records()" in source
