"""Unit tests for ``maestro.loop.step_execution.execute_step_groups``.

The stage is the verbatim lift of ``run``'s ``# 3.`` block (proposal 106): the
step-group execution engine — the ``while group_idx < len(groups)`` loop, its
two closures (``_checkpoint_progress`` and the concurrent-tier ``_run_step``),
the single-step fast path, the concurrent ``asyncio.gather`` path, the adaptive
replan/splice, the session-reuse lifecycle, and the trailing ``finally`` that
ends the executor session. These tests call the function directly with the agent
positional plus keyword args and pin each arm; the executor/planner seams are
``AsyncMock``\\ s and ``mem_fs`` backs the checkpoint round-trip, so nothing
shells out.

Behaviour-preservation of the *whole* block is proven by the full-``run``
regression harness (``tests/test_checkpoint.py`` for the checkpoint lifecycle,
``tests/test_agent.py`` for the session-reuse suite and loop mechanics), which
now flows through this function unchanged; these unit tests exercise the
extracted stage in isolation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.executor import StepResult
from maestro.loop.step_execution import execute_step_groups
from maestro.memory import PlanStep, TaskCheckpoint
from maestro.sdk import SDKError

_TASK_BRANCH = "maestro/task/tid"
_PR_BRANCH = "maestro/evolve/ep1"


def _kwargs(steps: list[PlanStep], **overrides: object) -> dict[str, object]:
    """Full keyword-arg set for ``execute_step_groups`` with sane defaults.

    Each call mints fresh ``errors`` / ``all_output`` / ``executed_commands``
    lists so a test can assert the function mutated *those* objects in place.
    """
    base: dict[str, object] = dict(
        steps=steps,
        resume_checkpoint=None,
        errors=[],
        all_output=[],
        executed_commands=[],
        identity_context="id-ctx",
        context="ctx",
        goal="the goal",
        run_session=None,
        artifacts_path=Path("/fake/agent/artifacts/tid"),
        allowed_branches=frozenset({_TASK_BRANCH}),
        branch_name=_TASK_BRANCH,
        pr_branch=None,
        task_id="tid",
        source="self_directed",
        linked_question=None,
        acceptance_criteria=None,
        workspace_moment_id=None,
        workspace_item_ids=None,
    )
    base.update(overrides)
    return base


def _stub_executor(agent, execute: AsyncMock) -> None:  # type: ignore[no-untyped-def]
    """Stub the three executor seams the stage touches."""
    agent.executor.execute = execute
    agent.executor.start_session = AsyncMock()
    agent.executor.end_session = AsyncMock()


@pytest.mark.asyncio
async def test_single_step_success_accumulates_and_returns_the_five_locals(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a)+(i): a single-step group succeeds → the step is ``done``, output and
    commands accumulate, and the returned 5-tuple is the very ``steps`` /
    ``errors`` / ``all_output`` / ``executed_commands`` objects passed in plus
    the ``replans`` count."""
    agent = make_agent()
    _stub_executor(
        agent,
        AsyncMock(
            return_value=StepResult(
                success=True, output="did it", bash_commands=["echo hi"],
            ),
        ),
    )
    step = PlanStep("s1", "only step")
    steps = [step]
    errors: list[str] = []
    all_output: list[str] = []
    executed_commands: list[str] = []

    ret = await execute_step_groups(
        agent,
        **_kwargs(
            steps,
            errors=errors,
            all_output=all_output,
            executed_commands=executed_commands,
        )
    )

    assert step.status == "done"
    assert step.result == "did it"
    # The returned 5-tuple is the very objects passed in (mutated in place),
    # plus the replan count — the stage reassigns nothing in the caller.
    assert ret[0] is steps
    assert ret[1] is errors
    assert ret[2] is all_output
    assert ret[3] is executed_commands
    assert ret[4] == 0
    assert all_output == ["did it"]
    assert executed_commands == ["echo hi"]
    assert errors == []
    agent.executor.end_session.assert_awaited()


@pytest.mark.asyncio
async def test_resume_checkpoint_seeds_replan_count(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The ``resume_checkpoint.replans if resume_checkpoint else 0`` derivation:
    a resume seeds the count so it survives into the returned tuple."""
    agent = make_agent()
    _stub_executor(
        agent, AsyncMock(return_value=StepResult(success=True, output="ok")),
    )
    cp = TaskCheckpoint(task_id="tid", goal="g", branch=_TASK_BRANCH, replans=3)

    ret = await execute_step_groups(
        agent,
        **_kwargs([PlanStep("s1", "step")], resume_checkpoint=cp)
    )

    assert ret[4] == 3


@pytest.mark.asyncio
async def test_single_step_failure_replans_and_splices_within_budget(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b): a single-step failure with budget left tears the session down,
    re-plans, debits ``replans``, splices the new steps, and rebuilds groups —
    the replanned step then runs to completion."""
    agent = make_agent()
    agent.config.loop_max_replans = 1

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        if step.step_id == "s1":
            return StepResult(success=False, output="", error="s1 failed")
        return StepResult(success=True, output="s2 ok")

    _stub_executor(agent, AsyncMock(side_effect=fake_exec))
    agent.planner.replan = AsyncMock(return_value=[PlanStep("s2", "replanned")])

    ret = await execute_step_groups(agent, **_kwargs([PlanStep("s1", "first")]))

    steps = ret[0]
    by_id = {s.step_id: s for s in steps}
    assert by_id["s1"].status == "failed"
    assert by_id["s2"].status == "done"
    assert ret[4] == 1  # replans debited exactly once
    agent.planner.replan.assert_awaited_once()
    agent.executor.end_session.assert_awaited()


@pytest.mark.asyncio
async def test_single_step_failure_exhausted_budget_no_splice(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(c): with ``loop_max_replans`` exhausted the failure records but never
    re-plans; the loop advances past the failed group and ends."""
    agent = make_agent()
    agent.config.loop_max_replans = 0
    _stub_executor(
        agent,
        AsyncMock(
            return_value=StepResult(success=False, output="", error="nope"),
        ),
    )
    agent.planner.replan = AsyncMock()

    ret = await execute_step_groups(agent, **_kwargs([PlanStep("s1", "first")]))

    assert ret[0][0].status == "failed"
    assert ret[1] == ["nope"]
    assert ret[4] == 0
    agent.planner.replan.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_step_empty_replan_advances_without_splice(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The ``if new_steps:`` false arm of the single-step path: a re-plan that
    returns nothing still debits the budget but splices no steps, and the loop
    advances past the failed group rather than restarting."""
    agent = make_agent()
    agent.config.loop_max_replans = 2
    _stub_executor(
        agent,
        AsyncMock(
            return_value=StepResult(success=False, output="", error="fail"),
        ),
    )
    agent.planner.replan = AsyncMock(return_value=[])

    ret = await execute_step_groups(agent, **_kwargs([PlanStep("s1", "first")]))

    assert ret[0] == [PlanStep("s1", "first", status="failed", result="fail")]
    assert ret[4] == 1  # debited once even though nothing was spliced
    agent.planner.replan.assert_awaited_once()


@pytest.mark.asyncio
async def test_loop_detected_logs_abort_warning(
    make_agent,  # type: ignore[no-untyped-def]
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """(d): a ``loop_detected`` step failure logs the abort warning."""
    agent = make_agent()
    agent.config.loop_max_replans = 0
    _stub_executor(
        agent,
        AsyncMock(
            return_value=StepResult(
                success=False, output="", error="looped", loop_detected=True,
            ),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        await execute_step_groups(agent, **_kwargs([PlanStep("s1", "spin")]))

    assert "Step aborted on tool loop" in caplog.text


@pytest.mark.asyncio
async def test_concurrent_group_success_via_gather(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(e): a multi-step tier runs concurrently via ``asyncio.gather`` and both
    steps land ``done`` with their output accumulated in positional order."""
    agent = make_agent()

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        return StepResult(success=True, output=f"ok-{step.step_id}")

    _stub_executor(agent, AsyncMock(side_effect=fake_exec))
    # s0 already done → the sole pending tier is the concurrent {s1, s2}.
    steps = [
        PlanStep("s0", "seed", status="done"),
        PlanStep("s1", "a", depends_on=["s0"]),
        PlanStep("s2", "b", depends_on=["s0"]),
    ]

    ret = await execute_step_groups(agent, **_kwargs(steps))

    by_id = {s.step_id: s for s in ret[0]}
    assert by_id["s1"].status == "done"
    assert by_id["s2"].status == "done"
    assert sorted(ret[2]) == ["ok-s1", "ok-s2"]


@pytest.mark.asyncio
async def test_sdk_error_in_concurrent_group_reraised_before_mutation(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(f): an ``SDKError`` anywhere in a concurrent tier is re-raised by the
    first-pass scan *before* any step status / output / error is mutated."""
    agent = make_agent()

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        if step.step_id == "s2":
            raise SDKError("infra blew up")
        return StepResult(success=True, output="s1 ok")

    _stub_executor(agent, AsyncMock(side_effect=fake_exec))
    steps = [
        PlanStep("s0", "seed", status="done"),
        PlanStep("s1", "a", depends_on=["s0"]),
        PlanStep("s2", "b", depends_on=["s0"]),
    ]
    errors: list[str] = []
    all_output: list[str] = []

    with pytest.raises(SDKError):
        await execute_step_groups(
            agent,
            **_kwargs(steps, errors=errors, all_output=all_output)
        )

    # No done/failed transition and no accumulation happened — only the
    # pre-gather ``running`` set, which is expected.
    assert {s.step_id: s.status for s in steps if s.step_id != "s0"} == {
        "s1": "running",
        "s2": "running",
    }
    assert errors == []
    assert all_output == []
    agent.executor.end_session.assert_awaited()  # finally still ran


@pytest.mark.asyncio
async def test_non_sdk_exception_in_concurrent_group_recorded_as_failure(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(g): a non-SDK ``BaseException`` from a concurrent step is recorded as a
    step failure (not re-raised), the surviving step still lands ``done``."""
    agent = make_agent()
    agent.config.loop_max_replans = 0  # no replan → isolate the record arm

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        if step.step_id == "s2":
            raise RuntimeError("boom in the step")
        return StepResult(success=True, output="s1 ok")

    _stub_executor(agent, AsyncMock(side_effect=fake_exec))
    agent.planner.replan = AsyncMock()
    steps = [
        PlanStep("s0", "seed", status="done"),
        PlanStep("s1", "a", depends_on=["s0"]),
        PlanStep("s2", "b", depends_on=["s0"]),
    ]

    ret = await execute_step_groups(agent, **_kwargs(steps))

    by_id = {s.step_id: s for s in ret[0]}
    assert by_id["s1"].status == "done"
    assert by_id["s2"].status == "failed"
    assert "boom in the step" in by_id["s2"].result
    assert any("boom in the step" in e for e in ret[1])
    agent.planner.replan.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_failure_loop_detected_replans_and_splices(
    make_agent,  # type: ignore[no-untyped-def]
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """The concurrent replan arm: a ``StepResult`` failure carrying
    ``loop_detected`` logs the abort warning, re-plans within budget, and
    splices the new step, which then runs to completion on the single-step
    path."""
    agent = make_agent()
    agent.config.loop_max_replans = 1

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        if step.step_id == "s2":
            return StepResult(
                success=False, output="", error="looped", loop_detected=True,
            )
        return StepResult(success=True, output=f"ok-{step.step_id}")

    _stub_executor(agent, AsyncMock(side_effect=fake_exec))
    agent.planner.replan = AsyncMock(return_value=[PlanStep("s3", "recovery")])
    steps = [
        PlanStep("s0", "seed", status="done"),
        PlanStep("s1", "a", depends_on=["s0"]),
        PlanStep("s2", "b", depends_on=["s0"]),
    ]

    with caplog.at_level(logging.WARNING, logger="maestro"):
        ret = await execute_step_groups(agent, **_kwargs(steps))

    by_id = {s.step_id: s for s in ret[0]}
    assert by_id["s2"].status == "failed"
    assert by_id["s3"].status == "done"
    assert ret[4] == 1
    assert "Step aborted on tool loop" in caplog.text
    agent.planner.replan.assert_awaited_once()


@pytest.mark.asyncio
async def test_checkpoint_written_once_per_group_for_owned_branch(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """(h) owned-branch arm: a checkpoint is persisted after each completed
    group for an owned task branch, and the persisted file reflects the live
    step statuses."""
    agent = make_agent()
    spy = MagicMock(wraps=agent.memory.save_checkpoint)
    agent.memory.save_checkpoint = spy  # type: ignore[method-assign]
    _stub_executor(
        agent, AsyncMock(return_value=StepResult(success=True, output="ok")),
    )

    await execute_step_groups(
        agent,
        **_kwargs([PlanStep("s1", "first"), PlanStep("s2", "second")])
    )

    # Two single-step groups → two checkpoint writes.
    assert spy.call_count == 2
    raw = mem_fs["/fake/agent/checkpoints/tid.json"]
    data = json.loads(raw)
    statuses = {s["step_id"]: s["status"] for s in data["steps"]}
    assert statuses == {"s1": "done", "s2": "done"}


@pytest.mark.asyncio
async def test_checkpoint_persists_workspace_citation(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """The conscious-access citation (proposal 130) is written into the
    checkpoint so a crash-resume keeps the moment provenance the queued task
    carried."""
    agent = make_agent()
    _stub_executor(
        agent, AsyncMock(return_value=StepResult(success=True, output="ok")),
    )

    await execute_step_groups(
        agent,
        **_kwargs(
            [PlanStep("s1", "first")],
            workspace_moment_id="cm-7",
            workspace_item_ids=["wi-1", "wi-2"],
        )
    )

    data = json.loads(mem_fs["/fake/agent/checkpoints/tid.json"])
    assert data["workspace_moment_id"] == "cm-7"
    assert data["workspace_item_ids"] == ["wi-1", "wi-2"]


@pytest.mark.asyncio
async def test_checkpoint_skipped_for_pr_branch(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(h) PR-branch arm: a remediation lap (``pr_branch`` set) never
    checkpoints — the branch is owned by Tend PR, not by ``run``."""
    agent = make_agent()
    agent.memory.save_checkpoint = MagicMock()  # type: ignore[method-assign]
    _stub_executor(
        agent, AsyncMock(return_value=StepResult(success=True, output="ok")),
    )

    await execute_step_groups(
        agent,
        **_kwargs(
            [PlanStep("s1", "first")],
            branch_name=_PR_BRANCH,
            pr_branch=_PR_BRANCH,
        )
    )

    agent.memory.save_checkpoint.assert_not_called()


@pytest.mark.asyncio
async def test_checkpoint_skipped_when_branch_none(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(h) no-branch arm: the branch-fallback run (``branch_name is None``)
    never checkpoints."""
    agent = make_agent()
    agent.memory.save_checkpoint = MagicMock()  # type: ignore[method-assign]
    _stub_executor(
        agent, AsyncMock(return_value=StepResult(success=True, output="ok")),
    )

    await execute_step_groups(
        agent,
        **_kwargs(
            [PlanStep("s1", "first")],
            branch_name=None,
            allowed_branches=None,
        )
    )

    agent.memory.save_checkpoint.assert_not_called()


@pytest.mark.asyncio
async def test_session_reuse_starts_and_recycles_around_concurrent_tier(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Session lifecycle: with reuse on and ≥2 single-step tiers the executor
    session is opened on the first tier, kept across the second, and torn down
    before a concurrent tier (and once more in the ``finally``)."""
    agent = make_agent()
    agent.config.enable_session_reuse = True

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        return StepResult(success=True, output=f"ok-{step.step_id}")

    _stub_executor(agent, AsyncMock(side_effect=fake_exec))
    # Tiers: [s0] [s1] [s2, s3] → two single-step tiers make reuse pay off.
    steps = [
        PlanStep("s0", "seed"),
        PlanStep("s1", "next", depends_on=["s0"]),
        PlanStep("s2", "fan-a", depends_on=["s1"]),
        PlanStep("s3", "fan-b", depends_on=["s1"]),
    ]

    ret = await execute_step_groups(agent, **_kwargs(steps))

    assert all(s.status == "done" for s in ret[0])
    # Opened once (first tier); the second single-step tier reuses it.
    agent.executor.start_session.assert_awaited_once()
    # Ended before the concurrent tier and again in the finally.
    assert agent.executor.end_session.await_count == 2


def _tier_recorder() -> tuple[list[tuple[str, str]], AsyncMock]:
    """An executor stub that fails ``s1``/``s2``, succeeds otherwise, and
    records ``(event, step_id)`` start/end pairs so tests can read order."""
    events: list[tuple[str, str]] = []

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        events.append(("start", step.step_id))
        events.append(("end", step.step_id))
        if step.step_id in {"s1", "s2"}:
            return StepResult(success=False, output="", error="boom")
        return StepResult(success=True, output=f"ok-{step.step_id}")

    return events, AsyncMock(side_effect=fake_exec)


@pytest.mark.asyncio
async def test_single_step_replan_reusing_historical_id_keeps_dependency_order(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """#3960 single-step arm: a second replan reusing ``recovery_1`` (already
    done from the first replan) must not let the historical step satisfy the
    new chain's dependency — the new steps get unique IDs and run in order,
    and the checkpoint holds those unique IDs."""
    agent = make_agent()
    agent.config.loop_max_replans = 2
    events, execute = _tier_recorder()
    _stub_executor(agent, execute)
    agent.planner.replan = AsyncMock(side_effect=[
        [PlanStep("recovery_1", "first fix"), PlanStep("s2", "retry")],
        [
            PlanStep("recovery_1", "second fix"),
            PlanStep("recovery_2", "follow", depends_on=["recovery_1"]),
        ],
    ])

    ret = await execute_step_groups(agent, **_kwargs([PlanStep("s1", "a")]))

    ids = [s.step_id for s in ret[0]]
    assert len(ids) == len(set(ids))
    new_first = ret[0][-2]
    assert new_first.step_id == "recovery_1_r2"
    assert ret[0][-1].depends_on == ["recovery_1_r2"]
    assert events.index(("end", "recovery_1_r2")) < events.index(
        ("start", "recovery_2"),
    )
    data = json.loads(mem_fs["/fake/agent/checkpoints/tid.json"])
    saved = [s["step_id"] for s in data["steps"]]
    assert saved == ids

    # Resume from the persisted snapshot with the new chain re-opened: no
    # remap happens and the prerequisite still runs before its dependent.
    resumed = [
        PlanStep(
            d["step_id"], d["description"],
            depends_on=d["depends_on"],
            status="pending" if i >= len(ids) - 2 else d["status"],
        )
        for i, d in enumerate(data["steps"])
    ]
    events.clear()
    ckpt = MagicMock(spec=TaskCheckpoint, replans=2)
    again = await execute_step_groups(
        agent, **_kwargs(resumed, resume_checkpoint=ckpt),
    )
    assert [s.step_id for s in again[0]] == ids
    assert events == [
        ("start", "recovery_1_r2"), ("end", "recovery_1_r2"),
        ("start", "recovery_2"), ("end", "recovery_2"),
    ]


@pytest.mark.asyncio
async def test_concurrent_replan_reusing_historical_id_keeps_dependency_order(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#3960 concurrent arm: a replan reusing the failed step's own ID must
    not collapse the new prerequisite and its dependent into one tier."""
    agent = make_agent()
    agent.config.loop_max_replans = 1
    execute = AsyncMock(side_effect=lambda step, *a, **k: StepResult(
        success=step.step_id != "s2", output="", error="boom",
    ))
    _stub_executor(agent, execute)
    agent.planner.replan = AsyncMock(return_value=[
        PlanStep("s2", "fix"),
        PlanStep("s3", "follow", depends_on=["s2"]),
    ])
    steps = [
        PlanStep("s0", "seed", status="done"),
        PlanStep("s1", "a", depends_on=["s0"]),
        PlanStep("s2", "b", depends_on=["s0"]),
    ]

    ret = await execute_step_groups(agent, **_kwargs(steps))

    ids = [s.step_id for s in ret[0]]
    assert ids == ["s0", "s1", "s2", "s2_r2", "s3"]
    assert ret[0][-1].depends_on == ["s2_r2"]
    order = [c.args[0].step_id for c in execute.await_args_list]
    assert order[-2:] == ["s2_r2", "s3"]


@pytest.mark.asyncio
async def test_resume_remaps_legacy_duplicate_ids(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A pre-#3960 checkpoint carrying duplicate IDs is normalised on resume
    rather than tripping the duplicate-ID raise."""
    agent = make_agent()
    execute = AsyncMock(return_value=StepResult(success=True, output="ok"))
    _stub_executor(agent, execute)
    steps = [
        PlanStep("recovery_1", "old", status="done"),
        PlanStep("recovery_1", "older retry", status="failed"),
        PlanStep("recovery_1", "new"),
        PlanStep("recovery_2", "follow", depends_on=["recovery_1"]),
    ]
    ckpt = MagicMock(spec=TaskCheckpoint, replans=2)

    ret = await execute_step_groups(
        agent, **_kwargs(steps, resume_checkpoint=ckpt),
    )

    ids = [s.step_id for s in ret[0]]
    assert ids == ["recovery_1", "recovery_1_r2", "recovery_1_r3", "recovery_2"]
    assert ret[0][-1].depends_on == ["recovery_1_r3"]
    order = [c.args[0].step_id for c in execute.await_args_list]
    assert order == ["recovery_1_r3", "recovery_2"]


def _keyed_execute(failing: set[str]) -> AsyncMock:
    """Executor whose outcome is keyed on ``step.step_id`` (#3959)."""

    async def _exec(step, *args, **kwargs):  # type: ignore[no-untyped-def]
        if step.step_id in failing:
            return StepResult(success=False, output="", error=f"{step.step_id} broke")
        return StepResult(success=True, output=f"{step.step_id} ok")

    return AsyncMock(side_effect=_exec)


@pytest.mark.asyncio
async def test_failed_prereq_exhausted_budget_blocks_dependent(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#3959: with the replan budget spent, a failed prerequisite leaves its
    dependent ``pending`` and never dispatched."""
    agent = make_agent()
    agent.config.loop_max_replans = 0
    execute = _keyed_execute({"s1"})
    _stub_executor(agent, execute)
    s1 = PlanStep("s1", "prepare")
    s2 = PlanStep("s2", "use", depends_on=["s1"])

    await execute_step_groups(agent, **_kwargs([s1, s2]))

    assert execute.await_count == 1
    assert s1.status == "failed"
    assert s2.status == "pending"


@pytest.mark.asyncio
async def test_failed_prereq_empty_replan_blocks_dependent(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#3959: an empty replan does not unblock the dependent either."""
    agent = make_agent()
    agent.config.loop_max_replans = 2
    execute = _keyed_execute({"s1"})
    _stub_executor(agent, execute)
    agent.planner.replan = AsyncMock(return_value=[])
    s1 = PlanStep("s1", "prepare")
    s2 = PlanStep("s2", "use", depends_on=["s1"])

    ret = await execute_step_groups(agent, **_kwargs([s1, s2]))

    assert execute.await_count == 1
    assert s2.status == "pending"
    assert ret[4] == 1


@pytest.mark.asyncio
async def test_failed_member_of_parallel_tier_blocks_dependent(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#3959: a failure inside a concurrent tier blocks only the steps that
    depend on it; independent ready work still runs."""
    agent = make_agent()
    agent.config.loop_max_replans = 0
    execute = _keyed_execute({"p2"})
    _stub_executor(agent, execute)
    p1 = PlanStep("p1", "one")
    p2 = PlanStep("p2", "two")
    d = PlanStep("d", "join", depends_on=["p1", "p2"])
    u = PlanStep("u", "after one", depends_on=["p1"])

    await execute_step_groups(agent, **_kwargs([p1, p2, d, u]))

    dispatched = [c.args[0].step_id for c in execute.await_args_list]
    assert "d" not in dispatched
    assert d.status == "pending"
    assert u.status == "done"
    assert p2.status == "failed"


@pytest.mark.asyncio
async def test_stale_schedule_dispatch_guard_skips_unready_step(
    make_agent,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3959 backstop: even a stale schedule that places a dependent after a
    failed prerequisite cannot dispatch it."""
    import maestro.loop.step_execution as step_execution_mod

    agent = make_agent()
    agent.config.loop_max_replans = 0
    execute = _keyed_execute({"s1"})
    _stub_executor(agent, execute)
    s1 = PlanStep("s1", "prepare")
    s2 = PlanStep("s2", "use", depends_on=["s1"])
    monkeypatch.setattr(
        step_execution_mod, "_build_execution_groups", lambda steps, done: [[s1], [s2]],
    )

    await execute_step_groups(agent, **_kwargs([s1, s2]))

    assert execute.await_count == 1
    assert s2.status == "pending"


@pytest.mark.asyncio
async def test_unreplaced_failure_ends_active_session_before_reschedule(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """#3959: the unreplaced-failure reschedule tears down a live session, so
    a rebuilt schedule that turns reuse off cannot strand it open."""
    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.config.loop_max_replans = 0
    execute = _keyed_execute({"s1"})
    _stub_executor(agent, execute)
    s0 = PlanStep("s0", "seed")
    s1 = PlanStep("s1", "next", depends_on=["s0"])
    s2 = PlanStep("s2", "after", depends_on=["s1"])

    await execute_step_groups(agent, **_kwargs([s0, s1, s2]))

    assert s2.status == "pending"
    agent.executor.start_session.assert_awaited_once()
    # Ended at the failure-arm reschedule and again in the finally.
    assert agent.executor.end_session.await_count == 2


@pytest.mark.asyncio
async def test_push_remotes_override(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The push remotes follow the override on the single-step, session and
    concurrent paths."""
    agent = make_agent()
    agent.config.enable_session_reuse = True
    agent.memory.save_checkpoint = MagicMock()  # type: ignore[method-assign]
    execute = AsyncMock(return_value=StepResult(success=True, output="ok"))
    _stub_executor(agent, execute)
    remotes = frozenset({"origin", "acme/tool"})
    steps = [
        PlanStep("s0", "seed"),
        PlanStep("s1", "next", depends_on=["s0"]),
        PlanStep("s2", "fan-a", depends_on=["s1"]),
        PlanStep("s3", "fan-b", depends_on=["s1"]),
    ]

    await execute_step_groups(
        agent,
        **_kwargs(
            steps,
            allowed_push_remotes=remotes,
        )
    )

    assert execute.await_count == 4
    for call in execute.await_args_list:
        assert call.args[1] == str(agent.config.workspace)
        assert call.kwargs["allowed_push_remotes"] == remotes
    start = agent.executor.start_session.await_args
    assert start.args[0] == str(agent.config.workspace)
    assert start.kwargs["allowed_push_remotes"] == remotes


@pytest.mark.asyncio
async def test_default_workspace_and_unscoped_remotes(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    execute = AsyncMock(return_value=StepResult(success=True, output="ok"))
    _stub_executor(agent, execute)

    await execute_step_groups(agent, **_kwargs([PlanStep("s1", "first")]))

    call = execute.await_args
    assert call.args[1] == str(agent.config.workspace)
    assert call.kwargs["allowed_push_remotes"] is None
