"""Task-scoped cost attribution (#4066).

``begin_task_run`` used to stamp ``task_id`` on the per-iteration usage
accumulator, so ``cost_per_task`` billed the *whole* iteration's spend —
contemplate, generate_task, evolve, dream — to the task. The accumulator now
carries a task-scoped slice (``task_cost_usd``) credited only while the task
run's scope is open, and per-task queries sum that slice.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from maestro.cost_tracking import cost_for_task, cost_per_task
from maestro.loop.lifecycle import begin_task_run, teardown_task_run
from maestro.sdk import IterationUsage, QueryUsage, write_iteration_usage

USAGE_PATH = Path("/fake/task_cost/usage.jsonl")


def _stub_run_collaborators(agent) -> None:  # type: ignore[no-untyped-def]
    agent._install_signal_handlers = MagicMock()
    agent._build_identity_context = MagicMock(return_value="identity")
    agent._teardown_task_branch = MagicMock()


@pytest.mark.asyncio
async def test_cost_for_task_excludes_non_task_phase_spend(
    make_agent, mem_fs: dict[str, str],  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    _stub_run_collaborators(agent)
    agent._iteration_usage = IterationUsage(iteration=1, started_at="t")

    agent._on_query_usage(QueryUsage(total_cost_usd=1.0, caller="contemplate"))
    task_id, _start, _identity = begin_task_run(
        agent, goal="g", task_id="task-abc",
    )
    agent._on_query_usage(QueryUsage(total_cost_usd=0.25, caller="execute_step"))
    await teardown_task_run(
        agent, run_session=None, branch_name=None, kept_branch=None,
        preserve_branch=False,
    )
    agent._on_query_usage(QueryUsage(total_cost_usd=2.0, caller="evolve"))

    write_iteration_usage(USAGE_PATH, agent._iteration_usage)

    assert cost_for_task(USAGE_PATH, task_id) == 0.25
    assert agent._iteration_usage.total_cost_usd == 3.25
    assert agent._iteration_usage.calls == 3
    assert agent._iteration_usage.task_calls == 1


def test_unmetered_in_run_call_marks_task_unmetered(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    _stub_run_collaborators(agent)
    agent._iteration_usage = IterationUsage(iteration=1, started_at="t")

    begin_task_run(agent, goal="g", task_id="t1")
    agent._on_query_usage(QueryUsage(total_cost_usd=None, unmetered=True))

    assert agent._iteration_usage.task_unmetered is True
    assert agent._iteration_usage.task_cost_usd == 0.0


def test_out_of_scope_unmetered_call_leaves_task_metered(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    agent._iteration_usage = IterationUsage(iteration=1, started_at="t")

    agent._on_query_usage(QueryUsage(total_cost_usd=None, unmetered=True))

    assert agent._iteration_usage.unmetered is True
    assert agent._iteration_usage.task_unmetered is False
    assert agent._iteration_usage.task_calls == 0


def test_begin_task_run_refuses_nested_scope(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    _stub_run_collaborators(agent)
    agent._iteration_usage = IterationUsage(iteration=1, started_at="t")
    begin_task_run(agent, goal="g", task_id="first")

    with pytest.raises(RuntimeError, match="already open"):
        begin_task_run(agent, goal="g2", task_id="second")

    assert agent._iteration_usage.task_id == "first"


@pytest.mark.asyncio
async def test_teardown_without_accumulator_is_safe(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    agent = make_agent()
    _stub_run_collaborators(agent)
    agent._iteration_usage = None

    await teardown_task_run(
        agent, run_session=None, branch_name=None, kept_branch=None,
        preserve_branch=False,
    )

    assert agent._running is False


def test_task_fields_round_trip_and_scope_flag_not_serialized() -> None:
    iu = IterationUsage(
        iteration=2, started_at="t", task_cost_usd=0.5, task_calls=3,
        task_unmetered=True,
    )
    iu._task_scope_active = True

    d = iu.to_dict()
    restored = IterationUsage.from_dict(d)

    assert "_task_scope_active" not in d
    assert restored.task_cost_usd == 0.5
    assert restored.task_calls == 3
    assert restored.task_unmetered is True
    assert restored._task_scope_active is False


def test_cost_per_task_sums_task_scoped_field() -> None:
    rows: list[dict[str, object]] = [
        {"task_id": "a", "total_cost_usd": 3.0, "task_cost_usd": 0.5},
        {"task_id": "a", "total_cost_usd": 1.0, "task_cost_usd": 0.25},
    ]

    assert cost_per_task(rows) == {"a": 0.75}


def test_cost_per_task_legacy_row_falls_back_to_total() -> None:
    rows: list[dict[str, object]] = [{"task_id": "a", "total_cost_usd": 3.0}]

    assert cost_per_task(rows) == {"a": 3.0}


def test_cost_per_task_honours_task_unmetered() -> None:
    rows: list[dict[str, object]] = [
        {
            "task_id": "a", "total_cost_usd": 3.0, "unmetered": False,
            "task_cost_usd": 0.4, "task_unmetered": True,
        },
    ]

    assert cost_per_task(rows) == {"a": 0.0}


def test_cost_per_task_untagged_row_sums_total_even_with_task_field() -> None:
    rows: list[dict[str, object]] = [
        {"task_id": None, "total_cost_usd": 2.0, "task_cost_usd": 0.0},
    ]

    assert cost_per_task(rows) == {None: 2.0}
