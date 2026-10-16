"""Tests for task checkpointing (proposal 006).

Covers the ``Memory`` persistence layer (round-trip, corruption tolerance,
clear, stale sweep) and the ``MaestroAgent.run()`` / ``resume_task()``
integration (checkpoint written per step group, survives a re-plan splice,
resume skips completed work, cleared on completion and on the graceful-
shutdown interrupt, orphaned-branch fallback). All I/O is mocked via
``mem_fs``; externals (executor, planner, distill, VCS) are mocked so no
real filesystem, subprocess, network, or sleep is touched.

Scope note: because ``_vcs`` is a ``MagicMock`` here, ``run()``'s teardown
(``discard_changes``/``delete_branch``) is a no-op, so these tests assert the
checkpoint *file* lifecycle (written per group, survives a crash, cleared on
record/interrupt) — they do **not** exercise whether a resumed branch
actually carries the completed steps' file changes. Completed-step edits are
committed only after the whole loop (``run()`` step 5), so real recovery
depends on the task branch surviving the crash; that working-tree/branch
interaction is out of reach with a mocked VCS and is verified at the
integration level, not here.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.executor import StepResult
from maestro.memory import Memory, PlanStep, TaskCheckpoint
from maestro.sdk import SDKError
from maestro.vcs import VCSError

FAKE_DIR = Path("/fake/mem")
AGENT_DIR = Path("/fake/agent")


def _make_memory() -> Memory:
    return Memory(FAKE_DIR)


def _cp_path(task_id: str, base: Path = FAKE_DIR) -> str:
    return str(base / "checkpoints" / f"{task_id}.json")


# ── Memory persistence layer ─────────────────────────────────────────


def test_checkpoint_round_trip_preserves_every_field(mem_fs: dict[str, str]) -> None:
    mem = _make_memory()
    cp = TaskCheckpoint(
        task_id="t1",
        goal="do the thing",
        source="self_directed",
        branch="maestro/task/t1",
        steps=[
            PlanStep("s1", "first", status="done", result="r1"),
            PlanStep("s2", "second", status="failed", result="oops"),
            PlanStep("s3", "third", status="pending", depends_on=["s1"]),
        ],
        errors=["oops"],
        all_output=["r1"],
        executed_commands=["python3.12 -m pytest tests/ -q"],
        artifacts_dir="/artifacts/t1",
        replans=2,
        timestamp="2026-06-17T00:00:00+00:00",
        linked_question="what am I?",
        acceptance_criteria=["criterion one", "criterion two"],
        workspace_moment_id="cm-42",
        workspace_item_ids=["wi-1", "wi-2"],
    )
    mem.save_checkpoint(cp)
    loaded = mem.load_checkpoint("t1")

    assert loaded == cp
    assert loaded is not None
    assert loaded.replans == 2
    assert loaded.executed_commands == ["python3.12 -m pytest tests/ -q"]
    assert loaded.linked_question == "what am I?"
    assert loaded.acceptance_criteria == ["criterion one", "criterion two"]
    assert loaded.workspace_moment_id == "cm-42"
    assert loaded.workspace_item_ids == ["wi-1", "wi-2"]
    statuses = {s.step_id: (s.status, s.result) for s in loaded.steps}
    assert statuses["s1"] == ("done", "r1")
    assert statuses["s2"] == ("failed", "oops")
    assert statuses["s3"] == ("pending", None)
    assert loaded.steps[2].depends_on == ["s1"]


def test_save_checkpoint_overwrites_same_file(mem_fs: dict[str, str]) -> None:
    mem = _make_memory()
    mem.save_checkpoint(
        TaskCheckpoint(task_id="t1", goal="g", branch="b", replans=0),
    )
    mem.save_checkpoint(
        TaskCheckpoint(task_id="t1", goal="g", branch="b", replans=5),
    )
    # One file per task, overwritten — not appended.
    raw = mem_fs[_cp_path("t1")]
    assert raw.count("task_id") == 1
    loaded = mem.load_checkpoint("t1")
    assert loaded is not None
    assert loaded.replans == 5


def test_load_checkpoint_absent_returns_none(mem_fs: dict[str, str]) -> None:
    mem = _make_memory()
    assert mem.load_checkpoint("nope") is None


def test_load_checkpoint_corrupt_returns_none(mem_fs: dict[str, str]) -> None:
    mem = _make_memory()
    mem_fs[_cp_path("bad")] = "{ this is not valid json"
    with patch("maestro.memory.logger.warning") as warn:
        assert mem.load_checkpoint("bad") is None
    warn.assert_called_once()


def test_load_checkpoint_unknown_keys_filtered(mem_fs: dict[str, str]) -> None:
    """A checkpoint with a stale/extra field still loads (filtered)."""
    mem = _make_memory()
    payload = {
        "task_id": "t1",
        "goal": "g",
        "source": "external",
        "branch": "b",
        "steps": [{"step_id": "s1", "description": "d", "status": "done",
                   "extra_step_field": 1}],
        "errors": [],
        "all_output": [],
        "artifacts_dir": None,
        "replans": 0,
        "timestamp": "2026-06-17T00:00:00+00:00",
        "obsolete_field": "ignored",
    }
    mem_fs[_cp_path("t1")] = json.dumps(payload)
    loaded = mem.load_checkpoint("t1")
    assert loaded is not None
    assert loaded.task_id == "t1"
    # A pre-``executed_commands`` snapshot deserializes to the default empty
    # list rather than crashing — old checkpoints simply read as "no commands".
    assert loaded.executed_commands == []
    # A pre-proposal-130 snapshot (no citation fields) reads them as ``None``:
    # the resumed run simply carries no conscious-access citation.
    assert loaded.workspace_moment_id is None
    assert loaded.workspace_item_ids is None
    assert loaded.steps[0].step_id == "s1"
    assert loaded.steps[0].status == "done"


def test_clear_checkpoint_removes_file_and_is_idempotent(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory()
    mem.save_checkpoint(TaskCheckpoint(task_id="t1", goal="g", branch="b"))
    assert _cp_path("t1") in mem_fs
    mem.clear_checkpoint("t1")
    assert _cp_path("t1") not in mem_fs
    # Clearing a non-existent checkpoint is a silent no-op.
    mem.clear_checkpoint("t1")
    mem.clear_checkpoint("never-existed")


def test_cleanup_stale_checkpoints_deletes_old_and_unparseable(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory()
    cp_dir = FAKE_DIR / "checkpoints"
    mem.save_checkpoint(
        TaskCheckpoint(
            task_id="old", goal="g", branch="b",
            timestamp="2000-01-01T00:00:00+00:00",
        ),
    )
    mem.save_checkpoint(
        TaskCheckpoint(
            task_id="recent", goal="g", branch="b",
            timestamp=datetime.now(UTC).isoformat(),
        ),
    )
    mem_fs[str(cp_dir / "broken.json")] = "{ not json"
    paths = [
        cp_dir / "old.json",
        cp_dir / "recent.json",
        cp_dir / "broken.json",
    ]
    with patch.object(Path, "glob", side_effect=lambda pat: iter(paths)):
        mem.cleanup_stale_checkpoints(7)

    assert str(cp_dir / "old.json") not in mem_fs
    assert str(cp_dir / "broken.json") not in mem_fs
    assert str(cp_dir / "recent.json") in mem_fs


def test_cleanup_stale_checkpoints_disabled_is_noop(
    mem_fs: dict[str, str],
) -> None:
    mem = _make_memory()
    mem.save_checkpoint(
        TaskCheckpoint(
            task_id="old", goal="g", branch="b",
            timestamp="2000-01-01T00:00:00+00:00",
        ),
    )
    with patch.object(Path, "glob") as glob:
        mem.cleanup_stale_checkpoints(0)
    assert _cp_path("old") in mem_fs
    glob.assert_not_called()


# ── Agent integration ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkpoint_written_after_each_group(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    steps = [PlanStep("s1", "first"), PlanStep("s2", "second")]
    agent.planner.decompose = AsyncMock(return_value=steps)
    calls = {"n": 0}

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return StepResult(success=True, output="ok1")
        raise SDKError("crash mid-task")

    agent.executor.execute = AsyncMock(side_effect=fake_exec)

    with pytest.raises(SDKError):
        await agent.run("goal", task_id="fixedid")

    raw = mem_fs[_cp_path("fixedid", AGENT_DIR)]
    data = json.loads(raw)
    statuses = {s["step_id"]: s["status"] for s in data["steps"]}
    assert statuses["s1"] == "done"
    assert statuses["s2"] == "pending"


@pytest.mark.asyncio
async def test_checkpoint_threads_executed_commands(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A step's ``bash_commands`` accumulate into the checkpoint's
    ``executed_commands`` — the executed-command record red-read keys on,
    threaded through the same success gate as ``all_output``."""
    agent = make_agent()
    steps = [PlanStep("s1", "first"), PlanStep("s2", "second")]
    agent.planner.decompose = AsyncMock(return_value=steps)
    calls = {"n": 0}

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return StepResult(
                success=True,
                output="ran the gate",
                bash_commands=["python3.12 -m pytest tests/ -q"],
            )
        raise SDKError("crash mid-task")

    agent.executor.execute = AsyncMock(side_effect=fake_exec)

    with pytest.raises(SDKError):
        await agent.run("goal", task_id="cmdid")

    data = json.loads(mem_fs[_cp_path("cmdid", AGENT_DIR)])
    assert data["executed_commands"] == ["python3.12 -m pytest tests/ -q"]


@pytest.mark.asyncio
async def test_checkpoint_survives_replan(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    # Precondition: re-planning must be permitted for this scenario.
    agent.config.loop_max_replans = 1
    agent.planner.decompose = AsyncMock(return_value=[PlanStep("s1", "first")])
    agent.planner.replan = AsyncMock(return_value=[PlanStep("s2", "replanned")])

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        if step.step_id == "s1":
            return StepResult(success=False, output="", error="failed s1")
        raise SDKError("stop after the replan splice")

    agent.executor.execute = AsyncMock(side_effect=fake_exec)

    with pytest.raises(SDKError):
        await agent.run("goal", task_id="replanid")

    data = json.loads(mem_fs[_cp_path("replanid", AGENT_DIR)])
    ids = [s["step_id"] for s in data["steps"]]
    assert "s2" in ids
    assert data["replans"] == 1


@pytest.mark.asyncio
async def test_resume_skips_completed_steps_and_clears(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    agent.memory.save_checkpoint(
        TaskCheckpoint(
            task_id="resumeid",
            goal="resume goal",
            source="self_directed",
            branch="maestro/task/resumeid",
            steps=[
                PlanStep("s1", "one", status="done", result="done1"),
                PlanStep("s2", "two", status="pending"),
            ],
            errors=[],
            all_output=["done1"],
            artifacts_dir=str(AGENT_DIR / "artifacts" / "resumeid"),
            replans=0,
            timestamp=datetime.now(UTC).isoformat(),
        ),
    )
    # decompose must NOT run on resume — the checkpoint carries the steps.
    agent.planner.decompose = AsyncMock(
        side_effect=AssertionError("decompose called on resume"),
    )
    executed: list[str] = []

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        executed.append(step.step_id)
        return StepResult(success=True, output="ok")

    agent.executor.execute = AsyncMock(side_effect=fake_exec)
    agent._distill_findings = AsyncMock(return_value="findings")

    record = await agent.run("resume goal", task_id="resumeid")

    assert executed == ["s2"]
    assert record.outcome == "success"
    # Checkpoint cleared once the record is the source of truth.
    assert _cp_path("resumeid", AGENT_DIR) not in mem_fs
    agent._vcs.checkout.assert_any_call("maestro/task/resumeid")


@pytest.mark.asyncio
async def test_resume_missing_branch_starts_fresh(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    agent.memory.save_checkpoint(
        TaskCheckpoint(
            task_id="goneid",
            goal="g",
            branch="maestro/task/goneid",
            steps=[PlanStep("s1", "old", status="done")],
            timestamp=datetime.now(UTC).isoformat(),
        ),
    )
    # The checkpoint's branch no longer resolves.
    agent._vcs.rev_parse = MagicMock(return_value=None)
    agent.planner.decompose = AsyncMock(return_value=[PlanStep("n1", "new")])
    executed: list[str] = []

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        executed.append(step.step_id)
        return StepResult(success=True, output="ok")

    agent.executor.execute = AsyncMock(side_effect=fake_exec)
    agent._distill_findings = AsyncMock(return_value="findings")

    record = await agent.run("g", task_id="goneid")

    agent.planner.decompose.assert_awaited_once()
    assert executed == ["n1"]
    assert record.outcome == "success"
    assert _cp_path("goneid", AGENT_DIR) not in mem_fs


@pytest.mark.asyncio
async def test_resume_checkout_failure_falls_back_to_fresh(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    agent.memory.save_checkpoint(
        TaskCheckpoint(
            task_id="cfid",
            goal="g",
            source="self_directed",
            branch="maestro/task/cfid",
            steps=[PlanStep("s1", "one", status="pending")],
            timestamp=datetime.now(UTC).isoformat(),
        ),
    )
    # The branch resolves (resume path taken) but switching onto it fails;
    # the run must fall back to the current branch, not crash.
    agent._vcs.checkout = MagicMock(side_effect=VCSError("cannot switch"))

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        return StepResult(success=True, output="ok")

    agent.executor.execute = AsyncMock(side_effect=fake_exec)
    agent._distill_findings = AsyncMock(return_value="findings")

    record = await agent.run("g", task_id="cfid")

    assert record.outcome == "success"
    # branch_name fell back to None, so nothing was committed/kept.
    assert record.branch is None


@pytest.mark.asyncio
async def test_checkpoint_cleared_when_shutdown_fires_mid_task(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """084 (retiring 057): a shutdown firing mid-task no longer interrupts the
    run — every step completes, the task is persisted with a normal outcome,
    and the checkpoint is cleared on the record path (never left dangling)."""
    agent = make_agent()
    agent.planner.decompose = AsyncMock(
        return_value=[PlanStep("s1", "one"), PlanStep("s2", "two")],
    )

    async def fake_exec(step: PlanStep, *a: object, **k: object) -> StepResult:
        # SIGTERM arrives mid-task: the event is set during the first step.
        # run() no longer breaks on it, so the task runs to completion and a
        # checkpoint written between groups is cleared on the normal record.
        agent._ensure_shutdown_event().set()
        return StepResult(success=True, output="ok")

    agent.executor.execute = AsyncMock(side_effect=fake_exec)
    agent._distill_findings = AsyncMock(return_value="findings")

    record = await agent.run("goal", task_id="interruptid")

    assert record.outcome == "success"
    assert _cp_path("interruptid", AGENT_DIR) not in mem_fs


@pytest.mark.asyncio
async def test_resume_task_raises_without_checkpoint(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    with pytest.raises(ValueError, match="No checkpoint"):
        await agent.resume_task("missing")


@pytest.mark.asyncio
async def test_resume_task_raises_when_branch_gone(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    agent.memory.save_checkpoint(
        TaskCheckpoint(task_id="t1", goal="g", branch="maestro/task/t1"),
    )
    agent._vcs.rev_parse = MagicMock(return_value=None)
    with pytest.raises(ValueError, match="no resumable branch"):
        await agent.resume_task("t1")


@pytest.mark.asyncio
async def test_resume_task_delegates_to_run(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    agent = make_agent()
    agent.memory.save_checkpoint(
        TaskCheckpoint(
            task_id="t1",
            goal="resume me",
            source="self_directed",
            branch="maestro/task/t1",
            steps=[PlanStep("s1", "one", status="pending")],
            timestamp=datetime.now(UTC).isoformat(),
        ),
    )
    sentinel = MagicMock(name="record")
    agent.run = AsyncMock(return_value=sentinel)  # type: ignore[method-assign]

    result = await agent.resume_task("t1")

    assert result is sentinel
    # No linked_question and empty acceptance_criteria → both pass through as
    # None (the ``acceptance_criteria or None`` falsy branch). A checkpoint with
    # no conscious-access citation (proposal 130) threads both fields as None.
    agent.run.assert_awaited_once_with(
        goal="resume me", source="self_directed", task_id="t1",
        linked_question=None, acceptance_criteria=None,
        workspace_moment_id=None, workspace_item_ids=None,
    )


@pytest.mark.asyncio
async def test_resume_task_threads_classification_context(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """linked_question and acceptance_criteria survive a resume.

    Without this the resumed run would lose criteria-anchored MET/UNMET
    outcome classification and write no QuestionResolution for the open
    question it was probing.
    """
    agent = make_agent()
    agent.memory.save_checkpoint(
        TaskCheckpoint(
            task_id="t2",
            goal="resume me",
            source="self_directed",
            branch="maestro/task/t2",
            steps=[PlanStep("s1", "one", status="pending")],
            timestamp=datetime.now(UTC).isoformat(),
            linked_question="am I making progress?",
            acceptance_criteria=["the gate passes"],
            workspace_moment_id="cm-5",
            workspace_item_ids=["wi-1"],
        ),
    )
    sentinel = MagicMock(name="record")
    agent.run = AsyncMock(return_value=sentinel)  # type: ignore[method-assign]

    result = await agent.resume_task("t2")

    assert result is sentinel
    # The conscious-access citation (proposal 130) also survives the resume so
    # the resumed run's TaskRecord cites the same moment the original saw.
    agent.run.assert_awaited_once_with(
        goal="resume me", source="self_directed", task_id="t2",
        linked_question="am I making progress?",
        acceptance_criteria=["the gate passes"],
        workspace_moment_id="cm-5",
        workspace_item_ids=["wi-1"],
    )
