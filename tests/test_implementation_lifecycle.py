"""implement_plan lifecycle, quality-gate wiring, open-PR cap / WIP deferral and
the uncalled-symbol gate (``maestro/loop/implementation.py``); split out of
``tests/test_agent_lifecycle.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.config import Config
from maestro.evolution import MergeCheckResult, QualityGateResult
from maestro.loop.implementation import _check_uncalled_symbol_gate
from maestro.memory import EvolutionPlan
from maestro.vcs import DirtyWorkingTreeError, VCSError
from tests.conftest import MockQueryResult
from tests.test_agent_lifecycle import (
    FAKE_DIR,
    _grep_no_match,
    _make_approved_plan,
    _make_plan,
    _stub_loop_externals,
    _sub_side_effect_factory,
)

_OK_REVIEW = QualityGateResult(
    passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
    raw_output="Ruff: clean\n\nMypy: clean\n\nTests: clean",
)


# ──────────────────────────────────────────────────────────────────────
# Defect check tests (DuplicatePlanError, submit_for_review)
# ──────────────────────────────────────────────────────────────────────


DEFECT_DIR = Path("/fake/defect_check")


def test_save_evolution_plan_raises_duplicate_plan_error_on_duplicate(
    mem_fs: dict[str, str],
) -> None:
    """save_evolution_plan must raise DuplicatePlanError (not plain ValueError)
    when called twice with the same plan_id."""
    from maestro.memory import DuplicatePlanError, Memory

    mem = Memory(DEFECT_DIR)
    plan = _make_plan("plan-dup-001", title="My plan")

    # First save should succeed
    mem.save_evolution_plan(plan)

    # Second save with same plan_id must raise DuplicatePlanError
    with pytest.raises(DuplicatePlanError, match="duplicate plan_id"):
        mem.save_evolution_plan(plan)


def test_duplicate_plan_error_is_subclass_of_value_error(
    mem_fs: dict[str, str],
) -> None:
    """DuplicatePlanError must be a subclass of ValueError for backward compat."""
    from maestro.memory import DuplicatePlanError, Memory

    mem = Memory(DEFECT_DIR)
    plan = _make_plan("plan-dup-002", title="Compat plan")

    mem.save_evolution_plan(plan)

    # Catching it as ValueError must also work (backward compatibility)
    with pytest.raises(ValueError, match="duplicate plan_id"):
        mem.save_evolution_plan(plan)

    # Verify it IS an instance of DuplicatePlanError
    assert issubclass(DuplicatePlanError, ValueError)


@pytest.mark.asyncio
async def test_submit_for_review_empty_title_retry_is_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """A retry of submit_for_review for an empty-titled plan must NOT raise."""
    from maestro.consciousness import Consciousness
    from maestro.evolution import EvolutionEngine
    from maestro.journal import Journal
    from maestro.memory import Memory
    from maestro.notifications.base import NullNotifier

    mem = Memory(DEFECT_DIR)
    consciousness = Consciousness(DEFECT_DIR)
    journal = Journal(DEFECT_DIR)
    config = Config(memory_dir=DEFECT_DIR, workspace=DEFECT_DIR)
    notifier = NullNotifier()

    engine = EvolutionEngine(
        memory=mem,
        consciousness=consciousness,
        journal=journal,
        notifier=notifier,
        config=config,
    )

    # An empty-titled plan (whitespace title skips dedup in submit_for_review)
    empty_title_plan = _make_plan("plan-empty-title-001", title="   ")

    # First call — should succeed (plan gets saved)
    await engine.submit_for_review(empty_title_plan)

    # Retry (second call) — must NOT raise, treated as idempotent
    await engine.submit_for_review(empty_title_plan)  # no exception expected

    # Plan should still be saved exactly once
    all_plans = mem.get_all_plans()
    matching = [p for p in all_plans if p.plan_id == "plan-empty-title-001"]
    assert len(matching) == 1, "Plan should exist exactly once after idempotent retry"


@pytest.mark.asyncio
async def test_implement_plan_creates_pr_and_increments(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()

    plan = _make_approved_plan("impl_1", "Implementation test")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("impl_1", "approved")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.contributions, "get_current_head", return_value="abc123"), \
         patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW):
        mock_q.return_value = MockQueryResult(response="implemented")
        mock_sub.side_effect = _sub_side_effect_factory()

        await agent.implement_plan("impl_1")

    # Journal should mention evolution implemented
    entries = agent.journal.read_recent_events(10)
    milestone_entries = [e for e in entries if e.entry_type == "milestone"]
    assert any("Evolution implemented" in e.content for e in milestone_entries)


@pytest.mark.asyncio
async def test_implement_plan_dirty_tree_at_push_fails_plan(make_agent):  # type: ignore[no-untyped-def]
    """A tree still dirty at ``create_pr`` is refused before the push and takes
    the ordinary plan-failure path, not the environment-fault re-raise (#4067)."""
    agent = make_agent()

    plan = _make_approved_plan("dirty_push", "Dirty push test")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("dirty_push", "approved")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.contributions, "get_current_head", return_value="abc123"), \
         patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW):
        mock_q.return_value = MockQueryResult(response="implemented")
        mock_sub.side_effect = _sub_side_effect_factory(clean_after_commit=False)

        with pytest.raises(VCSError, match="uncommitted changes") as excinfo:
            await agent.implement_plan("dirty_push")

    assert not isinstance(excinfo.value, DirtyWorkingTreeError)

    pushes = [
        c for c in mock_sub.call_args_list if c.args[0][:2] == ["git", "push"]
    ]
    assert pushes == []
    saved = agent.memory.get_plan("dirty_push")
    assert saved is not None
    assert saved.status == "failed"
    assert saved.failure_count == 1


@pytest.mark.asyncio
async def test_implement_plan_pre_pr_exception_resets_status(make_agent):  # type: ignore[no-untyped-def]
    """Exception before create_pr must reset plan status and call cleanup_branch."""
    agent = make_agent()

    plan = _make_approved_plan("pre_pr_err", "Pre-PR error test")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("pre_pr_err", "approved")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.contributions, "get_current_head") as mock_head, \
         patch.object(agent.evolution, "cleanup_branch") as mock_cleanup, \
         patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW):
        mock_q.return_value = MockQueryResult(response="implemented")
        mock_head.side_effect = RuntimeError("head failed")

        def sub_side_effect(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["git", "status"] and "--porcelain" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=" M maestro/agent.py\n", stderr="",
                )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="main\n", stderr="")

        mock_sub.side_effect = sub_side_effect

        with pytest.raises(RuntimeError, match="head failed"):
            await agent.implement_plan("pre_pr_err")

    # cleanup_branch MUST be called — no PR was created
    mock_cleanup.assert_called_once_with("pre_pr_err")
    # Plan must be marked 'failed' with failure reason so
    # _implement_approved_plans retries it on the next iteration.
    saved = agent.memory.get_plan("pre_pr_err")
    assert saved is not None
    assert saved.status == "failed"
    assert saved.failure_count == 1
    assert saved.last_failure_reason is not None
    assert "Implementation failed" in saved.last_failure_reason


@pytest.mark.asyncio
async def test_implement_plan_failure_sends_notification(make_agent):  # type: ignore[no-untyped-def]
    """Implementation failure must send a Telegram notification."""
    agent = make_agent()

    plan = _make_approved_plan("notify_err", "Notification failure test")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("notify_err", "approved")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.evolution, "cleanup_branch") as mock_cleanup, \
         patch.object(agent.notifier, "notify", new_callable=AsyncMock) as mock_notify, \
         patch.object(agent.evolution, "create_issue") as mock_issue:
        mock_q.side_effect = RuntimeError("SDK exploded")

        def sub_side_effect(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="main\n", stderr="")

        mock_sub.side_effect = sub_side_effect

        with pytest.raises(RuntimeError, match="SDK exploded"):
            await agent.implement_plan("notify_err")

    mock_cleanup.assert_called_once_with("notify_err")
    mock_notify.assert_called_once()
    msg = mock_notify.call_args[0][0]
    assert "Notification failure test" in msg
    assert "Implementation failed" in msg
    assert "attempt 1/" in msg
    # The actual failure cause must reach the operator, not just "something failed".
    assert "SDK exploded" in msg
    # Non-terminal failure (attempt 1 of 3 by default) — issue must NOT fire.
    mock_issue.assert_not_called()


@pytest.mark.asyncio
async def test_implement_plan_failure_notification_error_propagates(make_agent):  # type: ignore[no-untyped-def]
    """When the SDK fails AND the notifier fails, the notifier exception wins.

    ``implement_plan`` catches the SDK error, calls
    ``_demote_plan_to_failed`` which updates the plan status to
    ``failed`` first and then routes the failure ping through
    ``_notify_best_effort``. With the narrowed catch the notifier's
    ``RuntimeError`` propagates and replaces the original SDK error,
    but the persisted status / failure_count / last_failure_reason are
    already in place — those reflect the SDK error that triggered the
    demotion path.
    """
    agent = make_agent()

    plan = _make_approved_plan("tolerate_err", "Tolerate notification error")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("tolerate_err", "approved")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.evolution, "cleanup_branch"), \
         patch.object(agent.notifier, "notify", new_callable=AsyncMock, side_effect=RuntimeError("notify failed")), \
         patch.object(agent.evolution, "create_issue", side_effect=RuntimeError("issue failed")):
        mock_q.side_effect = RuntimeError("SDK exploded")

        def sub_side_effect(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="main\n", stderr="")

        mock_sub.side_effect = sub_side_effect

        with pytest.raises(RuntimeError, match="notify failed"):
            await agent.implement_plan("tolerate_err")

    saved = agent.memory.get_plan("tolerate_err")
    assert saved is not None
    assert saved.status == "failed"
    assert saved.failure_count == 1
    assert saved.last_failure_reason is not None
    assert "Implementation failed" in saved.last_failure_reason
    assert "SDK exploded" in saved.last_failure_reason


@pytest.mark.asyncio
@pytest.mark.parametrize("category_name", [
    "STREAM_STALL", "RATE_LIMIT", "CLI_CRASH", "STALE_SESSION",
])
async def test_implement_plan_transient_sdk_failure_leaves_plan_approved(  # type: ignore[no-untyped-def]
    make_agent, category_name,
):
    """Transient SDK failures must NOT demote the plan — they're retried by run_loop.

    When ``implement()`` raises an SDKError in one of the four categories the
    iteration-level handler in ``run_loop`` retries (stream stall, rate limit,
    CLI crash, stale session), ``implement_plan`` must leave the plan in
    ``approved`` status so ``_implement_approved_plans`` picks it up again on
    the retry. Demoting to ``pending_review`` would consume the human approval
    and strand the plan with a misleading rejection reason.
    """
    from maestro.sdk import ErrorCategory, SDKError

    agent = make_agent()
    category = getattr(ErrorCategory, category_name)

    plan = _make_approved_plan(
        f"transient_{category_name.lower()}",
        f"Transient {category_name} test",
    )
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status(plan.plan_id, "approved")

    with patch.object(
            agent.evolution, "implement", new_callable=AsyncMock,
         ) as mock_impl, \
         patch.object(agent.evolution, "cleanup_branch") as mock_cleanup, \
         patch.object(
            agent.notifier, "notify", new_callable=AsyncMock,
         ) as mock_notify, \
         patch.object(agent.evolution, "create_issue") as mock_issue, \
         patch.object(
            agent.evolution, "run_quality_gates", return_value=_OK_REVIEW,
         ):
        mock_impl.side_effect = SDKError("transient", category=category)

        with pytest.raises(SDKError) as ei:
            await agent.implement_plan(plan.plan_id)

    # Original category must propagate up unchanged so run_loop can retry
    assert ei.value.category is category

    # Plan MUST remain approved — no demote, no rejection reason
    saved = agent.memory.get_plan(plan.plan_id)
    assert saved is not None
    assert saved.status == "approved"
    assert saved.rejection_reason is None

    # Branch must still be cleaned up so the retry re-creates a fresh one
    mock_cleanup.assert_called_once_with(plan.plan_id)

    # Neither Telegram nor GitHub issue channel fires for transient failures
    mock_notify.assert_not_called()
    mock_issue.assert_not_called()


@pytest.mark.asyncio
async def test_implement_plan_non_transient_sdk_error_still_demotes(  # type: ignore[no-untyped-def]
    make_agent,
):
    """A non-transient SDKError (e.g. UNKNOWN) must still demote and notify.

    Regression guard: only the four categories the iteration-level handler
    actively retries belong in ``_TRANSIENT_SDK_CATEGORIES``. Every other
    SDKError category goes through the terminal path.
    """
    from maestro.sdk import ErrorCategory, SDKError

    agent = make_agent()

    plan = _make_approved_plan("non_transient", "Non-transient SDK error")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("non_transient", "approved")

    with patch.object(
            agent.evolution, "implement", new_callable=AsyncMock,
         ) as mock_impl, \
         patch.object(agent.evolution, "cleanup_branch") as mock_cleanup, \
         patch.object(
            agent.notifier, "notify", new_callable=AsyncMock,
         ) as mock_notify, \
         patch.object(agent.evolution, "create_issue") as mock_issue, \
         patch.object(
            agent.evolution, "run_quality_gates", return_value=_OK_REVIEW,
         ):
        mock_impl.side_effect = SDKError(
            "unknown explosion", category=ErrorCategory.UNKNOWN,
        )

        with pytest.raises(SDKError):
            await agent.implement_plan("non_transient")

    saved = agent.memory.get_plan("non_transient")
    assert saved is not None
    assert saved.status == "failed"
    assert saved.failure_count == 1
    assert saved.last_failure_reason is not None
    assert "Implementation failed" in saved.last_failure_reason

    mock_cleanup.assert_called_once_with("non_transient")
    mock_notify.assert_called_once()
    # Non-terminal failure — no GitHub issue until attempt reaches the cap.
    mock_issue.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# evolve — self-review abort
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evolve_aborts_on_quality_gates_failure(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()

    with patch.object(agent.evolution, "run_quality_gates", side_effect=RuntimeError("ruff crashed")), \
         patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_c, \
         patch("maestro.evolution.query", new_callable=AsyncMock) as mock_e:

        await agent.evolve()

    # Should NOT have called any Claude queries — evolution aborted early
    mock_c.assert_not_called()
    mock_e.assert_not_called()

    # No plans should be created
    plans = agent.memory.get_pending_plans()
    assert len(plans) == 0


# ──────────────────────────────────────────────────────────────────────
# Post-implementation quality gate
# ──────────────────────────────────────────────────────────────────────

_FAIL_REVIEW = QualityGateResult(
    passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
    raw_output="Ruff issues:\nE501 line too long",
)


@pytest.mark.asyncio
async def test_quality_gate_passes_creates_pr(make_agent):  # type: ignore[no-untyped-def]
    """When post-implementation review passes, create_pr is called normally."""
    agent = make_agent()

    plan = _make_approved_plan("qg_pass", "Quality gate pass test")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("qg_pass", "approved")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.contributions, "get_current_head", return_value="abc123"), \
         patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW):
        mock_q.return_value = MockQueryResult(response="implemented")
        mock_sub.side_effect = _sub_side_effect_factory()

        await agent.implement_plan("qg_pass")

    saved = agent.memory.get_plan("qg_pass")
    assert saved is not None
    assert saved.status == "implemented"
    assert saved.pr_url is not None


@pytest.mark.asyncio
async def test_quality_gate_fails_autofix_succeeds(make_agent):  # type: ignore[no-untyped-def]
    """When review fails then auto-fix succeeds, create_pr is called."""
    agent = make_agent()

    plan = _make_approved_plan("qg_fix", "Quality gate auto-fix test")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("qg_fix", "approved")

    review_call_count = 0

    def _quality_gates_side_effect() -> QualityGateResult:
        nonlocal review_call_count
        review_call_count += 1
        if review_call_count == 1:
            return _FAIL_REVIEW
        return _OK_REVIEW

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.contributions, "get_current_head", return_value="abc123"), \
         patch.object(agent.evolution, "run_quality_gates", side_effect=_quality_gates_side_effect), \
         patch.object(agent.evolution, "auto_fix", new_callable=AsyncMock, return_value=True):
        mock_q.return_value = MockQueryResult(response="implemented")
        mock_sub.side_effect = _sub_side_effect_factory()

        await agent.implement_plan("qg_fix")

    saved = agent.memory.get_plan("qg_fix")
    assert saved is not None
    assert saved.status == "implemented"
    assert saved.pr_url is not None


@pytest.mark.asyncio
async def test_quality_gate_fails_create_issue_exception_swallowed(make_agent):  # type: ignore[no-untyped-def]
    """If create_issue raises after a failed quality gate, the exception is
    logged and implement_plan returns normally — the plan is still marked
    rejected and the branch cleanup still runs.
    """
    agent = make_agent()

    plan = _make_approved_plan("qg_issue_fail", "Quality gate issue fail")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("qg_issue_fail", "approved")

    # Force the failure to be terminal on the first attempt so the
    # create_issue exception path actually runs.
    agent.config.max_implementation_attempts = 1

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.evolution, "run_quality_gates", return_value=_FAIL_REVIEW), \
         patch.object(agent.evolution, "auto_fix", new_callable=AsyncMock, return_value=True), \
         patch.object(agent.evolution, "cleanup_branch") as mock_cleanup, \
         patch.object(
             agent.evolution, "create_issue",
             side_effect=RuntimeError("GitHub API down"),
         ) as mock_issue:
        mock_q.return_value = MockQueryResult(response="implemented")
        mock_sub.side_effect = _sub_side_effect_factory()

        # Must NOT raise — the except block swallows the error.
        await agent.implement_plan("qg_issue_fail")

    # The failing create_issue attempt was actually made on the terminal
    # failure path (proves the swallow-and-log except block ran).
    mock_issue.assert_called_once()
    # The recovery sequence around the raising call still completed.
    mock_cleanup.assert_called_once_with("qg_issue_fail")
    saved = agent.memory.get_plan("qg_issue_fail")
    assert saved is not None
    assert saved.status == "failed"
    assert saved.last_failure_reason is not None
    assert "Quality gate failed" in saved.last_failure_reason


@pytest.mark.asyncio
async def test_quality_gate_disabled_skips_review(make_agent):  # type: ignore[no-untyped-def]
    """When post_implement_review=False, quality gate is skipped."""
    agent = make_agent()
    agent.config.post_implement_review = False

    plan = _make_approved_plan("qg_off", "Quality gate disabled test")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("qg_off", "approved")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.contributions, "get_current_head", return_value="abc123"), \
         patch.object(agent.evolution, "run_quality_gates") as mock_review:
        mock_q.return_value = MockQueryResult(response="implemented")
        mock_sub.side_effect = _sub_side_effect_factory()

        await agent.implement_plan("qg_off")

    # self_review should NOT have been called during implement_plan
    mock_review.assert_not_called()
    saved = agent.memory.get_plan("qg_off")
    assert saved is not None
    assert saved.status == "implemented"


# ──────────────────────────────────────────────────────────────────────
# check_merged_plans batch-stable snapshot hoist (#1377)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_merged_plans_hoists_snapshot_for_batch(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """A batch of merged plans pays the diagnostic-snapshot cost once.

    Regression for #1377: when multiple PRs land between two
    ``check_merged_plans`` ticks (post-restart catch-up, post-weekend
    approvals), each ``_snapshot_version`` call previously re-ran
    ``build_diagnostic_snapshot`` (a 16 MiB log-tail read + ``usage.jsonl``
    walk) and ``Memory.get_recent_tasks``. Both inputs are batch-stable
    inside one synchronous loop body and are now computed once.
    """
    agent = make_agent()
    # The attend phase reads recent tasks too; keep it out of the count.
    agent.config.conscious_workspace_observe_enabled = False
    _stub_loop_externals(agent)
    # Suppress the iteration-start status snapshot so the only
    # ``build_diagnostic_snapshot`` call we observe is the batch hoist
    # under test. Iteration 1 is the only iteration we run; setting the
    # cursor to 1 makes the status block's gate (``!= iteration``) skip.
    agent._last_status_iteration = 1
    # ``audit_self_model`` (the other in-loop snapshot caller) defaults
    # to ``audit_self_every=10``, so it will not fire on iteration 1.
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]

    plan_a = EvolutionPlan(
        plan_id="ep_a", title="Plan A",
        category="capability", rationale="r", proposed_changes=[],
        files_affected=[], expected_impact="",
        priority="capability", status="implemented",
        created_at="2026-01-01T00:00:00Z",
        pr_url="https://github.com/o/r/pull/1",
    )
    plan_b = EvolutionPlan(
        plan_id="ep_b", title="Plan B",
        category="capability", rationale="r", proposed_changes=[],
        files_affected=[], expected_impact="",
        priority="capability", status="implemented",
        created_at="2026-01-01T00:00:00Z",
        pr_url="https://github.com/o/r/pull/2",
    )
    agent.memory.save_evolution_plan(plan_a)
    agent.memory.save_evolution_plan(plan_b)
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(merged=["ep_a", "ep_b"]),
    )
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha"
    agent.evolution._host.get_pr_check_conclusion.return_value = "success"

    snap = MagicMock()
    snap.sdk_usage = MagicMock(
        total_cost_usd=0.0, total_calls=0,
        cache_hit_rate=0.0, rate_limit_hits=0,
    )
    snap.recent_error_count = 0

    get_recent_tasks_spy = MagicMock(
        wraps=agent.memory.get_recent_tasks,
    )
    agent.memory.get_recent_tasks = get_recent_tasks_spy  # type: ignore[method-assign]

    with patch(
        "maestro.agent.diagnostics.build_diagnostic_snapshot",
        return_value=snap,
    ) as mock_build:
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    # Hoist contract: one snapshot build for the whole batch, not N.
    assert mock_build.call_count == 1
    # And one batch-stable ``get_recent_tasks`` call for the same reason.
    assert get_recent_tasks_spy.call_count == 1
    # Both plans were snapshotted into the version archive.
    versions = agent.memory.get_recent_versions()
    plan_ids = sorted(v.plan_id for v in versions)
    assert plan_ids == ["ep_a", "ep_b"]


@pytest.mark.asyncio
async def test_check_merged_plans_falls_back_to_per_plan_on_hoist_failure(  # type: ignore[no-untyped-def]
    make_agent, caplog,
) -> None:
    """If the batch hoist fails, ``_snapshot_version`` recomputes per plan.

    Best-effort contract: a ``build_diagnostic_snapshot`` failure during
    the pre-loop hoist must log a warning and fall through with
    ``snapshot=None`` / ``recent_tasks=None`` so each ``_snapshot_version``
    invocation re-runs the slow path on its own. The merge-detection
    pipeline must still complete — version records may be lost if the
    inner snapshot also fails, but the loop never crashes.
    """
    agent = make_agent()
    _stub_loop_externals(agent)
    agent._last_status_iteration = 1
    agent._audit_self_model = AsyncMock()  # type: ignore[method-assign]

    plan_a = EvolutionPlan(
        plan_id="ep_a", title="Plan A",
        category="capability", rationale="r", proposed_changes=[],
        files_affected=[], expected_impact="",
        priority="capability", status="implemented",
        created_at="2026-01-01T00:00:00Z",
        pr_url="https://github.com/o/r/pull/1",
    )
    plan_b = EvolutionPlan(
        plan_id="ep_b", title="Plan B",
        category="capability", rationale="r", proposed_changes=[],
        files_affected=[], expected_impact="",
        priority="capability", status="implemented",
        created_at="2026-01-01T00:00:00Z",
        pr_url="https://github.com/o/r/pull/2",
    )
    agent.memory.save_evolution_plan(plan_a)
    agent.memory.save_evolution_plan(plan_b)
    # Two merged plans make the per-plan recompute count unambiguous: a
    # failed hoist plus N per-plan fallbacks is N+1 builds, distinguishable
    # from silently reusing the failed hoist value only when N > 1.
    agent.evolution.check_merged_plans = MagicMock(  # type: ignore[method-assign]
        return_value=MergeCheckResult(merged=["ep_a", "ep_b"]),
    )
    agent.evolution._host = MagicMock()
    agent.evolution._host.get_commit_sha_of_pr.return_value = "sha"
    agent.evolution._host.get_pr_check_conclusion.return_value = "success"

    snap = MagicMock()
    snap.sdk_usage = MagicMock(
        total_cost_usd=0.0, total_calls=0,
        cache_hit_rate=0.0, rate_limit_hits=0,
    )
    snap.recent_error_count = 0

    # Hoist call (call #1) raises; the per-plan fallback inside
    # ``_snapshot_version`` then fires once per merged plan (calls #2, #3).
    mock_build = MagicMock(side_effect=[
        RuntimeError("tail read blew up"),
        snap,
        snap,
    ])

    with patch(
        "maestro.agent.diagnostics.build_diagnostic_snapshot",
        mock_build,
    ), caplog.at_level("WARNING", logger="maestro.agent"):
        await agent.run_loop(
            interval=0, max_iters=1, task_every=0, evolve_every=999,
        )

    assert mock_build.call_count == 3
    assert any(
        "Failed to pre-compute batch snapshot inputs" in r.message
        for r in caplog.records
    )
    versions = agent.memory.get_recent_versions()
    assert len(versions) == 2
    assert {v.plan_id for v in versions} == {"ep_a", "ep_b"}


# ──────────────────────────────────────────────────────────────────────
# Open evolution PR cap (max_open_evolution_prs) tests
# ──────────────────────────────────────────────────────────────────────


def test_open_evolution_pr_count_defaults_to_zero(make_agent):  # type: ignore[no-untyped-def]
    """Fresh agent starts with _open_evolution_pr_count = 0 (gate open)."""
    agent = make_agent()
    assert agent._open_evolution_pr_count == 0


@pytest.mark.asyncio
async def test_implement_proceeds_when_below_cap(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Implementation runs normally when open PR count < cap."""
    agent = make_agent()
    agent.config.max_open_evolution_prs = 5
    agent._open_evolution_pr_count = 3  # below cap

    plan = EvolutionPlan(
        plan_id="cap_ok",
        title="Test plan",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="approved",
        acceptance_criteria=["Coverage stays at 100%"],
    )
    agent.memory.save_evolution_plan(plan)

    with patch.object(agent, "implement_plan", AsyncMock()) as mock_impl:
        await agent._implement_approved_plans()
        mock_impl.assert_awaited_once_with("cap_ok")


@pytest.mark.asyncio
async def test_implement_skipped_when_at_cap(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Implementation skipped when open PR count >= cap."""
    agent = make_agent()
    agent.config.max_open_evolution_prs = 5
    agent._open_evolution_pr_count = 5  # at cap

    plan = EvolutionPlan(
        plan_id="cap_hit",
        title="Test plan",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="approved",
        acceptance_criteria=["Coverage stays at 100%"],
    )
    agent.memory.save_evolution_plan(plan)

    with patch.object(agent, "implement_plan", AsyncMock()) as mock_impl:
        await agent._implement_approved_plans()
        mock_impl.assert_not_awaited()


@pytest.mark.asyncio
async def test_implement_skipped_when_above_cap(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Implementation skipped when open PR count > cap."""
    agent = make_agent()
    agent.config.max_open_evolution_prs = 3
    agent._open_evolution_pr_count = 7  # well above cap

    plan = EvolutionPlan(
        plan_id="cap_above",
        title="Test plan",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="approved",
        acceptance_criteria=["Coverage stays at 100%"],
    )
    agent.memory.save_evolution_plan(plan)

    with patch.object(agent, "implement_plan", AsyncMock()) as mock_impl:
        await agent._implement_approved_plans()
        mock_impl.assert_not_awaited()


@pytest.mark.asyncio
async def test_cap_skip_writes_no_directive(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Cap-skip with an empty directive slot writes no directive (092).

    The legacy "Too many open evolution PRs…" nudge is retired: the
    ``wip_primacy`` gate is the sole finish-this-PR directive author, and
    the integrate-existing-branches intent is carried at task-generation
    time by the promotable-backlog / integration-cadence bridges. The
    cap-skip only declines to mint; it never touches the directive slot.
    """
    agent = make_agent()
    agent.config.max_open_evolution_prs = 2
    agent._open_evolution_pr_count = 3

    plan = EvolutionPlan(
        plan_id="dir_none",
        title="Test plan",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="approved",
        acceptance_criteria=["Coverage stays at 100%"],
    )
    agent.memory.save_evolution_plan(plan)

    # Ensure directive starts empty
    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with patch.object(agent, "implement_plan", AsyncMock()) as mock_impl:
        await agent._implement_approved_plans()
        mock_impl.assert_not_awaited()

    model = agent.consciousness.load_self_model()
    assert model.next_task_directive is None


@pytest.mark.asyncio
async def test_cap_preserves_existing_directive(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Cap-skip leaves a pre-existing directive untouched (092).

    With the cap directive-write retired the method never touches the slot,
    so any directive set by another writer survives a cap-skip verbatim.
    """
    agent = make_agent()
    agent.config.max_open_evolution_prs = 2
    agent._open_evolution_pr_count = 3

    plan = EvolutionPlan(
        plan_id="dir_keep",
        title="Test plan",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="approved",
        acceptance_criteria=["Coverage stays at 100%"],
    )
    agent.memory.save_evolution_plan(plan)

    # Set an existing directive
    model = agent.consciousness.load_self_model()
    model.next_task_directive = "Investigate memory usage"
    agent.consciousness.save_self_model(model)

    with patch.object(agent, "implement_plan", AsyncMock()) as mock_impl:
        await agent._implement_approved_plans()
        mock_impl.assert_not_awaited()

    model = agent.consciousness.load_self_model()
    assert model.next_task_directive == "Investigate memory usage"


@pytest.mark.asyncio
async def test_cap_disabled_when_zero(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Gate disabled when max_open_evolution_prs <= 0."""
    agent = make_agent()
    agent.config.max_open_evolution_prs = 0
    agent._open_evolution_pr_count = 100  # would block if cap were active

    plan = EvolutionPlan(
        plan_id="cap_off",
        title="Test plan",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="approved",
        acceptance_criteria=["Coverage stays at 100%"],
    )
    agent.memory.save_evolution_plan(plan)

    with patch.object(agent, "implement_plan", AsyncMock()) as mock_impl:
        await agent._implement_approved_plans()
        mock_impl.assert_awaited_once_with("cap_off")


@pytest.mark.asyncio
async def test_cap_disabled_when_negative(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Gate disabled when max_open_evolution_prs is negative."""
    agent = make_agent()
    agent.config.max_open_evolution_prs = -1
    agent._open_evolution_pr_count = 50

    plan = EvolutionPlan(
        plan_id="cap_neg",
        title="Test plan",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="approved",
        acceptance_criteria=["Coverage stays at 100%"],
    )
    agent.memory.save_evolution_plan(plan)

    with patch.object(agent, "implement_plan", AsyncMock()) as mock_impl:
        await agent._implement_approved_plans()
        mock_impl.assert_awaited_once_with("cap_neg")


def test_wip_owns_slot_true_only_flag_on_and_tend_owned_pr(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """``_wip_owns_slot`` is True only with the flag on *and* a ``TendOwnedPR``.

    Also asserts the short-circuit: with ``wip_primacy_enabled`` off the
    ``_current_task_focus`` probe is never called (the ``and`` never reaches it).
    """
    from maestro.wip_primacy import Discretionary, TendOwnedPR

    agent = make_agent()

    # Flag off → short-circuits before ``_current_task_focus``.
    agent.config.wip_primacy_enabled = False
    focus = MagicMock(
        return_value=TendOwnedPR(pr_number=7, owned_blocker="CONFLICTING"),
    )
    agent._current_task_focus = focus  # type: ignore[method-assign]
    assert agent._wip_owns_slot() is False
    focus.assert_not_called()

    # Flag on + ``TendOwnedPR`` → owned WIP owns the slot.
    agent.config.wip_primacy_enabled = True
    assert agent._wip_owns_slot() is True
    focus.assert_called_once()

    # Flag on + ``Discretionary`` → no owned WIP, slot is free.
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=Discretionary(),
    )
    assert agent._wip_owns_slot() is False


@pytest.mark.asyncio
async def test_implement_deferred_under_owned_wip(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Flag on + ``TendOwnedPR``: mint nothing and write no directive."""
    from maestro.wip_primacy import TendOwnedPR

    agent = make_agent()
    agent.config.wip_primacy_enabled = True
    agent.config.max_open_evolution_prs = 5
    agent._open_evolution_pr_count = 0  # well below cap — WIP defers anyway
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=TendOwnedPR(pr_number=42, owned_blocker="CONFLICTING"),
    )

    plan = EvolutionPlan(
        plan_id="wip_defer",
        title="Test plan",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="approved",
        acceptance_criteria=["Coverage stays at 100%"],
    )
    agent.memory.save_evolution_plan(plan)

    # Directive must be untouched — the ``wip_primacy`` gate, not this path,
    # authors the finish-this-PR directive.
    model = agent.consciousness.load_self_model()
    model.next_task_directive = None
    agent.consciousness.save_self_model(model)

    with patch.object(agent, "implement_plan", AsyncMock()) as mock_impl:
        await agent._implement_approved_plans()

    mock_impl.assert_not_awaited()
    assert agent.consciousness.load_self_model().next_task_directive is None


@pytest.mark.asyncio
async def test_implement_proceeds_under_discretionary_wip(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Flag on but ``Discretionary`` focus: minting proceeds as usual (parity)."""
    from maestro.wip_primacy import Discretionary

    agent = make_agent()
    agent.config.wip_primacy_enabled = True
    agent.config.max_open_evolution_prs = 5
    agent._open_evolution_pr_count = 0
    agent._current_task_focus = MagicMock(  # type: ignore[method-assign]
        return_value=Discretionary(),
    )

    plan = EvolutionPlan(
        plan_id="wip_discretionary",
        title="Test plan",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="approved",
        acceptance_criteria=["Coverage stays at 100%"],
    )
    agent.memory.save_evolution_plan(plan)

    with patch.object(agent, "implement_plan", AsyncMock()) as mock_impl:
        await agent._implement_approved_plans()

    mock_impl.assert_awaited_once_with("wip_discretionary")


@pytest.mark.asyncio
async def test_implement_proceeds_when_wip_flag_off(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Flag off: minting proceeds and ``_current_task_focus`` is never probed."""
    from maestro.wip_primacy import TendOwnedPR

    agent = make_agent()
    agent.config.wip_primacy_enabled = False
    agent.config.max_open_evolution_prs = 5
    agent._open_evolution_pr_count = 0
    # Even a ``TendOwnedPR`` focus must not defer while the flag is off.
    focus = MagicMock(
        return_value=TendOwnedPR(pr_number=42, owned_blocker="CONFLICTING"),
    )
    agent._current_task_focus = focus  # type: ignore[method-assign]

    plan = EvolutionPlan(
        plan_id="wip_flag_off",
        title="Test plan",
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status="approved",
        acceptance_criteria=["Coverage stays at 100%"],
    )
    agent.memory.save_evolution_plan(plan)

    with patch.object(agent, "implement_plan", AsyncMock()) as mock_impl:
        await agent._implement_approved_plans()

    mock_impl.assert_awaited_once_with("wip_flag_off")
    focus.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Uncalled-symbol quality gate
# ──────────────────────────────────────────────────────────────────────

_GATE_DIFF_UNWIRED = """\
diff --git a/maestro/classify.py b/maestro/classify.py
--- a/maestro/classify.py
+++ b/maestro/classify.py
@@ -0,0 +1,4 @@
+def classify_foo():
+    pass
+class FooClassifier:
+    pass
"""


def _grep_has_match(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    """Simulate a *batched* ``git grep -n`` finding an external call site for
    every symbol in ``_GATE_DIFF_UNWIRED``.

    The gate now issues a single ``git grep`` carrying one ``-e <name>`` per
    symbol and attributes rows back per symbol by the ``--fixed-strings``
    substring test, so the mock must return a call-site row for *each* symbol
    (``classify_foo`` and ``FooClassifier``) — exactly what real ``git grep``
    returns for a genuinely-wired diff.  A single ``classify_foo`` row would
    (correctly) leave ``FooClassifier`` unattributed and flagged uncalled.  Each
    row lives in a different file from the symbol's definition, so it counts as a
    real call site under the definition-line filter.
    """
    return subprocess.CompletedProcess(
        cmd,
        returncode=0,
        stdout=(
            "maestro/loop.py:10:    classify_foo()\n"
            "maestro/loop.py:11:    FooClassifier()\n"
        ),
        stderr="",
    )


def test_uncalled_symbol_gate_blocks_unwired() -> None:
    """Diff with new defs but no call sites → gate returns (False, message)."""

    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    with patch("maestro.agent.subprocess.run", side_effect=_grep_no_match):
        ok, msg = _check_uncalled_symbol_gate(_GATE_DIFF_UNWIRED, cfg)

    assert ok is False
    assert "classify_foo" in msg
    assert "FooClassifier" in msg
    assert "2 new definition" in msg


def test_uncalled_symbol_gate_blocks_duplicate_definitions() -> None:
    """Regression for #3797: same-named declarations in other files are not
    call sites, so the blocking gate must still refuse a diff whose only grep
    rows are ``def`` declarations elsewhere in the tree."""

    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )

    def _grep_duplicate_defs(
        cmd: list[str], **kw: object
    ) -> subprocess.CompletedProcess[str]:
        """Every row is a declaration of the symbol in a *different* file."""
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout=(
                "maestro/dup.py:1:def classify_foo():\n"
                "maestro/dup.py:3:class FooClassifier:\n"
            ),
            stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=_grep_duplicate_defs):
        ok, msg = _check_uncalled_symbol_gate(_GATE_DIFF_UNWIRED, cfg)

    assert ok is False
    assert "classify_foo" in msg
    assert "FooClassifier" in msg


def test_uncalled_symbol_gate_no_wired_constant_collision() -> None:
    """Regression: ensure no second, byte-identical `_GATE_DIFF_WIRED` constant
    exists in this file. The wired-vs-unwired distinction is driven by the
    `subprocess.run` mock, not the diff body, so a separate `_WIRED` constant
    would be misleading dead code."""
    import re
    from pathlib import Path

    src = Path(__file__).read_text()
    assert re.search(r'_GATE_DIFF_UNWIRED\s*=\s*"""', src) is not None
    assert re.search(r'_GATE_DIFF_WIRED\s*=\s*"""', src) is None


def test_uncalled_symbol_gate_passes_wired() -> None:
    """Diff with new defs that have call sites → gate returns (True, '')."""

    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    with patch("maestro.agent.subprocess.run", side_effect=_grep_has_match):
        ok, msg = _check_uncalled_symbol_gate(_GATE_DIFF_UNWIRED, cfg)

    assert ok is True
    assert msg == ""


def test_uncalled_symbol_gate_passes_intra_module_call() -> None:
    """Regression for #2410: a private helper called only within its own
    module must pass the gate. ``git grep -n`` returns the definition line plus
    an intra-module call; only the definition line is discounted."""

    diff = """\
diff --git a/maestro/feature.py b/maestro/feature.py
--- a/maestro/feature.py
+++ b/maestro/feature.py
@@ -0,0 +1,2 @@
+def _gate_helper():
+    return 1
"""
    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    stdout = (
        "maestro/feature.py:1:def _gate_helper():\n"
        "maestro/feature.py:9:    return _gate_helper()\n"
    )

    def grep(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=grep):
        ok, msg = _check_uncalled_symbol_gate(diff, cfg)
    assert ok is True
    assert msg == ""


def test_uncalled_symbol_gate_blocks_definition_line_only() -> None:
    """A symbol whose sole ``git grep -n`` hit is its own definition line (a
    genuinely dead organ) must still block the gate."""

    diff = """\
diff --git a/maestro/dead.py b/maestro/dead.py
--- a/maestro/dead.py
+++ b/maestro/dead.py
@@ -0,0 +1,2 @@
+def gate_dead_organ():
+    pass
"""
    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    stdout = "maestro/dead.py:1:def gate_dead_organ():\n"

    def grep(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=grep):
        ok, msg = _check_uncalled_symbol_gate(diff, cfg)
    assert ok is False
    assert "gate_dead_organ" in msg
    assert "1 new definition" in msg


def test_uncalled_symbol_gate_disabled() -> None:
    """Gate disabled via config → always returns (True, '') even for unwired defs."""

    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=False,
        uncalled_symbol_max_allowed=0,
    )
    # subprocess.run should NOT be called when the gate is disabled,
    # so we don't mock it — an actual call would fail in the test
    # environment (no git repo at /fake/lifecycle).
    ok, msg = _check_uncalled_symbol_gate(_GATE_DIFF_UNWIRED, cfg)
    assert ok is True
    assert msg == ""


def test_uncalled_symbol_gate_threshold() -> None:
    """Threshold config: 1 uncalled allowed → 1 passes, 2 blocks."""

    cfg_threshold_1 = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=1,
    )

    # --- One uncalled def (below threshold) ---
    one_def_diff = """\
diff --git a/maestro/single.py b/maestro/single.py
--- a/maestro/single.py
+++ b/maestro/single.py
@@ -0,0 +1,2 @@
+def single_orphan():
+    pass
"""
    with patch("maestro.agent.subprocess.run", side_effect=_grep_no_match):
        ok, msg = _check_uncalled_symbol_gate(one_def_diff, cfg_threshold_1)
    assert ok is True
    assert msg == ""

    # --- Two uncalled defs (exceeds threshold of 1) ---
    with patch("maestro.agent.subprocess.run", side_effect=_grep_no_match):
        ok, msg = _check_uncalled_symbol_gate(_GATE_DIFF_UNWIRED, cfg_threshold_1)
    assert ok is False
    assert "2 new definition" in msg


def test_uncalled_symbol_gate_skips_dunders_and_test_helpers() -> None:
    """Dunder methods, _test* prefixed names, and test-file defs are skipped."""

    diff = """\
diff --git a/maestro/model.py b/maestro/model.py
--- a/maestro/model.py
+++ b/maestro/model.py
@@ -0,0 +1,6 @@
+    def __init__(self):
+        pass
+    def _test_helper():
+        pass
diff --git a/tests/test_foo.py b/tests/test_foo.py
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -0,0 +1,2 @@
+def test_something():
+    pass
"""
    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    # No subprocess.run should be called — all symbols are filtered.
    with patch("maestro.agent.subprocess.run") as mock_run:
        ok, msg = _check_uncalled_symbol_gate(diff, cfg)
    assert ok is True
    assert msg == ""
    mock_run.assert_not_called()


def test_uncalled_symbol_gate_grep_error_skips() -> None:
    """When git grep raises an exception, the symbol is skipped (not blocked)."""

    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    with patch("maestro.agent.subprocess.run", side_effect=OSError("no git")):
        ok, msg = _check_uncalled_symbol_gate(_GATE_DIFF_UNWIRED, cfg)
    # OSError means grep failed → symbols skipped, not counted as uncalled.
    assert ok is True
    assert msg == ""


# A two-file, two-symbol diff for the batching regression tests: proves the
# gate forks ``git grep`` exactly once for all symbols (#1856), mirroring the
# evolution-side ``_DIFF_MULTI_FILE`` in ``tests/test_uncalled_symbols.py``.
_GATE_DIFF_MULTI = """\
diff --git a/maestro/mod_a.py b/maestro/mod_a.py
--- a/maestro/mod_a.py
+++ b/maestro/mod_a.py
@@ -0,0 +1,2 @@
+def gate_alpha():
+    pass
diff --git a/maestro/mod_b.py b/maestro/mod_b.py
--- a/maestro/mod_b.py
+++ b/maestro/mod_b.py
@@ -0,0 +1,2 @@
+class GateBeta:
+    pass
"""


def test_uncalled_symbol_gate_batches_single_subprocess() -> None:
    """#1856 regression: a multi-symbol diff must fork ``git grep`` exactly once
    (carrying one ``-e <name>`` per symbol), not once per symbol, while the
    verdict still names every uncalled symbol."""

    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    calls: list[list[str]] = []

    def spy(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        # Empty stdout → no call sites for either symbol → both flagged.
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")

    with patch("maestro.agent.subprocess.run", side_effect=spy):
        ok, msg = _check_uncalled_symbol_gate(_GATE_DIFF_MULTI, cfg)

    # (a) exactly one fork for both symbols.
    assert len(calls) == 1
    # (b) that single command carries one ``-e <name>`` per symbol.
    assert calls[0].count("-e") == 2
    assert "gate_alpha" in calls[0]
    assert "GateBeta" in calls[0]
    # (c) the verdict still blocks and names both symbols.
    assert ok is False
    assert "gate_alpha" in msg
    assert "GateBeta" in msg
    assert "2 new definition" in msg


def test_uncalled_symbol_gate_batched_empty_symbols_skips_subprocess() -> None:
    """The empty-symbol guard: a diff whose only added symbols are filtered out
    (all dunders) yields an empty symbol list, so no ``git grep`` fork runs."""

    diff = """\
diff --git a/maestro/model.py b/maestro/model.py
--- a/maestro/model.py
+++ b/maestro/model.py
@@ -0,0 +1,2 @@
+    def __init__(self):
+        pass
"""
    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    with patch("maestro.agent.subprocess.run") as mock_run:
        ok, msg = _check_uncalled_symbol_gate(diff, cfg)
    mock_run.assert_not_called()
    assert ok is True
    assert msg == ""


def test_uncalled_symbol_gate_batched_grep_failure_skips_all_symbols(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A single batched-grep failure skips *all* symbols (the new blast radius):
    exactly one fork is attempted, the gate stays advisory (returns ``True``),
    and the debug log names the symbol count."""

    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    calls: list[list[str]] = []

    def boom(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        raise subprocess.TimeoutExpired(cmd, timeout=30)

    with caplog.at_level("DEBUG", logger="maestro"), \
         patch("maestro.agent.subprocess.run", side_effect=boom):
        ok, msg = _check_uncalled_symbol_gate(_GATE_DIFF_MULTI, cfg)

    # One fork attempted for both symbols; failure never blocks.
    assert len(calls) == 1
    assert ok is True
    assert msg == ""
    assert "batched git grep failed for 2 symbol(s)" in caplog.text


def test_uncalled_symbol_gate_batched_ignores_malformed_rows() -> None:
    """The batched row-split skips a ``git grep`` line that is not
    ``path:lineno:content`` (e.g. a binary-match notice) rather than counting it
    as a call site, so a symbol whose only well-formed row is its own definition
    line is still flagged — mirroring evolution-side
    ``test_definition_line_only_is_flagged``."""

    diff = """\
diff --git a/maestro/dead.py b/maestro/dead.py
--- a/maestro/dead.py
+++ b/maestro/dead.py
@@ -0,0 +1,2 @@
+def gate_dead_organ():
+    pass
"""
    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    stdout = (
        "orphan_row_without_colons\n"  # malformed — skipped, not a call site
        "maestro/dead.py:1:def gate_dead_organ():\n"
    )

    def grep(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=stdout, stderr="",
        )

    with patch("maestro.agent.subprocess.run", side_effect=grep):
        ok, msg = _check_uncalled_symbol_gate(diff, cfg)
    assert ok is False
    assert "gate_dead_organ" in msg
    assert "1 new definition" in msg


@pytest.mark.asyncio
async def test_uncalled_symbol_gate_blocks_in_implement_plan(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When the gate returns (False, msg), implement_plan demotes the plan."""
    agent = make_agent()
    # Enable the gate for this test
    agent.config = Config(
        memory_dir=agent.config.memory_dir,
        workspace=agent.config.workspace,
        self_review_max_rounds=0,
        enable_session_reuse=False,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
        post_implement_review=False,
    )

    plan = _make_approved_plan("gate_block", "Gate block test")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("gate_block", "approved")

    gate_diff = """\
diff --git a/maestro/new.py b/maestro/new.py
+++ b/maestro/new.py
@@ -0,0 +1,2 @@
+def unwired_func():
+    pass
"""

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.evolution, "_vcs") as mock_vcs, \
         patch.object(agent.evolution, "cleanup_branch") as mock_cleanup, \
         patch("maestro.agent.subprocess.run", side_effect=_grep_no_match):
        mock_q.return_value = MockQueryResult(response="implemented")
        mock_sub.side_effect = _sub_side_effect_factory()
        mock_vcs.get_head_hash.return_value = "head123"
        mock_vcs.get_diff.return_value = gate_diff

        await agent.implement_plan("gate_block")

    mock_cleanup.assert_called_once_with("gate_block")
    saved = agent.memory.get_plan("gate_block")
    assert saved is not None
    assert saved.status == "failed"
    assert saved.failure_count == 1
    assert saved.last_failure_reason is not None
    assert "Uncalled-symbol gate" in saved.last_failure_reason


@pytest.mark.asyncio
async def test_uncalled_symbol_gate_requests_uncapped_diff(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Regression for #4217: the gate parses the diff locally, so it must ask
    ``get_diff`` for the complete cumulative diff, not the 50K-capped splice."""
    agent = make_agent()
    agent.config = Config(
        memory_dir=agent.config.memory_dir,
        workspace=agent.config.workspace,
        self_review_max_rounds=0,
        enable_session_reuse=False,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
        post_implement_review=False,
    )

    plan = _make_approved_plan("gate_uncapped", "Gate uncapped test")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("gate_uncapped", "approved")

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.evolution, "_vcs") as mock_vcs, \
         patch.object(agent.evolution, "cleanup_branch"), \
         patch("maestro.agent.subprocess.run", side_effect=_grep_no_match):
        mock_q.return_value = MockQueryResult(response="implemented")
        mock_sub.side_effect = _sub_side_effect_factory()
        mock_vcs.get_head_hash.return_value = "head123"
        mock_vcs.get_diff.return_value = _GATE_DIFF_UNWIRED
        mock_vcs.merge_base.return_value = None

        await agent.implement_plan("gate_uncapped")

    mock_vcs.get_diff.assert_called_once()
    assert mock_vcs.get_diff.call_args.kwargs["max_chars"] == 0


def _large_gate_diff() -> str:
    """A >50K diff: ~30K of def-free production additions, then a test file
    whose many ``def test_case_N`` lines land in the kept tail of a splice."""
    prod = "".join(f"+x_{i} = {i}\n" for i in range(3000))
    tests = "".join(
        f"+def test_case_{i}():\n+    assert True\n" for i in range(1500)
    )
    return (
        "diff --git a/maestro/foo.py b/maestro/foo.py\n"
        "+++ b/maestro/foo.py\n"
        "@@ -0,0 +1,3000 @@\n"
        f"{prod}"
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "+++ b/tests/test_x.py\n"
        "@@ -0,0 +1,3000 @@\n"
        f"{tests}"
    )


def test_uncalled_symbol_gate_truncated_diff_misattributes_tests() -> None:
    """Regression for #4217: a head+tail spliced diff credits tail test defs
    to the head's production file and blocks; the full diff passes."""

    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    from maestro.vcs import SubprocessVCS

    full = _large_gate_diff()
    assert len(full) > 50_000
    # Drive the real ``get_diff`` splice so the test tracks its shape.
    vcs = SubprocessVCS(FAKE_DIR)
    with patch.object(
        vcs, "_run",
        return_value=subprocess.CompletedProcess([], 0, stdout=full, stderr=""),
    ):
        spliced = vcs.get_diff("base", "head")
        uncapped = vcs.get_diff("base", "head", max_chars=0)
    assert "truncated by get_diff cap" in spliced
    assert "+++ b/tests/test_x.py" not in spliced
    assert uncapped == full.strip()

    with patch("maestro.agent.subprocess.run", side_effect=_grep_no_match):
        bad_ok, bad_msg = _check_uncalled_symbol_gate(spliced, cfg)
        ok, msg = _check_uncalled_symbol_gate(uncapped, cfg)

    assert bad_ok is False
    assert "test_case_" in bad_msg
    assert ok is True
    assert msg == ""


def test_uncalled_symbol_gate_message_caps_symbol_list() -> None:
    """The gate message is persisted as ``last_failure_reason``; with the diff
    uncapped (#4217) it must bound the symbols it names."""

    cfg = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
    )
    diff = "+++ b/maestro/foo.py\n" + "".join(
        f"+def sym_{i}():\n" for i in range(15)
    )
    with patch("maestro.agent.subprocess.run", side_effect=_grep_no_match):
        ok, msg = _check_uncalled_symbol_gate(diff, cfg)

    assert ok is False
    assert "15 new definition(s)" in msg
    assert "'sym_9'" in msg
    assert "'sym_10'" not in msg
    assert msg.endswith("; ... and 5 more")
