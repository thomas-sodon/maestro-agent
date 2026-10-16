"""Tests for step dependency graph building and concurrent execution."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from maestro.executor import StepResult
from maestro.loop.step_execution import _build_execution_groups
from maestro.memory import PlanStep
from maestro.sdk import SDKError
from tests.conftest import MockQueryResult


def _step(
    sid: str, deps: list[str] | None = None, status: str = "pending",
) -> PlanStep:
    return PlanStep(
        step_id=sid, description=f"do {sid}",
        status=status, depends_on=deps or [],  # type: ignore[arg-type]
    )


# --- _build_execution_groups unit tests ---


def test_linear_chain() -> None:
    steps = [_step("a"), _step("b", ["a"]), _step("c", ["b"])]
    groups = _build_execution_groups(steps)
    assert len(groups) == 3
    assert [g[0].step_id for g in groups] == ["a", "b", "c"]


def test_two_independent_with_dependency_edge() -> None:
    steps = [_step("a"), _step("b"), _step("c", ["a", "b"])]
    groups = _build_execution_groups(steps)
    assert len(groups) == 2
    assert {s.step_id for s in groups[0]} == {"a", "b"}
    assert groups[1][0].step_id == "c"


def test_diamond_pattern() -> None:
    steps = [
        _step("a"), _step("b", ["a"]),
        _step("c", ["a"]), _step("d", ["b", "c"]),
    ]
    groups = _build_execution_groups(steps)
    assert len(groups) == 3
    assert groups[0][0].step_id == "a"
    assert {s.step_id for s in groups[1]} == {"b", "c"}
    assert groups[2][0].step_id == "d"


def test_cycle_detection_falls_back_to_sequential() -> None:
    steps = [_step("a", ["b"]), _step("b", ["a"])]
    groups = _build_execution_groups(steps)
    assert len(groups) == 2
    assert all(len(g) == 1 for g in groups)


def test_broken_ref_falls_back_to_sequential() -> None:
    steps = [_step("a", ["nonexistent"]), _step("b")]
    groups = _build_execution_groups(steps)
    assert len(groups) == 2
    assert groups[0][0].step_id == "b"
    assert groups[1][0].step_id == "a"


def test_all_empty_deps_sequential() -> None:
    steps = [_step("a"), _step("b"), _step("c")]
    groups = _build_execution_groups(steps)
    assert len(groups) == 3
    assert all(len(g) == 1 for g in groups)


def test_pre_completed_ids() -> None:
    steps = [_step("b", ["a"]), _step("c", ["b"])]
    groups = _build_execution_groups(steps, completed_ids={"a"})
    assert len(groups) == 2
    assert groups[0][0].step_id == "b"
    assert groups[1][0].step_id == "c"


def test_done_and_failed_steps_excluded() -> None:
    steps = [
        _step("a", status="done"),
        _step("b", ["a"], status="failed"),
        _step("c", ["a"]),
    ]
    groups = _build_execution_groups(steps)
    assert len(groups) == 1
    assert groups[0][0].step_id == "c"


def test_failed_prerequisite_blocks_dependent() -> None:
    """#3959: a dependent of a failed step is never scheduled, not even via
    the cycle/broken-ref sequential fallback."""
    steps = [_step("a", status="failed"), _step("b", ["a"]), _step("c")]
    groups = _build_execution_groups(steps)
    scheduled = [s.step_id for g in groups for s in g]
    assert scheduled == ["c"]


def test_transitively_blocked_dependent_excluded() -> None:
    steps = [_step("a", status="failed"), _step("b", ["a"]), _step("c", ["b"])]
    groups = _build_execution_groups(steps)
    assert groups == []


def test_running_step_does_not_satisfy_dependency() -> None:
    """A checkpoint restored with a ``running`` prerequisite blocks too."""
    steps = [_step("a", status="running"), _step("b", ["a"]), _step("c", ["x"])]
    steps.append(_step("x"))
    groups = _build_execution_groups(steps)
    scheduled = [s.step_id for g in groups for s in g]
    assert scheduled == ["x", "c"]


def test_done_step_ids_added_to_done_set() -> None:
    """Pending steps depending on done steps should resolve correctly,
    not trigger the broken-refs fallback to sequential execution."""
    steps = [
        _step("a", status="done"),
        _step("b", ["a"]),   # pending, depends on done "a"
        _step("c", ["a"]),   # pending, depends on done "a"
        _step("d", ["b", "c"]),  # pending, depends on "b" and "c"
    ]
    groups = _build_execution_groups(steps)
    # b and c should be concurrent (both only depend on done step a)
    assert len(groups) == 2, (
        f"Expected 2 groups (concurrent b,c + d), got {len(groups)}: "
        f"{[[s.step_id for s in g] for g in groups]}"
    )
    assert {s.step_id for s in groups[0]} == {"b", "c"}
    assert groups[1][0].step_id == "d"


def test_empty_steps_returns_empty() -> None:
    assert _build_execution_groups([]) == []


def test_single_step_no_deps() -> None:
    groups = _build_execution_groups([_step("a")])
    assert len(groups) == 1
    assert groups[0][0].step_id == "a"


# --- Integration tests ---


@pytest.mark.asyncio
async def test_concurrent_steps_run_via_gather(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    plan_response = json.dumps({"steps": [
        {"id": "s1", "description": "ALPHA_TASK_ONE", "depends_on": []},
        {"id": "s2", "description": "BETA_TASK_TWO", "depends_on": []},
        {"id": "s3", "description": "GAMMA_TASK_THREE", "depends_on": ["s1", "s2"]},
    ]})
    call_order: list[str] = []

    async def exec_side_effect(**kwargs):  # type: ignore[no-untyped-def]
        prompt = kwargs.get("prompt", "")
        # Match on the step's own task line (not prior-step context)
        if "GAMMA_TASK_THREE" in prompt:
            call_order.append("s3")
        elif "ALPHA_TASK_ONE" in prompt:
            call_order.append("s1_start")
            await asyncio.sleep(0)
            call_order.append("s1_end")
        elif "BETA_TASK_TWO" in prompt:
            call_order.append("s2_start")
            await asyncio.sleep(0)
            call_order.append("s2_end")
        return MockQueryResult(response="done")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=exec_side_effect), \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        record = await agent.run("Test concurrent steps")

    assert record.outcome == "success"
    assert len(record.plan) == 3
    # s1 and s2 should both start before s3 runs
    assert "s1_start" in call_order
    assert "s2_start" in call_order
    assert "s3" in call_order
    assert call_order.index("s3") > call_order.index("s1_end")
    assert call_order.index("s3") > call_order.index("s2_end")


@pytest.mark.asyncio
async def test_dependent_steps_run_sequentially(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    plan_response = json.dumps({"steps": [
        {"id": "s1", "description": "First", "depends_on": []},
        {"id": "s2", "description": "Second", "depends_on": ["s1"]},
    ]})
    exec_call = 0

    async def exec_side_effect(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal exec_call
        exec_call += 1
        return MockQueryResult(response=f"result {exec_call}")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=exec_side_effect), \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        record = await agent.run("Test sequential deps")

    assert record.outcome == "success"
    assert record.plan[0].status == "done"
    assert record.plan[1].status == "done"


@pytest.mark.asyncio
async def test_failure_in_concurrent_group_triggers_replan(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    plan_response = json.dumps({"steps": [
        {"id": "s1", "description": "Step A", "depends_on": []},
        {"id": "s2", "description": "Step B will fail", "depends_on": []},
        {"id": "s3", "description": "Final", "depends_on": ["s1", "s2"]},
    ]})
    replan_response = json.dumps({"steps": [
        {"id": "r1", "description": "Recovery step"},
    ]})
    exec_call = 0

    async def exec_side_effect(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal exec_call
        exec_call += 1
        prompt = kwargs.get("prompt", "")
        if "Step B will fail" in prompt:
            raise RuntimeError("Step B exploded")
        return MockQueryResult(response="ok")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=exec_side_effect), \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.side_effect = [
            MockQueryResult(response=plan_response),
            MockQueryResult(response=replan_response),
        ]
        mock_distill.return_value = MockQueryResult(response="findings")
        record = await agent.run("Test concurrent replan")

    assert record.replans == 1
    done_steps = [s for s in record.plan if s.status == "done"]
    failed_steps = [s for s in record.plan if s.status == "failed"]
    assert len(failed_steps) >= 1
    assert len(done_steps) >= 1


@pytest.mark.asyncio
async def test_replan_budget_debits_on_empty_replan_attempts(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An empty (unrecoverable/unparseable) ``replan()`` reply still debits the
    ``max_replans`` budget (#451).

    Three no-dep steps execute as three sequential single-step groups. Every
    step fails and ``replan()`` always returns ``[]`` (its documented give-up
    path). The replan entry point must be invoked exactly ``max_replans`` times
    — once — not once per failing group. Before the fix the increment lived
    inside ``if new_steps:``, so an empty reply never spent the budget and all
    three groups re-entered replan (three LLM calls).
    """
    agent = make_agent()
    assert agent.config.loop_max_replans == 1
    plan_response = json.dumps({"steps": [
        {"id": "s1", "description": "Step one fails", "depends_on": []},
        {"id": "s2", "description": "Step two fails", "depends_on": []},
        {"id": "s3", "description": "Step three fails", "depends_on": []},
    ]})

    async def execute_side_effect(
        step: PlanStep, *args: object, **kwargs: object,
    ) -> StepResult:
        return StepResult(success=False, output="", error="boom")

    agent.executor.execute = execute_side_effect  # type: ignore[method-assign]
    # replan() always gives up with an empty list — the unrecoverable /
    # unparseable reply this fix must still charge against the budget.
    agent.planner.replan = AsyncMock(return_value=[])  # type: ignore[method-assign]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        record = await agent.run("Test empty-replan budget")

    # Budget bounds *attempts*: exactly max_replans calls, not one per group.
    assert agent.planner.replan.call_count == agent.config.loop_max_replans
    # No steps were replaced (every replan was empty), so all three failed.
    failed_steps = [s for s in record.plan if s.status == "failed"]
    assert len(failed_steps) == 3


@pytest.mark.asyncio
async def test_replan_budget_debits_on_empty_concurrent_replan_attempts(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An empty ``replan()`` reply debits the budget from the *concurrent*
    replan site too (#451) — the mirror of the sequential-path test.

    ``s1`` and ``s2`` (no deps) form one concurrent group; ``s3`` depends on
    both, forcing topo-grouping instead of the sequential fallback. Every step
    fails and ``replan()`` always returns ``[]``. The concurrent group's empty
    replan must spend the budget so ``s3``'s later single-step failure cannot
    re-enter replan: exactly ``max_replans`` calls total. Before the fix the
    concurrent-site increment lived inside ``if new_steps:``, so the empty
    concurrent replan left ``replans`` at 0 and a later failure re-fired it
    — two LLM calls despite ``max_replans``. Since #3959 ``s3`` is blocked by
    its failed prerequisites and stays ``pending``.
    """
    agent = make_agent()
    assert agent.config.loop_max_replans == 1
    plan_response = json.dumps({"steps": [
        {"id": "s1", "description": "Step one fails", "depends_on": []},
        {"id": "s2", "description": "Step two fails", "depends_on": []},
        {"id": "s3", "description": "Step three fails", "depends_on": ["s1", "s2"]},
    ]})

    async def execute_side_effect(
        step: PlanStep, *args: object, **kwargs: object,
    ) -> StepResult:
        return StepResult(success=False, output="", error="boom")

    agent.executor.execute = execute_side_effect  # type: ignore[method-assign]
    # replan() always gives up with an empty list — the unrecoverable /
    # unparseable reply this fix must still charge against the budget.
    agent.planner.replan = AsyncMock(return_value=[])  # type: ignore[method-assign]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        record = await agent.run("Test empty concurrent-replan budget")

    # The concurrent group's empty replan spent the sole budget slot, so the
    # later single-step failure cannot re-fire replan: exactly max_replans.
    assert agent.planner.replan.call_count == agent.config.loop_max_replans
    assert record.replans == agent.config.loop_max_replans
    # Every replan was empty, so s1/s2 failed (none replaced) and s3, whose
    # prerequisites failed, was never dispatched (#3959).
    statuses = {s.step_id: s.status for s in record.plan}
    assert statuses == {"s1": "failed", "s2": "failed", "s3": "pending"}


@pytest.mark.asyncio
async def test_no_deps_preserves_sequential(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    plan_response = json.dumps({"steps": [
        {"description": "Step 1"},
        {"description": "Step 2"},
    ]})
    exec_call = 0

    async def exec_side_effect(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal exec_call
        exec_call += 1
        return MockQueryResult(response=f"done {exec_call}")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=exec_side_effect), \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        record = await agent.run("Old-style plan")

    assert record.outcome == "success"
    assert len(record.plan) == 2
    assert record.plan[0].status == "done"
    assert record.plan[1].status == "done"


@pytest.mark.asyncio
async def test_sdk_error_in_concurrent_group_propagates(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """SDKError raised in a concurrent group is re-raised immediately."""
    agent = make_agent()
    plan_response = json.dumps({"steps": [
        {"id": "s1", "description": "Base step", "depends_on": []},
        {"id": "s2", "description": "Ok step", "depends_on": ["s1"]},
        {"id": "s3", "description": "SDK error step", "depends_on": ["s1"]},
    ]})

    async def execute_side_effect(
        step: PlanStep, *args: object, **kwargs: object,
    ) -> StepResult:
        if "SDK error step" in step.description:
            raise SDKError("API failure")
        return StepResult(success=True, output="ok")

    agent.executor.execute = execute_side_effect  # type: ignore[method-assign]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        with pytest.raises(SDKError, match="API failure"):
            await agent.run("Test SDK error in concurrent group")


@pytest.mark.asyncio
async def test_cancelled_child_in_concurrent_group_propagates(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A child's CancelledError in a concurrent tier propagates (#3969).

    It must raise before any step status is committed, before later tiers
    dispatch, and without a replan — not be recorded as a step failure.
    """
    agent = make_agent()
    agent.config.loop_max_replans = 0
    plan_response = json.dumps({"steps": [
        {"id": "s1", "description": "Base step", "depends_on": []},
        {"id": "s2", "description": "Ok step", "depends_on": ["s1"]},
        {"id": "s3", "description": "Cancel step", "depends_on": ["s1"]},
        {"id": "s4", "description": "Later step", "depends_on": ["s2"]},
    ]})
    seen: dict[str, PlanStep] = {}

    async def execute_side_effect(
        step: PlanStep, *args: object, **kwargs: object,
    ) -> StepResult:
        seen[step.step_id] = step
        if step.step_id == "s3":
            raise asyncio.CancelledError()
        return StepResult(success=True, output="ok")

    agent.executor.execute = execute_side_effect  # type: ignore[method-assign]
    replan = AsyncMock(return_value=[])
    agent.planner.replan = replan  # type: ignore[method-assign]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        with pytest.raises(asyncio.CancelledError):
            await agent.run("Test cancel in concurrent group")

    assert seen["s2"].status == "running"
    assert seen["s3"].status == "running"
    assert "s4" not in seen
    replan.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_step_in_sequential_group_propagates(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Parity control: a single-step tier also propagates cancellation."""
    agent = make_agent()
    agent.config.loop_max_replans = 0
    plan_response = json.dumps({"steps": [
        {"id": "s1", "description": "Cancel step", "depends_on": []},
        {"id": "s2", "description": "Later step", "depends_on": ["s1"]},
    ]})
    seen: list[str] = []

    async def execute_side_effect(
        step: PlanStep, *args: object, **kwargs: object,
    ) -> StepResult:
        seen.append(step.step_id)
        raise asyncio.CancelledError()

    agent.executor.execute = execute_side_effect  # type: ignore[method-assign]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        with pytest.raises(asyncio.CancelledError):
            await agent.run("Test cancel in sequential group")

    assert seen == ["s1"]


@pytest.mark.asyncio
async def test_exception_in_concurrent_group_marks_step_failed(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A concurrent-group exception is attributed to the step that raised it.

    ``asyncio.gather`` preserves positional order, so ``raw_results[i]`` is
    ``group[i]``'s outcome; the attribution loop consumes that pairing directly
    (#1173). It previously re-derived the blamed step by scanning ``group`` for
    the first one still marked ``running``.

    This is a characterization test, not a failing-first regression test: the
    old scan was *latent*, not wrong. At iteration ``i`` every step below index
    ``i`` has already been resolved to ``done``/``failed``, so the scan always
    landed on ``group[i]`` and no input reachable through ``run()`` made it
    misattribute. What it pins is the structural guarantee that replaced the
    heuristic — the step named here is the one that raised, by construction
    rather than by coincidence.

    ``s2`` and ``s3`` both depend on ``s1``, so they form one concurrent tier.
    Only ``s3`` raises: it must be the failed step, carrying its own exception
    text, while ``s2`` stays ``done``.
    """
    agent = make_agent()
    agent.config.loop_max_replans = 0
    plan_response = json.dumps({"steps": [
        {"id": "s1", "description": "Base step", "depends_on": []},
        {"id": "s2", "description": "Good step", "depends_on": ["s1"]},
        {"id": "s3", "description": "Crash step", "depends_on": ["s1"]},
    ]})

    async def execute_side_effect(
        step: PlanStep, *args: object, **kwargs: object,
    ) -> StepResult:
        if "Crash step" in step.description:
            raise RuntimeError("unexpected crash")
        return StepResult(success=True, output="ok")

    agent.executor.execute = execute_side_effect  # type: ignore[method-assign]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        record = await agent.run("Test exception in concurrent group")

    by_id = {s.step_id: s for s in record.plan}
    assert by_id["s3"].status == "failed"
    s3_result = by_id["s3"].result
    assert s3_result is not None
    assert "unexpected crash" in s3_result
    assert by_id["s2"].status == "done"
    assert by_id["s1"].status == "done"
    failed = [s for s in record.plan if s.status == "failed"]
    assert len(failed) == 1


@pytest.mark.asyncio
async def test_concurrent_group_exceptions_each_recorded_with_own_message(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Every concurrent-group exception carries its own message.

    All three steps of the concurrent tier raise distinct exceptions. Each must
    be marked ``failed`` carrying *its own* text, so neither a dropped exception
    nor one duplicated across steps can pass.

    The old attribution loop scanned ``group`` for a step still marked
    ``running`` and, finding none, fell through to ``continue`` — dropping the
    exception text before it reached ``errors`` and letting a crashed group
    report clean, which CLAUDE.md's fail-loud invariant forbids (#1173). Like
    its sibling above, this is a characterization test: that fallthrough was
    *unreachable* through ``run()``, because ``group[i]`` is still ``running``
    at iteration ``i`` and the scan therefore always found it. This test does
    not reach a no-step-running state either — nothing driving ``run()`` can.
    What it pins is that positional pairing makes the drop unrepresentable
    rather than merely unreached.
    """
    agent = make_agent()
    agent.config.loop_max_replans = 0
    plan_response = json.dumps({"steps": [
        {"id": "s1", "description": "Base step", "depends_on": []},
        {"id": "s2", "description": "Crash alpha", "depends_on": ["s1"]},
        {"id": "s3", "description": "Crash beta", "depends_on": ["s1"]},
        {"id": "s4", "description": "Crash gamma", "depends_on": ["s1"]},
    ]})
    messages = {
        "Crash alpha": "alpha exploded",
        "Crash beta": "beta exploded",
        "Crash gamma": "gamma exploded",
    }

    async def execute_side_effect(
        step: PlanStep, *args: object, **kwargs: object,
    ) -> StepResult:
        for marker, message in messages.items():
            if marker in step.description:
                raise RuntimeError(message)
        return StepResult(success=True, output="ok")

    agent.executor.execute = execute_side_effect  # type: ignore[method-assign]

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.return_value = MockQueryResult(response=plan_response)
        mock_distill.return_value = MockQueryResult(response="findings")
        record = await agent.run("Test no concurrent exception is dropped")

    by_id = {s.step_id: s for s in record.plan}
    for step_id, expected in [
        ("s2", "alpha exploded"),
        ("s3", "beta exploded"),
        ("s4", "gamma exploded"),
    ]:
        assert by_id[step_id].status == "failed"
        result = by_id[step_id].result
        assert result is not None
        assert expected in result


def test_build_execution_groups_rejects_duplicate_step_ids() -> None:
    """#3960: a duplicate ID would let a historical step satisfy a new
    step's dependency, so tiering refuses it loudly."""
    steps = [
        PlanStep("recovery_1", "old", status="done"),
        PlanStep("recovery_1", "new"),
        PlanStep("other", "unique"),
    ]
    with pytest.raises(ValueError, match=r"duplicate step_id in plan: \['recovery_1'\]$"):
        _build_execution_groups(steps)
