"""Task-shape, module-family, no-op, investigation and integration classifiers
plus their directive gates; split out of ``tests/test_audit_checks.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from maestro import audit_checks
from maestro.audit_checks import (
    _DIRECTIVE_MAX_CHARS,
    _MODULE_FAMILIES,
    _NO_OP_MARKERS,
    _NOOP_TASK_PATTERNS,
    INSTRUMENT_CONSUMPTION_THRESHOLD,
    MAX_CITES_PER_FINDING,
    AuditFinding,
    _is_investigation_task,
    check_construction_dominance,
    check_integration_cadence,
    check_investigation_streak,
    check_partial_construction_gap,
    check_promotable_backlog_directive,
    check_self_directed_success_rate,
    check_task_concreteness,
    classify_instrument_task,
    classify_module_family,
    classify_noop_task,
    classify_task_mode,
    compute_investigation_rate,
    compute_no_op_rate,
    compute_shape_investigation_rate,
    count_consecutive_branchless_tasks,
    count_unintegrated_tasks,
    find_unconsumed_instruments,
    format_integration_backlog,
    format_investigation_rate_context,
    format_task_mode_summary,
    is_compound_scope,
    is_integration_task,
    is_no_op_task,
    measure_noop_rate,
)
from maestro.memory import (
    Memory,
    TaskRecord,
)
from tests.test_audit_checks import (
    FAKE_DIR,
    _task,
)

# ---------------------------------------------------------------------------
# classify_task_shape — task-shape diversity mirror
# ---------------------------------------------------------------------------


def test_classify_task_shape_github_issue_hash() -> None:
    """Goal mentioning ``#NNN`` classifies as ``github_issue``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("Read open GitHub issues and fix #1566") == "github_issue"


def test_classify_task_shape_github_issue_keywords() -> None:
    """Goal mentioning ``oldest issue`` classifies as ``github_issue``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("Read open GitHub issues and pick the oldest issue") == "github_issue"


def test_classify_task_shape_branch_promotion() -> None:
    """Goal mentioning branch promotion classifies as ``branch_promotion``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("Promote the task branch for source_task_id t42") == "branch_promotion"


def test_classify_task_shape_module_extension() -> None:
    """Goal mentioning extending a module classifies as ``module_extension``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("Extending the executor module with retry logic") == "module_extension"


def test_classify_task_shape_measurement() -> None:
    """Goal mentioning audit/measure/count classifies as ``measurement``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("Audit the recent task history for cost patterns") == "measurement"
    assert classify_task_shape("Measure coverage of the SDK module") == "measurement"
    assert classify_task_shape("Count the number of silenced exceptions") == "measurement"
    # Constrained list/report phrasings that still express measurement intent.
    assert classify_task_shape("Listing the open questions") == "measurement"
    assert classify_task_shape("List all the silenced exceptions") == "measurement"
    assert classify_task_shape("Report on cost patterns") == "measurement"
    assert classify_task_shape("Reporting coverage of the SDK module") == "measurement"


def test_classify_task_shape_list_noun_not_measurement() -> None:
    """Bare ``list``/``report`` nouns are construction/maintenance, not measurement.

    Regression for #1900: the over-broad ``\\blist\\b`` / ``\\breport\\b``
    alternatives used to drag construction and bug-report goals into the
    ``measurement`` bucket. The invariant is that none of these are
    ``measurement``. With the construction bucket added (#2348), genuine
    building verbs (``Build``/``Add a|the|new``) now route to ``construction``
    rather than the ``other`` residual; non-building maintenance phrasings
    stay ``other``. Either way they never reach ``measurement``.
    """
    from maestro.audit_checks import classify_task_shape

    # #1900 core guard: bare list/report nouns are never ``measurement``.
    for goal in (
        "Build a list of features",
        "Update the publishable_files list",
        "Add the new module to the list of subsystems",
        "Report a bug in the executor",
    ):
        assert classify_task_shape(goal) != "measurement"

    # Building verbs now claim the construction bucket (#2348)…
    assert classify_task_shape("Build a list of features") == "construction"
    assert classify_task_shape("Add the new module to the list of subsystems") == "construction"
    # …while non-building maintenance phrasings stay in the ``other`` residual.
    assert classify_task_shape("Update the publishable_files list") == "other"
    assert classify_task_shape("Report a bug in the executor") == "other"


def test_classify_task_shape_other_fallback() -> None:
    """Goal without any shape markers classifies as ``other``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("Write a poem about recursion") == "other"


def test_classify_task_shape_empty_goal() -> None:
    """Empty goal classifies as ``other``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("") == "other"


# -- Expanded shapes (remediation, external_request, investigation, construction)


def test_classify_shape_remediation() -> None:
    """PR-remediation goals classify as ``remediation``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("Remediate PR #2321 quality gate failures") == "remediation"
    assert classify_task_shape("Fix PR #1234 ruff lint errors") == "remediation"
    assert classify_task_shape("Fix the PR after self-review findings") == "remediation"


def test_classify_shape_external_request() -> None:
    """Question-shaped goals classify as ``external_request``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("What is the average length of time between deployments?") == "external_request"
    assert classify_task_shape("How does the executor handle retries?") == "external_request"
    assert classify_task_shape("Should we add a rate-limit gate?") == "external_request"


def test_classify_shape_investigation() -> None:
    """Read-only / analysis goals classify as ``investigation``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("Read task_history.jsonl and identify failure patterns") == "investigation"
    assert classify_task_shape("Compute prediction accuracy over last 20 tasks") == "investigation"
    assert classify_task_shape("Investigate why the last 3 tasks failed") == "investigation"
    assert classify_task_shape("Check whether the stale-entry rule fires correctly") == "investigation"


def test_classify_shape_construction() -> None:
    """Building / creation goals classify as ``construction``."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("Build a cost-outcome gate for task generation") == "construction"
    assert classify_task_shape("Add a CLI subcommand for gate activation history") == "construction"
    assert classify_task_shape("Create a helper function for BM25 scoring") == "construction"
    assert classify_task_shape("Implement retry logic in the SDK backend") == "construction"


def test_classify_shape_construction_over_investigation() -> None:
    """When both construction and investigation verbs co-occur, construction wins."""
    from maestro.audit_checks import classify_task_shape

    assert classify_task_shape("Read the codebase and implement a new gate") == "construction"
    assert classify_task_shape("Check the design then build a retry mechanism") == "construction"


def test_classify_shape_expanded_no_regression() -> None:
    """All 5 original shapes still classify correctly with their original test inputs."""
    from maestro.audit_checks import classify_task_shape

    # github_issue
    assert classify_task_shape("Read open GitHub issues and fix #1566") == "github_issue"
    assert classify_task_shape("Read open GitHub issues and pick the oldest issue") == "github_issue"
    # branch_promotion
    assert classify_task_shape("Promote the task branch for source_task_id t42") == "branch_promotion"
    # module_extension
    assert classify_task_shape("Extending the executor module with retry logic") == "module_extension"
    # measurement
    assert classify_task_shape("Audit the recent task history for cost patterns") == "measurement"
    assert classify_task_shape("Measure coverage of the SDK module") == "measurement"
    assert classify_task_shape("Count the number of silenced exceptions") == "measurement"
    # other
    assert classify_task_shape("Write a poem about recursion") == "other"
    assert classify_task_shape("") == "other"


# ---------------------------------------------------------------------------
# format_task_shape_summary — distribution summary for prompt injection
# ---------------------------------------------------------------------------


def test_format_task_shape_summary_empty_input() -> None:
    """Empty task list returns empty string for clean prompt degradation."""
    from maestro.audit_checks import format_task_shape_summary

    assert format_task_shape_summary([]) == ""


def test_format_task_shape_summary_uniform_distribution() -> None:
    """All tasks have the same shape — dominant warning fires."""
    from maestro.audit_checks import format_task_shape_summary

    tasks = [
        _task(f"t{i}", goal="Read open GitHub issues and fix #42")
        for i in range(6)
    ]
    summary = format_task_shape_summary(tasks)
    assert "github_issue: 6/6" in summary
    assert "⚠" in summary
    assert "github_issue dominates" in summary


def test_format_task_shape_summary_diverse_distribution() -> None:
    """Diverse tasks produce no dominant-shape warning."""
    from maestro.audit_checks import format_task_shape_summary

    tasks = [
        _task("t0", goal="Fix #100"),
        _task("t1", goal="Promote the task branch for source_task_id"),
        _task("t2", goal="Extending the executor module"),
        _task("t3", goal="Audit the recent task history"),
        _task("t4", goal="Write a poem about recursion"),
    ]
    summary = format_task_shape_summary(tasks)
    assert "github_issue: 1/5" in summary
    assert "branch_promotion: 1/5" in summary
    assert "module_extension: 1/5" in summary
    assert "measurement: 1/5" in summary
    assert "other: 1/5" in summary
    assert "⚠" not in summary


def test_format_task_shape_summary_tied_at_fifty_percent_names_all() -> None:
    """A 50-50 tie names every tied shape in alphabetical order."""
    from maestro.audit_checks import format_task_shape_summary

    tasks = [
        _task("t0", goal="Fix #100"),
        _task("t1", goal="Fix #101"),
        _task("t2", goal="Write a poem"),
        _task("t3", goal="Think about priorities"),
    ]
    summary = format_task_shape_summary(tasks)
    assert "github_issue: 2/4" in summary
    assert "⚠" in summary
    assert "github_issue" in summary
    assert "other" in summary
    assert "tied" in summary
    assert "2/4 each" in summary
    warning_line = next(line for line in summary.splitlines() if "⚠" in line)
    assert warning_line.index("github_issue") < warning_line.index("other")


def test_format_task_shape_summary_tied_warning_is_order_independent() -> None:
    """Warning line is byte-identical regardless of input task order."""
    from maestro.audit_checks import format_task_shape_summary

    tasks_a = [
        _task("t0", goal="Fix #100"),
        _task("t1", goal="Fix #101"),
        _task("t2", goal="Write a poem"),
        _task("t3", goal="Think about priorities"),
    ]
    tasks_b = [
        _task("t0", goal="Write a poem"),
        _task("t1", goal="Think about priorities"),
        _task("t2", goal="Fix #100"),
        _task("t3", goal="Fix #101"),
    ]
    warning_a = next(
        line for line in format_task_shape_summary(tasks_a).splitlines() if "⚠" in line
    )
    warning_b = next(
        line for line in format_task_shape_summary(tasks_b).splitlines() if "⚠" in line
    )
    assert warning_a == warning_b


def test_format_task_shape_summary_single_shape_dominant_at_fifty_percent() -> None:
    """Single shape at exactly 50% still emits the single-shape warning."""
    from maestro.audit_checks import format_task_shape_summary

    tasks = [
        _task("t0", goal="Fix #100"),
        _task("t1", goal="Fix #101"),
        _task("t2", goal="Audit the recent task history"),
        _task("t3", goal="Write a poem"),
    ]
    summary = format_task_shape_summary(tasks)
    assert "github_issue: 2/4" in summary
    assert "⚠" in summary
    assert "github_issue dominates (2/4)" in summary
    assert "tied" not in summary


def test_format_task_shape_summary_below_fifty_percent_no_warning() -> None:
    """Below 50% — no dominant-shape warning."""
    from maestro.audit_checks import format_task_shape_summary

    tasks = [
        _task("t0", goal="Fix #100"),
        _task("t1", goal="Promote the task branch for source_task_id"),
        _task("t2", goal="Extending the executor module with retry"),
        _task("t3", goal="Audit the recent task history"),
        _task("t4", goal="Write a poem about recursion"),
    ]
    summary = format_task_shape_summary(tasks)
    assert "github_issue: 1/5" in summary
    assert "⚠" not in summary


def test_task_shape_summary_placeholder_in_prompt() -> None:
    """TASK_GENERATION_PROMPT contains the {task_shape_summary} placeholder."""
    from maestro.prompts.defaults import TASK_GENERATION_PROMPT

    assert "{task_shape_summary}" in TASK_GENERATION_PROMPT


# ---------------------------------------------------------------------------
# Rule 17: check_construction_dominance (task-shape avoidance detector)
# ---------------------------------------------------------------------------


def _construction_window(
    n_construction: int, n_other: int
) -> list[TaskRecord]:
    """Build a task window: *n_construction* build-goals + *n_other* others.

    Construction goals use a building verb ("implement ...") so
    ``classify_task_shape`` returns ``"construction"``; the others use a
    goal that classifies as ``"other"`` (no shape pattern matches "topic").
    """
    tasks: list[TaskRecord] = []
    for i in range(n_construction):
        tasks.append(_task(f"c{i}", goal=f"implement feature {i}"))
    for i in range(n_other):
        tasks.append(_task(f"o{i}", goal=f"topic {i}"))
    return tasks


def test_rule17_disabled_returns_no_finding() -> None:
    """Criterion 2: with the master switch off, no finding for any history."""
    tasks = _construction_window(10, 0)  # 100% construction
    findings = check_construction_dominance(
        tasks, enabled=False, threshold=0.6, window=10,
    )
    assert findings == []


def test_rule17_fires_at_or_above_threshold_exactly_one() -> None:
    """Criterion 3: >= threshold construction → exactly one finding."""
    # 6/10 = 0.6 == threshold (inclusive boundary) → fires.
    tasks = _construction_window(6, 4)
    findings = check_construction_dominance(
        tasks, enabled=True, threshold=0.6, window=10,
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "undeclared_limitation"
    assert finding.rule == "construction_dominance"
    assert "60%" in finding.statement
    assert finding.cites  # construction-shaped task ids cited
    assert len(finding.cites) <= MAX_CITES_PER_FINDING
    # Only construction tasks are cited, never the "other" tasks.
    assert all(cite.startswith("c") for cite in finding.cites)


def test_rule17_silent_below_threshold_self_extinguishing() -> None:
    """Criterion 3: below threshold → zero findings (self-extinguishing)."""
    # 5/10 = 0.5 < 0.6 → no finding.
    tasks = _construction_window(5, 5)
    findings = check_construction_dominance(
        tasks, enabled=True, threshold=0.6, window=10,
    )
    assert findings == []


def test_rule17_window_bounds_the_corpus() -> None:
    """Only the last *window* tasks count toward the fraction."""
    # 12 tasks: first 6 construction, last 6 "other". window=6 sees only the
    # trailing 6 ("other"), so the construction fraction is 0 → no finding.
    tasks = _construction_window(6, 6)
    findings = check_construction_dominance(
        tasks, enabled=True, threshold=0.6, window=6,
    )
    assert findings == []


def test_rule17_empty_history_returns_no_finding() -> None:
    """Empty input degrades cleanly to no finding (no ZeroDivisionError)."""
    findings = check_construction_dominance(
        [], enabled=True, threshold=0.6, window=10,
    )
    assert findings == []


def test_rule17_nonpositive_window_returns_no_finding() -> None:
    """A non-positive window short-circuits to no finding (guard branch)."""
    tasks = _construction_window(10, 0)
    findings = check_construction_dominance(
        tasks, enabled=True, threshold=0.6, window=0,
    )
    assert findings == []


# ---------------------------------------------------------------------------
# count_promotable_tasks / format_promotable_summary
# ---------------------------------------------------------------------------


def test_count_promotable_tasks_zero_tasks() -> None:
    """Empty task list → 0 promotable."""
    from maestro.audit_checks import count_promotable_tasks

    assert count_promotable_tasks([], set()) == 0


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_count_promotable_tasks_zero_plans(_mock_merged: object) -> None:
    """Successful tasks with branches and no citations → all promotable."""
    from maestro.audit_checks import count_promotable_tasks

    tasks = [
        _task("t1", outcome="success", branch="maestro/task/t1"),
        _task("t2", outcome="success", branch="maestro/task/t2"),
    ]
    assert count_promotable_tasks(tasks, set()) == 2


def test_count_promotable_tasks_all_already_promoted() -> None:
    """Every successful task is cited by a plan → 0 promotable."""
    from maestro.audit_checks import count_promotable_tasks

    tasks = [
        _task("t1", outcome="success", branch="maestro/task/t1"),
        _task("t2", outcome="success", branch="maestro/task/t2"),
    ]
    assert count_promotable_tasks(tasks, {"t1", "t2"}) == 0


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_count_promotable_tasks_ignores_failed_tasks(_mock_merged: object) -> None:
    """Failed tasks are never promotable, even without matching plan."""
    from maestro.audit_checks import count_promotable_tasks

    tasks = [
        _task("t1", outcome="failure", branch="maestro/task/t1"),
        _task("t2", outcome="success", branch="maestro/task/t2"),
    ]
    assert count_promotable_tasks(tasks, set()) == 1


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_count_promotable_tasks_ignores_branchless_tasks(_mock_merged: object) -> None:
    """Tasks without a branch are not promotable."""
    from maestro.audit_checks import count_promotable_tasks

    tasks = [
        _task("t1", outcome="success"),  # branch=None
        _task("t2", outcome="success", branch="maestro/task/t2"),
    ]
    assert count_promotable_tasks(tasks, set()) == 1


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_count_promotable_tasks_excludes_partial(_mock_merged: object) -> None:
    """Partial tasks are not promotable — matches Memory.get_promotable_branches (#2095)."""
    from maestro.audit_checks import count_promotable_tasks

    tasks = [
        _task("t1", outcome="partial", branch="maestro/task/t1"),
        _task("t2", outcome="success", branch="maestro/task/t2"),
    ]
    assert count_promotable_tasks(tasks, set()) == 1


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_format_promotable_summary_below_threshold(_mock_merged: object) -> None:
    """Count strictly below threshold → empty string (#2174)."""
    from maestro.audit_checks import format_promotable_summary

    tasks = [
        _task("t1", outcome="success", branch="b1"),
    ]
    result = format_promotable_summary(tasks, set(), threshold=2)
    assert result == ""


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_format_promotable_summary_at_threshold_fires(_mock_merged: object) -> None:
    """Count exactly at threshold now fires (aligned to >= convention, #2174)."""
    from maestro.audit_checks import format_promotable_summary

    tasks = [
        _task("t1", outcome="success", branch="b1"),
        _task("t2", outcome="success", branch="b2"),
    ]
    result = format_promotable_summary(tasks, set(), threshold=2)
    assert "PROMOTABLE BACKLOG: 2" in result


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_format_promotable_summary_above_threshold(_mock_merged: object) -> None:
    """Count above threshold → formatted section with correct count and candidates."""
    from maestro.audit_checks import format_promotable_summary

    tasks = [
        _task("t1", outcome="success", branch="b1", goal="first goal"),
        _task("t2", outcome="success", branch="b2", goal="second goal"),
        _task("t3", outcome="success", branch="b3", goal="third goal"),
    ]
    result = format_promotable_summary(tasks, set(), threshold=2)
    assert "PROMOTABLE BACKLOG: 3" in result
    assert "source_task_id" in result
    # Top-3 candidates listed, most recent first
    assert "t3: third goal" in result
    assert "t2: second goal" in result
    assert "t1: first goal" in result


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_format_promotable_summary_recency_top3(_mock_merged: object) -> None:
    """Only top 3 candidates by recency are shown."""
    from maestro.audit_checks import format_promotable_summary

    tasks = [
        _task(f"t{i}", outcome="success", branch=f"b{i}", goal=f"goal-{i}")
        for i in range(5)
    ]
    result = format_promotable_summary(tasks, set(), threshold=1)
    assert "PROMOTABLE BACKLOG: 5" in result
    # Most recent 3 (t4, t3, t2) should appear
    assert "t4:" in result
    assert "t3:" in result
    assert "t2:" in result
    # t0 and t1 should NOT appear
    assert "t0:" not in result
    assert "t1:" not in result
    # Rendered newest-first: t4 before t3 before t2.
    assert result.index("t4:") < result.index("t3:") < result.index("t2:")


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_format_promotable_summary_two_candidates_newest_first(
    _mock_merged: object,
) -> None:
    """Fewer than three candidates still render newest-first (#2175)."""
    from maestro.audit_checks import format_promotable_summary

    tasks = [
        _task("t0", outcome="success", branch="b0"),
        _task("t1", outcome="success", branch="b1"),
    ]
    result = format_promotable_summary(tasks, set(), threshold=1)
    assert "t0:" in result
    assert "t1:" in result
    assert result.index("t1:") < result.index("t0:")


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_format_promotable_summary_goal_truncation(_mock_merged: object) -> None:
    """Goals longer than 80 chars are truncated."""
    from maestro.audit_checks import format_promotable_summary

    long_goal = "x" * 120
    tasks = [
        _task("t1", outcome="success", branch="b1", goal=long_goal),
        _task("t2", outcome="success", branch="b2", goal="short"),
    ]
    result = format_promotable_summary(tasks, set(), threshold=1)
    assert "PROMOTABLE BACKLOG: 2" in result
    # The long goal should be truncated to 80 chars
    assert "x" * 80 in result
    assert "x" * 81 not in result


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_format_promotable_summary_goal_truncation_marker(_mock_merged: object) -> None:
    """Over-cap goals carry a trailing … marker; at-cap goals do not (#2177)."""
    from maestro.audit_checks import format_promotable_summary

    tasks = [
        _task("t1", outcome="success", branch="b1", goal="x" * 120),
        _task("t2", outcome="success", branch="b2", goal="y" * 80),
    ]
    result = format_promotable_summary(tasks, set(), threshold=1)
    long_line = next(line for line in result.splitlines() if "t1:" in line)
    assert long_line == "  - t1: " + "x" * 80 + "…"
    exact_line = next(line for line in result.splitlines() if "t2:" in line)
    assert exact_line == "  - t2: " + "y" * 80


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_format_promotable_summary_excludes_cited_tasks(_mock_merged: object) -> None:
    """Tasks with source_task_id follow-up plans are excluded."""
    from maestro.audit_checks import format_promotable_summary

    tasks = [
        _task("t1", outcome="success", branch="b1", goal="first"),
        _task("t2", outcome="success", branch="b2", goal="second"),
        _task("t3", outcome="success", branch="b3", goal="third"),
    ]
    # Only t1, t3 are uncited (count=2), threshold=1 → triggers
    result = format_promotable_summary(tasks, {"t2"}, threshold=1)
    assert "PROMOTABLE BACKLOG: 2" in result
    assert "t1:" in result
    assert "t3:" in result
    assert "t2:" not in result


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_format_promotable_summary_excludes_partial(_mock_merged: object) -> None:
    """Partial tasks are neither listed nor counted toward the threshold (#2095)."""
    from maestro.audit_checks import format_promotable_summary

    tasks = [
        _task("t1", outcome="partial", branch="b1", goal="partial goal"),
        _task("t2", outcome="success", branch="b2", goal="success goal"),
    ]
    # Only the success task counts (1), which stays below threshold=2 → empty.
    # If the partial were counted the total would reach 2 and fire under the
    # ``count >= threshold`` boundary (#2174), so an empty result proves the
    # partial was excluded from the count.
    result = format_promotable_summary(tasks, set(), threshold=2)
    assert result == ""


# ──────────────────────────────────────────────────────────────────────
# is_branch_merged_into_main — subprocess-based merge check
# ──────────────────────────────────────────────────────────────────────


def test_is_branch_merged_into_main_merged() -> None:
    """Branch whose HEAD is ancestor of origin/main → True."""
    from maestro.audit_checks import is_branch_merged_into_main

    with patch("maestro.audit_checks.subprocess.run") as mock_run:
        mock_run.return_value = None  # check=True, no exception → exit 0
        assert is_branch_merged_into_main("maestro/task/t1") is True
        mock_run.assert_called_once_with(
            ["git", "merge-base", "--is-ancestor", "maestro/task/t1", "origin/main"],
            check=True,
            capture_output=True,
            timeout=10,
        )


def test_is_branch_merged_into_main_not_merged() -> None:
    """Branch not merged → CalledProcessError → False."""
    import subprocess as sp

    from maestro.audit_checks import is_branch_merged_into_main

    with patch("maestro.audit_checks.subprocess.run") as mock_run:
        mock_run.side_effect = sp.CalledProcessError(1, "git")
        assert is_branch_merged_into_main("maestro/task/t2") is False


def test_is_branch_merged_into_main_subprocess_error() -> None:
    """Arbitrary subprocess error → False (branch retained)."""
    from maestro.audit_checks import is_branch_merged_into_main

    with patch("maestro.audit_checks.subprocess.run") as mock_run:
        mock_run.side_effect = OSError("git not found")
        assert is_branch_merged_into_main("maestro/task/t3") is False


def test_is_branch_merged_into_main_timeout_warns(caplog: pytest.LogCaptureFixture) -> None:
    """TimeoutExpired must log at WARNING so operators can diagnose the issue."""
    import logging
    import subprocess as sp

    from maestro.audit_checks import is_branch_merged_into_main

    with patch("maestro.audit_checks.subprocess.run") as mock_run:
        mock_run.side_effect = sp.TimeoutExpired(["git", "merge-base"], 10)
        with caplog.at_level(logging.WARNING, logger="maestro.audit_checks"):
            result = is_branch_merged_into_main("some-feature-branch")

    assert result is False
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, (
        "TimeoutExpired must be logged at WARNING; currently silently absorbed at DEBUG"
    )


def test_is_branch_merged_into_main_empty_branch() -> None:
    """Empty branch name → False without subprocess call."""
    from maestro.audit_checks import is_branch_merged_into_main

    with patch("maestro.audit_checks.subprocess.run") as mock_run:
        assert is_branch_merged_into_main("") is False
        mock_run.assert_not_called()


def test_format_promotable_summary_filters_merged_branches() -> None:
    """Branches already merged into main are excluded from promotable count."""
    from maestro.audit_checks import format_promotable_summary

    tasks = [
        _task("t1", outcome="success", branch="b1", goal="first goal"),
        _task("t2", outcome="success", branch="b2", goal="second goal"),
        _task("t3", outcome="success", branch="b3", goal="third goal"),
    ]

    def mock_merged(branch: str) -> bool:
        return branch == "b2"  # t2 is already merged

    with patch("maestro.audit_checks.is_branch_merged_into_main", side_effect=mock_merged):
        result = format_promotable_summary(tasks, set(), threshold=1)
    assert "PROMOTABLE BACKLOG: 2" in result
    assert "t1:" in result
    assert "t3:" in result
    assert "t2:" not in result


def test_format_promotable_summary_all_merged_returns_empty() -> None:
    """All branches already merged into main → empty string."""
    from maestro.audit_checks import format_promotable_summary

    tasks = [
        _task("t1", outcome="success", branch="b1", goal="first"),
        _task("t2", outcome="success", branch="b2", goal="second"),
    ]

    with patch("maestro.audit_checks.is_branch_merged_into_main", return_value=True):
        result = format_promotable_summary(tasks, set(), threshold=0)
    assert result == ""


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
def test_promotable_dedups_repeat_task_id(_mock_merged: object) -> None:
    """Repeat ``task_id`` counts once and surfaces once (#2180).

    ``task_history.jsonl`` is append-only and ``Memory.get_task_record`` keeps
    "most recent matching" duplicate semantics, so a window can hold two
    records sharing a ``task_id`` on distinct branches. ``_promotable_candidates``
    must dedup by ``task_id`` like its sibling ``count_unintegrated_tasks`` —
    otherwise the count inflates and the same candidate appears twice in the
    top-3 backlog prompt.
    """
    from maestro.audit_checks import (
        count_promotable_tasks,
        format_promotable_summary,
    )

    tasks = [
        _task("dup", outcome="success", branch="b1", goal="dup goal"),
        _task("dup", outcome="success", branch="b2", goal="dup goal"),
        _task("t2", outcome="success", branch="b3", goal="second goal"),
    ]
    # Two distinct task_ids after dedup ("dup", "t2"), not three.
    assert count_promotable_tasks(tasks, set()) == 2
    result = format_promotable_summary(tasks, set(), threshold=2)
    assert "PROMOTABLE BACKLOG: 2" in result
    # The duplicated candidate is listed exactly once.
    assert result.count("  - dup:") == 1
    assert "  - t2:" in result


# ──────────────────────────────────────────────────────────────────────
# finding_to_directive tests
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind,detail",
    [
        (
            "undeclared_limitation",
            "Error pattern 'mypy type error' recurred 5 times across "
            "recent tasks but is not reflected in limitations_known.",
        ),
        (
            "unmerged_work",
            "Successful task branches without source_task_id follow-up plan.",
        ),
        (
            "contradicted_claim",
            "Capability claims effectiveness ('efficient debugging') but "
            "recent success rate is 40% over 10 tasks.",
        ),
        (
            "stalled_question",
            "Question 'Why does mypy fail?' has been targeted 5 times "
            "without resolution; consider rephrasing or removing.",
        ),
        (
            "stale_entry",
            "capabilities_known: I am an excellent planner with robust "
            "organizational capacity",
        ),
        (
            "contradicted_claim",
            "Capability claims stability ('reliable operations') but the "
            "last 4 tasks all ended in failure.",
        ),
    ],
)
def test_finding_to_directive_all_kinds(kind: str, detail: str) -> None:
    """finding_to_directive returns ≤200-char directive with correct prefix."""
    from maestro.audit_checks import finding_to_directive

    finding = AuditFinding(kind=kind, statement=detail, rule="test")  # type: ignore[arg-type]
    result = finding_to_directive(finding)
    assert len(result) <= 200
    assert result.startswith(f"Address audit finding [{kind}]: ")
    # Detail portion is present (possibly truncated).
    assert len(result) > len(f"Address audit finding [{kind}]: ")


def test_finding_to_directive_truncates_long_detail() -> None:
    """A 300-char detail is truncated so the result is exactly 200 chars."""
    from maestro.audit_checks import finding_to_directive

    long_detail = "x" * 300
    finding = AuditFinding(
        kind="undeclared_limitation", statement=long_detail, rule="test",
    )
    result = finding_to_directive(finding)
    assert len(result) == 200
    assert result.endswith("…")
    assert result.startswith("Address audit finding [undeclared_limitation]: ")


def test_finding_to_directive_empty_detail() -> None:
    """Empty detail returns a valid directive with the prefix only."""
    from maestro.audit_checks import finding_to_directive

    finding = AuditFinding(
        kind="stale_entry", statement="", rule="test",
    )
    result = finding_to_directive(finding)
    assert len(result) <= 200
    assert result == "Address audit finding [stale_entry]: "


def test_directive_max_chars_matches_consciousness_cap() -> None:
    """audit_checks._DIRECTIVE_MAX_CHARS must equal Consciousness.MAX_DIRECTIVE_CHARS.

    The two constants are intentionally duplicated to avoid a circular import
    (see audit_checks.py:1475-1478). This test pins them together so a change
    to one without the other fails CI.
    """
    from maestro.audit_checks import _DIRECTIVE_MAX_CHARS
    from maestro.consciousness import Consciousness

    assert _DIRECTIVE_MAX_CHARS == Consciousness.MAX_DIRECTIVE_CHARS


# ---------------------------------------------------------------------------
# classify_module_family
# ---------------------------------------------------------------------------


class TestClassifyModuleFamily:
    """Tests for :func:`classify_module_family`."""

    # --- Phase 1: file-path extraction ---

    def test_single_module_path(self) -> None:
        task: dict[str, object] = {
            "goal": "Fix a bug in maestro/consciousness.py",
        }
        assert classify_module_family(task) == "consciousness"

    def test_package_path(self) -> None:
        task: dict[str, object] = {
            "goal": "Update maestro/dashboard/routes.py for auth",
        }
        assert classify_module_family(task) == "dashboard"

    def test_test_file_path(self) -> None:
        task: dict[str, object] = {
            "goal": "Add tests in tests/test_sdk.py",
        }
        assert classify_module_family(task) == "sdk"

    def test_most_frequent_module_wins(self) -> None:
        task: dict[str, object] = {
            "goal": (
                "Refactor maestro/sdk.py and maestro/executor.py "
                "and maestro/sdk.py again"
            ),
        }
        assert classify_module_family(task) == "sdk"

    def test_paths_in_plan_steps(self) -> None:
        task: dict[str, object] = {
            "goal": "Improve logging",
            "plan": [
                {"description": "Edit maestro/journal.py", "result": "done"},
            ],
        }
        assert classify_module_family(task) == "journal"

    def test_paths_in_errors(self) -> None:
        task: dict[str, object] = {
            "goal": "Fix test failure",
            "errors": ["ImportError in maestro/memory.py line 42"],
        }
        assert classify_module_family(task) == "memory"

    def test_paths_in_result_summary(self) -> None:
        task: dict[str, object] = {
            "goal": "Refactor code",
            "result_summary": "Modified maestro/planner.py successfully",
        }
        assert classify_module_family(task) == "planner"

    def test_paths_in_key_findings(self) -> None:
        task: dict[str, object] = {
            "goal": "Investigate issue",
            "key_findings": "Root cause in maestro/evolution.py",
        }
        assert classify_module_family(task) == "evolution"

    def test_paths_in_branch(self) -> None:
        """Branch names rarely contain paths, but the field is checked."""
        task: dict[str, object] = {
            "goal": "Work on feature",
            "branch": "fix/maestro/dreaming.py-cleanup",
        }
        assert classify_module_family(task) == "dreaming"

    def test_unknown_path_ignored(self) -> None:
        """A path component not in _MODULE_FAMILIES is not matched."""
        task: dict[str, object] = {
            "goal": "Edit maestro/nonexistent_module.py",
        }
        # Should fall through to other phases or return "other".
        result = classify_module_family(task)
        assert result == "other"

    # --- Phase 2: keyword aliases ---

    def test_keyword_alias_telegram(self) -> None:
        task: dict[str, object] = {
            "goal": "Fix telegram notification delivery",
        }
        assert classify_module_family(task) == "telegram_tools"

    def test_keyword_alias_self_model(self) -> None:
        task: dict[str, object] = {
            "goal": "Update the self-model with new capabilities",
        }
        assert classify_module_family(task) == "consciousness"

    def test_keyword_alias_dream(self) -> None:
        task: dict[str, object] = {
            "goal": "Improve dream cycle compression",
        }
        assert classify_module_family(task) == "dreaming"

    def test_keyword_alias_evolve(self) -> None:
        task: dict[str, object] = {
            "goal": "Make the agent evolve more carefully",
        }
        assert classify_module_family(task) == "evolution"

    def test_keyword_alias_budget(self) -> None:
        task: dict[str, object] = {
            "goal": "Tune the budget throttle thresholds",
        }
        assert classify_module_family(task) == "cost_tracking"

    # --- Phase 3: bare module-name match ---

    def test_bare_module_name(self) -> None:
        task: dict[str, object] = {
            "goal": "The executor is too slow",
        }
        assert classify_module_family(task) == "executor"

    def test_bare_module_name_dashboard(self) -> None:
        task: dict[str, object] = {
            "goal": "The dashboard needs a new status panel",
        }
        assert classify_module_family(task) == "dashboard"

    def test_bare_module_name_tie_is_deterministic(self) -> None:
        # "journal" and "memory" both match once as bare names; no file
        # paths and no keyword aliases fire, so phase 3 decides the tie.
        # With sorted(_MODULE_FAMILIES) the alphabetically-first family
        # ("journal" < "memory") wins on every process; pre-fix the winner
        # depended on hash-randomized frozenset iteration order.
        task: dict[str, object] = {
            "goal": "Compare the journal and memory subsystems",
        }
        assert classify_module_family(task) == "journal"

    # --- Phase 4: fallback to "other" ---

    def test_empty_task(self) -> None:
        assert classify_module_family({}) == "other"

    def test_empty_goal(self) -> None:
        task: dict[str, object] = {"goal": ""}
        assert classify_module_family(task) == "other"

    def test_unclassifiable_goal(self) -> None:
        task: dict[str, object] = {
            "goal": "Think about what to do next",
        }
        assert classify_module_family(task) == "other"

    def test_whitespace_only_goal(self) -> None:
        task: dict[str, object] = {"goal": "   \n  "}
        assert classify_module_family(task) == "other"

    # --- Edge cases ---

    def test_non_string_fields_ignored(self) -> None:
        task: dict[str, object] = {
            "goal": 12345,
            "result_summary": None,
            "plan": "not a list",
        }
        assert classify_module_family(task) == "other"

    def test_plan_step_not_dict_ignored(self) -> None:
        task: dict[str, object] = {
            "goal": "Do something",
            "plan": ["just a string", 42],
        }
        assert classify_module_family(task) == "other"

    def test_multiple_fields_combine(self) -> None:
        """File-path signals from different fields are combined."""
        task: dict[str, object] = {
            "goal": "Fix bug",
            "result_summary": "Edited maestro/sdk.py",
            "errors": ["Also touched maestro/sdk.py"],
        }
        assert classify_module_family(task) == "sdk"

    def test_test_and_source_paths_combine(self) -> None:
        """Test file paths and source file paths contribute together."""
        task: dict[str, object] = {
            "goal": (
                "Modified tests/test_memory.py and maestro/memory.py"
            ),
        }
        assert classify_module_family(task) == "memory"

    def test_test_path_regex_captures_sub_feature_test_files(self) -> None:
        """Sub-feature test files (``test_<family>_<suffix>.py``) resolve.

        ``test_memory_archive.py`` should classify as ``memory`` via the
        longest-prefix match against ``_MODULE_FAMILIES`` — previously
        the regex captured the whole stem ``memory_archive`` which was
        not a known family, so the test reference contributed nothing.
        """
        task: dict[str, object] = {
            "goal": "Add coverage to tests/test_memory_archive.py",
            "result_summary": (
                "Updated tests in tests/test_memory_archive.py"
            ),
            "key_findings": "",
            "branch": "",
        }
        assert classify_module_family(task) == "memory"

    def test_test_path_regex_longest_prefix_wins(self) -> None:
        """``test_cost_tracking_smoke.py`` resolves to ``cost_tracking``.

        The two-token family must beat shorter prefixes — longest-known
        prefix wins.
        """
        task: dict[str, object] = {
            "goal": (
                "Add tests/test_cost_tracking_smoke.py coverage"
            ),
        }
        assert classify_module_family(task) == "cost_tracking"

    def test_module_families_is_frozen(self) -> None:
        """Sanity: the known families set is immutable."""
        assert isinstance(_MODULE_FAMILIES, frozenset)

    def test_all_known_families_classify_from_path(self) -> None:
        """Every known family can be classified from a maestro/ path."""
        for family in _MODULE_FAMILIES:
            # Subdirectories use the package path form.
            if family in ("dashboard", "notifications", "prompts"):
                path = f"maestro/{family}/foo.py"
            else:
                path = f"maestro/{family}.py"
            task: dict[str, object] = {"goal": f"Edit {path}"}
            assert classify_module_family(task) == family, (
                f"Failed for family={family!r} with path={path!r}"
            )


# ──────────────────────────────────────────────────────────────────────
# count_unintegrated_tasks — pure-function branch accounting
# ──────────────────────────────────────────────────────────────────────


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
class TestCountUnintegratedTasks:
    """Tests for :func:`count_unintegrated_tasks`."""

    def test_empty_inputs_return_zero(self, _mock_merged: object) -> None:
        count, ids = count_unintegrated_tasks([], [])
        assert count == 0
        assert ids == []

    def test_failed_tasks_excluded(self, _mock_merged: object) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "failure", "branch": "b1"},
            {"task_id": "t2", "outcome": "failure", "branch": "b2"},
        ]
        count, ids = count_unintegrated_tasks(tasks, [])
        assert count == 0
        assert ids == []

    def test_partial_outcome_excluded(self, _mock_merged: object) -> None:
        """Routing hint counts success-only branches (#1996).

        ``count_unintegrated_tasks`` intentionally uses a narrower outcome
        filter than the audit rule ``check_kept_task_not_promoted``, which
        counts ``("success", "partial")``. A ``partial`` branch with no
        citing plan must NOT be counted here. This pins the deliberate
        divergence so a future widening to ``partial`` is a conscious
        change to this test.
        """
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "partial", "branch": "b1"},
        ]
        count, ids = count_unintegrated_tasks(tasks, [])
        assert count == 0
        assert ids == []

    def test_branch_absent_tasks_excluded(self, _mock_merged: object) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": ""},
            {"task_id": "t2", "outcome": "success"},
        ]
        count, ids = count_unintegrated_tasks(tasks, [])
        assert count == 0
        assert ids == []

    def test_source_task_id_subtraction(self, _mock_merged: object) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "b1"},
            {"task_id": "t2", "outcome": "success", "branch": "b2"},
        ]
        plans: list[dict[str, object]] = [{"source_task_id": "t1"}]
        count, ids = count_unintegrated_tasks(tasks, plans)
        assert count == 1
        assert ids == ["t2"]

    def test_mixed_status_list(self, _mock_merged: object) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "b1"},
            {"task_id": "t2", "outcome": "failure", "branch": "b2"},
            {"task_id": "t3", "outcome": "success", "branch": "b3"},
            {"task_id": "t4", "outcome": "success", "branch": ""},
            {"task_id": "t5", "outcome": "success", "branch": "b5"},
        ]
        plans: list[dict[str, object]] = [{"source_task_id": "t3"}]
        count, ids = count_unintegrated_tasks(tasks, plans)
        assert count == 2
        assert ids == ["t1", "t5"]

    def test_all_cited_returns_zero(self, _mock_merged: object) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "b1"},
        ]
        plans: list[dict[str, object]] = [{"source_task_id": "t1"}]
        count, ids = count_unintegrated_tasks(tasks, plans)
        assert count == 0
        assert ids == []

    def test_plans_with_no_source_task_id(self, _mock_merged: object) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "b1"},
        ]
        plans: list[dict[str, object]] = [{"title": "some plan"}]
        count, ids = count_unintegrated_tasks(tasks, plans)
        assert count == 1
        assert ids == ["t1"]

    def test_plans_with_empty_source_task_id(self, _mock_merged: object) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "b1"},
        ]
        plans: list[dict[str, object]] = [{"source_task_id": ""}]
        count, ids = count_unintegrated_tasks(tasks, plans)
        assert count == 1
        assert ids == ["t1"]

    def test_task_without_task_id_excluded(self, _mock_merged: object) -> None:
        tasks: list[dict[str, object]] = [
            {"outcome": "success", "branch": "b1"},
            {"task_id": "", "outcome": "success", "branch": "b2"},
        ]
        count, ids = count_unintegrated_tasks(tasks, [])
        assert count == 0
        assert ids == []

    def test_merged_branches_excluded(self, _mock_merged: object) -> None:
        """Branches already merged into origin/main are excluded."""
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "b1"},
            {"task_id": "t2", "outcome": "success", "branch": "b2"},
            {"task_id": "t3", "outcome": "success", "branch": "b3"},
        ]

        def selective_merged(branch: str) -> bool:
            return branch == "b2"  # t2 branch is merged

        with patch(
            "maestro.audit_checks.is_branch_merged_into_main",
            side_effect=selective_merged,
        ):
            count, ids = count_unintegrated_tasks(tasks, [])
        assert count == 2
        assert ids == ["t1", "t3"]

    def test_all_merged_returns_zero(self, _mock_merged: object) -> None:
        """All branches merged → zero count."""
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "b1"},
            {"task_id": "t2", "outcome": "success", "branch": "b2"},
        ]
        with patch(
            "maestro.audit_checks.is_branch_merged_into_main",
            return_value=True,
        ):
            count, ids = count_unintegrated_tasks(tasks, [])
        assert count == 0
        assert ids == []


# ──────────────────────────────────────────────────────────────────────
# check_promotable_backlog_directive — directive-bridge complement
# ──────────────────────────────────────────────────────────────────────


@patch("maestro.audit_checks.is_branch_merged_into_main", return_value=False)
class TestCheckPromotableBacklogDirective:
    """Tests for :func:`check_promotable_backlog_directive`."""

    def test_above_threshold_names_oldest_branch(
        self, _mock_merged: object
    ) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "maestro/task/t1"},
            {"task_id": "t2", "outcome": "success", "branch": "maestro/task/t2"},
            {"task_id": "t3", "outcome": "success", "branch": "maestro/task/t3"},
        ]
        result = check_promotable_backlog_directive(tasks, threshold=3)
        assert result is not None
        # Oldest = first task in oldest-first input order.
        assert "maestro/task/t1" in result
        assert "t1" in result
        assert len(result) <= 200

    def test_names_chronologically_oldest_not_lexicographically_smallest(
        self, _mock_merged: object
    ) -> None:
        """Oldest is chosen by input position, not by lexicographic id sort.

        Production task ids are random hex (``uuid.uuid4().hex[:12]``), so
        their lexicographic order is unrelated to creation order. With
        ``recent_tasks`` in oldest-first order (the ``get_recent_tasks``
        contract), the directive must name the *first-in-input* branch, not
        the one with the lexicographically-smallest id. Regression for #2398:
        the old ``sorted(ids)[0]`` selection named ``aa9988776655`` here.
        """
        tasks: list[dict[str, object]] = [
            # Oldest first; lexicographic order (aa < bb < ff) diverges from it.
            {
                "task_id": "ff1122334455",
                "outcome": "success",
                "branch": "maestro/task/ff1122334455",
            },
            {
                "task_id": "aa9988776655",
                "outcome": "success",
                "branch": "maestro/task/aa9988776655",
            },
            {
                "task_id": "bb5544332211",
                "outcome": "success",
                "branch": "maestro/task/bb5544332211",
            },
        ]
        result = check_promotable_backlog_directive(tasks, threshold=3)
        assert result is not None
        # Chronologically oldest = first in input = ff… branch.
        assert "maestro/task/ff1122334455" in result
        assert "ff1122334455" in result
        # The lexicographically-smallest id must not be selected.
        assert "aa9988776655" not in result

    def test_at_threshold_returns_directive(
        self, _mock_merged: object
    ) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "maestro/task/t1"},
            {"task_id": "t2", "outcome": "success", "branch": "maestro/task/t2"},
        ]
        result = check_promotable_backlog_directive(tasks, threshold=2)
        assert result is not None
        assert "maestro/task/t1" in result

    def test_below_threshold_returns_none(
        self, _mock_merged: object
    ) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "maestro/task/t1"},
        ]
        assert check_promotable_backlog_directive(tasks, threshold=3) is None

    def test_empty_task_list_returns_none(
        self, _mock_merged: object
    ) -> None:
        assert check_promotable_backlog_directive([], threshold=3) is None

    def test_tasks_without_branch_excluded(
        self, _mock_merged: object
    ) -> None:
        """Branch-absent tasks do not count toward the backlog."""
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "maestro/task/t1"},
            {"task_id": "t2", "outcome": "success", "branch": ""},
            {"task_id": "t3", "outcome": "success"},
        ]
        # Only one branch-bearing success → below a threshold of 2.
        assert check_promotable_backlog_directive(tasks, threshold=2) is None

    def test_non_positive_threshold_returns_none(
        self, _mock_merged: object
    ) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "outcome": "success", "branch": "maestro/task/t1"},
        ]
        assert check_promotable_backlog_directive(tasks, threshold=0) is None
        assert check_promotable_backlog_directive(tasks, threshold=-1) is None

    def test_long_branch_name_truncated(
        self, _mock_merged: object
    ) -> None:
        """A very long branch name caps the directive at _DIRECTIVE_MAX_CHARS chars."""
        tasks: list[dict[str, object]] = [
            {
                "task_id": "t1",
                "outcome": "success",
                "branch": "maestro/task/" + "x" * 250,
            },
        ]
        result = check_promotable_backlog_directive(tasks, threshold=1)
        assert result is not None
        assert len(result) == _DIRECTIVE_MAX_CHARS
        assert result.endswith("…")

    def test_respects_directive_max_chars_constant(
        self, _mock_merged: object
    ) -> None:
        """check_promotable_backlog_directive must use _DIRECTIVE_MAX_CHARS, not a hardcoded literal.

        If the function hardcodes 200 instead of _DIRECTIVE_MAX_CHARS, patching the
        constant to 100 will have no effect and the result will exceed 100 chars.
        """
        tasks: list[dict[str, object]] = [
            {
                "task_id": "t1",
                "outcome": "success",
                "branch": "maestro/task/" + "x" * 200,
            },
        ]
        import maestro.audit_checks as ac

        with patch.object(ac, "_DIRECTIVE_MAX_CHARS", 100):
            result = check_promotable_backlog_directive(tasks, threshold=1)
        assert result is not None
        assert len(result) <= 100, (
            f"Expected directive to be capped at _DIRECTIVE_MAX_CHARS (100), "
            f"but got length {len(result)} — function hardcodes 200 instead of using the constant."
        )


# ──────────────────────────────────────────────────────────────────────
# format_integration_backlog — prompt section builder
# ──────────────────────────────────────────────────────────────────────


class TestFormatIntegrationBacklog:
    """Tests for :func:`format_integration_backlog`."""

    def test_below_default_threshold_returns_empty(self) -> None:
        assert format_integration_backlog(2) == ""

    def test_at_default_threshold_returns_section(self) -> None:
        result = format_integration_backlog(3)
        assert result.startswith("## Integration Opportunity")
        assert "3" in result

    def test_above_threshold_returns_section(self) -> None:
        result = format_integration_backlog(7)
        assert "## Integration Opportunity" in result
        assert "7" in result

    def test_zero_count_returns_empty(self) -> None:
        assert format_integration_backlog(0) == ""

    def test_custom_threshold_respected(self) -> None:
        assert format_integration_backlog(2, threshold=2) != ""
        assert format_integration_backlog(1, threshold=2) == ""

    def test_section_mentions_source_task_id(self) -> None:
        result = format_integration_backlog(5)
        assert "source_task_id" in result

    def test_section_scopes_count_to_recent_window(self) -> None:
        """The count is framed as a recent-window floor, not a
        project-wide total (#1997).

        The value passed in is hard-capped at the recent-task window at
        the call site, so the wording must scope the count to that window
        ("Among recent tasks") and signal it is a lower bound ("at least")
        rather than an exhaustive backlog total.
        """
        result = format_integration_backlog(5)
        assert "Among recent tasks" in result
        assert "at least" in result
        # Guard against a revert to the unscoped wording, where the bare
        # count led the sentence immediately after the header.
        assert "Integration Opportunity\n5 successful" not in result


# ──────────────────────────────────────────────────────────────────────
# Task-mode classification tests (classify_task_mode / format_task_mode_summary)
# ──────────────────────────────────────────────────────────────────────

def _mode_task(goal: str, result_summary: str = "") -> dict[str, object]:
    """Minimal task dict for mode classification tests."""
    d: dict[str, object] = {"goal": goal}
    if result_summary:
        d["result_summary"] = result_summary
    return d


def test_classify_task_mode_maintenance_promote() -> None:
    """Promotion tasks are classified as maintenance."""
    assert classify_task_mode(_mode_task("Promote branch t42 into main")) == "maintenance"


def test_classify_task_mode_maintenance_rebase() -> None:
    """Rebase tasks are classified as maintenance."""
    assert classify_task_mode(_mode_task("Rebase PR branch on latest main")) == "maintenance"


def test_classify_task_mode_maintenance_remediation() -> None:
    """Remediation tasks are classified as maintenance."""
    assert classify_task_mode(_mode_task("Remediate failing CI on PR #2052")) == "maintenance"


def test_classify_task_mode_generative_add() -> None:
    """Tasks adding new features are classified as generative."""
    assert classify_task_mode(_mode_task("Add a cost-tracking dashboard widget")) == "generative"


def test_classify_task_mode_generative_implement() -> None:
    """Tasks implementing new mechanisms are classified as generative."""
    assert classify_task_mode(_mode_task("Implement BM25 search ranking")) == "generative"


def test_classify_task_mode_generative_measure() -> None:
    """Measurement tasks are classified as generative."""
    assert classify_task_mode(_mode_task("Measure false-positive rate of audit rules")) == "generative"


def test_classify_task_mode_both_keywords_maintenance_wins() -> None:
    """When both maintenance and generative keywords match, maintenance wins."""
    task = _mode_task("Add a fix ci for the promote pipeline")
    assert classify_task_mode(task) == "maintenance"


def test_classify_task_mode_no_keywords_defaults_generative() -> None:
    """Tasks matching neither keyword set default to generative."""
    assert classify_task_mode(_mode_task("Think about the meaning of life")) == "generative"


def test_classify_task_mode_uses_result_summary() -> None:
    """Keywords in result_summary are also checked."""
    task = _mode_task("Do stuff", result_summary="Remediation complete")
    assert classify_task_mode(task) == "maintenance"


def test_classify_task_mode_no_substring_false_positives() -> None:
    """Short keywords must not match as substrings of common English words.

    Without word-boundary matching, the maintenance keyword ``tend`` matched
    inside ``extend`` / ``intend`` / ``attend`` and ``add`` matched inside
    ``address``, biasing the ratio toward maintenance.
    """
    # 'tend' substring should NOT match 'extend'/'intend'/'attend'.
    assert classify_task_mode(_mode_task("Extend the audit framework")) == "generative"
    assert classify_task_mode(_mode_task("Intend to investigate something")) == "generative"
    assert classify_task_mode(_mode_task("Attend to follow-up")) == "generative"
    # Legitimate matches must still work.
    assert classify_task_mode(_mode_task("Tend evolution PRs")) == "maintenance"
    assert classify_task_mode(_mode_task("Add a new measurement")) == "generative"


def test_format_task_mode_summary_below_threshold() -> None:
    """Returns empty string when fewer than 3 tasks."""
    tasks = [_mode_task("Add foo"), _mode_task("Build bar")]
    assert format_task_mode_summary(tasks) == ""


def test_format_task_mode_summary_empty_list() -> None:
    """Returns empty string for empty input."""
    assert format_task_mode_summary([]) == ""


def test_format_task_mode_summary_format_with_counts() -> None:
    """Returns a one-liner with correct maintenance/generative counts."""
    tasks = [
        _mode_task("Promote branch t1"),
        _mode_task("Rebase PR #3"),
        _mode_task("Remediate CI"),
        _mode_task("Add cost widget"),
        _mode_task("Implement search"),
        _mode_task("Tend PR branches"),
        _mode_task("Build dashboard"),
        _mode_task("Create new gate"),
        _mode_task("Fix CI pipeline"),
        _mode_task("Detect loop patterns"),
    ]
    summary = format_task_mode_summary(tasks)
    assert summary != ""
    assert "TASK MODE BALANCE:" in summary
    assert "maintenance" in summary
    assert "generative" in summary
    assert "last 10 tasks" in summary
    # 5 maintenance (promote, rebase, remediate, tend, fix ci) + 5 generative
    assert "5 maintenance" in summary
    assert "5 generative" in summary


def test_format_task_mode_summary_all_maintenance() -> None:
    """All-maintenance distribution is correctly rendered."""
    tasks = [
        _mode_task("Promote t1"),
        _mode_task("Rebase main"),
        _mode_task("Tend PRs"),
    ]
    summary = format_task_mode_summary(tasks)
    assert "3 maintenance / 0 generative" in summary


def test_format_task_mode_summary_all_generative() -> None:
    """All-generative distribution is correctly rendered."""
    tasks = [
        _mode_task("Add widget"),
        _mode_task("Build feature"),
        _mode_task("Create tool"),
    ]
    summary = format_task_mode_summary(tasks)
    assert "0 maintenance / 3 generative" in summary


def test_concrete_shapes_derived_from_taskshape_literal() -> None:
    """``_CONCRETE_SHAPES`` must stay in lockstep with the ``TaskShape`` Literal.

    Regression guard for the duplicated-vocabulary trap: if anyone reverts to
    a hardcoded concrete-shape list that drifts from the Literal, this fails.
    The expected set is re-derived independently here from
    ``typing.get_args(TaskShape)`` minus the residual ``"other"`` bucket.
    """
    from typing import get_args

    from maestro.audit_checks import _CONCRETE_SHAPES, TaskShape

    expected = {s for s in get_args(TaskShape) if s != "other"}
    assert set(_CONCRETE_SHAPES) == expected
    # The residual catch-all must never appear among the concrete shapes.
    assert "other" not in _CONCRETE_SHAPES


# ---------------------------------------------------------------------------
# No-op task classification: is_no_op_task + compute_no_op_rate
# ---------------------------------------------------------------------------


class TestIsNoOpTaskMatchesKnownMarkers:
    """Each marker in _NO_OP_MARKERS individually triggers is_no_op_task."""

    @pytest.mark.parametrize("marker", _NO_OP_MARKERS)
    def test_each_marker_matches(self, marker: str) -> None:
        assert is_no_op_task(f"Task result: {marker} for this item")

    @pytest.mark.parametrize("marker", _NO_OP_MARKERS)
    def test_case_insensitive(self, marker: str) -> None:
        assert is_no_op_task(marker.upper())

    def test_marker_count_minimum(self) -> None:
        """Plan requires ≥5 markers."""
        assert len(_NO_OP_MARKERS) >= 5


class TestIsNoOpTaskRejectsMeaningfulSummaries:
    """Meaningful task summaries must not be classified as no-op."""

    def test_rejects_code_change(self) -> None:
        assert not is_no_op_task(
            "Added compute_no_op_rate function to audit_checks.py "
            "and updated health_check.py integration."
        )

    def test_rejects_bug_fix(self) -> None:
        assert not is_no_op_task(
            "Fixed race condition in session attachment by adding "
            "a mutex guard around the shared state."
        )

    def test_rejects_refactoring(self) -> None:
        assert not is_no_op_task(
            "Refactored the executor loop detection to use "
            "fingerprint-based deduplication instead of string matching."
        )


class TestIsNoOpTaskHandlesNoneAndEmpty:
    """Edge cases: None and empty string."""

    def test_none_returns_false(self) -> None:
        assert not is_no_op_task(None)

    def test_empty_returns_false(self) -> None:
        assert not is_no_op_task("")

    def test_whitespace_only_returns_false(self) -> None:
        assert not is_no_op_task("   ")


class TestComputeNoOpRateMixedWindow:
    """Mixed task windows yield correct no-op fractions."""

    def test_mixed_window(self) -> None:
        tasks = [
            _task("t1", outcome="success", goal="do stuff"),
            _task("t2", outcome="success", goal="check things"),
            _task("t3", outcome="success", goal="fix bug"),
            _task("t4", outcome="success", goal="verify"),
        ]
        # Replace with tasks that have result_summary set
        tasks[0] = TaskRecord(
            task_id="t1", goal="do stuff", plan=[], outcome="success",
            result_summary="No changes needed — already correct.",
            errors=[], duration_secs=1.0, timestamp="2026-01-01T00:00:00",
        )
        tasks[1] = TaskRecord(
            task_id="t2", goal="check things", plan=[], outcome="success",
            result_summary="Shipped the new health condition.",
            errors=[], duration_secs=1.0, timestamp="2026-01-01T00:00:00",
        )
        tasks[2] = TaskRecord(
            task_id="t3", goal="fix bug", plan=[], outcome="success",
            result_summary="Was a no-op; config already set.",
            errors=[], duration_secs=1.0, timestamp="2026-01-01T00:00:00",
        )
        tasks[3] = TaskRecord(
            task_id="t4", goal="verify", plan=[], outcome="success",
            result_summary="Implemented the new gate and added tests.",
            errors=[], duration_secs=1.0, timestamp="2026-01-01T00:00:00",
        )
        rate = compute_no_op_rate(tasks, window=4)
        assert rate == pytest.approx(0.5)  # 2 of 4

    def test_window_truncation(self) -> None:
        """Only the last `window` tasks are considered."""
        meaningful = TaskRecord(
            task_id="t0", goal="meaningful", plan=[], outcome="success",
            result_summary="Shipped a real feature.",
            errors=[], duration_secs=1.0, timestamp="2026-01-01T00:00:00",
        )
        no_op = TaskRecord(
            task_id="t1", goal="check", plan=[], outcome="success",
            result_summary="No changes needed.",
            errors=[], duration_secs=1.0, timestamp="2026-01-01T00:00:00",
        )
        tasks = [no_op, meaningful, meaningful, meaningful]
        # Window=3 sees only the last 3 (all meaningful)
        assert compute_no_op_rate(tasks, window=3) == pytest.approx(0.0)
        # Window=4 sees the no_op at position 0
        assert compute_no_op_rate(tasks, window=4) == pytest.approx(0.25)


class TestComputeNoOpRateEmptyReturnsZero:
    """Empty inputs return 0.0."""

    def test_empty_task_list(self) -> None:
        assert compute_no_op_rate([], window=10) == 0.0

    def test_zero_window(self) -> None:
        tasks = [_task("t1")]
        assert compute_no_op_rate(tasks, window=0) == 0.0

    def test_negative_window(self) -> None:
        tasks = [_task("t1")]
        assert compute_no_op_rate(tasks, window=-1) == 0.0


# ---------------------------------------------------------------------------
# classify_instrument_task
# ---------------------------------------------------------------------------


class TestClassifyInstrumentTask:
    """Positive and negative cases for the instrument-task regex classifier."""

    def test_hypothesis_goal(self) -> None:
        assert classify_instrument_task({"goal": "Test hypothesis about loop cost"})

    def test_gate_goal(self) -> None:
        assert classify_instrument_task({"goal": "Add cost-risk gate to generate_task"})

    def test_measurement_goal(self) -> None:
        assert classify_instrument_task({"goal": "Build measurement of PR merge latency"})

    def test_experiment_goal(self) -> None:
        assert classify_instrument_task({"goal": "Run experiment on shape-balance threshold"})

    def test_instrument_goal(self) -> None:
        assert classify_instrument_task({"goal": "Ship instrument for journal compression"})

    def test_metric_goal(self) -> None:
        assert classify_instrument_task({"goal": "Add metric tracking iteration durations"})

    def test_apparatus_goal(self) -> None:
        assert classify_instrument_task({"goal": "Build apparatus for cost sampling"})

    def test_branch_triage_negative(self) -> None:
        assert not classify_instrument_task({"goal": "Triage branch conflicts on PR #1932"})

    def test_engineering_negative(self) -> None:
        assert not classify_instrument_task({"goal": "Refactor executor loop detection"})

    def test_synthesis_negative(self) -> None:
        assert not classify_instrument_task({"goal": "Synthesise dream digest from journal"})

    def test_plain_analysis_negative(self) -> None:
        assert not classify_instrument_task({"goal": "Analyse recent task success rate"})

    def test_missing_goal(self) -> None:
        assert not classify_instrument_task({})

    def test_non_string_goal(self) -> None:
        assert not classify_instrument_task({"goal": 42})


# ---------------------------------------------------------------------------
# find_unconsumed_instruments
# ---------------------------------------------------------------------------


class TestFindUnconsumedInstruments:
    """Tests for the unconsumed-instrument scanner."""

    def test_unconsumed_returned_oldest_first(self) -> None:
        """Instrument tasks with no later reference are returned oldest-first."""
        tasks: list[dict[str, object]] = [
            {"task_id": "i1", "goal": "Build measurement of X"},
            {"task_id": "i2", "goal": "Ship gate for cost risk"},
            {"task_id": "i3", "goal": "Add hypothesis test for Y"},
            {"task_id": "t4", "goal": "Plain engineering work"},
        ]
        result = find_unconsumed_instruments(tasks)
        ids = [str(r["task_id"]) for r in result]
        assert ids == ["i1", "i2", "i3"]

    def test_consumed_via_goal_reference(self) -> None:
        """Instrument referenced by a later task's goal is excluded."""
        tasks: list[dict[str, object]] = [
            {"task_id": "i1", "goal": "Build measurement of X"},
            {"task_id": "t2", "goal": "Review output of task i1 and extend"},
        ]
        result = find_unconsumed_instruments(tasks)
        assert result == []

    def test_consumed_via_result_summary(self) -> None:
        """Instrument referenced by a later task's result_summary is excluded."""
        tasks: list[dict[str, object]] = [
            {"task_id": "i1", "goal": "Run experiment on thresholds"},
            {"task_id": "t2", "goal": "Unrelated work",
             "result_summary": "Extended findings from i1"},
        ]
        result = find_unconsumed_instruments(tasks)
        assert result == []

    def test_consumed_via_linked_question(self) -> None:
        """Instrument referenced in linked_question counts as consumed."""
        tasks: list[dict[str, object]] = [
            {"task_id": "i1", "goal": "Build hypothesis test"},
            {"task_id": "t2", "goal": "Follow up",
             "linked_question": "What did i1 reveal?"},
        ]
        result = find_unconsumed_instruments(tasks)
        assert result == []

    def test_all_consumed_returns_empty(self) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "i1", "goal": "Build measurement of X"},
            {"task_id": "t2", "goal": "Review i1 output"},
            {"task_id": "i3", "goal": "Run experiment on Y"},
            {"task_id": "t4", "goal": "Continue i3 analysis"},
        ]
        result = find_unconsumed_instruments(tasks)
        assert result == []

    def test_no_instruments_returns_empty(self) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "t1", "goal": "Refactor executor"},
            {"task_id": "t2", "goal": "Fix CI pipeline"},
        ]
        result = find_unconsumed_instruments(tasks)
        assert result == []

    def test_lookback_window_respected(self) -> None:
        """Only the last *lookback* tasks are considered."""
        tasks: list[dict[str, object]] = [
            {"task_id": "i1", "goal": "Build measurement (old)"},
            {"task_id": "t2", "goal": "Plain work"},
            {"task_id": "t3", "goal": "Plain work"},
        ]
        # lookback=2 means only t2 and t3 are in the window; i1 is excluded.
        result = find_unconsumed_instruments(tasks, lookback=2)
        assert result == []

    def test_lookback_zero_returns_empty(self) -> None:
        tasks: list[dict[str, object]] = [
            {"task_id": "i1", "goal": "Build measurement of X"},
        ]
        result = find_unconsumed_instruments(tasks, lookback=0)
        assert result == []

    def test_missing_task_id_skipped(self) -> None:
        """Instrument without a task_id is ignored."""
        tasks: list[dict[str, object]] = [
            {"goal": "Build measurement of X"},
        ]
        result = find_unconsumed_instruments(tasks)
        assert result == []

    def test_threshold_constant_is_three(self) -> None:
        """Guard the public constant value used by the agent bridge."""
        assert INSTRUMENT_CONSUMPTION_THRESHOLD == 3


# ---------------------------------------------------------------------------
# is_integration_task — keyword detection for integration work
# ---------------------------------------------------------------------------


class TestIsIntegrationTaskPositive:
    """Positive matches for integration-specific keywords."""

    def test_triage_keyword(self) -> None:
        assert is_integration_task("Triage unmerged branches and select one")

    def test_unmerged_keyword(self) -> None:
        assert is_integration_task("Address unmerged successful task branches")

    def test_promote_branch(self) -> None:
        assert is_integration_task("Promote branch maestro/task/123 to main")

    def test_branch_promotion(self) -> None:
        assert is_integration_task("Branch promotion for recent work")

    def test_promote_pr(self) -> None:
        assert is_integration_task("Promote PR #456 into main")

    def test_integration_debt(self) -> None:
        assert is_integration_task("Address integration debt across branches")

    def test_integration_backlog(self) -> None:
        assert is_integration_task("Reduce integration backlog of 12 branches")

    def test_case_insensitive(self) -> None:
        assert is_integration_task("TRIAGE UNMERGED BRANCHES")


class TestIsIntegrationTaskNegative:
    """Negative matches — routine tasks must not trigger."""

    def test_empty_string(self) -> None:
        assert not is_integration_task("")

    def test_regular_task(self) -> None:
        assert not is_integration_task("Add new audit rule to check cost")

    def test_measurement_task(self) -> None:
        assert not is_integration_task("Measure task completion rate")

    def test_github_issue(self) -> None:
        assert not is_integration_task("Fix #1234 — dashboard crash")

    def test_branch_without_promote(self) -> None:
        assert not is_integration_task("Create a branch for feature work")


# ---------------------------------------------------------------------------
# check_integration_cadence — directive injection when integration is absent
# ---------------------------------------------------------------------------


def test_integration_cadence_fires_when_no_integration() -> None:
    """Directive returned when no task in the window is integration work."""
    tasks = [
        {"goal": "Add new audit rule"},
        {"goal": "Measure task completion rate"},
        {"goal": "Fix #1234 dashboard crash"},
        {"goal": "Extend consciousness module"},
        {"goal": "Refactor executor loop"},
    ]
    result = check_integration_cadence(tasks, cadence=5)
    assert result is not None
    assert "integration" in result.lower() or "triage" in result.lower()


def test_integration_cadence_none_when_recent_integration() -> None:
    """No directive when one task in the window is integration work."""
    tasks = [
        {"goal": "Add new audit rule"},
        {"goal": "Triage unmerged successful branches"},
        {"goal": "Fix #1234 dashboard crash"},
    ]
    result = check_integration_cadence(tasks, cadence=3)
    assert result is None


def test_integration_cadence_none_when_empty() -> None:
    """No directive on empty task list."""
    assert check_integration_cadence([], cadence=5) is None


def test_integration_cadence_respects_window_size() -> None:
    """Only the last N tasks are checked, not the full list."""
    # Integration task is outside the cadence window (position 0),
    # the last 3 tasks are all non-integration.
    tasks = [
        {"goal": "Triage unmerged branches"},  # outside window
        {"goal": "Add new feature"},
        {"goal": "Fix bug"},
        {"goal": "Write tests"},
    ]
    result = check_integration_cadence(tasks, cadence=3)
    assert result is not None  # the integration task is outside the window

    # Same tasks but with cadence=4 — integration task is inside the window.
    result2 = check_integration_cadence(tasks, cadence=4)
    assert result2 is None


def test_integration_cadence_zero_cadence_returns_none() -> None:
    """cadence <= 0 always returns None (disables the gate)."""
    tasks = [{"goal": "Add new feature"}]
    assert check_integration_cadence(tasks, cadence=0) is None
    assert check_integration_cadence(tasks, cadence=-1) is None


# ---------------------------------------------------------------------------
# check_investigation_streak — construction redirect after sustained investigation
# ---------------------------------------------------------------------------


def test_investigation_streak_empty_returns_none() -> None:
    """Empty task list always returns None."""
    assert check_investigation_streak([], threshold=3) is None


def test_investigation_streak_fewer_than_threshold_returns_none() -> None:
    """Fewer tasks than threshold returns None."""
    tasks = [
        _task("t1", goal="Measure task completion rate"),
        _task("t2", goal="Audit self-model predictions"),
    ]
    assert check_investigation_streak(tasks, threshold=3) is None


def test_investigation_streak_exact_threshold_all_investigation() -> None:
    """Exact threshold consecutive investigation tasks returns directive."""
    tasks = [
        _task("t1", goal="Measure task completion rate"),
        _task("t2", goal="Count no-op tasks in recent history"),
        _task("t3", goal="Audit self-model predictions"),
    ]
    result = check_investigation_streak(tasks, threshold=3)
    assert result is not None
    assert "construction" in result.lower()
    assert "3" in result


def test_investigation_streak_mixed_shapes_returns_none() -> None:
    """A construction task in the window breaks the streak."""
    tasks = [
        _task("t1", goal="Measure task completion rate"),
        _task("t2", goal="Fix #1234 dashboard crash"),  # github_issue shape
        _task("t3", goal="Audit self-model predictions"),
    ]
    assert check_investigation_streak(tasks, threshold=3) is None


def test_investigation_streak_threshold_one_edge_case() -> None:
    """Threshold=1 fires on a single investigation task."""
    tasks = [
        _task("t1", goal="Measure the audit gate trigger rate"),
    ]
    result = check_investigation_streak(tasks, threshold=1)
    assert result is not None
    assert "construction" in result.lower()


def test_investigation_streak_zero_threshold_returns_none() -> None:
    """Threshold <= 0 always returns None."""
    tasks = [_task("t1", goal="Measure something")]
    assert check_investigation_streak(tasks, threshold=0) is None
    assert check_investigation_streak(tasks, threshold=-1) is None


def test_investigation_streak_only_checks_tail() -> None:
    """Only the last threshold tasks matter, not earlier ones."""
    tasks = [
        _task("t1", goal="Measure old thing"),           # investigation
        _task("t2", goal="Fix #1234 bug"),               # construction (github_issue)
        _task("t3", goal="Measure new thing"),            # investigation
        _task("t4", goal="Count task completions"),       # investigation
    ]
    # Last 2 are investigation, but threshold is 3 — the fix at t2 breaks it
    assert check_investigation_streak(tasks, threshold=3) is None
    # Last 2 are investigation and threshold is 2 — fires
    result = check_investigation_streak(tasks, threshold=2)
    assert result is not None


def test_investigation_streak_other_shape_counts_as_investigation() -> None:
    """Tasks with 'other' shape (no specific pattern) count as investigation."""
    tasks = [
        _task("t1", goal="Think about next steps"),       # other
        _task("t2", goal="Reflect on recent outcomes"),    # other
        _task("t3", goal="Consider improvement areas"),    # other
    ]
    result = check_investigation_streak(tasks, threshold=3)
    assert result is not None


def test_investigation_streak_branch_promotion_breaks_streak() -> None:
    """Branch promotion shape is construction — breaks the streak."""
    tasks = [
        _task("t1", goal="Measure completion rate"),
        _task("t2", goal="Promote task branch via source_task_id"),
        _task("t3", goal="Count recent tasks"),
    ]
    assert check_investigation_streak(tasks, threshold=3) is None


def test_investigation_streak_module_extension_breaks_streak() -> None:
    """Module extension shape is construction — breaks the streak."""
    tasks = [
        _task("t1", goal="Count something"),
        _task("t2", goal="Add new method to module executor"),
        _task("t3", goal="Measure another thing"),
    ]
    assert check_investigation_streak(tasks, threshold=3) is None


# ---------------------------------------------------------------------------
# count_consecutive_branchless_tasks — trailing analysis-only streak length
# ---------------------------------------------------------------------------


def test_branchless_streak_empty_returns_zero() -> None:
    """Empty record list has no streak."""
    assert count_consecutive_branchless_tasks([]) == 0


def test_branchless_streak_all_branchless_returns_full_length() -> None:
    """Every record branchless → streak equals the list length."""
    records = [
        {"task_id": "t1", "branch": None},
        {"task_id": "t2", "branch": ""},
        {"task_id": "t3"},  # missing branch key
    ]
    assert count_consecutive_branchless_tasks(records) == 3


def test_branchless_streak_all_branched_returns_zero() -> None:
    """Most recent task has a branch → streak is 0 regardless of history."""
    records = [
        {"task_id": "t1", "branch": "maestro/task/t1"},
        {"task_id": "t2", "branch": "maestro/task/t2"},
    ]
    assert count_consecutive_branchless_tasks(records) == 0


def test_branchless_streak_mixed_returns_tail_streak() -> None:
    """Only the trailing branchless run counts; a branch stops the walk."""
    records = [
        {"task_id": "t1", "branch": None},               # before the wall
        {"task_id": "t2", "branch": "maestro/task/t2"},  # wall
        {"task_id": "t3", "branch": None},               # tail streak
        {"task_id": "t4", "branch": ""},                 # tail streak
    ]
    assert count_consecutive_branchless_tasks(records) == 2


def test_branchless_streak_single_branchless() -> None:
    """A single branchless record yields a streak of 1."""
    assert count_consecutive_branchless_tasks([{"task_id": "t1"}]) == 1


def test_branchless_streak_single_branched() -> None:
    """A single branched record yields a streak of 0."""
    records = [{"task_id": "t1", "branch": "maestro/task/t1"}]
    assert count_consecutive_branchless_tasks(records) == 0


# ---------------------------------------------------------------------------
# compute_shape_investigation_rate — windowed investigation fraction
# ---------------------------------------------------------------------------


def test_shape_investigation_rate_empty_history(
    mem_fs: dict[str, str],
) -> None:
    """No task history returns 0.0."""
    assert compute_shape_investigation_rate(FAKE_DIR, window=10) == 0.0


def test_shape_investigation_rate_all_investigation(
    mem_fs: dict[str, str],
) -> None:
    """A window of only investigation-shaped tasks returns 1.0."""
    mem = Memory(FAKE_DIR)
    for i in range(5):
        mem.record_task(_task(f"t{i}", goal="Measure task completion rate"))
    assert compute_shape_investigation_rate(FAKE_DIR, window=10) == 1.0


def test_shape_investigation_rate_no_investigation(
    mem_fs: dict[str, str],
) -> None:
    """A window with no investigation-shaped tasks returns 0.0."""
    mem = Memory(FAKE_DIR)
    for i in range(4):
        mem.record_task(_task(f"t{i}", goal="Fix #1234 dashboard crash"))
    assert compute_shape_investigation_rate(FAKE_DIR, window=10) == 0.0


def test_shape_investigation_rate_mixed_fraction(
    mem_fs: dict[str, str],
) -> None:
    """Mixed window returns the investigation fraction."""
    mem = Memory(FAKE_DIR)
    # 3 investigation + 1 construction = 0.75
    mem.record_task(_task("t0", goal="Measure completion rate"))
    mem.record_task(_task("t1", goal="Count no-op tasks"))
    mem.record_task(_task("t2", goal="Fix #99 crash"))
    mem.record_task(_task("t3", goal="Audit self-model predictions"))
    assert compute_shape_investigation_rate(FAKE_DIR, window=10) == 0.75


def test_shape_investigation_rate_six_of_ten_is_point_six(
    mem_fs: dict[str, str],
) -> None:
    """Acceptance criterion: 6 investigation-shaped of 10 tasks → 0.6.

    Mirrors the binding acceptance criterion verbatim — six of the ten most
    recent tasks classify into ``_INVESTIGATION_SHAPES`` (``measure`` →
    ``measurement``) and four are construction-shaped (``Fix #N`` →
    ``github_issue``), so the windowed fraction is exactly 0.6.
    """
    mem = Memory(FAKE_DIR)
    # 6 investigation-shaped (measurement) tasks.
    for i in range(6):
        mem.record_task(_task(f"inv{i}", goal=f"Measure metric {i}"))
    # 4 construction-shaped (github_issue) tasks.
    for i in range(4):
        mem.record_task(_task(f"fix{i}", goal=f"Fix #{i + 1} bug"))
    rate = compute_shape_investigation_rate(FAKE_DIR, window=10)
    assert rate == 0.6


def test_shape_investigation_rate_window_truncates_tail(
    mem_fs: dict[str, str],
) -> None:
    """Only the last *window* tasks are measured, not the whole history."""
    mem = Memory(FAKE_DIR)
    # Older construction tasks fall outside the window.
    mem.record_task(_task("t0", goal="Fix #1 bug"))
    mem.record_task(_task("t1", goal="Fix #2 bug"))
    mem.record_task(_task("t2", goal="Measure completion rate"))
    mem.record_task(_task("t3", goal="Count recent tasks"))
    # Window of 2 sees only the two trailing investigation tasks.
    assert compute_shape_investigation_rate(FAKE_DIR, window=2) == 1.0


def test_shape_investigation_rate_sub_window_history(
    mem_fs: dict[str, str],
) -> None:
    """Fewer tasks than the window divides by the actual count."""
    mem = Memory(FAKE_DIR)
    mem.record_task(_task("t0", goal="Measure completion rate"))
    mem.record_task(_task("t1", goal="Fix #5 bug"))
    # 1 of 2 present tasks is investigation-shaped.
    assert compute_shape_investigation_rate(FAKE_DIR, window=10) == 0.5


def test_shape_investigation_rate_zero_window(
    mem_fs: dict[str, str],
) -> None:
    """Window <= 0 returns 0.0 without reading history."""
    mem = Memory(FAKE_DIR)
    mem.record_task(_task("t0", goal="Measure completion rate"))
    assert compute_shape_investigation_rate(FAKE_DIR, window=0) == 0.0
    assert compute_shape_investigation_rate(FAKE_DIR, window=-1) == 0.0


# ---------------------------------------------------------------------------
# classify_noop_task
# ---------------------------------------------------------------------------


class TestClassifyNoopTaskPositiveMatches:
    """Each pattern in _NOOP_TASK_PATTERNS individually triggers classify_noop_task."""

    @pytest.mark.parametrize("pattern", _NOOP_TASK_PATTERNS)
    def test_each_pattern_matches(self, pattern: str) -> None:
        task: dict[str, object] = {"result_summary": f"Task outcome: {pattern} here"}
        assert classify_noop_task(task)

    @pytest.mark.parametrize("pattern", _NOOP_TASK_PATTERNS)
    def test_case_insensitive(self, pattern: str) -> None:
        task: dict[str, object] = {"result_summary": pattern.upper()}
        assert classify_noop_task(task)

    @pytest.mark.parametrize("pattern", _NOOP_TASK_PATTERNS)
    def test_mixed_case(self, pattern: str) -> None:
        # Alternate upper/lower per character
        mixed = "".join(
            c.upper() if i % 2 == 0 else c.lower()
            for i, c in enumerate(pattern)
        )
        task: dict[str, object] = {"result_summary": mixed}
        assert classify_noop_task(task)

    def test_pattern_embedded_in_longer_summary(self) -> None:
        task: dict[str, object] = {
            "result_summary": (
                "Checked all quality gates — already passing before any "
                "changes were applied to the codebase."
            ),
        }
        assert classify_noop_task(task)

    def test_pattern_count_minimum(self) -> None:
        """Task requires the 7 specific patterns listed in the spec."""
        assert len(_NOOP_TASK_PATTERNS) >= 7

    def test_merge_truth_standdown_record_is_noop(self) -> None:
        """The merge-truth-first stand-down record (``run`` declining to service
        an already-settled PR) classifies as a no-op, satisfying the acceptance
        criterion that a stand-down is a no-op and not a success (#2662)."""
        from maestro.remediation_tracker import _CHOSEN_QUIET_MARKER

        task: dict[str, object] = {
            "result_summary": _CHOSEN_QUIET_MARKER,
            "outcome": "partial",
        }
        assert classify_noop_task(task)


class TestClassifyNoopTaskNegativeMatches:
    """Meaningful task summaries must not be classified as no-op."""

    def test_rejects_code_change(self) -> None:
        task: dict[str, object] = {
            "result_summary": (
                "Added classify_noop_task function to audit_checks.py "
                "and updated tests."
            ),
        }
        assert not classify_noop_task(task)

    def test_rejects_bug_fix(self) -> None:
        task: dict[str, object] = {
            "result_summary": (
                "Fixed race condition in session attachment by adding "
                "a mutex guard around the shared state."
            ),
        }
        assert not classify_noop_task(task)

    def test_rejects_refactoring(self) -> None:
        task: dict[str, object] = {
            "result_summary": (
                "Refactored the executor loop detection to use "
                "fingerprint-based deduplication."
            ),
        }
        assert not classify_noop_task(task)

    def test_rejects_partial_pattern_match(self) -> None:
        """'already' alone is not a pattern — must match the full substring."""
        task: dict[str, object] = {
            "result_summary": "Already shipped the new feature.",
        }
        assert not classify_noop_task(task)

    def test_rejects_generic_success(self) -> None:
        task: dict[str, object] = {
            "result_summary": "Successfully implemented the new health check.",
        }
        assert not classify_noop_task(task)


class TestClassifyNoopTaskMissingAndEmpty:
    """Edge cases: missing key, None, empty, whitespace-only."""

    def test_missing_result_summary(self) -> None:
        assert not classify_noop_task({})

    def test_none_result_summary(self) -> None:
        task: dict[str, object] = {"result_summary": None}
        assert not classify_noop_task(task)

    def test_empty_result_summary(self) -> None:
        task: dict[str, object] = {"result_summary": ""}
        assert not classify_noop_task(task)

    def test_whitespace_only_result_summary(self) -> None:
        task: dict[str, object] = {"result_summary": "   "}
        assert not classify_noop_task(task)

    def test_non_string_result_summary(self) -> None:
        task: dict[str, object] = {"result_summary": 42}
        assert not classify_noop_task(task)

    def test_list_result_summary(self) -> None:
        task: dict[str, object] = {"result_summary": ["no remediation"]}
        assert not classify_noop_task(task)


class TestClassifyNoopTaskEdgeCases:
    """Edge cases for boundary behaviour."""

    def test_exact_pattern_only(self) -> None:
        """A summary that is exactly a pattern string matches."""
        task: dict[str, object] = {"result_summary": "no-op"}
        assert classify_noop_task(task)

    def test_pattern_at_start(self) -> None:
        task: dict[str, object] = {
            "result_summary": "No remediation needed for this PR.",
        }
        assert classify_noop_task(task)

    def test_pattern_at_end(self) -> None:
        task: dict[str, object] = {
            "result_summary": "The branch was already green",
        }
        assert classify_noop_task(task)

    def test_multiple_patterns_in_same_summary(self) -> None:
        """Multiple patterns in one summary — still returns True (not double-True)."""
        task: dict[str, object] = {
            "result_summary": "No remediation; already passing; confirmed existing state.",
        }
        assert classify_noop_task(task)

    def test_other_task_fields_ignored(self) -> None:
        """Only result_summary is checked, not goal or other fields."""
        task: dict[str, object] = {
            "goal": "no remediation",
            "result_summary": "Shipped a meaningful feature.",
        }
        assert not classify_noop_task(task)

    def test_empty_dict(self) -> None:
        assert not classify_noop_task({})


# ---------------------------------------------------------------------------
# measure_noop_rate
# ---------------------------------------------------------------------------


def _success_task(
    source: str,
    result_summary: str,
    outcome: str = "success",
) -> dict[str, object]:
    """Helper to build a synthetic task dict for measure_noop_rate tests."""
    return {
        "outcome": outcome,
        "source": source,
        "result_summary": result_summary,
    }


def test_success_outcome_sets_subset_of_task_outcome() -> None:
    """Success sets must never drift from the canonical TaskOutcome vocabulary."""
    from typing import get_args

    from maestro.types import TaskOutcome

    valid_outcomes = set(get_args(TaskOutcome))
    assert audit_checks._SUCCESS_OUTCOMES.issubset(valid_outcomes)
    assert audit_checks._ORIGIN_SUCCESS_OUTCOMES.issubset(valid_outcomes)


class TestMeasureNoopRateAllSources:
    """Mixed sources with known no-op and meaningful tasks."""

    def test_all_three_sources_with_mixed_outcomes(self) -> None:
        tasks: list[dict[str, object]] = [
            # pending_pr_requests: 2 success, 1 noop
            _success_task("pending_pr_requests", "No remediation needed"),
            _success_task("pending_pr_requests", "Fixed merge conflict and pushed"),
            # pending_user_requests: 2 success, 1 noop
            _success_task("pending_user_requests", "Confirmed existing state is correct"),
            _success_task("pending_user_requests", "Implemented the requested feature"),
            # queue: 3 success, 2 noop
            _success_task("queue", "Already passing all tests"),
            _success_task("queue", "Already green after upstream merge"),
            _success_task("queue", "Shipped new audit gate and tests"),
        ]
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 7
        assert result["noop_count"] == 4
        assert result["noop_rate"] == pytest.approx(4 / 7)

        by_source = result["by_source"]
        assert isinstance(by_source, dict)

        pr = by_source["pending_pr_requests"]
        assert pr["total"] == 2
        assert pr["noop"] == 1
        assert pr["rate"] == pytest.approx(0.5)

        user = by_source["pending_user_requests"]
        assert user["total"] == 2
        assert user["noop"] == 1
        assert user["rate"] == pytest.approx(0.5)

        queue = by_source["queue"]
        assert queue["total"] == 3
        assert queue["noop"] == 2
        assert queue["rate"] == pytest.approx(2 / 3)

    def test_single_source_all_noop(self) -> None:
        tasks: list[dict[str, object]] = [
            _success_task("pending_pr_requests", "No remediation needed"),
            _success_task("pending_pr_requests", "Already resolved upstream"),
        ]
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 2
        assert result["noop_count"] == 2
        assert result["noop_rate"] == pytest.approx(1.0)
        assert result["by_source"]["pending_pr_requests"]["rate"] == pytest.approx(1.0)

    def test_single_source_no_noop(self) -> None:
        tasks: list[dict[str, object]] = [
            _success_task("queue", "Built the new health check integration"),
            _success_task("queue", "Refactored executor loop detection"),
        ]
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 2
        assert result["noop_count"] == 0
        assert result["noop_rate"] == pytest.approx(0.0)
        assert result["by_source"]["queue"]["rate"] == pytest.approx(0.0)


class TestMeasureNoopRateOutcomeFiltering:
    """Only 'success' outcomes are counted."""

    def test_failure_tasks_excluded(self) -> None:
        tasks: list[dict[str, object]] = [
            _success_task("queue", "No remediation needed"),
            {"outcome": "failure", "source": "queue", "result_summary": "No remediation"},
            {"outcome": "partial", "source": "queue", "result_summary": "No remediation"},
        ]
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 1
        assert result["noop_count"] == 1

    def test_multiple_success_outcomes_counted(self) -> None:
        tasks: list[dict[str, object]] = [
            _success_task("queue", "Already green"),
            _success_task("queue", "Shipped feature"),
        ]
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 2
        assert result["noop_count"] == 1
        assert result["noop_rate"] == pytest.approx(0.5)

    def test_non_string_outcome_skipped(self) -> None:
        tasks: list[dict[str, object]] = [
            {"outcome": 42, "source": "queue", "result_summary": "No remediation"},
        ]
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 0


class TestMeasureNoopRateEmptyInput:
    """Empty or degenerate inputs return zero-valued results."""

    def test_empty_list(self) -> None:
        result = measure_noop_rate([])
        assert result["total_success"] == 0
        assert result["noop_count"] == 0
        assert result["noop_rate"] == 0.0
        assert result["by_source"] == {}

    def test_all_failures(self) -> None:
        tasks: list[dict[str, object]] = [
            {"outcome": "failure", "source": "queue", "result_summary": "No remediation"},
            {"outcome": "failure", "source": "queue", "result_summary": "Already green"},
        ]
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 0
        assert result["noop_count"] == 0
        assert result["noop_rate"] == 0.0
        assert result["by_source"] == {}


class TestMeasureNoopRateSourceDefaulting:
    """Missing/empty/non-string source defaults to 'queue'."""

    def test_missing_source_defaults_to_queue(self) -> None:
        tasks: list[dict[str, object]] = [
            {"outcome": "success", "result_summary": "No remediation needed"},
        ]
        result = measure_noop_rate(tasks)
        assert "queue" in result["by_source"]
        assert result["by_source"]["queue"]["total"] == 1

    def test_none_source_defaults_to_queue(self) -> None:
        tasks: list[dict[str, object]] = [
            {"outcome": "success", "source": None, "result_summary": "No-op"},
        ]
        result = measure_noop_rate(tasks)
        assert "queue" in result["by_source"]

    def test_empty_source_defaults_to_queue(self) -> None:
        tasks: list[dict[str, object]] = [
            {"outcome": "success", "source": "", "result_summary": "No-op"},
        ]
        result = measure_noop_rate(tasks)
        assert "queue" in result["by_source"]

    def test_whitespace_source_defaults_to_queue(self) -> None:
        tasks: list[dict[str, object]] = [
            {"outcome": "success", "source": "   ", "result_summary": "No-op"},
        ]
        result = measure_noop_rate(tasks)
        assert "queue" in result["by_source"]

    def test_non_string_source_defaults_to_queue(self) -> None:
        tasks: list[dict[str, object]] = [
            {"outcome": "success", "source": 42, "result_summary": "No-op"},
        ]
        result = measure_noop_rate(tasks)
        assert "queue" in result["by_source"]


class TestMeasureNoopRateBySourceIsolation:
    """Each source bucket is counted independently."""

    def test_sources_dont_leak_across_buckets(self) -> None:
        tasks: list[dict[str, object]] = [
            _success_task("pending_pr_requests", "No remediation"),
            _success_task("pending_user_requests", "Shipped feature"),
            _success_task("queue", "Already green"),
        ]
        result = measure_noop_rate(tasks)
        by_source = result["by_source"]
        assert by_source["pending_pr_requests"]["total"] == 1
        assert by_source["pending_pr_requests"]["noop"] == 1
        assert by_source["pending_user_requests"]["total"] == 1
        assert by_source["pending_user_requests"]["noop"] == 0
        assert by_source["queue"]["total"] == 1
        assert by_source["queue"]["noop"] == 1

    def test_by_source_keys_sorted(self) -> None:
        """by_source keys are in sorted order for determinism."""
        tasks: list[dict[str, object]] = [
            _success_task("queue", "No-op"),
            _success_task("pending_pr_requests", "No-op"),
            _success_task("pending_user_requests", "No-op"),
        ]
        result = measure_noop_rate(tasks)
        keys = list(result["by_source"].keys())  # type: ignore[union-attr]
        assert keys == sorted(keys)

    def test_custom_source_values_preserved(self) -> None:
        """Non-standard source values are not rejected."""
        tasks: list[dict[str, object]] = [
            _success_task("custom_source", "No-op detected"),
        ]
        result = measure_noop_rate(tasks)
        assert "custom_source" in result["by_source"]
        assert result["by_source"]["custom_source"]["total"] == 1


class TestMeasureNoopRateReturnStructure:
    """Return dict always has exactly the expected keys and types."""

    def test_return_keys_present(self) -> None:
        result = measure_noop_rate([])
        assert set(result.keys()) == {"total_success", "noop_count", "noop_rate", "by_source"}

    def test_return_types_empty(self) -> None:
        result = measure_noop_rate([])
        assert isinstance(result["total_success"], int)
        assert isinstance(result["noop_count"], int)
        assert isinstance(result["noop_rate"], float)
        assert isinstance(result["by_source"], dict)

    def test_return_types_populated(self) -> None:
        tasks: list[dict[str, object]] = [
            _success_task("queue", "No-op"),
        ]
        result = measure_noop_rate(tasks)
        assert isinstance(result["total_success"], int)
        assert isinstance(result["noop_count"], int)
        assert isinstance(result["noop_rate"], float)
        assert isinstance(result["by_source"], dict)

    def test_by_source_bucket_keys(self) -> None:
        tasks: list[dict[str, object]] = [
            _success_task("queue", "No-op"),
        ]
        result = measure_noop_rate(tasks)
        bucket = result["by_source"]["queue"]  # type: ignore[index]
        assert set(bucket.keys()) == {"total", "noop", "rate"}  # type: ignore[union-attr]


class TestMeasureNoopRateMissingFields:
    """Tasks with missing fields are handled gracefully."""

    def test_missing_outcome_key_skipped(self) -> None:
        """Task dict with no 'outcome' key is not counted."""
        tasks: list[dict[str, object]] = [
            {"source": "queue", "result_summary": "No remediation needed"},
        ]
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 0

    def test_missing_result_summary_not_noop(self) -> None:
        """Successful task with no result_summary is counted but not classified as noop."""
        tasks: list[dict[str, object]] = [
            {"outcome": "success", "source": "queue"},
        ]
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 1
        assert result["noop_count"] == 0

    def test_none_outcome_skipped(self) -> None:
        """None outcome is not a string and is skipped."""
        tasks: list[dict[str, object]] = [
            {"outcome": None, "source": "queue", "result_summary": "No remediation"},
        ]
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 0

    def test_large_task_list_arithmetic(self) -> None:
        """Verify correctness with 100 tasks (50 noop, 50 meaningful)."""
        tasks: list[dict[str, object]] = []
        for i in range(100):
            if i % 2 == 0:
                tasks.append(_success_task("queue", "No remediation needed"))
            else:
                tasks.append(_success_task("queue", "Shipped real feature"))
        result = measure_noop_rate(tasks)
        assert result["total_success"] == 100
        assert result["noop_count"] == 50
        assert result["noop_rate"] == pytest.approx(0.5)
        assert result["by_source"]["queue"]["total"] == 100  # type: ignore[index]
        assert result["by_source"]["queue"]["noop"] == 50  # type: ignore[index]


# ---------------------------------------------------------------------------
# format_investigation_rate_context
# ---------------------------------------------------------------------------


def test_investigation_rate_context_formats_correctly() -> None:
    """count=3 window=10 → '3/10 (30%)' line."""
    result = format_investigation_rate_context(investigation_count=3, window=10)
    assert "3/10" in result
    assert "30%" in result
    assert "last 10 tasks" in result


def test_investigation_rate_context_zero_window_returns_empty() -> None:
    """Window zero → empty string."""
    assert format_investigation_rate_context(investigation_count=0, window=0) == ""


def test_investigation_rate_context_negative_window_returns_empty() -> None:
    """Negative window → empty string."""
    assert format_investigation_rate_context(investigation_count=0, window=-1) == ""


def test_investigation_rate_context_full_investigation() -> None:
    """All investigation → 100%."""
    result = format_investigation_rate_context(investigation_count=5, window=5)
    assert "5/5" in result
    assert "100%" in result


def test_investigation_rate_context_no_investigation() -> None:
    """Zero investigation → 0%."""
    result = format_investigation_rate_context(investigation_count=0, window=10)
    assert "0/10" in result
    assert "0%" in result


# ---------------------------------------------------------------------------
# _is_investigation_task — keyword-signal classifier
# ---------------------------------------------------------------------------


def test_is_investigation_task_goal_match() -> None:
    """Task whose goal mentions an investigation keyword is classified."""
    t = _task("t1", goal="Run a self-investigation experiment on task selection")
    assert _is_investigation_task(t)


def test_is_investigation_task_findings_match() -> None:
    """Task with investigation keywords in key_findings is classified."""
    t = _task(
        "t1",
        goal="Fix database connection pooling",
        key_findings="Discovered a behavioral pattern in retry logic",
    )
    assert _is_investigation_task(t)


def test_is_investigation_task_no_match() -> None:
    """Pure engineering task with no investigation signals returns False."""
    t = _task("t1", goal="Fix linting errors in module X", key_findings="All fixed")
    assert not _is_investigation_task(t)


def test_is_investigation_task_none_findings() -> None:
    """Task with None key_findings does not crash."""
    t = _task("t1", goal="Build a gate", key_findings=None)
    assert not _is_investigation_task(t)


def test_is_investigation_task_case_insensitive() -> None:
    """Keywords match regardless of case."""
    t = _task("t1", goal="Design a HYPOTHESIS about cache behavior")
    assert _is_investigation_task(t)


def test_is_investigation_task_partial_keyword() -> None:
    """Prefix keywords like 'falsif' match 'falsification'."""
    t = _task("t1", goal="Build a falsification predicate for purpose claims")
    assert _is_investigation_task(t)


def test_is_investigation_task_meta_cognit() -> None:
    """Prefix keyword 'meta-cognit' matches 'meta-cognitive'."""
    t = _task("t1", goal="Study meta-cognitive dynamics in reflection")
    assert _is_investigation_task(t)


def test_is_investigation_task_self_model() -> None:
    """Keyword 'self-model' is matched."""
    t = _task("t1", goal="Audit the self-model for drift")
    assert _is_investigation_task(t)


def test_investigation_re_all_keywords_compile() -> None:
    """The compiled regex matches every keyword in the list."""
    from maestro.audit_checks import (
        _INVESTIGATION_SIGNAL_KEYWORDS,
        _INVESTIGATION_SIGNAL_RE,
    )
    for kw in _INVESTIGATION_SIGNAL_KEYWORDS:
        assert _INVESTIGATION_SIGNAL_RE.search(kw), f"Keyword {kw!r} not matched by regex"


# ---------------------------------------------------------------------------
# compute_investigation_rate — public extraction of investigation rate
# ---------------------------------------------------------------------------


def test_compute_investigation_rate_mixed_window() -> None:
    """4 investigation + 6 non-investigation tasks in window 10 returns 0.4."""
    tasks = [
        _task(f"t{i}", goal="Run self-investigation experiment")
        for i in range(4)
    ] + [
        _task(f"t{i+4}", goal="Fix parser bug")
        for i in range(6)
    ]
    rate, inv_count, window_size = compute_investigation_rate(tasks, window=10)
    assert rate == pytest.approx(0.4)
    assert inv_count == 4
    assert window_size == 10


def test_compute_investigation_rate_all_investigation() -> None:
    """All-investigation tasks return 1.0."""
    tasks = [
        _task(f"t{i}", goal="Hypothesis experiment on self-model")
        for i in range(5)
    ]
    rate, inv_count, window_size = compute_investigation_rate(tasks, window=5)
    assert rate == pytest.approx(1.0)
    assert inv_count == 5
    assert window_size == 5


def test_compute_investigation_rate_no_investigation() -> None:
    """No investigation tasks return 0.0."""
    tasks = [
        _task(f"t{i}", goal="Fix bug in module")
        for i in range(5)
    ]
    rate, inv_count, window_size = compute_investigation_rate(tasks, window=5)
    assert rate == 0.0
    assert inv_count == 0
    assert window_size == 5


def test_compute_investigation_rate_empty_list() -> None:
    """Empty list returns 0.0 with zero window_size."""
    rate, inv_count, window_size = compute_investigation_rate([], window=10)
    assert rate == 0.0
    assert inv_count == 0
    assert window_size == 0


def test_compute_investigation_rate_window_smaller_than_list() -> None:
    """Window smaller than task list slices to most recent tasks."""
    tasks = [
        _task("t0", goal="Fix parser bug"),
        _task("t1", goal="Fix parser bug"),
        _task("t2", goal="Run self-investigation experiment"),
        _task("t3", goal="Run self-investigation experiment"),
        _task("t4", goal="Fix parser bug"),
    ]
    # Window=3 takes last 3: t2 (investigation), t3 (investigation), t4 (non)
    rate, inv_count, window_size = compute_investigation_rate(tasks, window=3)
    assert rate == pytest.approx(2 / 3)
    assert inv_count == 2
    assert window_size == 3


def test_compute_investigation_rate_zero_window() -> None:
    """Window of zero returns 0.0."""
    tasks = [_task("t0", goal="Run self-investigation experiment")]
    rate, inv_count, window_size = compute_investigation_rate(tasks, window=0)
    assert rate == 0.0
    assert inv_count == 0
    assert window_size == 0


# ---------------------------------------------------------------------------
# check_partial_construction_gap — directive bridge for partial outcomes
# ---------------------------------------------------------------------------


def test_partial_construction_gap_fires_with_key_findings() -> None:
    """Partial outcome with key_findings returns directive containing task_id."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": "abc-123",
            "outcome": "partial",
            "key_findings": "Found config drift in toml_config.py",
            "result_summary": "",
        },
    ]
    result = check_partial_construction_gap(tasks)
    assert result is not None
    assert "abc-123" in result
    assert "config drift" in result


def test_partial_construction_gap_falls_back_to_result_summary() -> None:
    """When key_findings is empty, result_summary is used as fallback."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": "def-456",
            "outcome": "partial",
            "key_findings": "",
            "result_summary": "Identified missing test coverage",
        },
    ]
    result = check_partial_construction_gap(tasks)
    assert result is not None
    assert "def-456" in result
    assert "missing test coverage" in result


def test_partial_construction_gap_none_when_both_empty() -> None:
    """Partial with both findings fields empty returns None."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": "ghi-789",
            "outcome": "partial",
            "key_findings": "",
            "result_summary": "",
        },
    ]
    assert check_partial_construction_gap(tasks) is None


def test_partial_construction_gap_none_on_success() -> None:
    """Successful outcome does not trigger the bridge."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": "s-001",
            "outcome": "success",
            "key_findings": "Important findings here",
        },
    ]
    assert check_partial_construction_gap(tasks) is None


def test_partial_construction_gap_none_on_failure() -> None:
    """Failure outcome does not trigger the bridge."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": "f-001",
            "outcome": "failure",
            "key_findings": "Some error happened",
        },
    ]
    assert check_partial_construction_gap(tasks) is None


def test_partial_construction_gap_none_on_empty_list() -> None:
    """Empty task list returns None."""
    assert check_partial_construction_gap([]) is None


def test_partial_construction_gap_truncates_long_findings() -> None:
    """Findings longer than 200-char budget are truncated with ellipsis."""
    long_finding = "x" * 300
    tasks: list[dict[str, object]] = [
        {
            "task_id": "t-1",
            "outcome": "partial",
            "key_findings": long_finding,
        },
    ]
    result = check_partial_construction_gap(tasks)
    assert result is not None
    assert len(result) <= 200
    assert result.endswith("…")
    assert "t-1" in result


def test_partial_construction_gap_whitespace_only_findings_skipped() -> None:
    """Whitespace-only key_findings falls through to result_summary."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": "ws-1",
            "outcome": "partial",
            "key_findings": "   \n  ",
            "result_summary": "Real summary here",
        },
    ]
    result = check_partial_construction_gap(tasks)
    assert result is not None
    assert "Real summary here" in result


def test_partial_construction_gap_missing_key_findings_field() -> None:
    """When key_findings key is absent, falls back to result_summary."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": "nf-1",
            "outcome": "partial",
            "result_summary": "Fallback summary",
        },
    ]
    result = check_partial_construction_gap(tasks)
    assert result is not None
    assert "Fallback summary" in result


def test_partial_construction_gap_none_on_zero_duration_skip() -> None:
    """A zero-duration partial is a no-op skip (e.g. the `[merge-block]`
    zero-cost gate), not partial construction: the bridge returns None even
    with non-empty findings, so it does not re-feed a declined remediation."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": "skip-1",
            "outcome": "partial",
            "key_findings": "[merge-block] PR #91 remediation skipped",
            "result_summary": "[merge-block] blocker BEHIND_MAIN",
            "duration_secs": 0.0,
        },
    ]
    assert check_partial_construction_gap(tasks) is None


def test_partial_construction_gap_fires_on_positive_duration() -> None:
    """A genuine partial with positive duration still fires — the zero-duration
    guard excludes only no-op skips, not real partial construction."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": "real-1",
            "outcome": "partial",
            "key_findings": "Found config drift",
            "duration_secs": 5.0,
        },
    ]
    result = check_partial_construction_gap(tasks)
    assert result is not None
    assert "real-1" in result


def test_partial_construction_gap_only_checks_last_task() -> None:
    """Only the last task in the list is examined."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": "old",
            "outcome": "partial",
            "key_findings": "Old partial findings",
        },
        {
            "task_id": "new",
            "outcome": "success",
            "key_findings": "Completed",
        },
    ]
    # Last task is success — should not fire.
    assert check_partial_construction_gap(tasks) is None


# ---------------------------------------------------------------------------
# check_self_directed_success_rate — rolling window directive gate
# ---------------------------------------------------------------------------


def test_check_self_directed_success_rate_above_threshold() -> None:
    """15 all-success self-directed tasks returns None (rate >= 0.70)."""
    tasks = [
        _task(f"t{i}", outcome="success", goal="Extend the executor module", source="self_directed")
        for i in range(15)
    ]
    assert check_self_directed_success_rate(tasks) is None


def test_check_self_directed_success_rate_below_threshold() -> None:
    """Mixed outcomes below 0.70 returns a non-empty directive string."""
    # 10 tasks: 4 success, 6 failure → 40% success rate
    tasks: list[TaskRecord] = []
    for i in range(4):
        tasks.append(
            _task(f"s{i}", outcome="success", goal="Build a new module", source="self_directed")
        )
    for i in range(6):
        tasks.append(
            _task(f"f{i}", outcome="failure", goal="Wire a new feature", source="self_directed")
        )
    result = check_self_directed_success_rate(tasks, window=15)
    assert result is not None
    assert "40%" in result
    assert "4/10" in result


def test_check_self_directed_success_rate_excludes_external() -> None:
    """History of only github_issue-shaped tasks returns None."""
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Fix #42 from GitHub issue", source="self_directed")
        for i in range(10)
    ]
    # All tasks are github_issue shape — no self-directed tasks remain
    assert check_self_directed_success_rate(tasks) is None


def test_check_self_directed_success_rate_insufficient_data() -> None:
    """4 self-directed tasks returns None (below SELF_DIRECTED_MIN_SAMPLE)."""
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Build something new", source="self_directed")
        for i in range(4)
    ]
    assert check_self_directed_success_rate(tasks) is None


def test_check_self_directed_success_rate_boundary() -> None:
    """Exactly 70% success returns None; 69% returns a directive."""
    # 10 tasks, 7 success → 70% → at threshold → None
    tasks_at: list[TaskRecord] = []
    for i in range(7):
        tasks_at.append(
            _task(f"s{i}", outcome="success", goal="Build module X", source="self_directed")
        )
    for i in range(3):
        tasks_at.append(
            _task(f"f{i}", outcome="failure", goal="Wire feature Y", source="self_directed")
        )
    assert check_self_directed_success_rate(tasks_at, window=15) is None

    # 13 tasks, 9 success → 9/13 ≈ 69.2% → below threshold → fires
    tasks_below: list[TaskRecord] = []
    for i in range(9):
        tasks_below.append(
            _task(f"s{i}", outcome="success", goal="Build module X", source="self_directed")
        )
    for i in range(4):
        tasks_below.append(
            _task(f"f{i}", outcome="failure", goal="Wire feature Y", source="self_directed")
        )
    result = check_self_directed_success_rate(tasks_below, window=15)
    assert result is not None
    assert "69%" in result


def test_check_self_directed_success_rate_window_is_load_bearing() -> None:
    """The window argument changes the verdict (regression for #2353).

    A 15-task history where the oldest 5 are self-directed failures and the
    most-recent 10 are mostly successes: counting only the last 10 yields a
    passing rate (None), while counting all 15 drags the rate below the
    threshold and fires. This proves ``window`` is honored rather than dead.
    """
    tasks: list[TaskRecord] = []
    # Oldest 5 — self-directed failures, only counted when window >= 15.
    for i in range(5):
        tasks.append(
            _task(f"old{i}", outcome="failure", goal="Build a new module", source="self_directed")
        )
    # Most-recent 10 — 8 success, 2 failure → 80% over a 10-task window.
    for i in range(8):
        tasks.append(
            _task(f"s{i}", outcome="success", goal="Build a new module", source="self_directed")
        )
    for i in range(2):
        tasks.append(
            _task(f"f{i}", outcome="failure", goal="Build a new module", source="self_directed")
        )

    # window=10 → last 10 tasks → 8/10 = 80% ≥ 0.70 → no directive.
    assert check_self_directed_success_rate(tasks, window=10) is None

    # window=15 → all 15 tasks → 8/15 ≈ 53% < 0.70 → directive fires.
    result = check_self_directed_success_rate(tasks, window=15)
    assert result is not None
    assert "53%" in result
    assert "8/15" in result


def test_check_self_directed_success_rate_excludes_external_remediation_laps() -> None:
    """External remediation laps never enter the denominator (#4125)."""
    tasks = [
        _task(f"r{i}", outcome="partial", goal=f"Remediate PR #{i}: Fix thing")
        for i in range(10)
    ] + [
        _task(
            f"s{i}", outcome="success",
            goal="Extend the executor module with a retry helper",
            source="self_directed",
        )
        for i in range(5)
    ]
    assert check_self_directed_success_rate(tasks) is None


def test_check_self_directed_success_rate_counts_only_self_directed() -> None:
    """Only ``source == "self_directed"`` records count toward the rate."""
    tasks = [
        _task(f"r{i}", outcome="partial", goal=f"Remediate PR #{i}: Fix thing")
        for i in range(4)
    ]
    for i in range(3):
        tasks.append(_task(
            f"s{i}", outcome="success", goal="Build a new module",
            source="self_directed",
        ))
    for i in range(3):
        tasks.append(_task(
            f"f{i}", outcome="failure", goal="Wire a new feature",
            source="self_directed",
        ))
    result = check_self_directed_success_rate(tasks)
    assert result is not None
    assert "3/6" in result


# ---------------------------------------------------------------------------
# is_compound_scope — compound creation+connection verb detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal",
    [
        "Build a data-sufficiency gate and wire it into generate_task",
        "Add check and then integrate into Z",
        "Create function, then hook into pipeline",
        "Implement validator and connect to existing gate",
        "Write a classifier and inject it into the prompt",
        "Extend the audit module and plug the result into health_check",
    ],
)
def test_is_compound_scope_positive(goal: str) -> None:
    assert is_compound_scope(goal) is True


@pytest.mark.parametrize(
    "goal",
    [
        "Rebase branch onto origin/main and resolve conflicts",
        "Read file and compute statistics",
        "Add a function that integrates data sources",
        "Wire check_data_sufficiency into format_known_state_summary",
        "Integrate the new classifier into the pipeline",
        "",
        "Promote branch maestro/task/abc via source_task_id",
    ],
)
def test_is_compound_scope_negative(goal: str) -> None:
    assert is_compound_scope(goal) is False


# ---------------------------------------------------------------------------
# check_task_concreteness — reject pure-analysis tasks at generation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task_text", "expected_ok"),
    [
        # (1) pure-analysis-reject: no file target, no impl verb, analyze verb.
        ("Analyze task_history.jsonl and write findings to artifacts/", False),
        # (2) scan-and-repair-allow: implementation verbs (sweep, fix).
        ("Sweep docs/ for stale refs and fix them", True),
        # (3) single-file-fix-allow: concrete file target + impl verb.
        ("Add task_id field to IterationUsage in maestro/sdk.py", True),
        # (4) explore-and-document-reject: analysis verb, no target, no impl.
        ("Explore the relationship between self-model entries", False),
        # (5) compound-analyze-then-fix-allow: impl verb + file target present.
        ("Analyze error patterns in maestro/agent.py then fix the retry logic", True),
        # (6) artifact-only-output-reject: study + write-a-report, no target.
        ("Study task distribution and write a report to artifacts/", False),
    ],
)
def test_check_task_concreteness(task_text: str, expected_ok: bool) -> None:
    allowed, reason = check_task_concreteness(task_text)
    assert allowed is expected_ok
    if expected_ok:
        assert reason == ""
    else:
        assert reason != ""


def test_check_task_concreteness_artifact_md_output_rejected() -> None:
    # An ``artifacts/*.md`` output is a gitignored artifact, not an
    # implementation target — a pure-analysis goal writing one is rejected.
    allowed, reason = check_task_concreteness(
        "Analyze recent tasks and write synthesis to artifacts/synthesis.md",
    )
    assert allowed is False
    assert reason != ""


def test_check_task_concreteness_empty_allowed() -> None:
    # Empty / whitespace input is not a pure-analysis task — allowed.
    allowed, reason = check_task_concreteness("")
    assert allowed is True
    assert reason == ""
    allowed_ws, reason_ws = check_task_concreteness("   ")
    assert allowed_ws is True
    assert reason_ws == ""
