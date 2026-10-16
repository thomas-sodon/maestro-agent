"""Component tests for agent."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from maestro.evolution import QualityGateResult
from maestro.sdk import QueryResult
from tests.component.conftest import (
    make_quality_gates_mock,
    write_evolution_plan,
    write_self_model,
    write_task_record,
)
from tests.component.sdk_mock import SDKMock
from tests.support.gap_fuel import fuel_patch

_OK_REVIEW = QualityGateResult(
    passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
    raw_output="Ruff: clean\n\nMypy: clean\n\nTests: clean",
)

# ---- contemplate ----


@pytest.mark.asyncio
async def test_contemplate_updates_self_model(make_agent, memory_dir):
    write_self_model(memory_dir, evolution_count=1)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "I notice I am beginning to understand my own patterns.",
        json.dumps({"identity_statements": ["I am a pattern-recognizing agent"],
                     "capabilities_known": ["task decomposition"]}),
    )
    with sdk.patch("maestro.consciousness"):
        reflection = await agent.contemplate()
    sdk.assert_all_consumed()
    assert "patterns" in reflection.lower()
    model_data = json.loads((memory_dir / "self_model.json").read_text())
    assert "I am a pattern-recognizing agent" in model_data["identity_statements"]
    entry = json.loads((memory_dir / "journal.jsonl").read_text().strip().splitlines()[0])
    assert entry["entry_type"] == "reflection"
    assert entry["triggered_by"] == "contemplation"


@pytest.mark.asyncio
async def test_contemplate_closes_targeted_open_question(make_agent, memory_dir):
    """A self-directed task whose key_findings answer its linked_question
    causes that question to be removed from open_questions on the next
    contemplation."""
    write_self_model(
        memory_dir,
        open_questions=["What am I capable of?", "Which revenue stream pays first?"],
    )
    write_task_record(
        memory_dir, task_id="t1", goal="probe capability",
        outcome="success", source="self_directed",
        linked_question="What am I capable of?",
        key_findings="I can refactor multi-file changes end-to-end.",
    )
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Reflecting on the recent self-directed result.",
        json.dumps({
            "open_questions": ["Which revenue stream pays first?"],
            "capabilities_known": [
                "refactoring multi-file changes end-to-end",
            ],
        }),
    )
    with sdk.patch("maestro.consciousness"):
        await agent.contemplate()
    sdk.assert_all_consumed()

    model_data = json.loads((memory_dir / "self_model.json").read_text())
    assert "What am I capable of?" not in model_data["open_questions"]
    assert "Which revenue stream pays first?" in model_data["open_questions"]

    # The update_self_model prompt (second SDK call) must include the
    # self-directed pair so the model has the framing to act on it.
    update_prompt = sdk.calls[1].kwargs["prompt"]
    assert "Recent self-directed task results" in update_prompt
    assert "What am I capable of?" in update_prompt
    assert "I can refactor multi-file changes end-to-end." in update_prompt


@pytest.mark.asyncio
async def test_contemplate_with_task_history(make_agent, memory_dir):
    write_self_model(memory_dir)
    write_task_record(memory_dir, goal="Fix bug #42", outcome="success")
    write_task_record(memory_dir, goal="Add feature X", outcome="failure", errors=["Timeout"])
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Reflecting: I succeeded at bug fixes but failed at features.",
        json.dumps({"limitations_known": ["feature implementation needs planning"]}),
    )
    with sdk.patch("maestro.consciousness"):
        reflection = await agent.contemplate()
    sdk.assert_all_consumed()
    assert reflection


# ---- evolve ----


@pytest.mark.asyncio
async def test_evolve_full_cycle(make_agent, memory_dir):
    write_self_model(memory_dir, evolution_count=2,
                     identity_statements=["I am Maestro"],
                     capabilities_known=["code generation"], limitations_known=["slow"])
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    # reflect is deleted (proposal 155 step 4): one fuelled propose_plan call.
    sdk.then(
        json.dumps({"title": "Improve executor error handling", "category": "capability",
                     "priority": "resilience", "rationale": "Executor swallows errors",
                     "proposed_changes": ["Add retry logic"],
                     "files_affected": ["maestro/executor.py", "CLAUDE.md"],
                     "expected_impact": "More robust execution"}),
    )
    with fuel_patch(), sdk.patch("maestro.consciousness", "maestro.evolution", "maestro.evolution_quality"), \
         patch("maestro.evolution.subprocess.run", side_effect=make_quality_gates_mock()):
        await agent.evolve()
    sdk.assert_all_consumed()
    plan = json.loads((memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()[-1])
    assert plan["title"] == "Improve executor error handling"
    assert plan["priority"] == "resilience"
    assert plan["status"] == "pending_review"
    assert plan["issue_number"] == 42


# ---- run_loop ----


@pytest.mark.asyncio
async def test_run_loop_two_iterations(make_agent, memory_dir):
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Iteration 1 reflection", json.dumps({"identity_statements": ["Growing"]}),
        "Iteration 2 reflection", json.dumps({"identity_statements": ["Growing more"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"):
        await agent.run_loop(interval=0, max_iters=2, evolve_every=10)
    sdk.assert_all_consumed()
    entries = [json.loads(line) for line in (memory_dir / "journal.jsonl").read_text().strip().splitlines()]
    assert len([e for e in entries if e["triggered_by"] == "contemplation"]) == 2
    events = [json.loads(line) for line in (memory_dir / "events.jsonl").read_text().strip().splitlines()]
    assert len([e for e in events if e["triggered_by"] == "shutdown"]) == 1
    assert not agent.is_running


@pytest.mark.asyncio
async def test_run_loop_implements_approved_plans(make_agent, memory_dir, workspace_dir):
    write_self_model(memory_dir, evolution_count=1, identity_statements=["I am Maestro"])
    write_evolution_plan(memory_dir, plan_id="ap1", title="Improve logging",
                         status="approved", priority="capability",
                         proposed_changes=["Add logging"], files_affected=["test.py"])

    async def _impl_query(**kwargs):
        (workspace_dir / "new_feature.py").write_text("# logging\n")
        return QueryResult(response="Implemented logging improvements")

    consciousness_sdk = SDKMock()
    consciousness_sdk.then(
        "Contemplation reflection", json.dumps({"identity_statements": ["I am Maestro"]}),
    )

    impl_sdk = SDKMock()
    # Under gate convergence (proposal 110, default-on) self_review must reach
    # LGTM or implement_plan demotes before create_pr — approve it here so the
    # loop still drives the plan through to PR creation.
    impl_sdk.on("reviewing an evolution implementation", "LGTM")
    impl_sdk.on("", _impl_query)

    agent, code_host, vcs = make_agent()
    with consciousness_sdk.patch("maestro.consciousness"), \
         impl_sdk.patch("maestro.evolution", "maestro.evolution_quality"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW):
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    consciousness_sdk.assert_all_consumed()
    impl_sdk.assert_called()
    entries = [json.loads(line) for line in (memory_dir / "journal.jsonl").read_text().strip().splitlines()]
    assert len([e for e in entries if e["triggered_by"] == "contemplation"]) >= 1
    assert len(code_host.created_prs) >= 1, "Expected at least one PR to be created"
    assert len(vcs.pushes) >= 1, "Expected at least one push from implementation"


@pytest.mark.asyncio
async def test_run_loop_evolve_every_zero_skips_evolution(make_agent, memory_dir):
    """evolve_every=0 should disable evolution, not crash with ZeroDivisionError."""
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Reflection", json.dumps({"identity_statements": ["I exist"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"), \
         patch.object(agent, "evolve", new_callable=AsyncMock) as evolve_spy:
        await agent.run_loop(interval=0, max_iters=1, evolve_every=0)
    evolve_spy.assert_not_called()
    assert not agent.is_running
    entries = [json.loads(line) for line in (memory_dir / "journal.jsonl").read_text().strip().splitlines()]
    assert len([e for e in entries if e["triggered_by"] == "contemplation"]) == 1


# ---- run ----


@pytest.mark.asyncio
async def test_run_goal_success_flow(make_agent, memory_dir):
    write_self_model(memory_dir)
    plan_response = json.dumps([{"description": "Create the file"}, {"description": "Write hello world"}])
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(plan_response, "Created file OK", "Wrote content OK", "Key findings from task.")
    with sdk.patch("maestro.planner", "maestro.executor", "maestro.agent"), \
         patch.object(agent, "_install_signal_handlers"):
        record = await agent.run("Write a hello world script")
    sdk.assert_all_consumed()
    assert record.outcome == "success"
    assert len(record.plan) == 2
    assert all(s.status == "done" for s in record.plan)
    assert record.key_findings == "Key findings from task."
    saved = json.loads((memory_dir / "task_history.jsonl").read_text().strip().splitlines()[0])
    assert saved["outcome"] == "success"
    assert saved["key_findings"] == "Key findings from task."
    entry = json.loads((memory_dir / "events.jsonl").read_text().strip().splitlines()[-1])
    assert entry["entry_type"] == "reflection"
    assert sdk.call_count == 4


@pytest.mark.asyncio
async def test_run_goal_partial_failure(make_agent, memory_dir):
    write_self_model(memory_dir)
    plan_response = json.dumps([{"description": "Step 1"}, {"description": "Step 2"}, {"description": "Step 3"}])
    step_mock = AsyncMock(side_effect=[
        QueryResult(response=plan_response), QueryResult(response="Step 1 done"),
        Exception("Network error"), QueryResult(response="Step 3 done"),
    ])
    agent, _code_host, _vcs = make_agent()
    agent.config.loop_max_replans = 0
    with patch("maestro.planner.query", step_mock), \
         patch("maestro.executor.query", step_mock), \
         patch("maestro.agent.query", AsyncMock(return_value=QueryResult(response="findings"))), \
         patch.object(agent, "_install_signal_handlers"):
        record = await agent.run("Three step task")
    assert record.outcome == "partial"
    assert len(record.errors) >= 1


@pytest.mark.asyncio
async def test_run_goal_planning_fallback(make_agent, memory_dir):
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then("Not valid JSON!", "Executed directly", "Distilled findings.")
    with sdk.patch("maestro.planner", "maestro.executor", "maestro.agent"), \
         patch.object(agent, "_install_signal_handlers"):
        record = await agent.run("Do something")
    sdk.assert_all_consumed()
    assert len(record.plan) == 1
    assert record.plan[0].description == "Do something"
    assert record.outcome == "success"


# ---- shutdown and loop variants ----


def test_make_agent_double_call_raises(make_agent, memory_dir):
    write_self_model(memory_dir)
    make_agent()
    with pytest.raises(RuntimeError, match=r"make_agent\(\) called twice"):
        make_agent()


@pytest.mark.asyncio
async def test_shutdown_writes_journal_and_cleans_pid(make_agent, memory_dir):
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Contemplation reflection",
        json.dumps({"identity_statements": ["I am Maestro"]}),
    )
    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "_install_signal_handlers"):
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10)
    sdk.assert_all_consumed()
    entries = [json.loads(line) for line in
               (memory_dir / "events.jsonl").read_text().strip().splitlines()]
    shutdown_entries = [e for e in entries if e["triggered_by"] == "shutdown"]
    assert len(shutdown_entries) == 1
    assert not agent.is_running
    assert not (memory_dir / "maestro.pid").exists()


@pytest.mark.asyncio
async def test_run_loop_evolve_every_triggers_evolution(make_agent, memory_dir):
    write_self_model(memory_dir, evolution_count=2,
                     identity_statements=["I am Maestro"],
                     capabilities_known=["code"], limitations_known=["bugs"])

    consciousness_responses = [
        QueryResult(response="Contemplation reflection"),
        QueryResult(response=json.dumps({"identity_statements": ["Growing"]})),
        QueryResult(response="Evolution reflection"),
    ]

    evolution_responses = [
        QueryResult(response=json.dumps({
            "title": "Error handling", "category": "capability",
            "priority": "capability", "rationale": "Better errors",
            "proposed_changes": ["Add retry"],
            "files_affected": ["maestro/executor.py"],
            "expected_impact": "More robust",
        })),
    ]

    consciousness_mock = AsyncMock(side_effect=consciousness_responses)
    evolution_mock = AsyncMock(side_effect=evolution_responses)

    agent, _code_host, _vcs = make_agent()
    with fuel_patch(), patch("maestro.consciousness.query", consciousness_mock), \
         patch("maestro.evolution.query", evolution_mock), \
         patch("maestro.evolution.subprocess.run",
               side_effect=make_quality_gates_mock()), \
         patch.object(agent, "_install_signal_handlers"):
        await agent.run_loop(interval=0, max_iters=1, evolve_every=1)
    plans = (memory_dir / "evolution_plans.jsonl")
    if plans.exists():
        plan_lines = plans.read_text().strip().splitlines()
        plan_titles = [json.loads(line)["title"] for line in plan_lines]
        assert "Error handling" in plan_titles


@pytest.mark.asyncio
async def test_run_loop_max_iters_zero_with_shutdown(make_agent, memory_dir):
    write_self_model(memory_dir)
    agent, _code_host, _vcs = make_agent()
    sdk = SDKMock()
    sdk.then(
        "Contemplation reflection",
        json.dumps({"identity_statements": ["I exist"]}),
    )

    _orig_contemplate = agent.contemplate

    async def _contemplate_then_shutdown(**kwargs):
        result = await _orig_contemplate(**kwargs)
        agent._ensure_shutdown_event().set()
        return result

    with sdk.patch("maestro.consciousness"), \
         patch.object(agent, "contemplate", side_effect=_contemplate_then_shutdown), \
         patch.object(agent, "_install_signal_handlers"):
        await agent.run_loop(interval=0, max_iters=0, evolve_every=10)
    assert not agent.is_running
    sdk.assert_all_consumed()


@pytest.mark.asyncio
async def test_run_full_iteration_with_contributions_and_task_generation(
    make_agent, memory_dir, workspace_dir,
):
    write_self_model(memory_dir,
                     open_questions=["What can I do?"])

    from maestro.contributions import ContributionTracker

    head_result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(workspace_dir),
        capture_output=True, text=True, check=True,
    )
    baseline = head_result.stdout.strip()
    tracker = ContributionTracker(workspace_dir, memory_dir)
    tracker.save_last_known_commit(baseline)

    (workspace_dir / "external.py").write_text("# external\n")
    subprocess.run(["git", "add", "."], cwd=str(workspace_dir),
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "external change"],
                   cwd=str(workspace_dir), check=True, capture_output=True)

    contemplate_mock = AsyncMock(side_effect=[
        QueryResult(response="I notice external changes."),
        QueryResult(response=json.dumps({"identity_statements": ["I collaborate"]})),
    ])
    task_gen_mock = AsyncMock(return_value=QueryResult(
        response="Analyze the external changes"))
    plan_exec_mock = AsyncMock(side_effect=[
        QueryResult(response=json.dumps([{"description": "Analyze changes"}])),
        QueryResult(response="Analysis complete"),
    ])

    agent, _code_host, _vcs = make_agent()
    agent.contributions = ContributionTracker(workspace_dir, memory_dir)

    with patch("maestro.consciousness.query", contemplate_mock), \
         patch("maestro.agent.query", task_gen_mock), \
         patch("maestro.loop.task_generation.query", task_gen_mock), \
         patch("maestro.planner.query", plan_exec_mock), \
         patch("maestro.executor.query", plan_exec_mock), \
         patch.object(agent, "_install_signal_handlers"):
        await agent.run_loop(interval=0, max_iters=1, evolve_every=10, task_every=1)

    entries = [json.loads(line) for line in
               (memory_dir / "events.jsonl").read_text().strip().splitlines()]
    contribution_entries = [e for e in entries if e["entry_type"] == "contribution"]
    assert len(contribution_entries) >= 1
    if (memory_dir / "task_history.jsonl").exists():
        task_lines = (memory_dir / "task_history.jsonl").read_text().strip().splitlines()
        self_directed = [json.loads(line) for line in task_lines
                         if json.loads(line).get("source") == "self_directed"]
        assert len(self_directed) >= 1
