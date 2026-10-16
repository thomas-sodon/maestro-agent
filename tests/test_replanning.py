"""Tests for adaptive re-planning on step failure."""

from __future__ import annotations

import ast
import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from maestro.config import Config
from maestro.config_settings import _SECTION_MODELS
from maestro.memory import PlanStep, TaskRecord
from maestro.planner import (
    _REPLAN_COMPLETED_TEXT_MAX_CHARS,
    Planner,
    _parse_steps,
)
from tests.conftest import MockQueryResult

# Source file under test, resolved the same way the AST-based structural
# tests do (see ``tests/test_no_silenced_exceptions.py``).
_PLANNER_SOURCE = Path(__file__).resolve().parent.parent / "maestro" / "planner.py"

# --- _parse_steps() tests ---


def test_parse_steps_valid_list() -> None:
    data = [{"description": "Step A"}, {"description": "Step B"}]
    steps = _parse_steps(data)
    assert len(steps) == 2
    assert steps[0].description == "Step A"
    assert steps[1].description == "Step B"


def test_parse_steps_dict_wrapped() -> None:
    data = {"steps": [{"description": "X"}]}
    steps = _parse_steps(data)
    assert len(steps) == 1
    assert steps[0].description == "X"


def test_parse_steps_prefers_steps_key() -> None:
    data = {"reasoning": ["r1"], "steps": [{"description": "real step"}]}
    steps = _parse_steps(data)
    assert len(steps) == 1
    assert steps[0].description == "real step"


def test_parse_steps_non_list_steps_key_yields_no_steps() -> None:
    """An explicit non-list "steps" value expresses the model's intent;
    a sibling list must not be mistaken for steps."""
    data = {"steps": "cannot recover", "reasoning": ["a", "b"]}
    assert _parse_steps(data) == []


def test_parse_steps_steps_dict_value_unwraps_first_list() -> None:
    """A dict under "steps" unwraps to its first list value, matching the
    pre-centralization call-site composition."""
    data = {"steps": {"items": [{"description": "nested"}]}}
    steps = _parse_steps(data)
    assert len(steps) == 1
    assert steps[0].description == "nested"


def test_parse_steps_dict_no_list_returns_empty() -> None:
    data = {"key": "value"}
    assert _parse_steps(data) == []


def test_parse_steps_non_list_non_dict_returns_empty() -> None:
    assert _parse_steps(42) == []
    assert _parse_steps("hello") == []


def test_parse_steps_empty_list() -> None:
    assert _parse_steps([]) == []


def test_parse_steps_skips_none_and_empty() -> None:
    data = [None, {"description": ""}, {"description": "  "}, {"description": "ok"}]
    steps = _parse_steps(data)
    assert len(steps) == 1
    assert steps[0].description == "ok"


def test_parse_steps_string_items() -> None:
    data = ["Do this", "Do that"]
    steps = _parse_steps(data)
    assert len(steps) == 2
    assert steps[0].description == "Do this"


def test_parse_steps_truncates_long_descriptions() -> None:
    data = [{"description": "A" * 5000}]
    steps = _parse_steps(data)
    assert len(steps[0].description) == 2000


# --- _parse_steps dependency tests ---


def test_parse_steps_with_id_and_depends_on() -> None:
    data = [
        {"id": "s1", "description": "First"},
        {"id": "s2", "description": "Second", "depends_on": ["s1"]},
    ]
    steps = _parse_steps(data)
    assert len(steps) == 2
    assert steps[0].step_id == "s1"
    assert steps[0].depends_on == []
    assert steps[1].step_id == "s2"
    assert steps[1].depends_on == ["s1"]


def test_parse_steps_id_without_depends_on() -> None:
    data = [{"id": "x", "description": "Task"}]
    steps = _parse_steps(data)
    assert steps[0].step_id == "x"
    assert steps[0].depends_on == []


def test_parse_steps_duplicate_id_gets_uuid() -> None:
    data = [
        {"id": "dup", "description": "First"},
        {"id": "dup", "description": "Second", "depends_on": ["dup"]},
    ]
    steps = _parse_steps(data)
    assert steps[0].step_id == "dup"
    assert steps[1].step_id != "dup"  # UUID fallback
    # "dup" names the first step, which kept the id, so the dependency
    # resolves to it.
    assert steps[1].depends_on == ["dup"]


def test_parse_steps_nonexistent_dep_dropped() -> None:
    data = [{"id": "a", "description": "Task", "depends_on": ["ghost"]}]
    steps = _parse_steps(data)
    assert steps[0].depends_on == []


def test_parse_steps_self_ref_dep_dropped() -> None:
    data = [{"id": "a", "description": "Task", "depends_on": ["a"]}]
    steps = _parse_steps(data)
    assert steps[0].depends_on == []


def test_parse_steps_old_format_no_deps() -> None:
    """Old-format responses (no id/depends_on) parse with empty deps."""
    data = [{"description": "Step A"}, {"description": "Step B"}]
    steps = _parse_steps(data)
    for s in steps:
        assert s.depends_on == []


def test_parse_steps_has_no_identity_id_mapping() -> None:
    """Regression: the dead ``llm_id_to_step_id`` identity map is gone.

    Every entry it stored satisfied ``key == value`` (the write only fired
    when the LLM id was adopted verbatim), so ``llm_id_to_step_id.get(dep,
    dep)`` returned ``dep`` on every path — pure indirection over the
    ``valid_ids`` membership test. Assert the name is absent from
    ``maestro/planner.py`` so the dead mapping cannot silently return.
    """
    tree = ast.parse(
        _PLANNER_SOURCE.read_text(encoding="utf-8"), filename=str(_PLANNER_SOURCE)
    )
    referenced = [
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "llm_id_to_step_id"
    ]
    assert referenced == []


@pytest.mark.parametrize(
    ("data", "step_index", "expected_depends_on"),
    [
        pytest.param(
            [
                {"id": "s1", "description": "First"},
                {"id": "s2", "description": "Second", "depends_on": ["s1"]},
            ],
            1,
            ["s1"],
            id="adopted-id",
        ),
        pytest.param(
            [
                {"id": "dup", "description": "First"},
                {"id": "dup", "description": "Second", "depends_on": ["dup"]},
            ],
            1,
            ["dup"],
            id="duplicate-id-uuid-fallback",
        ),
        pytest.param(
            [{"id": "a", "description": "Task", "depends_on": ["ghost"]}],
            0,
            [],
            id="nonexistent-id",
        ),
        pytest.param(
            [{"id": "a", "description": "Task", "depends_on": ["a"]}],
            0,
            [],
            id="self-reference",
        ),
    ],
)
def test_parse_steps_dependency_resolution_equivalence(
    data: object, step_index: int, expected_depends_on: list[str]
) -> None:
    """Pin the four dependency-resolution outcomes so the identity-map
    removal is provably behavior-preserving: a dependency on an adopted id
    keeps it, one naming a duplicated id resolves to the first step (which
    retained the id), and nonexistent / self references are dropped.
    """
    steps = _parse_steps(data)
    assert steps[step_index].depends_on == expected_depends_on


# --- Planner.replan() tests ---


@pytest.mark.asyncio
async def test_replan_generates_new_steps() -> None:
    planner = Planner()
    response = json.dumps([
        {"description": "Workaround step 1"},
        {"description": "Workaround step 2"},
    ])
    completed = [PlanStep(step_id="s1", description="Read file", status="done", result="ok")]
    failed = PlanStep(step_id="s2", description="Write file", status="failed", result="Permission denied")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        steps = await planner.replan(
            goal="Fix the bug",
            identity_context="ctx",
            completed_steps=completed,
            failed_step=failed,
        )

    assert len(steps) == 2
    assert steps[0].description == "Workaround step 1"
    for s in steps:
        assert s.step_id != ""
        assert s.status == "pending"


@pytest.mark.asyncio
async def test_replan_includes_context_in_prompt() -> None:
    planner = Planner()
    response = json.dumps([{"description": "Recovery"}])
    completed = [PlanStep(step_id="c1", description="First step done", status="done", result="output")]
    failed = PlanStep(step_id="f1", description="Second step broke", status="failed", result="Error: timeout")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        await planner.replan(
            goal="Deploy service",
            identity_context="ctx",
            completed_steps=completed,
            failed_step=failed,
            context="Production env",
        )

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "Deploy service" in prompt
    assert "First step done" in prompt
    assert "DONE" in prompt
    assert "Second step broke" in prompt
    assert "FAILED" in prompt
    assert "Error: timeout" in prompt
    assert "Production env" in prompt


@pytest.mark.asyncio
async def test_replan_bad_json_returns_empty() -> None:
    planner = Planner()
    completed = []
    failed = PlanStep(step_id="f1", description="Fail", status="failed", result="err")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="Not JSON at all")
        steps = await planner.replan(
            goal="goal",
            identity_context="ctx",
            completed_steps=completed,
            failed_step=failed,
        )

    assert steps == []


@pytest.mark.asyncio
async def test_replan_empty_array_returns_empty() -> None:
    planner = Planner()
    failed = PlanStep(step_id="f1", description="Fail", status="failed", result="err")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="[]")
        steps = await planner.replan(
            goal="goal",
            identity_context="ctx",
            completed_steps=[],
            failed_step=failed,
        )

    assert steps == []


@pytest.mark.asyncio
async def test_replan_completed_text_total_cap() -> None:
    """The completed-steps block is bounded regardless of step count."""
    planner = Planner()
    completed = [
        PlanStep(
            step_id=f"s{i}",
            description=f"Completed step number {i:02d}",
            status="done",
            result=f"result-{i:02d}-" + "x" * 200,
        )
        for i in range(30)
    ]
    failed = PlanStep(step_id="f1", description="Broken step", status="failed", result="err")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="[]")
        await planner.replan(
            goal="goal",
            identity_context="ctx",
            completed_steps=completed,
            failed_step=failed,
        )

    prompt = mock_q.call_args.kwargs["prompt"]
    block = prompt.split("Completed steps:\n")[1].split("\n\nFailed step:")[0]
    lines = block.split("\n")
    marker = lines[0]
    assert marker.startswith("- [... ")
    assert marker.endswith(" earlier completed steps omitted ...]")
    # The block is at most the total cap plus the omission-marker line.
    assert len(block) <= _REPLAN_COMPLETED_TEXT_MAX_CHARS + len(marker) + 1
    # Most recent step kept, oldest elided; chronological order preserved.
    assert "Completed step number 29" in block
    assert "Completed step number 00" not in block
    assert lines[-1].startswith("- [DONE] Completed step number 29")
    # The marker names the actual elided count.
    omitted = 30 - (len(lines) - 1)
    assert omitted > 0
    assert marker == f"- [... {omitted} earlier completed steps omitted ...]"


@pytest.mark.asyncio
async def test_replan_completed_text_under_cap_unmarked() -> None:
    """A small completed-steps list emits no omission marker and loses no steps."""
    planner = Planner()
    completed = [
        PlanStep(step_id="c1", description="Small step one", status="done", result="ok1"),
        PlanStep(step_id="c2", description="Small step two", status="done", result="ok2"),
    ]
    failed = PlanStep(step_id="f1", description="Broken step", status="failed", result="err")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="[]")
        await planner.replan(
            goal="goal",
            identity_context="ctx",
            completed_steps=completed,
            failed_step=failed,
        )

    prompt = mock_q.call_args.kwargs["prompt"]
    block = prompt.split("Completed steps:\n")[1].split("\n\nFailed step:")[0]
    assert "omitted" not in block
    assert "Small step one" in block
    assert "Small step two" in block


@pytest.mark.asyncio
async def test_replan_no_completed_steps_shows_none() -> None:
    planner = Planner()
    failed = PlanStep(step_id="f1", description="Fail", status="failed", result="err")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps([{"description": "Retry"}]))
        await planner.replan(
            goal="goal",
            identity_context="ctx",
            completed_steps=[],
            failed_step=failed,
        )

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "(none)" in prompt


# --- agent.run() replanning integration tests ---


@pytest.mark.asyncio
async def test_run_replans_on_step_failure(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()

    plan_response = json.dumps([
        {"description": "Step 1"},
        {"description": "Step 2 will fail"},
        {"description": "Step 3 original"},
    ])
    replan_response = json.dumps([
        {"description": "Recovery step"},
    ])

    exec_call = 0

    async def exec_side_effect(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal exec_call
        exec_call += 1
        if exec_call == 1:
            return MockQueryResult(response="Step 1 done")
        if exec_call == 2:
            raise RuntimeError("Step 2 failed")
        return MockQueryResult(response="Recovery done")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=exec_side_effect), \
         patch("maestro.agent.query", new_callable=AsyncMock) as mock_distill:
        mock_plan.side_effect = [
            MockQueryResult(response=plan_response),
            MockQueryResult(response=replan_response),
        ]
        mock_distill.return_value = MockQueryResult(response="findings")

        record = await agent.run("Test replanning")

    assert record.replans == 1
    assert record.outcome == "partial"
    # Plan should have: Step 1 (done), Step 2 (failed), Recovery step (done)
    assert len(record.plan) == 3
    assert record.plan[0].status == "done"
    assert record.plan[1].status == "failed"
    assert record.plan[2].description == "Recovery step"
    assert record.plan[2].status == "done"


@pytest.mark.asyncio
async def test_run_no_replan_when_max_zero(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.loop_max_replans = 0

    plan_response = json.dumps([
        {"description": "Step 1 will fail"},
        {"description": "Step 2"},
    ])

    async def exec_side_effect(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("fail")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=exec_side_effect), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.return_value = MockQueryResult(response=plan_response)

        record = await agent.run("No replan goal")

    assert record.replans == 0
    # planner.query called once for decompose, never for replan
    mock_plan.assert_called_once()


@pytest.mark.asyncio
async def test_run_respects_max_replans_limit(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.loop_max_replans = 1

    plan_response = json.dumps([
        {"description": "Step 1 fail"},
        {"description": "Step 2"},
    ])
    replan_response = json.dumps([
        {"description": "Recovery 1 fail"},
    ])

    async def exec_side_effect(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("all fail")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=exec_side_effect), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.side_effect = [
            MockQueryResult(response=plan_response),
            MockQueryResult(response=replan_response),
        ]

        record = await agent.run("Limited replans")

    assert record.replans == 1
    # decompose + 1 replan = 2 calls to planner.query
    assert mock_plan.call_count == 2


@pytest.mark.asyncio
async def test_run_replan_empty_continues(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.config.loop_max_replans = 1

    plan_response = json.dumps([
        {"description": "Step 1 fail"},
        {"description": "Step 2"},
    ])

    exec_call = 0

    async def exec_side_effect(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal exec_call
        exec_call += 1
        if exec_call == 1:
            raise RuntimeError("fail")
        return MockQueryResult(response="Step 2 done")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=exec_side_effect), \
         patch("maestro.agent.query", new_callable=AsyncMock):
        mock_plan.side_effect = [
            MockQueryResult(response=plan_response),
            MockQueryResult(response="[]"),  # replan returns empty
        ]

        record = await agent.run("Replan empty")

    # An empty replan reply still debits the budget (#451): the *attempt* is
    # counted even though no new steps were spliced in, so a persistently
    # empty replan cannot re-fire once per failing group.
    assert record.replans == 1
    # Original Step 2 should still execute (the loop still continues past the
    # empty replan — only the budget count changed).
    assert len(record.plan) == 2
    assert record.plan[1].status == "done"


@pytest.mark.asyncio
async def test_run_replan_exception_propagates(make_agent) -> None:  # type: ignore[no-untyped-def]
    """When planner.replan() raises, the exception propagates out of run()."""
    agent = make_agent()
    agent.config.loop_max_replans = 1

    plan_response = json.dumps([
        {"description": "Step 1 fail"},
        {"description": "Step 2"},
    ])

    exec_call = 0

    async def exec_side_effect(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal exec_call
        exec_call += 1
        if exec_call == 1:
            raise RuntimeError("fail")
        return MockQueryResult(response="Step 2 done")

    with patch("maestro.planner.query", new_callable=AsyncMock) as mock_plan, \
         patch("maestro.executor.query", side_effect=exec_side_effect), \
         patch("maestro.agent.query", new_callable=AsyncMock), \
         pytest.raises(RuntimeError, match="replan SDK crashed"):
        mock_plan.side_effect = [
            MockQueryResult(response=plan_response),
            RuntimeError("replan SDK crashed"),
        ]

        await agent.run("Replan crash")


# --- TaskRecord.replans field tests ---


def test_task_record_replans_default() -> None:
    record = TaskRecord(
        task_id="t1", goal="g", plan=[], outcome="success",
        result_summary="ok", errors=[], duration_secs=1.0, timestamp="ts",
    )
    assert record.replans == 0


def test_task_record_replans_serialization() -> None:
    record = TaskRecord(
        task_id="t1", goal="g", plan=[], outcome="success",
        result_summary="ok", errors=[], duration_secs=1.0, timestamp="ts",
        replans=2,
    )
    d = asdict(record)
    assert d["replans"] == 2
    loaded = json.loads(json.dumps(d))
    assert loaded["replans"] == 2


# --- Config tests ---


def test_config_loop_max_replans_default() -> None:
    config = Config()
    assert config.loop_max_replans == 1


def test_config_loop_max_replans_zero_valid() -> None:
    Config(loop_max_replans=0).validate()


# ``test_config_negative_loop_max_replans_rejected`` was retired in proposal 106:
# the retired ``Config.validate`` flat-scalar ladder no longer rejects a negative
# ``loop_max_replans`` on direct construction. That guard now lives on the
# ``_LoopSettings`` ``Field(ge=0)`` (the ``max_replans`` alias) and is pinned by
# ``tests/test_config_settings_parity.py::TestConstraintRejects``'s per-section
# backfill.


def test_toml_max_replans_key_maps_to_loop_max_replans_field() -> None:
    """The ``[loop] max_replans`` TOML key populates ``loop_max_replans``.

    Locks in the #452 rename invariant: the user-facing TOML key stays
    ``max_replans`` while the ``Config`` field is renamed to ``loop_max_replans``.
    The section model's ``Field(alias=...)`` — not the key — carries the rename, so
    an operator's ``maestro.toml`` still writes ``max_replans`` and loads into the
    renamed field.
    """
    field = _SECTION_MODELS["loop"].model_fields["loop_max_replans"]
    assert field.alias == "max_replans"


# --- Load-time normalisation ---


def test_legacy_task_record_gets_replans_default_on_read(
    mem_fs: dict[str, str],
) -> None:
    """A legacy TaskRecord without ``replans`` loads with ``replans=0``.

    ``ensure_schema`` advances the fingerprint but performs no rewrite (proposal
    106); the loader fills the field's default on read.
    """
    from maestro.memory import Memory
    from maestro.migration import ensure_schema

    d = Path("/fake/migration_test")
    task = {
        "task_id": "t1", "goal": "test", "plan": [], "outcome": "success",
        "result_summary": "ok", "errors": [], "duration_secs": 1.0,
        "timestamp": "ts1",
    }
    # Old record without the replans field, plus a stale fingerprint.
    mem_fs[str(d / "task_history.jsonl")] = json.dumps(task) + "\n"
    mem_fs[str(d / "schema_version.json")] = json.dumps({"fingerprint": "old"})

    ensure_schema(d)

    # No rewrite: the on-disk row still omits replans...
    raw = json.loads(mem_fs[str(d / "task_history.jsonl")].strip())
    assert "replans" not in raw
    # ...yet the loader fills the default on read.
    [loaded] = Memory(d)._load_all_tasks()
    assert loaded.replans == 0


def test_legacy_plan_step_gets_depends_on_default_on_read(
    mem_fs: dict[str, str],
) -> None:
    """A legacy PlanStep without ``depends_on`` loads with ``depends_on=[]``.

    ``ensure_schema`` advances the fingerprint but performs no rewrite (proposal
    106); the loader fills the field's default on read.
    """
    from maestro.memory import Memory
    from maestro.migration import ensure_schema

    d = Path("/fake/migration_dep_test")
    task = {
        "task_id": "t1", "goal": "test",
        "plan": [{"step_id": "s1", "description": "Do thing", "status": "done", "result": "ok"}],
        "outcome": "success", "result_summary": "ok", "errors": [],
        "duration_secs": 1.0, "timestamp": "ts1",
    }
    mem_fs[str(d / "task_history.jsonl")] = json.dumps(task) + "\n"
    mem_fs[str(d / "schema_version.json")] = json.dumps({"fingerprint": "old"})

    ensure_schema(d)

    # No rewrite: the on-disk plan step still omits depends_on...
    raw = json.loads(mem_fs[str(d / "task_history.jsonl")].strip())
    assert "depends_on" not in raw["plan"][0]
    # ...yet the loader fills the default on read.
    [loaded] = Memory(d)._load_all_tasks()
    assert loaded.plan[0].depends_on == []
