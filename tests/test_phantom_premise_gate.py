"""Tests for the phantom-premise post-call content gate predicate (issue #3666).

The predicate's tests live here rather than in ``tests/test_audit_checks.py``
because that module sits near SPLIT_BUDGET under the tool-result
frame cap (``tests/test_source_file_size.py``) — new tests must land in a
sibling. The gate's *wiring* tests live in ``tests/test_loop_task_generation.py``
alongside the collaborator that applies it.
"""

from __future__ import annotations

from maestro.audit_checks import check_phantom_premise

# ---------------------------------------------------------------------------
# check_phantom_premise — refuse a defect claim about an absent .py path
# ---------------------------------------------------------------------------


def test_check_phantom_premise_defect_claim_against_missing_path_rejected() -> None:
    """A goal asserting an existing bug in an absent path is refused."""
    allowed, reason = check_phantom_premise(
        "Fix the broken swallow in maestro/nonexistent.py",
        ["maestro/nonexistent.py"],
    )
    assert allowed is False
    assert "maestro/nonexistent.py" in reason


def test_check_phantom_premise_all_paths_present_allowed() -> None:
    """The same defect claim is admitted when no referenced path is missing."""
    allowed, reason = check_phantom_premise(
        "Fix the broken swallow in maestro/agent.py", [],
    )
    assert allowed is True
    assert reason == ""


def test_check_phantom_premise_creation_goal_allowed() -> None:
    """A goal whose framing is to *create* the absent path is admitted."""
    allowed, reason = check_phantom_premise(
        "Create maestro/newgate.py to fix the missing-refusal bug",
        ["maestro/newgate.py"],
    )
    assert allowed is True
    assert reason == ""


def test_check_phantom_premise_extract_into_goal_allowed() -> None:
    """The ``extract … into`` creation phrasing also admits the goal."""
    allowed, reason = check_phantom_premise(
        "Extract the crash handler into maestro/newmod.py",
        ["maestro/newmod.py"],
    )
    assert allowed is True
    assert reason == ""


def test_check_phantom_premise_no_defect_claim_allowed() -> None:
    """A missing path with no defect-claim token is not a phantom premise."""
    allowed, reason = check_phantom_premise(
        "Document the behaviour of maestro/nonexistent.py",
        ["maestro/nonexistent.py"],
    )
    assert allowed is True
    assert reason == ""


def test_check_phantom_premise_empty_goal_allowed() -> None:
    """An empty / whitespace goal is admitted regardless of missing paths.

    There is no dedicated empty-goal guard — an empty goal cannot carry a
    defect-claim token, so the defect-claim check already admits it.
    """
    allowed, reason = check_phantom_premise("", ["maestro/nonexistent.py"])
    assert allowed is True
    assert reason == ""
    allowed_ws, reason_ws = check_phantom_premise("   ", ["maestro/nope.py"])
    assert allowed_ws is True
    assert reason_ws == ""


def test_check_phantom_premise_reason_caps_named_paths() -> None:
    """The reason names at most the first three missing paths."""
    allowed, reason = check_phantom_premise(
        "Fix the regression",
        ["a/b.py", "c/d.py", "e/f.py", "g/h.py"],
    )
    assert allowed is False
    assert "g/h.py" not in reason
    assert "e/f.py" in reason
