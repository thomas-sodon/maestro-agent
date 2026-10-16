"""Tests for PR feedback → ``pending_pr_requests`` routing.

Covers the bridge between ``tend_evolution_prs`` (which detects PRs needing
LLM judgment) and ``SelfModel.pending_pr_requests`` (the FIFO that
``_run_task_phase`` drains for remediation work). Three categories share
the FIFO: CHANGES_REQUESTED PRs, mechanical conflicts, and post-main-merge
quality-gate breakage. Each queued entry is a multi-section Markdown
remediation prompt composed at routing time so the Planner sees the
originating ``EvolutionPlan`` + PR body even after the plan is archived
to cold storage. The trailing ``head_ref`` on every routing tuple is
threaded into ``PendingPRRequest.pr_branch`` so the remediation task
commits on the PR branch — run() then pushes it directly, Tend PR is
the fallback deliverer.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from maestro.consciousness import PendingPRRequest
from maestro.evolution_tend import TendPRsResult
from maestro.memory import EvolutionPlan
from maestro.pr_facts import MergeTruth
from maestro.pr_ledger import PRLedger
from maestro.remediation_tracker import record_preflight_pass


def _plant_plan(
    agent: Any,
    *,
    plan_id: str = "ep1",
    title: str = "Refactor planner",
    rationale: str = "The planner is too coupled.",
    proposed_changes: list[str] | None = None,
    files_affected: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    category: str = "capability",
    priority: str = "capability",
) -> EvolutionPlan:
    plan = EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category=category,  # type: ignore[arg-type]
        rationale=rationale,
        proposed_changes=proposed_changes if proposed_changes is not None else ["Extract helper"],
        files_affected=files_affected if files_affected is not None else ["maestro/planner.py"],
        expected_impact="",
        acceptance_criteria=acceptance_criteria if acceptance_criteria is not None else ["Coverage stays at 100%."],
        priority=priority,  # type: ignore[arg-type]
        status="implemented",
        pr_url="https://github.com/x/y/pull/?",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)
    return plan


def _head(plan_id: str) -> str:
    return f"maestro/evolve/{plan_id}"


def test_no_pr_feedback_leaves_state_empty(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    result = TendPRsResult()
    agent._route_pr_feedback_to_pending_requests(result)
    model = agent.consciousness.load_self_model()
    assert model.pending_pr_requests == []
    assert model.next_task_directive is None


def test_existing_directive_untouched(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """PR-tend must not write or clear ``next_task_directive``."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep7")
    existing = "Do something else first"
    model = agent.consciousness.load_self_model()
    model.next_task_directive = existing
    agent.consciousness.save_self_model(model)

    result = TendPRsResult(
        changes_requested_prs=[
            (7, "Refactor planner", "ep7", "PR body here",
             "https://github.com/x/y/pull/7", _head("ep7")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)

    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == existing
    assert len(model.pending_pr_requests) == 1
    entry = model.pending_pr_requests[0]
    assert entry.directive.startswith("Remediate PR #7: Refactor planner")


def test_rich_goal_has_all_section_headers(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        changes_requested_prs=[
            (42, "Refactor planner", "ep42", "PR body",
             "https://github.com/x/y/pull/42", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    idx_problem = goal.index("## Problem")
    idx_plan = goal.index("## Original Evolution Plan")
    idx_pr = goal.index("## Original PR Description")
    idx_guidance = goal.index("## Remediation Guidance")
    assert idx_problem < idx_plan < idx_pr < idx_guidance


def test_rich_goal_includes_plan_rationale_and_acceptance(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(
        agent,
        plan_id="ep42",
        rationale="Planner is brittle under nested deps.",
        proposed_changes=["Add ToposortHelper", "Inline Planner._build"],
        files_affected=["maestro/planner.py", "tests/test_planner.py"],
        acceptance_criteria=[
            "Existing tests stay green.",
            "Coverage stays at 100%.",
        ],
    )
    result = TendPRsResult(
        changes_requested_prs=[
            (42, "Refactor planner", "ep42", "body",
             "https://github.com/x/y/pull/42", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "Planner is brittle under nested deps." in goal
    assert "- Add ToposortHelper" in goal
    assert "- Inline Planner._build" in goal
    assert "maestro/planner.py" in goal
    assert "tests/test_planner.py" in goal
    assert "Existing tests stay green." in goal
    assert "Coverage stays at 100%." in goal


def test_rich_goal_includes_pr_body_verbatim(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    body = "## Summary\nA detailed multi-line PR description.\n\n## Test plan\n- foo\n- bar"
    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", body,
             "https://github.com/x/y/pull/42", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert body in goal


def test_rich_goal_falls_back_when_plan_archived(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """When ``Memory.get_plan`` returns ``None`` the entry still routes;
    the plan section carries a note instead of plan fields."""
    agent = make_agent()
    # No plant — get_plan("missing") returns None.
    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "missing", "pr body",
             "https://github.com/x/y/pull/42", _head("missing")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "## Original Evolution Plan" in goal
    assert "EvolutionPlan archived" in goal
    assert "pr body" in goal
    assert "Branch: maestro/evolve/missing" in goal


def test_rich_goal_empty_pr_body_uses_literal(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(agent, plan_id="ep1")
    result = TendPRsResult(
        changes_requested_prs=[
            (1, "title", "ep1", "", "https://github.com/x/y/pull/1",
             _head("ep1")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "(none — PR was opened without a body)" in goal


def test_rich_goal_empty_lists_omit_sub_bullets(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """Empty proposed_changes / files_affected / acceptance_criteria
    must NOT render an empty bullet block."""
    agent = make_agent()
    _plant_plan(
        agent,
        plan_id="ep1",
        proposed_changes=[],
        files_affected=[],
        acceptance_criteria=[],
    )
    result = TendPRsResult(
        changes_requested_prs=[
            (1, "title", "ep1", "body",
             "https://github.com/x/y/pull/1", _head("ep1")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "Proposed changes:" not in goal
    assert "Files affected:" not in goal
    assert "Acceptance criteria:" not in goal


def test_problem_section_review_feedback(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "reviewer requested changes" in goal
    assert "gh pr view 42 --comments" in goal


def test_problem_section_merge_conflict(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(agent, plan_id="ep11")
    result = TendPRsResult(
        conflicts_routed=[
            (11, "title", ["a.py", "b.py"], "ep11", "body", "url",
             _head("ep11")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    # The routing/dedup marker substring must survive verbatim.
    assert "conflicts with origin/main" in goal
    assert "a.py, b.py" in goal
    # Owned-PR self-delivery is unconditional (proposal 093): the prompt
    # instructs a commit on this branch then a direct push to the PR branch.
    assert "Commit the resolution" in goal
    assert "git push origin HEAD:" in goal
    assert "Do NOT push" not in goal


def test_problem_section_quality_gates(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(agent, plan_id="ep17")
    result = TendPRsResult(
        quality_gates_routed=[
            (17, "title", "mypy, tests failed", "ep17", "body", "url",
             _head("ep17")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "quality gates failed: mypy, tests failed" in goal
    assert "do not skip or weaken any gate" in goal


def test_tend_skips_preflight_passed_pr(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A PR whose current head SHA already passed the preflight gate is
    not re-routed into pending_pr_requests (#2348)."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep2348")
    # The PR branch head resolves to a SHA already recorded as passing.
    agent._vcs.rev_parse.return_value = "passing_sha"
    record_preflight_pass(2348, "passing_sha", agent.config.memory_dir)
    result = TendPRsResult(
        quality_gates_routed=[
            (2348, "title", "mypy failed", "ep2348", "body", "url",
             _head("ep2348")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    model = agent.consciousness.load_self_model()
    assert model.pending_pr_requests == []


def test_tend_routes_when_head_advanced_past_preflight_pass(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """When the PR head advanced past the recorded preflight pass the
    merged tree changed, so the PR is re-routed for fresh verification."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep2348")
    agent._vcs.rev_parse.return_value = "new_sha"
    record_preflight_pass(2348, "old_sha", agent.config.memory_dir)
    result = TendPRsResult(
        quality_gates_routed=[
            (2348, "title", "mypy failed", "ep2348", "body", "url",
             _head("ep2348")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    model = agent.consciousness.load_self_model()
    assert len(model.pending_pr_requests) == 1


def test_conflict_empty_files_uses_fallback_hint(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(agent, plan_id="ep4")
    result = TendPRsResult(
        conflicts_routed=[(4, "PR", [], "ep4", "body", "url", _head("ep4"))],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "probe could not enumerate" in goal


def test_remediation_guidance_has_branch_hint(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body",
             "https://github.com/x/y/pull/42", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "Branch: maestro/evolve/ep42" in goal
    assert "PR URL: https://github.com/x/y/pull/42" in goal
    assert "acceptance criteria above remain the success contract" in goal


def test_multiple_categories_for_same_pr_merge_into_one_entry(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """Multiple problem categories on PR #42 in one tend pass collapse
    into a single queue entry with multiple paragraphs in ``## Problem``."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
        conflicts_routed=[
            (42, "title", ["a.py"], "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    goal = queue[0].directive
    assert "reviewer requested changes" in goal
    assert "conflicts with origin/main" in goal


def test_multiple_prs_sorted_ascending(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """Multiple PRs sort by ascending number so the oldest PR drains first."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep30")
    _plant_plan(agent, plan_id="ep10")
    _plant_plan(agent, plan_id="ep20")
    result = TendPRsResult(
        changes_requested_prs=[
            (30, "Newest", "ep30", "body", "url", _head("ep30")),
            (10, "Oldest", "ep10", "body", "url", _head("ep10")),
            (20, "Newer", "ep20", "body", "url", _head("ep20")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert queue[0].directive.startswith("Remediate PR #10:")
    assert queue[1].directive.startswith("Remediate PR #20:")
    assert queue[2].directive.startswith("Remediate PR #30:")


def test_per_pr_dedup_skips_duplicates_across_tend_cycles(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A PR already represented in the FIFO is not appended again."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1


def test_per_pr_dedup_no_false_positive_on_digit_prefix(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """An existing entry for PR #420 must not suppress PR #42."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep420")
    _plant_plan(agent, plan_id="ep42")
    agent._route_pr_feedback_to_pending_requests(
        TendPRsResult(
            changes_requested_prs=[
                (420, "Newer PR", "ep420", "body", "url", _head("ep420")),
            ],
        ),
    )
    agent._route_pr_feedback_to_pending_requests(
        TendPRsResult(
            changes_requested_prs=[
                (42, "Older PR", "ep42", "body", "url", _head("ep42")),
            ],
        ),
    )
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 2
    assert queue[0].directive.startswith("Remediate PR #420:")
    assert queue[1].directive.startswith("Remediate PR #42:")


def test_per_pr_dedup_across_categories(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A PR queued via one category dedups against a re-route from another."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep17")
    agent._route_pr_feedback_to_pending_requests(
        TendPRsResult(
            quality_gates_routed=[
                (17, "title", "ruff failed", "ep17", "body", "url",
                 _head("ep17")),
            ],
        ),
    )
    agent._route_pr_feedback_to_pending_requests(
        TendPRsResult(
            changes_requested_prs=[
                (17, "title", "ep17", "body", "url", _head("ep17")),
            ],
        ),
    )
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    assert queue[0].directive.startswith("Remediate PR #17:")


def test_rich_goal_is_not_truncated_on_save(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """``pending_pr_requests`` entries skip the per-item 200-char cap."""
    agent = make_agent()
    _plant_plan(
        agent,
        plan_id="ep1",
        rationale="X" * 500,
        proposed_changes=["Y" * 500],
    )
    result = TendPRsResult(
        changes_requested_prs=[
            (1, "title", "ep1", "Z" * 500, "url", _head("ep1")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    reloaded = agent.consciousness.load_self_model()
    assert len(reloaded.pending_pr_requests) == 1
    goal = reloaded.pending_pr_requests[0].directive
    assert "X" * 500 in goal
    assert "Y" * 500 in goal
    assert "Z" * 500 in goal


def test_pending_user_requests_unaffected_by_pr_route(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """The PR-feedback router never touches ``pending_user_requests``."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    model = agent.consciousness.load_self_model()
    model.pending_user_requests = ["Address Thomas's request"]
    agent.consciousness.save_self_model(model)

    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.pending_user_requests == ["Address Thomas's request"]
    assert len(reloaded.pending_pr_requests) == 1


def test_head_ref_threads_into_pr_branch(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """The trailing ``head_ref`` on each routing tuple is threaded into
    :attr:`PendingPRRequest.pr_branch` so the next ``_run_task_phase``
    can commit remediation on the PR branch."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep10")
    _plant_plan(agent, plan_id="ep11")
    _plant_plan(agent, plan_id="ep12")
    result = TendPRsResult(
        changes_requested_prs=[
            (10, "title", "ep10", "body", "url", "feat/cr-branch"),
        ],
        conflicts_routed=[
            (11, "title", ["a.py"], "ep11", "body", "url", "feat/conflict-branch"),
        ],
        quality_gates_routed=[
            (12, "title", "ruff failed", "ep12", "body", "url",
             "feat/gate-branch"),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert [e.pr_branch for e in queue] == [
        "feat/cr-branch",
        "feat/conflict-branch",
        "feat/gate-branch",
    ]


def test_head_ref_first_write_wins_on_dedup(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """If the same PR re-routes with a different ``head_ref`` (e.g. the
    branch was force-pushed to a new name), the dedup guard wins and the
    original ``pr_branch`` survives."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep7")
    agent._route_pr_feedback_to_pending_requests(
        TendPRsResult(
            changes_requested_prs=[
                (7, "Original", "ep7", "body", "url", "original-branch"),
            ],
        ),
    )
    agent._route_pr_feedback_to_pending_requests(
        TendPRsResult(
            changes_requested_prs=[
                (7, "Updated", "ep7", "body", "url", "renamed-branch"),
            ],
        ),
    )
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    assert queue[0].pr_branch == "original-branch"


def test_legacy_string_entries_load_as_pending_pr_request(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A persisted ``list[str]`` (legacy v1 shape) coerces into
    ``list[PendingPRRequest]`` with ``pr_branch=None``."""
    agent = make_agent()
    # Bypass the dataclass writer to plant a legacy-shaped record.
    model = agent.consciousness.load_self_model()
    model.pending_pr_requests = ["legacy directive text"]  # type: ignore[list-item]
    agent.consciousness.save_self_model(model)

    reloaded = agent.consciousness.load_self_model()
    assert reloaded.pending_pr_requests == [
        PendingPRRequest(directive="legacy directive text", pr_branch=None),
    ]


# ──────────────────────────────────────────────────────────────────────
# Remediation repetition gate tests
# ──────────────────────────────────────────────────────────────────────


def test_repetition_gate_blocks_after_consecutive_noops(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """After 2 consecutive no-op remediations, routing is blocked."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")

    # Record 2 consecutive no-ops for PR #42
    agent.remediation_tracker.record_outcome(42, noop=True)
    agent.remediation_tracker.record_outcome(42, noop=True)

    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 0


def test_repetition_gate_allows_after_reset(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """After reset (e.g. new commits), routing resumes."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")

    agent.remediation_tracker.record_outcome(42, noop=True)
    agent.remediation_tracker.record_outcome(42, noop=True)
    agent.remediation_tracker.reset(42)

    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1


def test_repetition_gate_resets_on_branch_state_change(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """PRs appearing in conflicts_routed/quality_gates_routed trigger reset."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")

    # Block the PR via consecutive no-ops
    agent.remediation_tracker.record_outcome(42, noop=True)
    agent.remediation_tracker.record_outcome(42, noop=True)

    # Now the PR shows up with a conflict (branch state changed)
    result = TendPRsResult(
        conflicts_routed=[
            (42, "title", ["a.py"], "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    # Reset fired first, so the PR is routed
    assert len(queue) == 1


def test_post_merge_quality_gate_failure_resets_tracker(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A *post-merge* quality-gate failure (real ruff/mypy/tests summary)
    changed the branch via main-merge, so it resets the tracker (#2440)."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")

    agent.remediation_tracker.record_outcome(42, noop=True)
    agent.remediation_tracker.record_outcome(42, noop=True)

    result = TendPRsResult(
        quality_gates_routed=[
            (42, "title", "mypy, tests failed", "ep42", "body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    # Reset fired (branch state changed), so the PR is routed.
    assert len(queue) == 1


def test_ci_failure_on_head_route_does_not_reset_tracker(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """The pre-merge ``ci_failure_on_head`` route returns before any merge —
    the branch is unchanged, so it must NOT reset the repetition tracker, or a
    conflicting + failing-head PR loops forever (#2440)."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")

    agent.remediation_tracker.record_outcome(42, noop=True)
    agent.remediation_tracker.record_outcome(42, noop=True)

    result = TendPRsResult(
        quality_gates_routed=[
            (42, "title", "ci_failure_on_head: review", "ep42", "body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    # No reset: the gate stays tripped, so the PR is NOT re-routed.
    assert len(queue) == 0
    assert agent.remediation_tracker.should_reroute(42) is False


def test_ci_failure_on_head_bare_sentinel_does_not_reset_tracker(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """The bare ``ci_failure_on_head`` sentinel (no failing names) also skips
    the reset (#2440)."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")

    agent.remediation_tracker.record_outcome(42, noop=True)
    agent.remediation_tracker.record_outcome(42, noop=True)

    result = TendPRsResult(
        quality_gates_routed=[
            (42, "title", "ci_failure_on_head", "ep42", "body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 0


def _exhaust_ledger(agent: Any, pr_num: int) -> None:
    """Drive the real PR ledger to budget exhaustion (default budget 3)."""
    agent.pr_ledger.observe_head(pr_num, _head("ep42"), "sha-static")
    for _ in range(3):
        agent.pr_ledger.record_attempt(
            pr_num, outcome="noop", stuck_reason="no commit produced",
        )


def test_ledger_grant_on_conflict_route_unblocks_routing(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A budget-exhausted (stuck) PR that develops a new merge conflict is
    granted a fresh ledger budget and routed: origin head can only advance
    by pushing the resolution the exhausted budget would otherwise block,
    so without the grant the PR could never recover autonomously."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    _exhaust_ledger(agent, 42)
    assert agent.pr_ledger.can_attempt(42) is False

    result = TendPRsResult(
        conflicts_routed=[
            (42, "title", ["a.py"], "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    entry = agent.pr_ledger.get(42)
    assert entry is not None
    assert entry.attempts_on_head == 0
    assert entry.stuck is False


def test_ledger_grant_on_post_merge_quality_gate_route(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A post-merge quality-gate failure is the same branch-state-change
    grant the tracker reset already covers — the ledger gate must not
    override it."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    _exhaust_ledger(agent, 42)

    result = TendPRsResult(
        quality_gates_routed=[
            (42, "title", "mypy, tests failed", "ep42", "body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    assert agent.pr_ledger.can_attempt(42) is True


def test_ledger_gate_blocks_changes_requested_when_exhausted(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """CHANGES_REQUESTED re-fires from GitHub state every tend pass without
    any branch change, so it earns no fresh-budget grant — the exhausted
    ledger keeps the PR parked (surfaced via STUCK-PR/PARKED-PR instead)."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    _exhaust_ledger(agent, 42)

    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 0
    entry = agent.pr_ledger.get(42)
    assert entry is not None
    assert entry.stuck is True


def test_ledger_gate_blocks_bug_comments_when_exhausted(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """Bug-comment detection reads GitHub state and never touches the
    branch — no grant, the exhausted ledger blocks re-routing (mirrors the
    tracker's deliberate non-reset for this channel)."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    _exhaust_ledger(agent, 42)

    result = TendPRsResult(
        bug_comments_routed=[
            (42, "title", [(100, "a.py", "Bug", "bug")], "ep42", "body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 0


def test_ledger_no_grant_on_ci_failure_on_head_route(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """The pre-merge ``ci_failure_on_head`` route returns before any merge —
    the branch is unchanged, so it must not refill the ledger budget either
    (mirrors the tracker's #2440 exclusion)."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    _exhaust_ledger(agent, 42)

    result = TendPRsResult(
        quality_gates_routed=[
            (42, "title", "ci_failure_on_head: review", "ep42", "body",
             "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 0
    assert agent.pr_ledger.can_attempt(42) is False


def test_problem_section_ci_failure_on_head_names_real_check(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """The ``ci_failure_on_head`` directive names the real failing check rather
    than claiming ruff/mypy/tests failed (#2440)."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep17")
    # The oracle now renders unconditionally for a routed ci_failure_on_head
    # PR (proposal 102); stub the log seam to "" so no ## CI Failure excerpt is
    # produced — this test asserts only the problem-section naming.
    agent.evolution.code_host.read_run_log_failed = MagicMock(  # type: ignore[attr-defined]
        return_value="",
    )
    result = TendPRsResult(
        quality_gates_routed=[
            (17, "title", "ci_failure_on_head: review", "ep17", "body", "url",
             _head("ep17")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "failing CI check(s): review" in goal
    assert "before any main-merge" in goal
    assert "which of ruff / mypy / tests" not in goal


def test_ci_failure_section_threaded_when_flag_on(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A pre-merge head-CI-failure route reads the failing run's log once and
    threads the concrete ``## CI Failure`` excerpt into the remediation
    directive (proposal 102)."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    read_log = MagicMock(
        return_value=(
            "=== FAILURES ===\n"
            "FAILED tests/test_widget.py::test_frobnicate - AssertionError\n"
        ),
    )
    agent.evolution.code_host.read_run_log_failed = read_log  # type: ignore[attr-defined]
    result = TendPRsResult(
        quality_gates_routed=[
            (42, "title", "ci_failure_on_head: quality", "ep42", "body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    directive = (
        agent.consciousness.load_self_model().pending_pr_requests[0].directive
    )
    assert "## CI Failure" in directive
    assert "FAILED tests/test_widget.py::test_frobnicate" in directive
    # The failing-run log is read exactly once, for this PR.
    read_log.assert_called_once_with(42)


def test_ci_failure_section_bare_sentinel_renders_unnamed_check(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A bare ``ci_failure_on_head`` sentinel (no failing names) still renders a
    ``## CI Failure`` block for one unnamed check (proposal 102), mirroring the
    red-root ``ci_failing_checks or ("",)`` fallback."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    read_log = MagicMock(
        return_value="FAILED tests/test_zeta.py::test_omega - AssertionError\n",
    )
    agent.evolution.code_host.read_run_log_failed = read_log  # type: ignore[attr-defined]
    result = TendPRsResult(
        quality_gates_routed=[
            (42, "title", "ci_failure_on_head", "ep42", "body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    directive = (
        agent.consciousness.load_self_model().pending_pr_requests[0].directive
    )
    assert "## CI Failure" in directive
    assert "(unnamed check)" in directive
    assert "FAILED tests/test_zeta.py::test_omega" in directive
    read_log.assert_called_once_with(42)


def test_ci_failure_section_absent_for_post_merge_gate_with_flag_on(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A post-merge quality-gate route (not ``ci_failure_on_head``) carries no
    ``## CI Failure`` block and reads no log: only the pre-merge head-CI route
    sets ``ci_failure_summary`` (proposal 102)."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    read_log = MagicMock(
        return_value="FAILED tests/test_x.py::test_y - AssertionError\n",
    )
    agent.evolution.code_host.read_run_log_failed = read_log  # type: ignore[attr-defined]
    result = TendPRsResult(
        quality_gates_routed=[
            (42, "title", "mypy, tests failed", "ep42", "body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    directive = (
        agent.consciousness.load_self_model().pending_pr_requests[0].directive
    )
    assert "## CI Failure" not in directive
    read_log.assert_not_called()


def test_repetition_gate_allows_below_threshold(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A single no-op does not block routing."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")

    agent.remediation_tracker.record_outcome(42, noop=True)

    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1


def test_repetition_gate_does_not_block_other_prs(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """Gate for PR #42 does not affect PR #10."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    _plant_plan(agent, plan_id="ep10")

    agent.remediation_tracker.record_outcome(42, noop=True)
    agent.remediation_tracker.record_outcome(42, noop=True)

    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
            (10, "title", "ep10", "body", "url", _head("ep10")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    assert queue[0].directive.startswith("Remediate PR #10:")


# ---------------------------------------------------------------------------
# Admission-face merged-PR guard
# ---------------------------------------------------------------------------
# These tests assert the guard reads git-merge-truth (a mocked
# ``_branch_merged_into_main``, never task stamps) at the admission seam and
# refuses to admit a confirmed-merged PR into ``pending_pr_requests`` — the
# *first* lap is prevented, not the Nth evicted.


def test_admission_merged_pr_is_not_appended(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A PR whose branch is already merged into origin/main is never admitted."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    # git-merge-truth says the branch is an ancestor of origin/main.
    agent._branch_merged_into_main.return_value = True
    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    model = agent.consciousness.load_self_model()
    assert model.pending_pr_requests == []
    # The guard read the PR head ref (not a task stamp) via the resolvable
    # ``origin/<branch>`` remote-tracking form — a bare ref would not resolve
    # in the loop container and would silently admit every merged PR.
    agent._branch_merged_into_main.assert_called_once_with(
        f"origin/{_head('ep42')}",
    )


def test_admission_unmerged_pr_is_admitted_unchanged(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """An unmerged PR (git-truth False) is admitted exactly as before."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    agent._branch_merged_into_main.return_value = False
    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    assert queue[0].directive.startswith("Remediate PR #42:")
    assert queue[0].pr_branch == _head("ep42")


def test_admission_guard_runs_before_dedup_path(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """The merged guard fires before the dedup check.

    A PR that merged between tend cycles and is therefore already absent from
    the FIFO stays absent: the merged guard short-circuits admission before
    ``_pr_already_pending`` is consulted, so no entry is ever re-created. We
    assert dedup is never reached by spying on the membership scan.
    """
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    # Queue starts empty; the PR merged, so the guard must refuse admission.
    agent._branch_merged_into_main.return_value = True

    # Spy on the dedup membership read: the guard must short-circuit before it.
    model = agent.consciousness.load_self_model()
    dedup_reads: list[int] = []
    real_pending = model.pending_pr_requests

    class _Tripwire(list):  # type: ignore[type-arg]
        def __iter__(self) -> Any:
            dedup_reads.append(1)
            return super().__iter__()

    model.pending_pr_requests = _Tripwire(real_pending)
    agent.consciousness.load_self_model = lambda: model  # type: ignore[method-assign]

    result = TendPRsResult(
        changes_requested_prs=[
            (42, "title", "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)

    assert model.pending_pr_requests == []
    # Guard ran (read git-truth via the resolvable origin/<branch> ref) and
    # the dedup membership scan never did.
    agent._branch_merged_into_main.assert_called_once_with(
        f"origin/{_head('ep42')}",
    )
    assert dedup_reads == []


def test_branch_merged_into_main_delegates_to_git_truth(
    make_agent: Any, mem_fs: dict[str, str], monkeypatch: Any,
) -> None:
    """The real method body delegates to the zero-SDK git-merge-truth helper.

    The ``make_agent`` fixture default-mocks the bound method, so exercise the
    unbound function directly with the module-level helper patched — proving
    the seam reads git ancestry, not task stamps.
    """
    import maestro.agent as agent_mod
    from maestro.agent import MaestroAgent

    agent = make_agent()
    seen: list[str] = []
    monkeypatch.setattr(
        agent_mod,
        "is_branch_merged_into_main",
        lambda branch: seen.append(branch) or True,
    )
    result = MaestroAgent._branch_merged_into_main(agent, "maestro/evolve/ep1")
    assert result is True
    assert seen == ["maestro/evolve/ep1"]


# ---------------------------------------------------------------------------
# Bug-comment routing (the fourth FIFO category)
# ---------------------------------------------------------------------------


def test_bug_comments_routed_into_pending_request(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        bug_comments_routed=[
            (42, "title", [(100, "maestro/agent.py", "Off-by-one", "bug")],
             "ep42", "PR body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    entry = queue[0]
    assert entry.pr_branch == _head("ep42")
    assert entry.directive.startswith("Remediate PR #42: title")
    assert "unresolved AI bug review comment(s)" in entry.directive
    assert "Off-by-one (maestro/agent.py, comment 100)" in entry.directive
    # Verdicts are recorded as reply comments, never by resolving the thread.
    assert "resolveReviewThread" not in entry.directive
    assert "Do NOT resolve any thread" in entry.directive
    assert "Fixed locally with implementation-plan context." in entry.directive
    # The bug-comment problem must keep its standard remediation sections.
    assert "## Original Evolution Plan" in entry.directive


def test_routed_directive_plan_block_names_no_stage(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """Proposal 149: the tend route's directive plan block names no stage."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        bug_comments_routed=[
            (42, "title", [(100, "maestro/agent.py", "Off-by-one", "bug")],
             "ep42", "PR body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    directive = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "Stage:" not in directive
    assert "- Category: capability / Priority: capability" in directive


def test_bug_comments_do_not_reset_repetition_tracker(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """Bug-comment detection reads GitHub state and never touches the branch,
    so mere *routing* must NOT clear the no-op counter (mirrors
    ``ci_failure_on_head``). This is the unchanged half of the #2744 fix: only a
    *delivered-progress* lap resets, via ``_record_remediation_outcome`` (see
    ``test_comment_progress_lap_reopens_latched_pr``), never the route itself —
    so the #2440 / #2663 guard against re-routing genuine no-op comment work
    forever stays intact."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")

    agent.remediation_tracker.record_outcome(42, noop=True)
    agent.remediation_tracker.record_outcome(42, noop=True)

    result = TendPRsResult(
        bug_comments_routed=[
            (42, "title", [(100, "a.py", "Bug", "bug")], "ep42", "body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    # Gate stays tripped — no reset — so the PR is NOT re-routed.
    assert len(queue) == 0
    assert agent.remediation_tracker.should_reroute(42) is False


def test_bug_comments_collapse_with_quality_gates(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A PR firing both quality-gates and bug-comments collapses into one
    entry, and the combined directive is NOT preflight-skippable."""
    from maestro.agent import _is_pure_quality_gates_directive

    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        quality_gates_routed=[
            (42, "title", "mypy failed", "ep42", "body", "url", _head("ep42")),
        ],
        bug_comments_routed=[
            (42, "title", [(100, "a.py", "Bug", "bug")], "ep42", "body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    directive = queue[0].directive
    assert "quality gates failed: mypy failed" in directive
    assert "unresolved AI bug review comment(s)" in directive
    # Combined directive carries a bug-comment problem → never preflight-skip.
    assert _is_pure_quality_gates_directive(directive) is False


def test_bug_comments_problem_carries_validation_and_repair_discipline() -> None:
    """The rendered bug-comment directive folds in the retired processor's
    validate-with-a-failing-test and repair-broken-tests discipline
    (proposal 063, step 1)."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(
        7, [(100, "maestro/agent.py", "Off-by-one", "bug")],
    )
    # Failing-test-first validation with static-analysis fallback
    # (VALIDATION_PROMPT substance).
    assert "failing test that reproduces it" in directive
    assert "static analysis only when a test is impractical" in directive
    # Minimal, targeted fix (FIX_PROMPT substance).
    assert "minimal, targeted fix" in directive
    # Repair the tests the fix breaks rather than weakening it
    # (FINAL_GATE_FIX_PROMPT substance).
    assert "repair those tests" in directive
    assert "rather than weakening the fix" in directive


def test_bug_comments_problem_carries_disagreement_and_tracked_issue_paths() -> None:
    """The rendered bug-comment directive instructs the disagreement reply for
    unprovable defects and the tracked-issue-on-failure path for fixes that
    cannot land (proposal 063, step 2)."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(
        7, [(100, "maestro/agent.py", "Off-by-one", "bug")],
    )
    # Disagreement reply wording (_DISAGREEMENT_MARKER) so the next reviewer
    # run escalates an unprovable defect to an issue.
    assert "Could not prove this defect" in directive
    assert "escalates it to a tracked issue" in directive
    # Tracked-issue-on-failure path with the "Tracked in #" terminal marker
    # and dedup against existing open issues.
    assert "Tracked in #N" in directive
    assert "deduplicated GitHub issue" in directive


def test_bug_comments_problem_records_reply_not_self_posted() -> None:
    """The inline-thread sections (bug / suggestion / nit / human) instruct the
    task to RECORD each terminal reply into the deterministic thread-replies
    manifest — the loop is the single trusted deliverer — rather than POST it
    itself from the ``pulls/<n>/comments`` Bash path (#2817), which would
    double-post the thread. The terminal-marker phrases and the ungated
    ``gh api graphql`` thread read are unchanged."""
    from maestro.agent import _format_bug_comments_problem
    from maestro.loop.pr_remediation import _REPLY_MANIFEST_FILENAME

    directive = _format_bug_comments_problem(
        7,
        [
            (100, "a.py", "Off-by-one", "bug"),
            (101, "b.py", "Use a helper", "suggestion"),
            (102, "c.py", "Please clarify", "human"),
        ],
    )
    # Records into the deterministic, code-owned manifest — not a model-invented
    # filename — and the instruction verb is "record", not "reply".
    assert _REPLY_MANIFEST_FILENAME in directive
    assert "RECORD each terminal-marker reply" in directive
    # The self-posted reply is explicitly forbidden (the loop is the sole
    # deliverer; a task-side post would double-post), and the old
    # "reply on that thread" wording (which the model executed as that POST) is
    # gone from every section.
    assert "do NOT post the reply yourself" in directive
    assert "double-post the thread" in directive
    assert "reply on that thread" not in directive
    # The ungated thread READ stays — reads are never action-gated.
    assert "gh api graphql" in directive
    # Terminal markers survive verbatim for every routed type so
    # ``_BUG_COMMENT_TERMINAL_MARKERS`` / ``_NON_BUG_COMMENT_TERMINAL_MARKERS``
    # matching is unchanged.
    assert "Fixed locally with implementation-plan context." in directive
    assert "Could not prove this defect" in directive
    assert "Declined with implementation-plan context." in directive
    assert "Answered with implementation-plan context." in directive


def test_bug_comments_problem_single_pass_mandate_for_multiple_items() -> None:
    """When more than one review item is routed for a PR, the rendered directive
    leads with an explicit single-pass completeness mandate carrying the total
    count, so the remediation task addresses the whole list in one run instead
    of letting leftovers trickle across tend laps."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(
        7,
        [
            (100, "a.py", "Off-by-one", "bug"),
            (101, "b.py", "Use a helper", "suggestion"),
        ],
    )
    assert "Address EVERY one of the 2 review item(s)" in directive
    assert "do not stop after the first few" in directive
    assert "not complete until all" in directive
    # The mandate leads the directive, ahead of the per-type sections.
    assert directive.index("Address EVERY one") < directive.index(
        "unresolved AI bug review comment(s)",
    )


def test_single_pass_mandate_counts_threads_and_overflow_together() -> None:
    """The total in the mandate spans every routed item type — inline threads
    and body-overflow findings alike — so a PR with one thread and one overflow
    finding is told to address both in the single pass."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(
        7, [(100, "a.py", "Off-by-one", "bug")], [_finding()],
    )
    assert "Address EVERY one of the 2 review item(s)" in directive


def test_bug_comments_problem_no_single_pass_mandate_for_single_item() -> None:
    """A lone routed item needs no "do not stop after the first few" mandate —
    the per-type section already instructs addressing it — so the header is
    omitted to keep single-comment directives lean."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(
        7, [(100, "a.py", "Off-by-one", "bug")],
    )
    assert "Address EVERY one of the" not in directive


def test_eligible_bug_comments_filters_disagreement_and_tracked_markers() -> None:
    """Threads already carrying the disagreement marker or a "Tracked in #N"
    reply are excluded by ``_eligible_bug_comments``, so the autonomous path
    does not re-route a thread the remediation task terminally marked
    (proposal 063, step 2).

    Complements the canonical marker-coverage test in
    ``tests/test_tend_bug_comments.py`` (which loops over every entry of
    ``_BUG_COMMENT_TERMINAL_MARKERS``); this case anchors the two markers
    step 2's directive emits to that filtering guarantee.
    """
    from maestro.evolution_tend import _eligible_bug_comments

    def _thread(reply: str) -> dict[str, Any]:
        return {
            "id": "PRRT_1",
            "isResolved": False,
            "isOutdated": False,
            "comments": {
                "nodes": [
                    {
                        "databaseId": 1,
                        "path": "maestro/agent.py",
                        "body": "🐛 **Off-by-one in loop**\n\nDetail.",
                    },
                    {"databaseId": 2, "path": "maestro/agent.py", "body": reply},
                ],
            },
        }

    assert _eligible_bug_comments([_thread("Could not prove this defect — see test.")]) == []
    assert _eligible_bug_comments([_thread("Tracked in #4321 for follow-up.")]) == []


# ---------------------------------------------------------------------------
# Overflow-finding routing (proposal 064 — folded into the bug-comment block)
# ---------------------------------------------------------------------------


def _finding(
    fingerprint: str = "of-aaaaaaaa",
    file: str = "a.py",
    line: int = 0,
    title: str = "Null deref",
    description: str = "boom",
    severity: str = "bug",
) -> Any:
    from maestro.evolution_tend import OverflowBugFinding

    return OverflowBugFinding(
        fingerprint=fingerprint,
        file=file,
        line=line,
        title=title,
        description=description,
        severity=severity,
    )


def test_overflow_routed_into_pending_request(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        bug_overflow_routed=[
            (42, "title", [_finding()], "ep42", "PR body", "url",
             _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    entry = queue[0]
    assert entry.pr_branch == _head("ep42")
    assert entry.directive.startswith("Remediate PR #42: title")
    # References the finding by fingerprint and instructs the roll-up
    # acknowledgement-comment upsert — not a thread reply or resolution.
    assert "unresolved AI bug review overflow finding(s)" in entry.directive
    assert "of-aaaaaaaa" in entry.directive
    assert "Null deref (a.py:0)" in entry.directive
    assert "<!-- maestro:overflow-ack -->" in entry.directive
    assert "of-<hash> — <verdict>" in entry.directive
    assert "resolveReviewThread" not in entry.directive
    # Owned-PR self-delivery is unconditional (proposal 093): direct push, no
    # "Do NOT push".
    assert "git push origin HEAD:" in entry.directive
    assert "Do NOT push" not in entry.directive
    assert "## Original Evolution Plan" in entry.directive


def test_overflow_and_threads_collapse_into_one_problem(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """Thread bugs and overflow findings on the same PR render as one
    bug-review problem block in a single queue entry."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        bug_comments_routed=[
            (42, "title", [(100, "a.py", "Bug", "bug")], "ep42", "body", "url",
             _head("ep42")),
        ],
        bug_overflow_routed=[
            (42, "title", [_finding()], "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    directive = queue[0].directive
    assert "Bug (a.py, comment 100)" in directive
    assert "of-aaaaaaaa" in directive
    # Neither section resolves threads — the inline section replies, the
    # overflow section upserts the roll-up acknowledgement comment.
    assert "resolveReviewThread" not in directive
    assert "<!-- maestro:overflow-ack -->" in directive


def test_overflow_directive_not_preflight_skippable(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A quality-gates + overflow directive must never be preflight-skipped:
    overflow findings are reviewer-found defects invisible to ruff/mypy/pytest."""
    from maestro.agent import _is_pure_quality_gates_directive

    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")
    result = TendPRsResult(
        quality_gates_routed=[
            (42, "title", "mypy failed", "ep42", "body", "url", _head("ep42")),
        ],
        bug_overflow_routed=[
            (42, "title", [_finding()], "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 1
    directive = queue[0].directive
    assert "quality gates failed: mypy failed" in directive
    assert _is_pure_quality_gates_directive(directive) is False


def test_overflow_does_not_reset_repetition_tracker(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """Overflow detection reads GitHub state and never touches the branch, so
    mere *routing* must NOT clear the no-op counter (mirrors
    ``bug_comments_routed``). The #2744 reset is conditioned on *delivered
    progress* in ``_record_remediation_outcome`` (see
    ``test_comment_progress_lap_reopens_latched_pr``), not on routing — so this
    path stays unchanged."""
    agent = make_agent()
    _plant_plan(agent, plan_id="ep42")

    agent.remediation_tracker.record_outcome(42, noop=True)
    agent.remediation_tracker.record_outcome(42, noop=True)

    result = TendPRsResult(
        bug_overflow_routed=[
            (42, "title", [_finding()], "ep42", "body", "url", _head("ep42")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    queue = agent.consciousness.load_self_model().pending_pr_requests
    assert len(queue) == 0
    assert agent.remediation_tracker.should_reroute(42) is False


def test_overflow_problem_carries_validation_discipline_and_verdicts() -> None:
    """The overflow section is self-contained: it carries the validate→fix
    discipline plus all three terminal verdict wordings, since it may render
    without a thread section preceding it."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(7, [], [_finding()])
    assert "failing test that reproduces it" in directive
    assert "static analysis only when a test is impractical" in directive
    assert "minimal, targeted fix" in directive
    assert "rather than weakening the fix" in directive
    # All three terminal verdicts, in the canonical marker wording.
    assert "Fixed locally with implementation-plan context." in directive
    assert "Could not prove this defect" in directive
    assert "Tracked in #N" in directive
    # Upsert mechanics: locate-or-create plus append-only PATCH.
    assert "gh pr comment 7" in directive
    assert "--method PATCH" in directive
    assert "only append" in directive
    # No thread section rendered for an empty comments list.
    assert "comment-processor" not in directive
    assert "resolveReviewThread" not in directive


def test_bug_comments_problem_replies_instead_of_resolving() -> None:
    """The inline bug-thread section records every verdict as a reply comment
    and never instructs ``resolveReviewThread`` — a fixed thread gets a
    terminal "Fixed locally …" reply, not a resolution."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(7, [(100, "a.py", "Bug", "bug")], [])
    # Never resolves; replies for every verdict instead.
    assert "resolveReviewThread" not in directive
    assert "Do NOT resolve any thread" in directive
    # All three terminal verdict wordings posted as replies.
    assert "Fixed locally with implementation-plan context." in directive
    assert "Could not prove this defect" in directive
    assert "Tracked in #N" in directive


def test_inline_thread_sections_record_reply_to_manifest_not_top_level_comment() -> None:
    """Every inline-thread section (bug, suggestion, nit, human) must steer its
    verdict into the deterministic thread-replies manifest the loop delivers,
    never a top-level ``gh pr comment`` or a self-posted task-side reply (#2819
    landed via the #2817 trusted-delivery mechanism).

    On PR #2819 the loop "answered" an inline 💡 suggestion by posting two
    top-level PR issue comments instead of replying on the thread. A top-level
    comment never attaches to the thread, so ``_eligible_bug_comments`` and the
    reply-coverage audit both keep seeing the thread as unreplied and re-route
    it lap after lap. The #2817 fix lands the reply ON the thread for the model:
    the task RECORDS each terminal reply into ``thread-replies.jsonl`` and
    ``run()`` delivers it via a trusted ``reply_to_review_comment`` host call
    (which posts with ``in_reply_to`` — i.e. on the thread, the same place
    #2819 required). The loop is the single trusted deliverer, so each inline
    section must instruct the record-to-manifest contract and must NOT direct
    the task to post the ``pulls/<n>/comments`` reply itself (which would
    double-post the thread, even though the endpoint is gate-allowed since #2819).
    """
    from maestro.agent import _format_bug_comments_problem
    from maestro.loop.pr_remediation import _REPLY_MANIFEST_FILENAME

    for ctype in ("bug", "suggestion", "nit", "human"):
        directive = _format_bug_comments_problem(
            7, [(100, "a.py", "An item", ctype)],
        )
        # The deterministic manifest the loop reads and delivers to the thread.
        assert _REPLY_MANIFEST_FILENAME in directive, ctype
        assert "RECORD each terminal-marker reply" in directive, ctype
        # The self-posted reply is explicitly forbidden (loop is sole deliverer).
        assert "do NOT post the reply yourself" in directive, ctype
        assert "double-post the thread" in directive, ctype
        # The old top-level-comment / threaded-POST shortcut wording is gone.
        assert "threaded reply ON that review comment" not in directive, ctype
        assert "reply on that thread" not in directive, ctype


def test_overflow_only_directive_keeps_rollup_not_thread_reply_manifest() -> None:
    """Overflow findings have no review thread, so an overflow-only directive
    must keep the roll-up acknowledgement path and must NOT carry the inline
    thread-reply manifest contract (which only applies to inline comments)."""
    from maestro.agent import _format_bug_comments_problem
    from maestro.loop.pr_remediation import _REPLY_MANIFEST_FILENAME

    directive = _format_bug_comments_problem(7, [], [_finding()])
    assert "<!-- maestro:overflow-ack -->" in directive
    assert _REPLY_MANIFEST_FILENAME not in directive
    assert "RECORD each terminal-marker reply" not in directive


def test_overflow_problem_carries_bug_comments_marker() -> None:
    """Both section shapes must contain ``_BUG_COMMENTS_PROBLEM_MARKER`` so the
    preflight gate never classifies an overflow-only directive as
    quality-gates-only."""
    from maestro.agent import (
        _BUG_COMMENTS_PROBLEM_MARKER,
        _format_bug_comments_problem,
    )

    overflow_only = _format_bug_comments_problem(7, [], [_finding()])
    assert _BUG_COMMENTS_PROBLEM_MARKER in overflow_only
    threads_only = _format_bug_comments_problem(
        7, [(100, "a.py", "Bug", "bug")],
    )
    assert _BUG_COMMENTS_PROBLEM_MARKER in threads_only


# ---------------------------------------------------------------------------
# Suggestion / nit / human thread rendering (addressing all comment types)
# ---------------------------------------------------------------------------


def test_suggestion_nit_threads_render_apply_or_decline() -> None:
    """💡 suggestion and ✏️ nit threads carry the validate-as-improvement /
    apply-or-decline discipline with the Declined terminal marker."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(
        7,
        [
            (100, "a.py", "Use a helper", "suggestion"),
            (101, "b.py", "Trailing space", "nit"),
        ],
    )
    assert "unresolved AI review suggestion/nit comment(s)" in directive
    assert "💡 Use a helper (a.py, comment 100)" in directive
    assert "✏️ Trailing space (b.py, comment 101)" in directive
    assert "genuine, in-scope improvement" in directive
    assert "Fixed locally with implementation-plan context." in directive
    assert "Declined with implementation-plan context." in directive
    # Suggestions are not defects — no disagreement / tracked-issue wording.
    assert "Could not prove this defect" not in directive
    assert "Tracked in #N" not in directive
    assert "Do NOT resolve any thread" in directive
    assert "resolveReviewThread" not in directive


def test_human_threads_render_apply_decline_or_answer() -> None:
    """Human review threads carry apply / decline / answer paths with the
    Answered terminal marker for questions."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(
        7, [(100, "a.py", "Please clarify this", "human")],
    )
    assert "unresolved human reviewer thread(s)" in directive
    assert "Please clarify this (a.py, comment 100)" in directive
    assert "Fixed locally with implementation-plan context." in directive
    assert "Declined with implementation-plan context." in directive
    assert "Answered with implementation-plan context." in directive
    assert "Do NOT resolve any thread" in directive


def test_nonbug_overflow_renders_apply_or_decline() -> None:
    """Suggestion/nit overflow findings carry the apply-or-decline discipline
    recorded on the roll-up acknowledgement comment."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(
        7, [], [_finding(severity="suggestion", title="Rename")],
    )
    assert "unresolved AI review suggestion/nit overflow finding(s)" in directive
    assert "of-aaaaaaaa" in directive
    assert "💡 Rename" in directive
    assert "Fixed locally with implementation-plan context." in directive
    assert "Declined with implementation-plan context." in directive
    assert "<!-- maestro:overflow-ack -->" in directive
    assert "--method PATCH" in directive
    # Not a defect channel.
    assert "Could not prove this defect" not in directive


def test_all_comment_types_collapse_into_one_problem() -> None:
    """Bug, suggestion, nit, human threads, and overflow findings of mixed
    severity all render in a single problem block."""
    from maestro.agent import _format_bug_comments_problem

    directive = _format_bug_comments_problem(
        7,
        [
            (100, "a.py", "Bug here", "bug"),
            (101, "b.py", "A suggestion", "suggestion"),
            (102, "c.py", "A nit", "nit"),
            (103, "d.py", "Human ask", "human"),
        ],
        [
            _finding(fingerprint="of-11111111", severity="bug", title="OF bug"),
            _finding(
                fingerprint="of-22222222", severity="nit", title="OF nit",
            ),
        ],
    )
    assert "unresolved AI bug review comment(s)" in directive
    assert "unresolved AI review suggestion/nit comment(s)" in directive
    assert "unresolved human reviewer thread(s)" in directive
    assert "unresolved AI bug review overflow finding(s)" in directive
    assert "unresolved AI review suggestion/nit overflow finding(s)" in directive


def test_non_bug_directive_not_preflight_skippable() -> None:
    """A quality-gates + suggestion/nit/human directive must never be
    preflight-skipped — reviewer/human feedback is invisible to the gates."""
    from maestro.agent import _is_pure_quality_gates_directive
    from maestro.loop.pr_remediation import (
        _format_bug_comments_problem,
        _format_quality_gates_problem,
    )

    for ctype, title in (
        ("suggestion", "A suggestion"),
        ("nit", "A nit"),
        ("human", "Human ask"),
    ):
        directive = "\n\n".join([
            _format_quality_gates_problem(7, "mypy failed"),
            _format_bug_comments_problem(7, [(100, "a.py", title, ctype)]),
        ])
        assert _is_pure_quality_gates_directive(directive) is False, ctype


def test_provenance_write_failure_does_not_drop_pr_admission(
    make_agent: Any, mem_fs: dict[str, str], monkeypatch: Any,
) -> None:
    """A provenance (measurement) write failure must not drop the PR admission.

    Regression guard for the save-before-provenance ordering contract (#2531).
    In ``_route_pr_feedback_to_pending_requests`` the operational admission is
    persisted via ``save_self_model`` *before* the ``agent.pr_provenance.record``
    loop appends to the measurement backend. Because the provenance write is
    measurement-only ("gates nothing"), a disk-full / lock-contention error
    there must under-count the readmission sensor rather than unwind the
    function before the queue is saved and silently drop the operational PR
    admission.

    This test forces the provenance backend's ``record`` to raise and asserts
    the PR was still queued — it would turn RED if the ordering were reversed
    (provenance before save). ``contextlib.suppress`` tolerates the propagated
    error so the assertion isolates the persistence guarantee, independent of
    whether the measurement error is re-raised or swallowed.
    """
    agent = make_agent()
    _plant_plan(agent, plan_id="ep9")

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full while appending provenance")

    monkeypatch.setattr(agent.pr_provenance, "record", _boom)

    result = TendPRsResult(
        changes_requested_prs=[
            (9, "Refactor planner", "ep9", "PR body",
             "https://github.com/x/y/pull/9", _head("ep9")),
        ],
    )
    with contextlib.suppress(OSError):
        agent._route_pr_feedback_to_pending_requests(result)

    model = agent.consciousness.load_self_model()
    assert len(model.pending_pr_requests) == 1
    assert model.pending_pr_requests[0].directive.startswith(
        "Remediate PR #9: Refactor planner",
    )


# ---------------------------------------------------------------------------
# Thread-reply completeness gate (#2663)
# A stand-down (or any no-commit termination) on an open PR whose routed 🐛
# threads remain eligible is a failure, not a no-op/chosen-quiet.
# ---------------------------------------------------------------------------


def _eligible_thread() -> dict[str, Any]:
    """Minimal review-thread node that ``_eligible_bug_comments`` marks eligible."""
    return {
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {
                    "databaseId": 100,
                    "path": "maestro/agent.py",
                    "body": "🐛 **Off-by-one in loop**\n\nDetail.",
                },
            ],
        },
    }


def _terminal_reply_thread() -> dict[str, Any]:
    """Thread whose root is an AI 🐛 finding but a terminal reply was posted."""
    return {
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {
                    "databaseId": 101,
                    "path": "maestro/agent.py",
                    "body": "🐛 **Null pointer**\n\nDetail.",
                },
                {"databaseId": 102, "path": "", "body": "Fixed automatically"},
            ],
        },
    }


@pytest.mark.asyncio
async def test_stand_down_on_open_pr_with_eligible_thread_is_failure(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A [stood-down] / no-commit remediation on an open PR whose 🐛 thread
    still lacks a terminal reply is recorded as noop=True with stuck_reason
    'unresolved review items', not silently re-routed (#2663)."""
    agent = make_agent()
    # Shrink budget to 1 so this single attempt latches stuck and we can
    # verify stuck_reason without multiple calls.
    agent.pr_ledger = PRLedger(agent.config.memory_dir, max_attempts_per_head=1)
    # Open PR: is_pr_merged=False (conftest default), is_pr_closed=False (default).
    agent.evolution._host.get_pr_review_threads = MagicMock(
        return_value=[_eligible_thread()],
    )
    agent._last_remediation_merge_block = None
    # No commit landed (stood-down / no-commit termination).
    agent._vcs.rev_parse = MagicMock(return_value="same-sha")

    record = MagicMock(result_summary="[stood-down]", key_findings="", goal="")
    await agent._record_remediation_outcome(
        goal="Remediate PR #55: Fix off-by-one",
        pr_branch="maestro/evolve/ep55",
        record=record,
        head_before="same-sha",
    )

    assert agent.remediation_tracker._data["55"] == [True]
    entry = agent.pr_ledger.get(55)
    assert entry is not None
    assert entry.last_outcome == "noop"
    assert entry.stuck is True
    assert entry.stuck_reason == "unresolved review items"


@pytest.mark.asyncio
async def test_stand_down_on_merged_pr_is_not_thread_failure(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """A no-commit remediation on an already-merged PR skips the thread check
    — a merge-truth stand-down is legitimate and uses the existing noop path
    with stuck_reason 'no commit produced' (#2663)."""
    agent = make_agent()
    agent.pr_ledger = PRLedger(agent.config.memory_dir, max_attempts_per_head=1)
    # Merged PR (settled per the ``is_pr_settled`` chokepoint): the thread check
    # must be skipped even if threads exist — a merge-truth stand-down is
    # legitimate (proposal 087).
    agent.is_pr_settled = MagicMock(
        return_value=MergeTruth(
            pr_number=56, settled=True, state="MERGED",
            branch_landed=True, source="host_api", observed_at=0.0,
        ),
    )
    agent.evolution._host.get_pr_review_threads = MagicMock(
        return_value=[_eligible_thread()],
    )
    agent._last_remediation_merge_block = None
    agent._vcs.rev_parse = MagicMock(return_value="same-sha")

    record = MagicMock(result_summary="Resolved on branch, awaiting tend push", key_findings="")
    await agent._record_remediation_outcome(
        goal="Remediate PR #56: Fix merge",
        pr_branch="maestro/evolve/ep56",
        record=record,
        head_before="same-sha",
    )

    assert agent.remediation_tracker._data["56"] == [True]
    entry = agent.pr_ledger.get(56)
    assert entry is not None
    assert entry.stuck_reason == "no commit produced"


@pytest.mark.asyncio
async def test_terminal_reply_posted_clears_thread_check(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """When a terminal-marker reply was already posted the thread is no longer
    eligible, so a no-commit remediation on an open PR uses the ordinary
    'no commit produced' stuck_reason — not the thread-noop path (#2663)."""
    agent = make_agent()
    agent.pr_ledger = PRLedger(agent.config.memory_dir, max_attempts_per_head=1)
    # Open PR with a thread that already has a terminal reply.
    agent.evolution._host.get_pr_review_threads = MagicMock(
        return_value=[_terminal_reply_thread()],
    )
    agent._last_remediation_merge_block = None
    agent._vcs.rev_parse = MagicMock(return_value="same-sha")

    record = MagicMock(result_summary="", key_findings="")
    await agent._record_remediation_outcome(
        goal="Remediate PR #57: Fix null ptr",
        pr_branch="maestro/evolve/ep57",
        record=record,
        head_before="same-sha",
    )

    assert agent.remediation_tracker._data["57"] == [True]
    entry = agent.pr_ledger.get(57)
    assert entry is not None
    assert entry.stuck_reason == "no commit produced"


@pytest.mark.asyncio
async def test_unaddressed_overflow_finding_is_failure(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """The fallback no-op check (gate count unset) scans body-overflow findings
    too: a remediation that committed and cleared every inline thread but left
    an overflow finding un-acknowledged is still a no-op, recorded with
    stuck_reason 'unresolved review items' (#2663)."""
    from maestro.evolution_tend import _OVERFLOW_SENTINEL_CLOSE, _OVERFLOW_SENTINEL_OPEN

    agent = make_agent()
    agent.pr_ledger = PRLedger(agent.config.memory_dir, max_attempts_per_head=1)
    # No inline threads remain, but one overflow finding is un-acknowledged.
    agent.evolution._host.get_pr_review_threads = MagicMock(return_value=[])
    agent.evolution._host.get_pr_review_bodies = MagicMock(
        return_value=[
            f"Summary.\n\n{_OVERFLOW_SENTINEL_OPEN}\n"
            "- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom\n"
            f"{_OVERFLOW_SENTINEL_CLOSE}\n",
        ],
    )
    agent.evolution._host.get_pr_comment_bodies = MagicMock(return_value=[])
    agent._last_remediation_merge_block = None
    # A real commit landed — isolating the overflow finding as the noop cause.
    agent._vcs.rev_parse = MagicMock(return_value="after")

    record = MagicMock(result_summary="Committed a real fix.", key_findings="")
    await agent._record_remediation_outcome(
        goal="Remediate PR #58: address comments",
        pr_branch="maestro/evolve/ep58",
        record=record,
        head_before="before",
    )

    assert agent.remediation_tracker._data["58"] == [True]
    entry = agent.pr_ledger.get(58)
    assert entry is not None
    assert entry.stuck_reason == "unresolved review items"


# --- Delivery-note direct-push wording (proposal 093) ----------------------
#
# The trailing delivery note every problem block embeds is unconditional: it
# instructs the agent to push its own committed fix to the PR branch and drops
# the "delivered automatically" framing, matching the widened action gate. The
# reply-manifest contract (``_REPLY_DELIVERY_NOTE``) is unaffected.


def test_delivery_note_instructs_direct_push_no_dangerous_verbs() -> None:
    """The delivery note names the PR-branch push and forbids the dangerous verbs.

    The widened action gate authorizes only a plain push to the own PR head, so
    the note names that push, drops the "delivered automatically" framing, and
    explicitly forbids a force-push, a ``gh pr merge``, and a push to
    ``main``/``master`` — the human approval/merge gate is unchanged.
    """
    from maestro.loop.pr_remediation import _delivery_note

    note = _delivery_note()
    assert "git push origin HEAD:" in note
    assert "Do NOT push" not in note
    assert "delivered to the PR automatically" not in note
    assert "handled for you" not in note
    assert "force-push" in note
    assert "gh pr merge" in note
    assert "`main`/`master`" in note


def test_delivery_note_interpolates_shared_push_template() -> None:
    """The note's push literal is the shared ``OWN_BRANCH_PUSH_TEMPLATE`` (096).

    Centralising the command shape means the form the loop *instructs* and the
    form the push-conformance golden test proves the gate *allows* share one
    source, so they cannot drift.  The interpolated template is present and the
    prose is unchanged from the byte-for-byte wording.
    """
    from maestro.loop.pr_remediation import (
        OWN_BRANCH_PUSH_TEMPLATE,
        _delivery_note,
    )

    assert OWN_BRANCH_PUSH_TEMPLATE == "git push origin HEAD:{branch}"
    push_command = OWN_BRANCH_PUSH_TEMPLATE.format(branch="maestro/evolve/<id>")
    assert push_command == "git push origin HEAD:maestro/evolve/<id>"
    assert f"`{push_command}`" in _delivery_note()


def test_conflict_problem_embeds_direct_push_delivery_note() -> None:
    """``_format_conflict_problem`` embeds the direct-push delivery note."""
    from maestro.loop.pr_remediation import _format_conflict_problem

    problem = _format_conflict_problem(11, ["a.py"])
    assert "git push origin HEAD:" in problem
    assert "Do NOT push" not in problem


def test_quality_gates_problem_embeds_direct_push_delivery_note() -> None:
    """The pre-merge CI-failure branch embeds the direct-push delivery note.

    Only the ``is_ci_failure_on_head_summary`` branch carries a delivery note;
    the post-merge branch has none (see the sibling test below).
    """
    from maestro.loop.pr_remediation import _format_quality_gates_problem

    problem = _format_quality_gates_problem(11, "ci_failure_on_head: mypy")
    assert "git push origin HEAD:" in problem
    assert "Do NOT push" not in problem


def test_quality_gates_post_merge_branch_has_no_delivery_note() -> None:
    """The post-merge gate-failure branch carries no delivery note."""
    from maestro.loop.pr_remediation import _format_quality_gates_problem

    problem = _format_quality_gates_problem(11, "mypy failed after merge")
    assert "Do NOT push" not in problem
    assert "git push origin HEAD:" not in problem


def test_bug_comments_problem_embeds_direct_push_and_reply_note() -> None:
    """Bug-comment blocks embed the direct-push note and keep the reply contract.

    The delivery note names the direct push; the reply-manifest instruction
    (``_REPLY_DELIVERY_NOTE``) stays present because reply delivery is
    loop-owned (a reply POST is not idempotent).
    """
    from maestro.loop.pr_remediation import (
        _REPLY_DELIVERY_NOTE,
        _format_bug_comments_problem,
    )

    comments = [(100, "a.py", "Null deref", "bug")]
    problem = _format_bug_comments_problem(11, comments)
    assert "git push origin HEAD:" in problem
    assert "Do NOT push" not in problem
    assert _REPLY_DELIVERY_NOTE in problem


def test_user_demand_problem_embeds_direct_push_delivery_note() -> None:
    """``_format_user_demand_problem`` embeds the direct-push delivery note."""
    from maestro.loop.pr_remediation import (
        _REPLY_DELIVERY_NOTE,
        _format_user_demand_problem,
    )

    problem = _format_user_demand_problem(11, "please finish this")
    assert "git push origin HEAD:" in problem
    assert "Do NOT push" not in problem
    assert _REPLY_DELIVERY_NOTE in problem


def test_route_feedback_instructs_direct_push(
    make_agent: Any, mem_fs: dict[str, str],
) -> None:
    """Routed conflict directives instruct a direct push.

    Exercises the ``route_feedback`` call-site: the stored directive drops
    "Do NOT push", names the direct PR-branch push, and the composed prompt's
    own guidance sentence also flips to direct push.
    """
    agent = make_agent()
    _plant_plan(agent, plan_id="ep11")
    result = TendPRsResult(
        conflicts_routed=[
            (11, "title", ["a.py", "b.py"], "ep11", "body", "url",
             _head("ep11")),
        ],
    )
    agent._route_pr_feedback_to_pending_requests(result)
    goal = agent.consciousness.load_self_model().pending_pr_requests[0].directive
    assert "Do NOT push" not in goal
    assert "git push origin HEAD:" in goal
    # The composed prompt's own guidance sentence also flips to direct push.
    assert "handled for you" not in goal
