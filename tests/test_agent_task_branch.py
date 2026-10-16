"""Task branch isolation, artifacts dirs and the full-``run`` summary/record
harness; split out of ``tests/test_agent.py``.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.sdk import ErrorCategory, SDKError
from tests.conftest import MockQueryResult

# --- Task branch isolation tests ---


@pytest.mark.asyncio
async def test_run_creates_task_branch(make_agent):  # type: ignore[no-untyped-def]
    """run() creates a task branch before executing steps."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        await agent.run("goal")

    agent._vcs.checkout_new_branch.assert_called_once()
    branch_arg = agent._vcs.checkout_new_branch.call_args[0][0]
    assert branch_arg.startswith("maestro/task/")


@pytest.mark.asyncio
async def test_run_with_pr_branch_skips_task_branch_creation(make_agent):  # type: ignore[no-untyped-def]
    """When ``pr_branch`` is supplied, run() checks out that branch instead
    of creating a fresh ``maestro/task/{id}`` branch.

    Why: PR-remediation tasks must commit on the PR's head ref so
    run() can deliver them by pushing that branch directly.
    """
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        await agent.run("goal", pr_branch="maestro/evolve/p99")

    agent._vcs.checkout_new_branch.assert_not_called()
    # ``checkout`` was called with main (entry-time reset) and the PR branch.
    checkout_args = [c.args for c in agent._vcs.checkout.call_args_list]
    assert ("maestro/evolve/p99",) in checkout_args


@pytest.mark.asyncio
async def test_run_with_pr_branch_falls_back_to_origin_on_local_miss(make_agent):  # type: ignore[no-untyped-def]
    """When the PR branch is not present locally, run() falls back to
    ``checkout("origin/<branch>")`` then ``checkout_new_branch`` —
    matching the same pattern used by ``_tend_single_pr``.
    """
    from maestro.vcs import VCSError

    agent = make_agent()
    # First checkout call ("main", entry-time) succeeds; second ("p99")
    # raises so we fall into the fallback path.
    pr_branch = "maestro/evolve/p99"

    def _checkout(name: str) -> None:
        if name == pr_branch:
            raise VCSError("not local")
        return None

    agent._vcs.checkout.side_effect = _checkout
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        await agent.run("goal", pr_branch=pr_branch)

    # Fallback path was taken: checkout("origin/<branch>") then
    # checkout_new_branch(<branch>).
    checkout_args = [c.args for c in agent._vcs.checkout.call_args_list]
    assert (f"origin/{pr_branch}",) in checkout_args
    agent._vcs.checkout_new_branch.assert_called_with(pr_branch)


@pytest.mark.asyncio
async def test_run_with_pr_branch_origin_fallback_failure_clears_branch_name(make_agent):  # type: ignore[no-untyped-def]
    """When both the local checkout and the origin fallback fail, the run
    proceeds on the current branch (``branch_name=None``) and teardown is
    a no-op for branch deletion. Mirrors the existing task-branch fallback
    behavior — failing branch creation does not abort the task.
    """
    from maestro.vcs import VCSError

    agent = make_agent()
    pr_branch = "maestro/evolve/p99"
    # Both the local checkout and the origin checkout raise; the
    # checkout_new_branch fallback also raises.
    agent._vcs.checkout.side_effect = VCSError("not local")
    agent._vcs.checkout_new_branch.side_effect = VCSError("no git")
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal", pr_branch=pr_branch)

    # branch_name was cleared after the fallback failed, so the record
    # carries no branch and no delete_branch occurred (the run still
    # completed the task body on the current branch).
    assert record.branch is None
    agent._vcs.delete_branch.assert_not_called()


@pytest.mark.asyncio
async def test_run_with_pr_branch_preserves_branch_on_teardown(make_agent):  # type: ignore[no-untyped-def]
    """Teardown must NOT delete the PR branch, and run() delivers the
    resolution by pushing it directly even when no commit was made."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = False
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        await agent.run("goal", pr_branch="maestro/evolve/p99")

    agent._vcs.delete_branch.assert_not_called()
    # run() delivers the resolution to the published PR branch directly; the
    # push is unconditional best-effort, so it fires even though has_changes
    # was False and no commit landed this lap.
    agent._vcs.push.assert_called_once_with("maestro/evolve/p99")


@pytest.mark.asyncio
async def test_run_pushes_pr_branch_on_successful_remediation(make_agent):  # type: ignore[no-untyped-def]
    """A successful remediation on the PR branch is delivered by run()'s
    direct push to ``origin/<pr_branch>`` — not deferred to Tend PR."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Done.")
        record = await agent.run("goal", pr_branch="maestro/evolve/p99")

    assert record.outcome == "success"
    agent._vcs.push.assert_called_once_with("maestro/evolve/p99")


@pytest.mark.asyncio
async def test_run_pushes_pr_branch_on_partial_remediation(make_agent):  # type: ignore[no-untyped-def]
    """A partial remediation still delivers its committed work — the §5-push
    gate mirrors the §5 commit gate (success OR partial)."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    agent.config.loop_max_replans = 0
    plan_response = json.dumps(
        [{"description": "Step OK"}, {"description": "Step fail"}],
    )

    call_count = 0

    async def alternating_exec(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return MockQueryResult(response="Done")
        raise RuntimeError("Step 2 failed")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=alternating_exec), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        record = await agent.run("goal", pr_branch="maestro/evolve/p99")

    assert record.outcome == "partial"
    agent._vcs.push.assert_called_once_with("maestro/evolve/p99")


@pytest.mark.asyncio
async def test_run_push_is_idempotent_backstop_when_branch_up_to_date(make_agent):  # type: ignore[no-untyped-def]
    """The loop's §5-push coexists with the agent's own push — no double-delivery.

    Owned-PR self-delivery is unconditional (proposal 093): the agent pushes its
    resolution to the PR branch itself, so by §5-push time ``origin/<pr_branch>``
    is already up to date. A real ``git push`` of an up-to-date branch exits 0
    (a no-op); the mocked VCS models that as a single successful push. The loop
    must still record ``push_delivered=True`` cleanly — exactly one push, no
    fetch-and-retry, no error — proving the agent's push and the loop's push
    cannot double-deliver (proposal 093 step 5).
    """
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    # An up-to-date branch push is a git no-op (exit 0) — a successful mock call.
    agent._vcs.push.return_value = None
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Done.")
        record = await agent.run("goal", pr_branch="maestro/evolve/p99")

    assert record.outcome == "success"
    # Exactly one push, no fetch-driven retry, delivery recorded cleanly.
    agent._vcs.push.assert_called_once_with("maestro/evolve/p99")
    agent._vcs.fetch.assert_not_called()
    assert record.push_delivered is True
    assert record.push_error is None


@pytest.mark.asyncio
async def test_run_does_not_push_for_task_branch(make_agent):  # type: ignore[no-untyped-def]
    """A non-PR task (``pr_branch is None``) is never pushed — task branches
    stay local for Tend/promotion, never delivered to a remote."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Done.")
        record = await agent.run("goal")

    assert record.outcome == "success"
    agent._vcs.push.assert_not_called()


@pytest.mark.asyncio
async def test_run_does_not_push_on_failed_remediation(make_agent):  # type: ignore[no-untyped-def]
    """A failed remediation delivers nothing — the §5-push gate excludes the
    ``failure`` outcome (only success/partial deliver)."""
    agent = make_agent()
    agent.config.loop_max_replans = 0
    plan_response = json.dumps([{"description": "Will fail"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.side_effect = RuntimeError("SDK error")
        record = await agent.run("goal", pr_branch="maestro/evolve/p99")

    assert record.outcome == "failure"
    agent._vcs.push.assert_not_called()


@pytest.mark.asyncio
async def test_run_does_not_push_when_pr_checkout_fell_back(make_agent):  # type: ignore[no-untyped-def]
    """When the PR-branch checkout falls back (``branch_name = None``) the run
    is NOT on the PR branch, so it must not push it (guard is
    ``branch_name == pr_branch``, not bare ``pr_branch is not None``)."""
    from maestro.vcs import VCSError

    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    pr_branch = "maestro/evolve/p99"
    agent._vcs.checkout.side_effect = VCSError("not local")
    agent._vcs.checkout_new_branch.side_effect = VCSError("no git")
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Done.")
        record = await agent.run("goal", pr_branch=pr_branch)

    assert record.branch is None
    agent._vcs.push.assert_not_called()


@pytest.mark.asyncio
async def test_run_logs_and_continues_on_push_failure(make_agent, caplog):  # type: ignore[no-untyped-def]
    """A rejected push is retried once after a best-effort fetch (proposal
    091 step 3). When the retry also fails, run() logs a warning, records
    push_delivered=False with the error, and returns its record unchanged,
    leaving delivery to Tend PR."""
    from maestro.vcs import VCSError

    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    agent._vcs.push.side_effect = VCSError("remote rejected")
    plan_response = json.dumps([{"description": "Step 1"}])

    with caplog.at_level(logging.WARNING, logger="maestro"), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Done.")
        record = await agent.run("goal", pr_branch="maestro/evolve/p99")

    # The push was attempted, a best-effort fetch ran, and the push was retried
    # once — both attempts raised, but the run completed normally.
    assert record.outcome == "success"
    assert agent._vcs.push.call_count == 2
    agent._vcs.fetch.assert_called_once_with("origin")
    assert record.push_delivered is False
    assert record.push_error is not None
    assert "remote rejected" in record.push_error
    assert any(
        "Failed to push remediation to PR branch" in r.message
        and "after retry" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_run_push_retry_succeeds_after_fetch(make_agent):  # type: ignore[no-untyped-def]
    """The first push fails but the retry (after a best-effort fetch) succeeds:
    delivery truth is recorded as push_delivered=True (proposal 091 step 3)."""
    from maestro.vcs import VCSError

    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    # First push raises (origin advanced); the retry succeeds.
    agent._vcs.push.side_effect = [VCSError("non-fast-forward"), None]
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Done.")
        record = await agent.run("goal", pr_branch="maestro/evolve/p99")

    assert record.outcome == "success"
    assert agent._vcs.push.call_count == 2
    agent._vcs.fetch.assert_called_once_with("origin")
    assert record.push_delivered is True
    assert record.push_error is None


@pytest.mark.asyncio
async def test_run_push_retry_tolerates_fetch_failure(make_agent, caplog):  # type: ignore[no-untyped-def]
    """A failing best-effort fetch before the retry is logged and the retry
    still runs — the fetch is not allowed to abort delivery (proposal 091
    step 3)."""
    from maestro.vcs import VCSError

    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    agent._vcs.push.side_effect = VCSError("remote rejected")
    agent._vcs.fetch.side_effect = VCSError("network down")
    plan_response = json.dumps([{"description": "Step 1"}])

    with caplog.at_level(logging.WARNING, logger="maestro"), \
         patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Done.")
        record = await agent.run("goal", pr_branch="maestro/evolve/p99")

    # Fetch raised (logged) but the retry push still ran; both pushes failed.
    assert record.outcome == "success"
    assert agent._vcs.push.call_count == 2
    assert record.push_delivered is False
    assert any(
        "Fetch before push retry failed" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_run_commits_on_success_with_changes(make_agent):  # type: ignore[no-untyped-def]
    """run() commits artifacts and keeps the branch on success when changes exist."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.task_delivery.check_merge_clean", return_value=None), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal")

    agent._vcs.stage_all.assert_called_once()
    agent._vcs.commit.assert_called_once()
    assert record.branch is not None
    assert record.branch.startswith("maestro/task/")
    # Branch should NOT be deleted when kept
    agent._vcs.delete_branch.assert_not_called()


@pytest.mark.asyncio
async def test_run_stores_artifacts_dir_in_record(make_agent):  # type: ignore[no-untyped-def]
    """run() stores artifacts_dir path in the TaskRecord."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="output")
        record = await agent.run("Test artifacts")

    assert record.artifacts_dir is not None
    assert "artifacts" in record.artifacts_dir
    assert record.task_id in record.artifacts_dir


@pytest.mark.asyncio
async def test_run_passes_artifacts_dir_to_executor(make_agent):  # type: ignore[no-untyped-def]
    """run() passes the artifacts_dir to executor.execute()."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch.object(agent.executor, "execute", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        from maestro.executor import StepResult
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = StepResult(success=True, output="done")
        record = await agent.run("Test artifacts executor")

    mock_exec.assert_called_once()
    call_kwargs = mock_exec.call_args
    assert call_kwargs.kwargs.get("artifacts_dir") is not None
    assert record.task_id in call_kwargs.kwargs["artifacts_dir"]


@pytest.mark.asyncio
async def test_run_passes_prior_steps_to_executor(make_agent):  # type: ignore[no-untyped-def]
    """run() passes completed prior steps: step 1 gets None, step 2 gets [step1]."""
    agent = make_agent()
    plan_response = json.dumps([
        {"description": "Step 1"},
        {"description": "Step 2"},
        {"description": "Step 3"},
    ])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch.object(agent.executor, "execute", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        from maestro.executor import StepResult
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = StepResult(success=True, output="done")
        await agent.run("Multi-step goal")

    assert mock_exec.call_count == 3
    calls = mock_exec.call_args_list

    # Step 1: no prior steps
    assert calls[0].kwargs.get("prior_steps") is None

    # Step 2: step 1 completed
    prior_2 = calls[1].kwargs.get("prior_steps")
    assert prior_2 is not None
    assert len(prior_2) == 1
    assert prior_2[0].description == "Step 1"

    # Step 3: steps 1 and 2 completed
    prior_3 = calls[2].kwargs.get("prior_steps")
    assert prior_3 is not None
    assert len(prior_3) == 2


@pytest.mark.asyncio
async def test_run_excludes_failed_steps_from_prior_steps(make_agent):  # type: ignore[no-untyped-def]
    """run() excludes failed steps from prior_steps passed to executor."""
    agent = make_agent()
    agent.config = agent.config.__class__(
        **{**agent.config.__dict__, "loop_max_replans": 0},
    )
    plan_response = json.dumps([
        {"description": "Step 1"},
        {"description": "Step 2"},
    ])

    call_count = 0

    from maestro.executor import StepResult

    async def execute_side_effect(*args: object, **kwargs: object) -> StepResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return StepResult(success=False, output="", error="boom")
        return StepResult(success=True, output="ok")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch.object(agent.executor, "execute", new_callable=AsyncMock,
                      side_effect=execute_side_effect) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        await agent.run("Partial failure goal")

    # Step 2 runs even though step 1 failed; both executor calls must fire.
    assert mock_exec.call_count == 2
    # Step 2 gets prior_steps=None because step 1 failed (not "done").
    prior_2 = mock_exec.call_args_list[1].kwargs.get("prior_steps")
    assert prior_2 is None


@pytest.mark.asyncio
async def test_run_summary_includes_branch_when_kept(make_agent):  # type: ignore[no-untyped-def]
    """result_summary includes branch reference when branch is kept."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.task_delivery.check_merge_clean", return_value=None), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step done")
        record = await agent.run("goal with branch")

    assert record.branch is not None
    assert f"Branch: {record.branch}" in record.result_summary


@pytest.mark.asyncio
async def test_run_summary_excludes_branch_when_not_kept(make_agent):  # type: ignore[no-untyped-def]
    """result_summary does not include branch reference when branch is deleted."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = False
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="Step done")
        record = await agent.run("goal without branch")

    assert record.branch is None
    assert "Branch:" not in record.result_summary


@pytest.mark.asyncio
async def test_run_summary_branch_always_present_when_output_very_long(make_agent):  # type: ignore[no-untyped-def]
    """Branch suffix is always present in result_summary even when raw output is very long."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    plan_response = json.dumps([{"description": "Step 1"}])
    # Produce output longer than 500 chars to trigger truncation
    long_output = "x" * 600

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.task_delivery.check_merge_clean", return_value=None), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=long_output)
        record = await agent.run("goal with long output")

    assert record.branch is not None
    branch_suffix = f"\n\nBranch: {record.branch}"
    assert record.result_summary.endswith(branch_suffix)
    assert len(record.result_summary) <= 500


@pytest.mark.asyncio
async def test_run_summary_total_length_at_most_500(make_agent):  # type: ignore[no-untyped-def]
    """result_summary total length never exceeds 500 chars, including branch suffix."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    plan_response = json.dumps([{"description": "Step 1"}])
    # Use output of exactly 500 chars — without the fix this would overflow when branch is appended
    exact_output = "y" * 500

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.task_delivery.check_merge_clean", return_value=None), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=exact_output)
        record = await agent.run("goal with exact output")

    assert len(record.result_summary) <= 500
    assert f"Branch: {record.branch}" in record.result_summary


@pytest.mark.asyncio
async def test_run_summary_no_output_with_branch(make_agent):  # type: ignore[no-untyped-def]
    """All-whitespace output + a kept branch yields ``"No output"`` plus the branch suffix.

    Pins the one summary-assembly branch that ignores the ``available`` budget:
    when ``joined`` collapses to empty the body is the literal ``"No output"``
    sentinel and the ``Branch: <name>`` suffix is still appended, within the
    500-char cap. This is a reachable state — a step can mutate the working
    tree without emitting meaningful stdout, so ``all_output`` strips to empty
    while ``has_changes()`` is true and the branch is kept.

    The executor response must be *whitespace* rather than empty: an empty
    response trips the executor's empty-response guard (``_is_productive``),
    failing the step and discarding the branch. A whitespace-only response is
    productive, so the step succeeds and the branch is kept, yet
    ``"\\n".join(all_output).strip()`` is still empty — the exact state that
    reaches the ``else "No output"`` fallback with a kept branch.
    """
    agent = make_agent()
    agent._vcs.has_changes.return_value = True
    plan_response = json.dumps([{"description": "Step 1"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.task_delivery.check_merge_clean", return_value=None), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response="   ")
        record = await agent.run("goal with whitespace-only output")

    assert record.branch is not None
    assert record.result_summary.startswith("No output")
    assert record.result_summary.endswith(f"\n\nBranch: {record.branch}")
    assert len(record.result_summary) <= 500


@pytest.mark.asyncio
async def test_run_discards_and_deletes_branch_on_failure(make_agent):  # type: ignore[no-untyped-def]
    """run() discards changes and deletes the branch on failure."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Will fail"}])

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.side_effect = RuntimeError("boom")
        record = await agent.run("goal")

    assert record.outcome == "failure"
    assert record.branch is None
    agent._vcs.discard_changes.assert_called()
    agent._vcs.delete_branch.assert_called_once()


@pytest.mark.asyncio
async def test_run_returns_to_main_after_task(make_agent):  # type: ignore[no-untyped-def]
    """run() checks out main after task execution."""
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        await agent.run("goal")

    agent._vcs.checkout.assert_called_with("main")


@pytest.mark.asyncio
async def test_run_tolerates_vcs_branch_failure(make_agent):  # type: ignore[no-untyped-def]
    """run() still completes if task branch creation fails.

    The entry-time defense calls ``checkout('main')`` before
    ``checkout_new_branch``, so ``checkout`` will have been invoked with
    ``"main"``. With branch_name never assigned, the teardown closure
    performs only discard_changes (no checkout, no delete_branch).
    """
    from maestro.vcs import VCSError

    agent = make_agent()
    agent._vcs.checkout_new_branch.side_effect = VCSError("no git")
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal")

    assert record.outcome == "success"
    assert record.branch is None
    # No commit attempted (no successful branch to commit to)
    agent._vcs.stage_all.assert_not_called()
    # Entry-time defense calls checkout('main') before checkout_new_branch
    agent._vcs.checkout.assert_called_with("main")
    # Branch creation failed, so deletion must not have been attempted
    agent._vcs.delete_branch.assert_not_called()


@pytest.mark.asyncio
async def test_run_no_commit_when_no_changes(make_agent):  # type: ignore[no-untyped-def]
    """run() deletes the branch without committing when there are no changes."""
    agent = make_agent()
    agent._vcs.has_changes.return_value = False
    # Pin the no-work commit count rather than leaning on the make_agent
    # default: this test is the cover for the "measured zero" arm of the #2938
    # keep predicate, so a conftest change must fail here loudly instead of
    # silently flipping the delete assertion below.
    agent._vcs.non_merge_commits_ahead.return_value = 0
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal")

    assert record.outcome == "success"
    agent._vcs.commit.assert_not_called()
    assert record.branch is None
    agent._vcs.delete_branch.assert_called_once()


@pytest.mark.asyncio
async def test_run_keeps_branch_when_steps_self_committed(make_agent):  # type: ignore[no-untyped-def]
    """A clean tree whose owned branch holds its own commits is kept (#2938).

    Task steps may commit their work mid-run (the Bash action-gate permits
    ``git commit``); by step 5 the tree is clean, so the keep predicate must
    read the branch's own commit count instead of concluding "no work" and
    letting teardown force-delete the committed branch. Keeping it also
    re-arms the ``merge_clean`` stamp, which is gated on ``kept_branch``.
    """
    agent = make_agent()
    agent._vcs.has_changes.return_value = False
    agent._vcs.non_merge_commits_ahead.return_value = 1
    # The steps commit mid-run, so the branch head read at step 5 differs
    # from the watermark stamped right after checkout_new_branch.
    head_reads: list[str] = []

    def _moving_head(ref: str) -> str:
        head_reads.append(ref)
        return "b" * 40 if len(head_reads) > 1 else "a" * 40

    agent._vcs.rev_parse.side_effect = _moving_head
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.task_delivery.check_merge_clean", return_value=True) as mock_mc, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal")

    assert record.outcome == "success"
    assert record.branch is not None
    assert record.branch.startswith("maestro/task/")
    # The steps committed; step 5 has nothing left to commit itself.
    agent._vcs.commit.assert_not_called()
    agent._vcs.delete_branch.assert_not_called()
    # Pin each probe's args and ref order: swapping to ("main", branch) would
    # silently invert the range and still satisfy a bare return_value. Both
    # bases are measured — ``main`` for "did my steps commit?", then
    # ``origin/main`` to veto commits merely fast-forwarded in from upstream.
    probe_args = [c.args for c in agent._vcs.non_merge_commits_ahead.call_args_list]
    assert probe_args == [
        (record.branch, "main"),
        (record.branch, "origin/main"),
    ]
    # The raw ahead count is the *fallback* only — a measured count must not
    # consult it, else the merge-commit filter is bypassed.
    agent._vcs.commits_ahead_behind.assert_not_called()
    # Watermark stamped at creation, re-read at step 5 — both on the branch.
    assert head_reads == [record.branch, record.branch]
    # The restored consumer: merge_clean is stamped only for a kept branch.
    assert record.merge_clean is True
    mock_mc.assert_called_once_with(
        record.branch, cwd=str(agent.config.workspace),
    )


@pytest.mark.asyncio
async def test_run_deletes_branch_when_only_a_merge_moved_head(make_agent):  # type: ignore[no-untyped-def]
    """A branch moved only by merging ``main`` carries no work of its own.

    A step is free to run ``git merge origin/main`` on the task branch: that
    advances the head past the creation watermark and leaves the branch ahead
    of ``main``, so both the raw ahead count and the watermark veto would keep
    it — and then stamp ``merge_clean`` and mint a promotion directive for a
    branch holding nothing. The ``--no-merges`` count reads the merge commit
    as zero work, so teardown deletes the branch.
    """
    agent = make_agent()
    agent._vcs.has_changes.return_value = False
    agent._vcs.non_merge_commits_ahead.return_value = 0
    # What the old proxy would have seen: ahead of main, head moved.
    agent._vcs.commits_ahead_behind.return_value = (4, 0)
    agent._vcs.rev_parse.side_effect = ["a" * 40, "b" * 40]
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.task_delivery.check_merge_clean", return_value=True) as mock_mc, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal")

    assert record.outcome == "success"
    assert record.branch is None
    assert record.merge_clean is None
    mock_mc.assert_not_called()
    agent._vcs.commit.assert_not_called()
    agent._vcs.delete_branch.assert_called_once()
    # A measured zero short-circuits: neither the fallback nor the watermark
    # veto runs, so rev_parse is the creation stamp alone.
    agent._vcs.commits_ahead_behind.assert_not_called()
    assert agent._vcs.rev_parse.call_count == 1


@pytest.mark.asyncio
async def test_run_deletes_branch_when_only_a_fast_forward_moved_head(make_agent):  # type: ignore[no-untyped-def]
    """A branch moved only by fast-forwarding ``origin/main`` holds no work.

    When local ``main`` lags the remote (routine between tend passes), a
    step's ``git merge origin/main`` *fast-forwards* the task branch instead
    of creating a merge commit. The upstream commits it lands are real,
    non-merge, and absent from local ``main``, so the ``--no-merges`` count
    against ``main`` reads them as work and the head moved past the watermark
    — the branch would be kept, ``merge_clean`` stamped, and a promotion
    directive minted for a branch carrying nothing of its own. Re-measuring
    the count against ``origin/main`` vetoes it.
    """
    agent = make_agent()
    agent._vcs.has_changes.return_value = False

    def _counts(ref: str, base: str) -> int:
        # Ahead of the lagging local main by the fast-forwarded upstream
        # commits, but holding nothing origin/main does not already have.
        return 0 if base == "origin/main" else 3

    agent._vcs.non_merge_commits_ahead.side_effect = _counts
    # What the head-moved watermark veto would have seen: a moved head.
    agent._vcs.rev_parse.side_effect = ["a" * 40, "b" * 40]
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.task_delivery.check_merge_clean", return_value=True) as mock_mc, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal")

    assert record.outcome == "success"
    assert record.branch is None
    assert record.merge_clean is None
    mock_mc.assert_not_called()
    agent._vcs.commit.assert_not_called()
    agent._vcs.delete_branch.assert_called_once()
    branch_arg = agent._vcs.checkout_new_branch.call_args[0][0]
    probe_args = [c.args for c in agent._vcs.non_merge_commits_ahead.call_args_list]
    assert probe_args == [
        (branch_arg, "main"),
        (branch_arg, "origin/main"),
    ]
    # The upstream veto short-circuits ahead of the watermark veto, so
    # rev_parse is the creation stamp alone.
    assert agent._vcs.rev_parse.call_count == 1


@pytest.mark.asyncio
async def test_run_keeps_branch_when_upstream_probe_fails(make_agent):  # type: ignore[no-untyped-def]
    """An unknown upstream count skips its veto rather than discarding work.

    ``non_merge_commits_ahead`` returns ``None`` when the rev-list probe
    fails — including the ordinary case of no ``origin/main`` ref to measure
    against. Like the other two filters, this one degrades toward *keeping*:
    a failed probe must never be the thing that force-deletes committed work.
    """
    agent = make_agent()
    agent._vcs.has_changes.return_value = False

    def _counts(ref: str, base: str) -> int | None:
        return None if base == "origin/main" else 2

    agent._vcs.non_merge_commits_ahead.side_effect = _counts
    agent._vcs.rev_parse.side_effect = ["a" * 40, "b" * 40]
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.task_delivery.check_merge_clean", return_value=True), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal")

    assert record.branch is not None
    agent._vcs.delete_branch.assert_not_called()
    # The unknown upstream count falls through to the watermark veto, which
    # reads the moved head and keeps the branch.
    assert agent._vcs.rev_parse.call_count == 2


@pytest.mark.asyncio
async def test_run_keeps_branch_when_commit_count_probe_fails(make_agent):  # type: ignore[no-untyped-def]
    """An unknown count degrades to the ahead count, not to "no work".

    ``non_merge_commits_ahead`` returns ``None`` when the rev-list probe
    fails, which must never be read as a measured zero: force-deleting
    committed work is the failure #2938 reports, so the predicate falls back
    to the ahead count and keeps the branch.
    """
    agent = make_agent()
    agent._vcs.has_changes.return_value = False
    agent._vcs.non_merge_commits_ahead.return_value = None
    agent._vcs.commits_ahead_behind.return_value = (1, 0)
    agent._vcs.rev_parse.side_effect = ["a" * 40, "b" * 40]
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.loop.task_delivery.check_merge_clean", return_value=True), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal")

    assert record.branch is not None
    agent._vcs.delete_branch.assert_not_called()
    agent._vcs.commits_ahead_behind.assert_called_once_with(
        record.branch, "main",
    )


@pytest.mark.asyncio
async def test_run_pr_branch_clean_tree_does_not_widen_keep(make_agent):  # type: ignore[no-untyped-def]
    """The clean-tree keep arm must NOT fire on a remediation lap.

    A remediation PR branch is nearly always ahead of ``main``; letting it
    set ``kept_branch`` on a clean tree would degrade ``addressed_red`` to
    ``push_delivered is True`` and silently disarm the CI-red downgrade, so
    the widened arm is scoped to ``pr_branch is None``.
    """
    agent = make_agent()
    agent._vcs.has_changes.return_value = False
    agent._vcs.non_merge_commits_ahead.return_value = 5
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal", pr_branch="maestro/evolve/p99")

    assert record.branch is None
    agent._vcs.commit.assert_not_called()
    # The ``pr_branch is None`` scoping short-circuits before the probe runs.
    agent._vcs.non_merge_commits_ahead.assert_not_called()


def test_branch_carries_own_commits_without_watermark_skips_veto(make_agent):  # type: ignore[no-untyped-def]
    """An unknown creation watermark keeps a branch on a positive count.

    A resumed task checks its checkpoint's branch back out instead of minting
    one, so no watermark is stamped. The veto is skipped rather than treated
    as "head never moved" — otherwise a resume would force-delete the commits
    its earlier steps made.
    """
    agent = make_agent()
    agent._vcs.non_merge_commits_ahead.return_value = 2

    assert agent._branch_carries_own_commits("maestro/task/abc", None) is True
    # With nothing to compare against, no head read is paid for.
    agent._vcs.rev_parse.assert_not_called()


@pytest.mark.asyncio
async def test_run_cleans_up_working_tree_when_branch_creation_fails(make_agent):  # type: ignore[no-untyped-def]
    """run() cleans up the working tree even when branch creation fails.

    _teardown_task_branch() calls discard_changes() unconditionally,
    before its `if branch_name is None: return` early-exit, so a failed
    checkout_new_branch (branch_name still None) cannot skip cleanup.
    """
    from maestro.vcs import VCSError

    agent = make_agent()
    agent._vcs.checkout_new_branch.side_effect = VCSError("no git")
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal")

    assert record.outcome == "success"
    # The working tree must be cleaned up even when branch creation failed.
    agent._vcs.discard_changes.assert_called()


@pytest.mark.asyncio
async def test_delete_branch_not_called_when_checkout_main_fails(
    make_agent,
) -> None:
    """delete_branch() must NOT be called when checkout('main') raises VCSError.

    When checkout('main') fails the agent is still on the task branch.
    Git refuses to delete the currently checked-out branch, so the call
    would silently fail and leave an orphan branch.  The fix gates deletion
    on a successful return to main via the ``returned_to_main`` flag.
    """
    from maestro.vcs import VCSError

    agent = make_agent()
    # checkout_new_branch succeeds (branch_name is set), checkout('main') fails
    agent._vcs.checkout_new_branch.return_value = None
    agent._vcs.checkout.side_effect = VCSError("cannot checkout main")
    agent._vcs.has_changes.return_value = False

    plan_response = json.dumps([{"description": "Do something"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        await agent.run("test goal")

    agent._vcs.checkout.assert_called()
    # delete_branch must NOT be called — we never returned to main
    agent._vcs.delete_branch.assert_not_called()


@pytest.mark.asyncio
async def test_run_planner_stream_stall_still_tears_down_branch(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A STREAM_STALL-style SDKError from planner.decompose must not skip teardown.

    The failing iteration in production was exactly this shape: planner.decompose
    raised SDKError(STREAM_STALL). Before the fix, the escaping exception bypassed
    all VCS cleanup in run()'s linear body, so the retry inherited a dirty tree.
    Here, decompose raises BEFORE the task branch is cut — so branch_name stays
    None and teardown only needs to discard changes.
    """
    agent = make_agent()

    async def _fail_decompose(*_a, **_k):  # type: ignore[no-untyped-def]
        raise SDKError("stream stalled", category=ErrorCategory.UNKNOWN)

    with patch.object(agent.planner, "decompose", side_effect=_fail_decompose), \
         patch("maestro.agent.query", new_callable=AsyncMock), \
         pytest.raises(SDKError):
        await agent.run("goal")

    # decompose raised before branch creation, so no checkout_new_branch.
    agent._vcs.checkout_new_branch.assert_not_called()
    # But the teardown closure still discards any stray changes.
    agent._vcs.discard_changes.assert_called()


@pytest.mark.asyncio
async def test_run_executor_sdkerror_tears_down_branch(make_agent) -> None:  # type: ignore[no-untyped-def]
    """An SDKError escaping from the concurrent-tier executor must trigger teardown.

    The concurrent tier re-raises SDKError directly (see the ``raise item`` inside
    the asyncio.gather result loop in run()). With the fix, the outer finally's
    teardown returns to main and deletes the task branch on the way out.
    """
    agent = make_agent()
    # Two independent steps → concurrent tier (no depends_on between them).
    plan_response = json.dumps([
        {"id": "s1", "description": "Step A"},
        {"id": "s2", "description": "Step B"},
    ])

    async def _fail_exec(**_k):  # type: ignore[no-untyped-def]
        raise SDKError("transient", category=ErrorCategory.UNKNOWN)

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=_fail_exec), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        with pytest.raises(SDKError):
            await agent.run("goal")

    # Teardown must have returned to main and deleted the branch.
    agent._vcs.discard_changes.assert_called()
    agent._vcs.checkout.assert_called_with("main")
    agent._vcs.delete_branch.assert_called_once()


@pytest.mark.parametrize(
    "category", [ErrorCategory.RATE_LIMIT, ErrorCategory.UNKNOWN],
)
@pytest.mark.asyncio
async def test_run_distill_findings_sdk_error_still_records_task(make_agent, category) -> None:  # type: ignore[no-untyped-def]
    """An SDKError from _distill_findings must not discard the record.

    _distill_findings runs at phase 5b, after every step has executed and the
    branch is committed. Enrichment is best-effort, so an SDK failure there
    degrades to ``key_findings=None`` and the TaskRecord is still built and
    persisted — the outcome, errors, and duration of a completed task are never
    thrown away. Parametrized over a transient (RATE_LIMIT) and a
    non-transient (UNKNOWN) category because the 5b guard is deliberately
    category-blind: the record must survive regardless of why the cosmetic
    enrichment failed. VCS teardown still runs (and here the branch is deleted,
    since has_changes defaulted to False so nothing was committed).
    """
    agent = make_agent()
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    async def _fail_distill(**_k):  # type: ignore[no-untyped-def]
        raise SDKError("distill stalled", category=category)

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", side_effect=_fail_distill):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        # No SDKError escapes run() — the enrichment failure is absorbed.
        record = await agent.run("goal")

    assert record.outcome == "success"
    assert record.key_findings is None
    # The completed task's record reached memory rather than being discarded.
    tasks = agent.memory.get_recent_tasks()
    assert tasks[0].task_id == record.task_id
    assert tasks[0].key_findings is None

    agent._vcs.discard_changes.assert_called()
    agent._vcs.checkout.assert_called_with("main")
    agent._vcs.delete_branch.assert_called_once()


@pytest.mark.asyncio
async def test_run_teardown_tolerates_vcs_errors(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Teardown and entry-time defense must survive unexpected VCS errors.

    ``has_changes``, ``discard_changes``, and ``delete_branch`` can fail on a
    degraded git state. Each call site is individually wrapped, so run() must
    complete without propagating these errors. This simultaneously exercises:
      - entry-time has_changes() except branch,
      - commit-if-dirty except branch (has_changes raises during the guard),
      - teardown discard_changes/delete_branch/has_changes except branches.
    """
    agent = make_agent()
    agent._vcs.has_changes.side_effect = AttributeError("mock-misconfig")
    agent._vcs.discard_changes.side_effect = AttributeError("mock-misconfig")
    agent._vcs.delete_branch.side_effect = AttributeError("mock-misconfig")
    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        record = await agent.run("goal")

    assert record.outcome == "success"
    # Teardown exercised every cleanup step despite each raising
    agent._vcs.discard_changes.assert_called()
    agent._vcs.checkout.assert_called_with("main")
    agent._vcs.delete_branch.assert_called_once()


@pytest.mark.asyncio
async def test_run_entry_time_reset_discards_before_branching(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Entry-time defense discards changes and checks out main before branching."""
    agent = make_agent()
    # Simulate entering run() with a dirty tree: first has_changes() returns True
    # (entry-time check sees leftover); subsequent calls return False.
    agent._vcs.has_changes.side_effect = [True, False, False, False]

    plan_response = json.dumps([{"description": "Step 1"}])
    step_response = "Done."

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", new_callable=AsyncMock) as mock_exec, \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_exec.return_value = MockQueryResult(response=step_response)
        await agent.run("goal")

    # discard_changes must fire at least once before checkout_new_branch
    names = [c[0] for c in agent._vcs.method_calls]
    discard_idx = names.index("discard_changes")
    new_branch_idx = names.index("checkout_new_branch")
    assert discard_idx < new_branch_idx, (
        "discard_changes must be called before checkout_new_branch on entry"
    )


@pytest.mark.asyncio
async def test_shutdown_guard_defeated_by_run_loop_inner_finally(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Issue #189: run_loop() inner finally restores _running=True, defeating shutdown() guard.

    When shutdown() is called directly during a sub-operation (e.g. contemplate),
    run_loop()'s compensating ``finally: self._running = True`` reverts the flag
    that shutdown() set to False.  The outer finally then calls shutdown() a
    second time and the idempotency guard (``if not self._running: return``)
    passes because _running was restored to True — producing duplicate journal
    entries and duplicate "Maestro loop stopped." notifications.

    This test reproduces the exact interleaved sequence:
      1. run_loop → contemplate phase
      2. contemplate body calls shutdown() directly (simulating external caller)
      3. contemplate returns → contemplate's own finally sets _running=False
      4. run_loop's compensating finally sets _running=True  ← defeats guard
      5. shutdown event check → loop breaks
      6. outer finally → shutdown() again → guard SHOULD block but doesn't
    """
    agent = make_agent()

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]

    # Track shutdown side effects via the actual journal write — the most
    # reliable signal that shutdown()'s body ran past the idempotency guard.
    shutdown_journal_calls: list[str] = []
    original_record_event = agent.journal.record_event

    def tracking_record_event(*args: object, **kwargs: object) -> object:
        triggered = kwargs.get("triggered_by", "")
        if triggered == "shutdown":
            shutdown_journal_calls.append("shutdown_journal_written")
        return original_record_event(*args, **kwargs)

    agent.journal.record_event = tracking_record_event  # type: ignore[method-assign]

    async def contemplate_that_triggers_shutdown(**kwargs: object) -> str:
        """Simulate an external shutdown() call arriving mid-contemplate.

        In production this happens when application code or a test harness
        calls shutdown() directly while run_loop() is inside a sub-operation.
        """
        # Call shutdown directly — this sets _running=False and fires side effects
        await agent.shutdown()
        # Also set the shutdown event so the loop breaks after this phase
        agent._ensure_shutdown_event().set()
        return "reflection"

    agent.contemplate = contemplate_that_triggers_shutdown  # type: ignore[method-assign]

    # Run the loop — it will:
    #   1. Enter contemplate phase
    #   2. contemplate calls shutdown() → _running=False, side effects fire once
    #   3. contemplate's own finally → _running=False (harmless)
    #   4. run_loop compensating finally → _running=True  (BUG: defeats guard)
    #   5. shutdown event → break
    #   6. outer finally → shutdown() again → guard SHOULD block but _running=True
    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # The shutdown journal entry should have been written exactly ONCE.
    # If the bug is present (run_loop inner finally restores _running=True),
    # the outer finally's shutdown() call bypasses the guard and writes a
    # duplicate journal entry + fires a duplicate notification.
    assert len(shutdown_journal_calls) == 1, (
        f"shutdown() journal write executed {len(shutdown_journal_calls)} times, "
        f"expected 1. The run_loop() inner finally restored _running=True after "
        f"the first shutdown() call, defeating the idempotency guard and causing "
        f"a duplicate shutdown (journal entry + notification). Issue #189."
    )


@pytest.mark.asyncio
async def test_shutdown_idempotent(make_agent):  # type: ignore[no-untyped-def]
    """Verify that calling shutdown() twice results in only one event write (idempotent)."""
    agent = make_agent()
    agent._running = True

    record_event_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    original_record = agent.journal.record_event

    def tracking_record(*args: object, **kwargs: object) -> object:
        record_event_calls.append((args, kwargs))
        return original_record(*args, **kwargs)

    agent.journal.record_event = tracking_record  # type: ignore[method-assign]

    # First call: expected shutdown behavior
    await agent.shutdown()
    assert agent._running is False
    assert len(record_event_calls) == 1

    # Second call: should be a no-op if shutdown() is idempotent
    await agent.shutdown()

    # Should still be 1 (not 2) - idempotent
    assert len(record_event_calls) == 1, (
        f"shutdown() is NOT idempotent: journal.record_event() was called "
        f"{len(record_event_calls)} times (expected 1). "
        f"Duplicate shutdown events will be written."
    )
