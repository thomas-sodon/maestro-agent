"""Collaborator-direct tests for ``maestro.loop.task_generation`` (068 step 2).

These import :class:`TaskGenerator` and the prior-task-context helper
directly from the new module, per the proposal's acceptance criterion.
Behavioural equivalence with the pre-extraction ``generate_task`` is
carried by the unchanged ``tests/test_agent*.py`` suites, which drive
the same body through the delegating shim — this file covers the
collaborator's own surface: construction, delegation, and the pre-call
short-circuit running without an SDK call.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from maestro import conscious_workspace as cw
from maestro.loop.task_generation import (
    _WORKSPACE_CITATION_PROMPT,
    TaskGenerator,
    _format_prior_task_context,
)
from maestro.memory import TaskRecord
from maestro.task_queue import QueuedTask

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent


def _broadcast_moment() -> cw.ConsciousMoment:
    """Assemble a real moment carrying one admitted, above-floor item."""
    item = cw.build_workspace_item(
        item_id="i1", source_kind="audit_finding", source_ref="ref",
        observed_at="2026-01-01T00:00:00+00:00", trust="mechanical_observation",
        confidence=0.5, expires_at=None, summary="a bounded summary",
    )
    return cw.assemble_moment(
        [item], assembled_at="2026-01-01T00:00:00+00:00",
        permission_summary="all", blind_spots=["gap"],
        predecessor_id=None, continuity="genesis",
    )


def _record(outcome: str, findings: str) -> TaskRecord:
    return TaskRecord(
        task_id="t1",
        goal="g",
        plan=[],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="summary",
        errors=[],
        duration_secs=0.0,
        timestamp="2026-06-11T00:00:00+00:00",
        key_findings=findings,
    )


def test_format_prior_task_context_imports_from_collaborator_module() -> None:
    """The helper is importable and functional at its new home."""
    block = _format_prior_task_context([_record("partial", "found X")], True)
    assert block is not None
    assert "partial — unfinished" in block
    assert "found X" in block
    assert _format_prior_task_context([], True) is None


@pytest.mark.asyncio
async def test_shim_delegates_to_collaborator(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``MaestroAgent.generate_task`` is a one-line delegation."""
    agent: MaestroAgent = make_agent()
    agent._task_generator.generate_task = AsyncMock()  # type: ignore[method-assign]
    await agent.generate_task(session=None)
    agent._task_generator.generate_task.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_quiet_skip_runs_without_sdk_call(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The collaborator's quiet-skip gate fires before any SDK call."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = True
    agent.task_queue.save([QueuedTask(goal="existing queued work")])

    generator = TaskGenerator(agent)
    with patch("maestro.loop.task_generation.query", new_callable=AsyncMock) as mock_query:
        await generator.generate_task(session=None)
    mock_query.assert_not_awaited()
    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].goal == "existing queued work"


@pytest.mark.asyncio
async def test_collaborator_enqueues_through_patched_agent_seam(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """``maestro.loop.task_generation.query`` patches keep intercepting the moved body."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    generator = TaskGenerator(agent)

    response = json.dumps({
        "prune": [],
        "edit": [],
        "enqueue": [{"goal": "Fix the journal cap behaviour in maestro/journal.py"}],
    })
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: json.loads(response)
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ) as mock_query:
        await generator.generate_task(session=None)
    mock_query.assert_awaited_once()
    queue = agent.task_queue.load()
    assert [q.goal for q in queue] == ["Fix the journal cap behaviour in maestro/journal.py"]


@pytest.mark.asyncio
async def test_generate_task_broadcasts_conscious_moment(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """The task-generation decision receives the shared conscious moment
    (proposal 130): flag-on prepends the rendered
    ``## Conscious moment (broadcast to task_generation)`` block to the prompt
    and records one ``task_generation`` receipt stamped with the moment's
    selection_hash; flag-off carries no block and the prompt is byte-identical to
    the pre-broadcast assembly. An empty curation response mutates no queue or
    self-model state, so the two runs assemble the same base prompt."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    moment = _broadcast_moment()

    response = json.dumps({"prune": [], "edit": [], "enqueue": []})

    def _result() -> AsyncMock:
        r = AsyncMock()
        r.parse_json = lambda: json.loads(response)
        return r

    # --- Flag off: no block prepended, no receipt recorded ---
    agent.config.conscious_workspace_broadcast_enabled = False
    agent._active_conscious_moment = moment
    agent._active_broadcast_receipts = []
    generator = TaskGenerator(agent)
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=_result(),
    ) as q_off:
        await generator.generate_task(session=None)
    prompt_off = q_off.call_args.kwargs["prompt"]
    assert "## Conscious moment" not in prompt_off
    assert "CONSCIOUS-ACCESS CITATION" not in prompt_off
    assert agent._active_broadcast_receipts == []

    # --- Flag on: block prepended at the front, one task_generation receipt ---
    agent.config.conscious_workspace_broadcast_enabled = True
    agent._active_conscious_moment = moment
    agent._active_broadcast_receipts = []
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=_result(),
    ) as q_on:
        await generator.generate_task(session=None)
    prompt_on = q_on.call_args.kwargs["prompt"]

    block, _ = cw.render_active_moment(moment, "task_generation")
    assert "## Conscious moment (broadcast to task_generation)" in block
    assert prompt_on.startswith(block + "\n\n")
    assert len(agent._active_broadcast_receipts) == 1
    receipt = agent._active_broadcast_receipts[0]
    assert receipt.consumer == "task_generation"
    assert receipt.moment_hash == moment.selection_hash
    # Flag-on prepends the moment block AND appends the advisory citation
    # instruction (proposal 130); the flag-off path carries neither, so it stays
    # byte-identical to today's assembly.
    assert _WORKSPACE_CITATION_PROMPT in prompt_on
    assert prompt_on == block + "\n\n" + prompt_off + _WORKSPACE_CITATION_PROMPT


@pytest.mark.asyncio
async def test_enqueue_stamps_resolve_filtered_workspace_citation(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag-on (proposal 130): each enqueued task carries the delivered moment
    id, its declared ``workspace_item_ids`` resolve-filtered against the visible
    admitted set (a hallucinated / non-visible id is dropped), and a task that
    declares no id but saw the moment carries ``[]`` (distinct from ``None``).
    The surviving cited task ids accumulate on ``_active_cited_task_ids``."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _broadcast_moment()  # one admitted item with item_id "i1"
    agent._active_conscious_moment = moment
    agent._active_broadcast_receipts = []
    agent._active_cited_task_ids = []

    response = json.dumps({
        "prune": [],
        "edit": [],
        "enqueue": [
            {
                "goal": "Fix the journal cap behaviour in maestro/journal.py",
                # "i1" is visible → kept; "i2" was never admitted → dropped;
                # the duplicate "i1" is de-duplicated.
                "workspace_item_ids": ["i1", "i2", "i1"],
            },
            {
                "goal": "Fix the memory bounds check in maestro/memory.py",
                # Saw the moment but cited nothing → stamped ``[]``.
            },
        ],
    })
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: json.loads(response)
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ):
        generator = TaskGenerator(agent)
        await generator.generate_task(session=None)

    queue = agent.task_queue.load()
    by_goal = {q.goal: q for q in queue}
    cited = by_goal["Fix the journal cap behaviour in maestro/journal.py"]
    uncited = by_goal["Fix the memory bounds check in maestro/memory.py"]
    assert cited.workspace_moment_id == moment.moment_id
    assert cited.workspace_item_ids == ["i1"]
    assert uncited.workspace_moment_id == moment.moment_id
    assert uncited.workspace_item_ids == []
    # Both surviving tasks cited the moment → both ids on the accumulator.
    assert set(agent._active_cited_task_ids) == {
        cited.task_id, uncited.task_id,
    }


@pytest.mark.asyncio
async def test_enqueue_tampered_citation_nulled_by_guard(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """Fail-closed guard (proposal 130): when the delivered visible set is more
    permissive than the moment's true visible admitted ids — a producer/validator
    disagreement the resolve filter alone would miss — the post-cap guard catches
    the stamped unknown id, nulls that task's provenance on the persisted queue
    row, and keeps it out of the cited-task accumulator, while a task citing a
    genuinely visible id survives. The task itself is never dropped."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.config.conscious_workspace_broadcast_enabled = True
    moment = _broadcast_moment()  # true visible ids == {"i1"}
    agent._active_conscious_moment = moment
    agent._active_broadcast_receipts = []
    agent._active_cited_task_ids = []

    # Hand the producer a too-permissive visible set including a ghost id the
    # moment never admitted, so the resolve filter keeps the ghost id and only the
    # guard (which re-derives visibility from the moment) rejects it.
    block, _ = cw.render_active_moment(moment, "task_generation")
    tampered_delivery = (block, moment, frozenset({"i1", "i-ghost"}))

    response = json.dumps({
        "prune": [],
        "edit": [],
        "enqueue": [
            {
                "goal": "Fix the journal cap behaviour in maestro/journal.py",
                "workspace_item_ids": ["i-ghost"],  # not truly visible → nulled
            },
            {
                "goal": "Fix the memory bounds check in maestro/memory.py",
                "workspace_item_ids": ["i1"],  # truly visible → survives
            },
        ],
    })
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: json.loads(response)
    with patch.object(
        agent, "_deliver_moment_to_consumer", return_value=tampered_delivery,
    ), patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ), caplog.at_level(logging.WARNING, logger="maestro"):
        generator = TaskGenerator(agent)
        await generator.generate_task(session=None)

    queue = agent.task_queue.load()
    by_goal = {q.goal: q for q in queue}
    tampered = by_goal["Fix the journal cap behaviour in maestro/journal.py"]
    valid = by_goal["Fix the memory bounds check in maestro/memory.py"]
    # The tampered citation is nulled on the persisted queue row.
    assert tampered.workspace_moment_id is None
    assert tampered.workspace_item_ids is None
    # The valid citation survives intact.
    assert valid.workspace_moment_id == moment.moment_id
    assert valid.workspace_item_ids == ["i1"]
    # Only the valid task reaches the cited-task accumulator.
    assert agent._active_cited_task_ids == [valid.task_id]
    assert "Nulling invalid conscious-access citation on task" in caplog.text
    assert "unknown_item" in caplog.text


@pytest.mark.asyncio
async def test_enqueue_leaves_citation_none_when_broadcast_off(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag-off: an enqueued task carries no citation (both fields ``None``) even
    if the model declares ``workspace_item_ids``, and the cited-task accumulator
    stays empty — the queue write is byte-identical to today's."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.config.conscious_workspace_broadcast_enabled = False
    agent._active_conscious_moment = _broadcast_moment()
    agent._active_cited_task_ids = []

    response = json.dumps({
        "prune": [],
        "edit": [],
        "enqueue": [
            {
                "goal": "Fix the journal cap behaviour in maestro/journal.py",
                "workspace_item_ids": ["i1"],
            },
        ],
    })
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: json.loads(response)
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ):
        generator = TaskGenerator(agent)
        await generator.generate_task(session=None)

    queue = agent.task_queue.load()
    assert len(queue) == 1
    assert queue[0].workspace_moment_id is None
    assert queue[0].workspace_item_ids is None
    assert agent._active_cited_task_ids == []


@pytest.mark.asyncio
async def test_generate_task_routes_unlanded_sensor_through_host_oracle(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The unlanded-remediation sensor in the task-generation prompt is fed the
    agent's host-first merge-truth oracle (proposal 080 Phase 1), not the bare
    git oracle — converging this call site on the host API alongside the
    contemplation block and the escalation bridge.

    It is also measured *exactly once* per iteration: the UNLANDED-REMEDIATION
    and PR-LAPS lines both derive from one ``measure_unlanded_remediations``
    pass, so the host (network) merge-truth oracle is not re-run a second time
    over the same window (proposal 080 step 4)."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    generator = TaskGenerator(agent)

    response = json.dumps({"prune": [], "edit": [], "enqueue": []})
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: json.loads(response)
    with patch(
        "maestro.loop.task_generation.measure_unlanded_remediations",
        return_value={},
    ) as mock_measure, patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ):
        await generator.generate_task(session=None)

    mock_measure.assert_called_once()
    assert (
        mock_measure.call_args.kwargs["is_pr_merged"]
        == agent._host_or_git_pr_merged
    )


async def _run_enqueue(agent: MaestroAgent, goal: str) -> None:
    """Drive the accept-loop once with a single enqueue *goal* (SDK mocked)."""
    agent.config.quiet_skip_enabled = False
    generator = TaskGenerator(agent)
    response = json.dumps(
        {"prune": [], "edit": [], "enqueue": [{"goal": goal}]},
    )
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: json.loads(response)
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ):
        await generator.generate_task(session=None)


@pytest.mark.asyncio
async def test_accept_site_records_premise_miss(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """An accepted goal referencing a nonexistent ``.py`` path records a miss.

    The premise-observation is written at accept time keyed by the queued
    task's id (issue #2304).
    """
    agent: MaestroAgent = make_agent()
    await _run_enqueue(agent, "Fix the retry backoff in maestro/does_not_exist.py")

    queue = agent.task_queue.load()
    assert len(queue) == 1
    rows = agent.premise_observations.read_all()
    assert len(rows) == 1
    assert rows[0].referenced == 1
    assert rows[0].missing == 1
    assert rows[0].task_id == queue[0].task_id


@pytest.mark.asyncio
async def test_accept_site_records_premise_hit(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """An accepted goal whose referenced file exists records a hit (missing 0)."""
    agent: MaestroAgent = make_agent()
    workspace = str(agent.config.workspace)
    mem_fs[f"{workspace}/maestro/journal.py"] = "x = 1\n"
    await _run_enqueue(agent, "Fix the journal cap behaviour in maestro/journal.py")

    rows = agent.premise_observations.read_all()
    assert len(rows) == 1
    assert rows[0].referenced == 1
    assert rows[0].missing == 0


@pytest.mark.asyncio
async def test_dropped_before_accept_records_no_premise(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A goal dropped by a gate before accept records nothing.

    ``Fix maestro/ghost.py for #42`` references a ``.py`` path *and* an issue,
    so the terrain-exhaustion gate drops it (missing file) before the accept
    site — proving the recorder sits after the gates, not before.
    """
    agent: MaestroAgent = make_agent()
    await _run_enqueue(agent, "Fix maestro/ghost.py for #42")

    assert agent.task_queue.load() == []
    assert agent.premise_observations.read_all() == []


async def _run_enqueue_many(agent: MaestroAgent, goals: list[str]) -> None:
    """Drive the accept-loop once with several enqueue *goals* (SDK mocked)."""
    agent.config.quiet_skip_enabled = False
    generator = TaskGenerator(agent)
    response = json.dumps(
        {"prune": [], "edit": [], "enqueue": [{"goal": g} for g in goals]},
    )
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: json.loads(response)
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ):
        await generator.generate_task(session=None)


@pytest.mark.asyncio
async def test_queue_cap_dropped_task_records_no_premise(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A task dropped by the queue cap records no premise observation (#2304).

    With ``task_queue_max`` at 1, enqueuing two accepted goals truncates the
    second away *after* the accept loop. The premise trace must record exactly
    one observation — for the surviving task only — never one for a task_id
    that never entered the persisted queue.
    """
    agent: MaestroAgent = make_agent()
    agent.config.task_queue_max = 1
    await _run_enqueue_many(
        agent,
        [
            "Fix the retry backoff in maestro/does_not_exist_a.py",
            "Fix the retry backoff in maestro/does_not_exist_b.py",
        ],
    )

    queue = agent.task_queue.load()
    assert len(queue) == 1
    rows = agent.premise_observations.read_all()
    assert len(rows) == 1
    # The single recorded observation belongs to the surviving queued task.
    assert rows[0].task_id == queue[0].task_id
    assert rows[0].referenced == 1
    assert rows[0].missing == 1


@pytest.mark.asyncio
async def test_live_cap_drop_records_no_premise_observation(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A new entry the *live* cap drops leaves no premise-observation row.

    The pre-call trim measured room against the snapshot; a concurrent enqueue
    during the priced call consumes it, so the rebase admits nothing. The
    bookkeeping keys off the admitted list, not ``accepted_new``, so a recorded
    task id always resolves to a really-queued task (#2304).
    """
    from maestro.task_queue import QueuedTask

    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.config.task_queue_max = 1
    agent.task_queue.save([])
    generator = TaskGenerator(agent)
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: {
        "prune": [], "edit": [],
        "enqueue": [
            {"goal": "Fix the retry backoff in maestro/does_not_exist.py"},
        ],
    }

    async def _enqueue_during_the_call(*args: object, **kwargs: object) -> object:
        agent.task_queue.save([
            *agent.task_queue.load(),
            QueuedTask(task_id="concurrent", goal="arrived mid-call"),
        ])
        return fake_result

    with patch(
        "maestro.loop.task_generation.query", AsyncMock(side_effect=_enqueue_during_the_call),
    ):
        await generator.generate_task(session=None)

    assert [q.task_id for q in agent.task_queue.load()] == ["concurrent"]
    assert agent.premise_observations.read_all() == []


# ──────────────────────────────────────────────────────────────────────
# Phantom-premise post-call content gate (issue #3666)
# ──────────────────────────────────────────────────────────────────────


#: A defect claim against a ``.py`` path deliberately absent from the tree.
_PHANTOM_GOAL = "Fix the broken swallow in maestro/nonexistent_phantom.py"


def _gate_rows(agent: MaestroAgent, mem_fs: dict[str, str]) -> list[dict]:
    """Return the gate-activation rows written so far (empty when unwritten)."""
    from maestro.audit_checks import GATE_ACTIVATIONS_FILENAME

    path = str(agent.config.memory_dir / GATE_ACTIVATIONS_FILENAME)
    return [
        json.loads(line)
        for line in mem_fs.get(path, "").strip().splitlines()
        if line
    ]


@pytest.mark.asyncio
async def test_phantom_premise_gate_rejects_enqueued_goal(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A defect claim about an absent path is dropped, with an activation row."""
    agent: MaestroAgent = make_agent()
    agent.config.phantom_premise_gate_enabled = True

    await _run_enqueue(agent, _PHANTOM_GOAL)

    assert not any("nonexistent_phantom" in q.goal for q in agent.task_queue.load())
    rows = _gate_rows(agent, mem_fs)
    assert any(r["gate"] == "phantom_premise" for r in rows)


@pytest.mark.asyncio
async def test_phantom_premise_gate_disabled_admits_goal(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """Flag off — the identical goal is admitted, and no row is written."""
    agent: MaestroAgent = make_agent()
    agent.config.phantom_premise_gate_enabled = False

    await _run_enqueue(agent, _PHANTOM_GOAL)

    assert any("nonexistent_phantom" in q.goal for q in agent.task_queue.load())
    rows = _gate_rows(agent, mem_fs)
    assert not any(r["gate"] == "phantom_premise" for r in rows)


@pytest.mark.asyncio
async def test_phantom_premise_gate_admits_creation_goal(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A goal whose framing is to *create* the absent path stays admitted."""
    agent: MaestroAgent = make_agent()
    agent.config.phantom_premise_gate_enabled = True

    await _run_enqueue(
        agent,
        "Create maestro/nonexistent_phantom.py to fix the missing-refusal bug",
    )

    assert any("nonexistent_phantom" in q.goal for q in agent.task_queue.load())


@pytest.mark.asyncio
async def test_phantom_premise_gate_admits_present_but_unparseable_path(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A defect claim about a file that *exists* but fails to parse is admitted.

    ``_missing_file_references`` reports absent **and** present-but-unparseable
    paths; only the former is a phantom premise. "Fix the syntax error in
    <existing file>" is a true premise, so the gate must not refuse it.
    """
    from maestro.loop.task_generation import _vet_goal_content

    agent: MaestroAgent = make_agent()
    agent.config.phantom_premise_gate_enabled = True

    with patch.object(
        agent, "_missing_file_references", return_value=["maestro/unparseable.py"],
    ), patch.object(agent, "_file_reference_exists", return_value=True):
        verdict = _vet_goal_content(
            agent, "Fix the broken parse in maestro/unparseable.py",
        )

    assert verdict is None


@pytest.mark.asyncio
async def test_phantom_premise_gate_refuses_edit_rewrite(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """The edit pass declines a rewrite into a phantom-premise goal (#2939)."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    agent.config.phantom_premise_gate_enabled = True
    original_goal = "Update the gate ordering in maestro/task_gates.py"
    agent.task_queue.save([
        QueuedTask(task_id="phantom-edit-1", goal=original_goal),
    ])

    generator = TaskGenerator(agent)
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: {
        "prune": [],
        "edit": [{"task_id": "phantom-edit-1", "goal": _PHANTOM_GOAL}],
        "enqueue": [],
    }
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ):
        await generator.generate_task(session=None)

    rewritten = [q for q in agent.task_queue.load() if q.task_id == "phantom-edit-1"]
    assert len(rewritten) == 1
    assert rewritten[0].goal == original_goal


_FAILED_TOKEN = "maestro/task/16db56580b86"


def _record_failed_branch(agent: MaestroAgent) -> None:
    """Seed one ``partial`` task-history row on :data:`_FAILED_TOKEN`."""
    agent.memory.record_task(TaskRecord(
        task_id="16db56580b86",
        goal="wire X",
        plan=[],
        outcome="partial",
        result_summary="",
        errors=[],
        duration_secs=0.0,
        timestamp="2026-09-24T00:00:00+00:00",
        source="self_directed",
        branch=_FAILED_TOKEN,
    ))


@pytest.mark.asyncio
async def test_failed_branch_target_gate_refuses_edit_rewrite(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """The edit pass declines a rewrite that re-attacks a failed branch (#4049)."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    _record_failed_branch(agent)
    original_goal = "Update the gate ordering in maestro/task_gates.py"
    agent.task_queue.save([QueuedTask(task_id="fb-edit-1", goal=original_goal)])

    generator = TaskGenerator(agent)
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: {
        "prune": [],
        "edit": [{
            "task_id": "fb-edit-1",
            "goal": f"Fix the retry loop in maestro/agent.py from {_FAILED_TOKEN}",
        }],
        "enqueue": [],
    }
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ):
        await generator.generate_task(session=None)

    rewritten = [q for q in agent.task_queue.load() if q.task_id == "fb-edit-1"]
    assert len(rewritten) == 1
    assert rewritten[0].goal == original_goal
    rows = _gate_rows(agent, mem_fs)
    assert any(r["gate"] == "failed_branch_target" for r in rows)


@pytest.mark.asyncio
async def test_failed_branch_target_gate_refuses_rationale_only_edit(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """A rationale-only edit naming a failed branch keeps the old rationale."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    _record_failed_branch(agent)
    goal = "Update the gate ordering in maestro/task_gates.py"
    agent.task_queue.save([
        QueuedTask(task_id="fb-edit-2", goal=goal, rationale="old why"),
    ])

    generator = TaskGenerator(agent)
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: {
        "prune": [],
        "edit": [{
            "task_id": "fb-edit-2",
            "goal": goal,
            "rationale": f"finishes {_FAILED_TOKEN}",
        }],
        "enqueue": [],
    }
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ):
        await generator.generate_task(session=None)

    kept = [q for q in agent.task_queue.load() if q.task_id == "fb-edit-2"]
    assert len(kept) == 1
    assert kept[0].rationale == "old why"
    rows = _gate_rows(agent, mem_fs)
    assert any(r["gate"] == "failed_branch_target" for r in rows)


@pytest.mark.asyncio
async def test_failed_branch_target_gate_refuses_rationale_only_enqueue(
    make_agent,  # type: ignore[no-untyped-def]
    mem_fs: dict[str, str],
) -> None:
    """An enqueue naming the failed branch only in its rationale is refused."""
    agent: MaestroAgent = make_agent()
    agent.config.quiet_skip_enabled = False
    _record_failed_branch(agent)
    goal = "Fix the retry loop in maestro/agent.py"

    generator = TaskGenerator(agent)
    fake_result = AsyncMock()
    fake_result.parse_json = lambda: {
        "prune": [],
        "edit": [],
        "enqueue": [{"goal": goal, "rationale": f"finishes {_FAILED_TOKEN}"}],
    }
    with patch(
        "maestro.loop.task_generation.query", new_callable=AsyncMock, return_value=fake_result,
    ):
        await generator.generate_task(session=None)

    assert not any(q.goal == goal for q in agent.task_queue.load())
    rows = _gate_rows(agent, mem_fs)
    assert any(r["gate"] == "failed_branch_target" for r in rows)
