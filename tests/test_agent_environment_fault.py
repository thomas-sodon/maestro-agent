"""Environment-fault handling in ``AutonomousAgent.implement_plan``.

A permanent *environment* fault (OOM kill, missing or non-executable binary,
CLI usage error) reaches ``implement_plan``'s exception handler as a
non-transient ``SDKError``. It must re-raise without demoting: the fault is the
host's, so it may not spend one of the plan's ``max_implementation_attempts``
nor file an issue blaming the plan (#3488, ledger r1).

Lives in a sibling module because ``tests/test_agent.py`` is pinned shrink-only
by ``tests/test_source_file_size.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.sdk import ErrorCategory, SDKError


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [2, 126, 127, 137])
async def test_implement_plan_environment_fault_leaves_plan_undemoted(  # type: ignore[no-untyped-def]
    make_agent, exit_code: int,
) -> None:
    """A permanent environment fault must not spend one of the plan's attempts.

    An OOM kill (137) or a missing/non-executable binary (126/127) is a fault of
    the host, not of the plan, so ``implement_plan`` re-raises without demoting:
    the status stays ``approved``, ``failure_count`` stays 0, and no issue is
    filed blaming the plan (#3488, ledger r1). The error still propagates, and
    its category sits outside ``_TRANSIENT_SDK_CATEGORIES``, so the loop crashes
    loudly rather than retrying.
    """
    from maestro.memory import EvolutionPlan
    from maestro.sdk import ErrorCategory, SDKError

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-env-fault",
        title="Environment Fault Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    fault = SDKError(
        "cli killed",
        category=ErrorCategory.UNKNOWN,
        exit_code=exit_code,
    )
    cleanup_mock = MagicMock()
    create_issue_mock = MagicMock()
    notify_mock = AsyncMock()
    with (
        patch.object(agent.evolution, "implement", AsyncMock(side_effect=fault)),
        patch.object(agent.evolution, "cleanup_branch", cleanup_mock),
        patch.object(agent.evolution, "create_issue", create_issue_mock),
        patch.object(agent.notifier, "notify", notify_mock),
        pytest.raises(SDKError, match="cli killed"),
    ):
        await agent.implement_plan("p-env-fault")

    stored = agent.memory.get_plan("p-env-fault")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.failure_count == 0
    assert stored.last_failure_reason is None
    # The branch is still cleaned up, exactly as on the transient arm — the
    # fault is handed along so cleanup can skip the discard for a dirty-tree
    # refusal (#3711), which this SDK fault is not.
    cleanup_mock.assert_called_once_with("p-env-fault", fault)
    create_issue_mock.assert_not_called()
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_implement_plan_permanent_non_environment_fault_still_demotes(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A permanent fault with no environmental exit code keeps demoting.

    The carve-out is exit-code-scoped: a buffer-overflow ``UNKNOWN`` (exit_code
    ``None``) can be provoked by the work itself, so it still charges the plan.
    """
    from maestro.memory import EvolutionPlan
    from maestro.sdk import ErrorCategory, SDKError

    agent = make_agent()

    plan = EvolutionPlan(
        plan_id="p-non-env-fault",
        title="Non-Environment Fault Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    fault = SDKError("frame too large", category=ErrorCategory.UNKNOWN)
    with (
        patch.object(agent.evolution, "implement", AsyncMock(side_effect=fault)),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.notifier, "notify", AsyncMock()),
        pytest.raises(SDKError, match="frame too large"),
    ):
        await agent.implement_plan("p-non-env-fault")

    stored = agent.memory.get_plan("p-non-env-fault")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1


def _environment_faults() -> list[BaseException]:
    from maestro.sdk import ErrorCategory, SDKError
    from maestro.vcs import DirtyWorkingTreeError

    faults: list[BaseException] = [
        SDKError("cli killed", category=ErrorCategory.UNKNOWN, exit_code=code)
        for code in (2, 126, 127, 137)
    ]
    faults.append(DirtyWorkingTreeError("working tree is dirty"))
    return faults


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", _environment_faults(), ids=["2", "126", "127", "137", "dirty-tree"])
async def test_implement_approved_plans_propagates_environment_fault(  # type: ignore[no-untyped-def]
    make_agent, fault: BaseException,
) -> None:
    """The dispatcher must not swallow a fault ``implement_plan`` left un-demoted.

    ``implement_plan`` re-raises a permanent environment fault without charging
    the plan, so isolating it at the dispatcher would leave the plan
    ``approved`` and re-attempt it every iteration forever (#4039). It must
    propagate to ``run_loop``/crash handling, and no sibling plan is attempted.
    """
    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.max_implementation_attempts = 3
    agent.config.max_implementations_per_iteration = 2
    for plan_id in ("plan_A", "plan_B"):
        agent.memory.save_evolution_plan(
            EvolutionPlan(
                plan_id=plan_id,
                title=plan_id,
                category="capability",
                rationale="test",
                proposed_changes=["change"],
                files_affected=["file.py"],
                expected_impact="better",
                status="approved",
                created_at="2025-01-01T00:00:00Z",
            ),
        )

    implement_mock = AsyncMock(side_effect=fault)
    with (
        patch.object(agent.evolution, "implement", implement_mock),
        patch.object(agent.evolution, "ensure_main_branch", MagicMock()),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", MagicMock()),
        patch.object(agent.notifier, "notify", AsyncMock()),
        pytest.raises(type(fault)) as raised,
    ):
        await agent._implement_approved_plans()

    assert raised.value is fault
    assert implement_mock.await_count == 1
    for plan_id in ("plan_A", "plan_B"):
        stored = agent.memory.get_plan(plan_id)
        assert stored is not None
        assert stored.status == "approved"
        assert stored.failure_count == 0


def _replan_plan():  # type: ignore[no-untyped-def]
    from maestro.memory import EvolutionPlan

    return EvolutionPlan(
        plan_id="p-replan-sdk",
        title="Replan SDK",
        category="capability",
        rationale="test",
        proposed_changes=["Modify maestro/foo.py", "Modify maestro/bar.py"],
        files_affected=["maestro/foo.py", "maestro/bar.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )


async def _run_replan_with_error(agent, error: SDKError) -> MagicMock:  # type: ignore[no-untyped-def]
    agent.memory.save_evolution_plan(_replan_plan())
    implement_mock = AsyncMock()
    with (
        patch("maestro.loop.implementation.query", AsyncMock(side_effect=error)),
        patch.object(agent.evolution, "implement", implement_mock),
        patch.object(agent, "_notify_best_effort", AsyncMock()),
        pytest.raises(SDKError) as raised,
    ):
        await agent.implement_plan("p-replan-sdk")
    assert raised.value is error
    return implement_mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        SDKError("rl", category=ErrorCategory.RATE_LIMIT),
        SDKError("stall", category=ErrorCategory.STREAM_STALL),
        SDKError("crash", category=ErrorCategory.CLI_CRASH),
        SDKError("oom", category=ErrorCategory.UNKNOWN, exit_code=137),
    ],
    ids=["rate_limit", "stream_stall", "cli_crash", "env_fault"],
)
async def test_implement_plan_replan_sdk_error_keeps_approval(  # type: ignore[no-untyped-def]
    make_agent, error: SDKError,
) -> None:
    """#4063: a transient or environment-fault SDK error during the scope
    replan re-raises with the plan still ``approved`` and unspent."""
    agent = make_agent()
    implement_mock = await _run_replan_with_error(agent, error)

    implement_mock.assert_not_called()
    stored = agent.memory.get_plan("p-replan-sdk")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.failure_count == 0


@pytest.mark.asyncio
async def test_implement_plan_replan_sdk_error_demotes_non_transient(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """#4063: a non-transient, non-environment SDK error during the scope
    replan demotes with a replan-naming reason, then re-raises."""
    agent = make_agent()
    implement_mock = await _run_replan_with_error(
        agent, SDKError("denied", category=ErrorCategory.AUTH),
    )

    implement_mock.assert_not_called()
    stored = agent.memory.get_plan("p-replan-sdk")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1
    assert stored.last_failure_reason is not None
    assert stored.last_failure_reason.startswith("Scope replan SDK call failed")


async def _run_slice_with_error(agent, error: SDKError) -> MagicMock:  # type: ignore[no-untyped-def]
    agent.config.multi_pr_proposals_enabled = True
    agent.memory.save_evolution_plan(_replan_plan())
    implement_mock = AsyncMock()
    replan_query = AsyncMock()
    with (
        patch.object(agent.evolution, "propose_plan_chain", AsyncMock(side_effect=error)),
        patch("maestro.loop.implementation.query", replan_query),
        patch.object(agent.evolution, "implement", implement_mock),
        patch.object(agent, "_notify_best_effort", AsyncMock()),
        pytest.raises(SDKError) as raised,
    ):
        await agent.implement_plan("p-replan-sdk")
    assert raised.value is error
    replan_query.assert_not_awaited()
    return implement_mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        SDKError("rl", category=ErrorCategory.RATE_LIMIT),
        SDKError("stall", category=ErrorCategory.STREAM_STALL),
        SDKError("crash", category=ErrorCategory.CLI_CRASH),
        SDKError("oom", category=ErrorCategory.UNKNOWN, exit_code=137),
    ],
    ids=["rate_limit", "stream_stall", "cli_crash", "env_fault"],
)
async def test_implement_plan_slice_sdk_error_keeps_approval(  # type: ignore[no-untyped-def]
    make_agent, error: SDKError,
) -> None:
    """#4162: a transient or environment-fault SDK error during the scope
    slice call re-raises with the plan still ``approved`` and unspent."""
    agent = make_agent()
    implement_mock = await _run_slice_with_error(agent, error)

    implement_mock.assert_not_called()
    stored = agent.memory.get_plan("p-replan-sdk")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.failure_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        SDKError("denied", category=ErrorCategory.AUTH),
        SDKError("bad request", category=ErrorCategory.UNKNOWN),
    ],
    ids=["auth", "unknown_no_exit_code"],
)
async def test_implement_plan_slice_sdk_error_demotes_non_transient(  # type: ignore[no-untyped-def]
    make_agent, error: SDKError,
) -> None:
    """#4162: a non-transient, non-environment SDK error during the scope
    slice call demotes with a slice-naming reason, then re-raises."""
    agent = make_agent()
    implement_mock = await _run_slice_with_error(agent, error)

    implement_mock.assert_not_called()
    stored = agent.memory.get_plan("p-replan-sdk")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.failure_count == 1
    assert stored.last_failure_reason is not None
    assert stored.last_failure_reason.startswith("Scope slice SDK call failed")


@pytest.mark.asyncio
async def test_implement_approved_plans_slice_sdk_error_not_resliced(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """#4162: once a non-transient slice failure demotes the plan, the next
    dispatcher pass does not pay for another ``propose_plan_chain`` call."""
    agent = make_agent()
    agent.config.multi_pr_proposals_enabled = True
    agent.memory.save_evolution_plan(_replan_plan())
    chain_mock = AsyncMock(side_effect=SDKError("denied", category=ErrorCategory.AUTH))
    with (
        patch.object(agent.evolution, "propose_plan_chain", chain_mock),
        patch.object(agent.evolution, "implement", AsyncMock()),
        patch.object(agent.evolution, "ensure_main_branch", MagicMock()),
        patch.object(agent, "_notify_best_effort", AsyncMock()),
        patch.object(agent.notifier, "notify", AsyncMock()),
    ):
        await agent._implement_approved_plans()
        await agent._implement_approved_plans()

    assert chain_mock.await_count == 1
    stored = agent.memory.get_plan("p-replan-sdk")
    assert stored is not None
    assert stored.status == "failed"


@pytest.mark.asyncio
async def test_implement_plan_session_connect_oom_leaves_plan_undemoted(  # type: ignore[no-untyped-def]
    make_agent,
) -> None:
    """An OOM kill while the implementation session connects must not demote (#4570).

    ``create_session`` stays real; only the SDK client is faked, so the raw
    ``ProcessError`` must be classified at the connect seam for
    ``is_environment_fault`` to recognise it.
    """
    from claude_agent_sdk import ProcessError

    from maestro.memory import EvolutionPlan

    agent = make_agent()
    agent.config.enable_session_reuse = True
    plan = EvolutionPlan(
        plan_id="p-connect-oom",
        title="Connect OOM Test",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="approved",
        created_at="2025-01-01T00:00:00Z",
    )
    agent.memory.save_evolution_plan(plan)

    def make_client(options: object = None, **_: object) -> MagicMock:
        client = MagicMock()
        client.connect = AsyncMock(side_effect=ProcessError("killed", exit_code=137))
        client.disconnect = AsyncMock()
        return client

    create_issue_mock = MagicMock()
    notify_mock = AsyncMock()
    with (
        patch("maestro.sdk.ClaudeSDKClient", side_effect=make_client),
        patch.object(agent.evolution, "implement", AsyncMock(return_value="base")),
        patch.object(agent.evolution, "cleanup_branch", MagicMock()),
        patch.object(agent.evolution, "create_issue", create_issue_mock),
        patch.object(agent.notifier, "notify", notify_mock),
        pytest.raises(SDKError) as excinfo,
    ):
        await agent.implement_plan("p-connect-oom")

    assert excinfo.value.exit_code == 137
    stored = agent.memory.get_plan("p-connect-oom")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.failure_count == 0
    assert stored.last_failure_reason is None
    create_issue_mock.assert_not_called()
    notify_mock.assert_not_awaited()
