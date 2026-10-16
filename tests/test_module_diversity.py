"""Tests for classify_module_family — module-family classification from task dicts.

Uses mem_fs for all filesystem access and mocked externals.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maestro.audit_checks import (
    _KEYWORD_ALIASES,
    _MODULE_FAMILIES,
    classify_module_family,
)
from maestro.memory import PlanStep, TaskRecord

FAKE_DIR = Path("/fake/diversity")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_dict(
    goal: str = "",
    result_summary: str = "",
    key_findings: str = "",
    branch: str = "",
    plan: list[dict[str, str]] | None = None,
    errors: list[str] | None = None,
) -> dict[str, object]:
    """Minimal task dict mimicking task_history.jsonl entries."""
    d: dict[str, object] = {}
    if goal:
        d["goal"] = goal
    if result_summary:
        d["result_summary"] = result_summary
    if key_findings:
        d["key_findings"] = key_findings
    if branch:
        d["branch"] = branch
    if plan is not None:
        d["plan"] = plan
    if errors is not None:
        d["errors"] = errors
    return d


def _task_record(
    task_id: str,
    goal: str = "some goal",
    outcome: str = "success",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="",
        errors=[],
        duration_secs=1.0,
        timestamp="2025-01-01T00:00:00Z",
    )


# ===========================================================================
# classify_module_family — various task shapes
# ===========================================================================


class TestClassifyModuleFamilyTaskShapes:
    """Classify module family from diverse task dict structures."""

    # --- Tasks with only a goal field ---

    def test_goal_with_module_path(self) -> None:
        task = _task_dict(goal="Refactor maestro/sdk.py error handling")
        assert classify_module_family(task) == "sdk"

    def test_goal_with_test_path(self) -> None:
        task = _task_dict(goal="Add tests/test_memory.py unit test for archive")
        assert classify_module_family(task) == "memory"

    def test_goal_with_package_subpath(self) -> None:
        task = _task_dict(goal="Fix maestro/notifications/telegram.py bug")
        assert classify_module_family(task) == "notifications"

    def test_goal_with_keyword_only(self) -> None:
        task = _task_dict(goal="Improve self-model prompting")
        assert classify_module_family(task) == "consciousness"

    def test_goal_with_bare_module_name(self) -> None:
        task = _task_dict(goal="The planner needs better step grouping")
        assert classify_module_family(task) == "planner"

    def test_goal_unclassifiable(self) -> None:
        task = _task_dict(goal="Improve overall code quality")
        assert classify_module_family(task) == "other"

    # --- Tasks with only result_summary ---

    def test_result_summary_with_path(self) -> None:
        task = _task_dict(
            result_summary="Fixed the import in maestro/journal.py line 45"
        )
        assert classify_module_family(task) == "journal"

    # --- Tasks with only key_findings ---

    def test_key_findings_with_path(self) -> None:
        task = _task_dict(
            key_findings="Root cause is a missing await in maestro/executor.py"
        )
        assert classify_module_family(task) == "executor"

    # --- Tasks with only plan steps ---

    def test_plan_steps_with_paths(self) -> None:
        task = _task_dict(
            plan=[
                {"description": "Edit maestro/config.py", "result": "done"},
                {"description": "Run tests", "result": "passed"},
            ]
        )
        assert classify_module_family(task) == "config"

    def test_plan_step_result_counts(self) -> None:
        """Module reference in step result also counts."""
        task = _task_dict(
            goal="Fix a bug",
            plan=[
                {
                    "description": "Investigate",
                    "result": "Found issue in maestro/dreaming.py",
                },
            ],
        )
        assert classify_module_family(task) == "dreaming"

    # --- Tasks with only errors ---

    def test_errors_with_paths(self) -> None:
        task = _task_dict(
            errors=[
                "TypeError in maestro/evolution.py:123",
                "AttributeError in maestro/evolution.py:456",
            ]
        )
        assert classify_module_family(task) == "evolution"

    # --- Tasks with only branch ---

    def test_branch_with_module_path(self) -> None:
        task = _task_dict(branch="maestro/task/fix-maestro/tools.py-import")
        assert classify_module_family(task) == "tools"

    # --- Multi-field tasks ---

    def test_multiple_modules_most_frequent_wins(self) -> None:
        """When multiple modules appear, the most cited one wins."""
        task = _task_dict(
            goal="Fix maestro/sdk.py and maestro/executor.py",
            result_summary="Changed maestro/sdk.py and maestro/sdk.py tests",
            errors=["Error in maestro/executor.py"],
        )
        # sdk appears 3 times, executor 2 times
        assert classify_module_family(task) == "sdk"

    def test_path_takes_priority_over_keyword(self) -> None:
        """File-path match is phase 1; keyword alias is phase 2."""
        task = _task_dict(
            goal="Improve telegram handling in maestro/consciousness.py"
        )
        # consciousness.py path match takes priority over "telegram" keyword
        assert classify_module_family(task) == "consciousness"

    def test_keyword_takes_priority_over_bare_name(self) -> None:
        """Keyword alias is phase 2; bare name is phase 3."""
        # "self-model" → consciousness via keyword alias
        # "executor" → executor via bare name
        # But since keyword phase runs first, consciousness should win
        task = _task_dict(goal="The self-model and executor are coupled")
        # Both "self-model" keyword alias (→consciousness) and "executor" bare name
        # Keyword phase fires first
        assert classify_module_family(task) == "consciousness"

    # --- Empty / degenerate shapes ---

    def test_completely_empty_dict(self) -> None:
        assert classify_module_family({}) == "other"

    def test_none_values_in_fields(self) -> None:
        task: dict[str, object] = {
            "goal": None,
            "result_summary": None,
            "key_findings": None,
            "branch": None,
        }
        assert classify_module_family(task) == "other"

    def test_numeric_fields_ignored(self) -> None:
        task: dict[str, object] = {"goal": 42, "result_summary": 3.14}
        assert classify_module_family(task) == "other"

    def test_plan_with_non_dict_entries_ignored(self) -> None:
        task: dict[str, object] = {
            "goal": "Something",
            "plan": [123, None, "not a dict"],
        }
        assert classify_module_family(task) == "other"

    def test_plan_as_non_list_ignored(self) -> None:
        task: dict[str, object] = {
            "goal": "Something general",
            "plan": "not a list",
        }
        assert classify_module_family(task) == "other"

    def test_errors_as_non_list_ignored(self) -> None:
        task: dict[str, object] = {
            "goal": "Something",
            "errors": "not a list",
        }
        assert classify_module_family(task) == "other"

    # --- Keyword alias coverage ---

    @pytest.mark.parametrize(
        "keyword,expected_family",
        list(_KEYWORD_ALIASES.items()),
    )
    def test_each_keyword_alias(self, keyword: str, expected_family: str) -> None:
        """Every keyword alias should classify to its mapped family."""
        task = _task_dict(goal=f"Work involving {keyword} improvements")
        result = classify_module_family(task)
        # The result should either be the expected family (via keyword) or
        # something matched at a higher-priority phase (path or another keyword).
        # Since we construct goals without file paths, keyword should dominate.
        assert result == expected_family

    # --- Generic-word over-classification regressions (issue #1966) ---

    def test_generic_cost_word_not_cost_tracking(self) -> None:
        """Bare ``cost`` prose with no module ref must not route to a family."""
        task = _task_dict(goal="evaluating the cost of doing X")
        assert classify_module_family(task) == "other"

    def test_generic_branch_word_not_vcs(self) -> None:
        """Bare ``branch`` prose with no module ref must not route to vcs."""
        task = _task_dict(goal="the fix landed on a feature branch")
        assert classify_module_family(task) == "other"

    def test_generic_identity_word_not_consciousness(self) -> None:
        """Bare ``identity`` prose with no module ref must not route there."""
        task = _task_dict(goal="the identity of the root cause is unclear")
        assert classify_module_family(task) == "other"

    def test_git_substring_word_not_vcs(self) -> None:
        """Word-boundary guard: ``git`` ⊂ ``legitimate`` must not match vcs."""
        task = _task_dict(goal="this is a legitimate concern")
        assert classify_module_family(task) == "other"

    # --- All module families via path ---

    @pytest.mark.parametrize("family", sorted(_MODULE_FAMILIES))
    def test_each_family_classifiable_from_source_path(self, family: str) -> None:
        """Every known family must be reachable from a maestro/ path."""
        if family in ("dashboard", "notifications", "prompts"):
            path = f"maestro/{family}/foo.py"
        else:
            path = f"maestro/{family}.py"
        task = _task_dict(goal=f"Edit {path}")
        assert classify_module_family(task) == family
