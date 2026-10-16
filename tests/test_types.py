"""Tests for the canonical type definitions in maestro.types."""

from __future__ import annotations

import re
from typing import get_args

import pytest

from maestro.types import (
    ACCEPTANCE_CRITERIA_MAX_ITEMS,
    VALID_PRIORITIES,
    Category,
    JournalEntryType,
    PlanStatus,
    Priority,
    ProposalStatus,
    StepStatus,
    TaskOutcome,
    Verdict,
    validate_acceptance_criteria_count,
)


def test_valid_priorities_derived_from_literal() -> None:
    assert frozenset(get_args(Priority)) == VALID_PRIORITIES


def test_valid_priorities_members() -> None:
    assert {
        "task_followup", "capability", "resilience", "experiment", "maintenance",
    } == VALID_PRIORITIES


def test_all_type_aliases_are_literals() -> None:
    for alias in (
        Priority, Category, PlanStatus,
        StepStatus, TaskOutcome, JournalEntryType,
        Verdict, ProposalStatus,
    ):
        args = get_args(alias)
        assert len(args) > 0, f"{alias} has no members"
        assert all(isinstance(a, str) for a in args), f"{alias} has non-string members"


def test_verdict_members() -> None:
    assert set(get_args(Verdict)) == {"publish", "improve", "reuse", "skip"}


def test_proposal_status_members() -> None:
    assert set(get_args(ProposalStatus)) == {
        "pending_approval",
        "approved",
        "rejected",
        "executing",
        "published",
        "failed",
    }


def test_validate_acceptance_criteria_count_accepts_at_cap_rejects_cap_plus_one() -> None:
    """The cap is 10 (#4504): an at-cap list passes, one more raises."""
    assert ACCEPTANCE_CRITERIA_MAX_ITEMS == 10
    validate_acceptance_criteria_count([f"c{i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS)])
    with pytest.raises(ValueError):
        validate_acceptance_criteria_count([f"c{i}" for i in range(ACCEPTANCE_CRITERIA_MAX_ITEMS + 1)])


def test_no_prompt_literal_disagrees_with_criteria_cap() -> None:
    """Every ``1-N`` count literal beside "acceptance" in a prompt equals the cap.

    The task-generation templates are scanned as rendered module strings; the
    propose-plan and curate prompts are f-string / ``.format`` templates, so
    their source is scanned — a placeholder never matches, a hardcoded drifted
    literal does.
    """
    import inspect

    import maestro.evolution as evolution
    import maestro.prompts.curate_plan as curate_plan
    from maestro.prompts.defaults import TASK_GENERATION_PROMPT, TASK_GENERATION_PROMPT_EXTERNAL

    texts = {
        "TASK_GENERATION_PROMPT": TASK_GENERATION_PROMPT,
        "TASK_GENERATION_PROMPT_EXTERNAL": TASK_GENERATION_PROMPT_EXTERNAL,
        "evolution": inspect.getsource(evolution),
        "curate_plan": inspect.getsource(curate_plan),
    }
    pattern = re.compile(r"acceptance.{0,80}?\b1[-\u2013](\d+)\b|\b1[-\u2013](\d+)\b.{0,80}?acceptance", re.S | re.I)
    seen_in_templates = 0
    for name, text in texts.items():
        for match in pattern.finditer(text):
            upper = int(match.group(1) or match.group(2))
            assert upper == ACCEPTANCE_CRITERIA_MAX_ITEMS, f"{name}: {match.group(0)!r}"
            if name.startswith("TASK_GENERATION"):
                seen_in_templates += 1
    # The rendered task-generation templates do carry the literal — the scan is not vacuous.
    assert seen_in_templates == 2
