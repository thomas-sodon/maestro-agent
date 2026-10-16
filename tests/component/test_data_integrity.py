"""Component tests for data integrity — corrupted files, edge cases, persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from maestro.consciousness import Consciousness
from maestro.journal import Journal
from maestro.memory import Memory
from tests.component.conftest import write_evolution_plan, write_task_record


def test_corrupted_journal_lines_skipped(memory_dir):
    journal = Journal(memory_dir)
    journal.write_thought(content="Good entry", entry_type="reflection", triggered_by="agent_tool")
    # Write corrupted line
    with open(memory_dir / "journal.jsonl", "a") as f:
        f.write("THIS IS NOT JSON\n")
    journal.write_thought(content="Another good entry",
                          entry_type="reflection", triggered_by="agent_tool")
    entries = journal.read_recent(100)
    assert len(entries) == 2
    assert entries[0].content == "Good entry"
    assert entries[1].content == "Another good entry"


def test_corrupted_task_records_skipped(memory_dir):
    memory = Memory(memory_dir)
    write_task_record(memory_dir, task_id="t001", goal="Good task", outcome="success")
    with open(memory_dir / "task_history.jsonl", "a") as f:
        f.write("{totally broken json\n")
    write_task_record(memory_dir, task_id="t002", goal="Also good", outcome="failure")
    tasks = memory.get_recent_tasks(100)
    assert len(tasks) == 2
    assert tasks[0].goal == "Good task"
    assert tasks[1].goal == "Also good"


def test_corrupted_evolution_plans_skipped(memory_dir):
    memory = Memory(memory_dir)
    write_evolution_plan(memory_dir, plan_id="p001", title="Good plan")
    with open(memory_dir / "evolution_plans.jsonl", "a") as f:
        f.write("NOT VALID JSON AT ALL\n")
    write_evolution_plan(memory_dir, plan_id="p002", title="Another good plan")
    plans = memory.get_all_plans()
    assert len(plans) == 2
    assert plans[0].title == "Good plan"
    assert plans[1].title == "Another good plan"


def test_empty_memory_dir_no_crash(memory_dir):
    memory = Memory(memory_dir)
    journal = Journal(memory_dir)
    consciousness = Consciousness(memory_dir)

    assert memory.get_recent_tasks() == []
    assert memory.get_all_plans() == []
    assert memory.get_pending_plans() == []
    assert memory.get_success_rate() == 0.0
    assert journal.read_recent() == []
    assert journal.search("anything") == []
    assert journal.read_by_type("reflection") == []
    model = consciousness.load_self_model()
    assert model.evolution_count == 0


def test_self_model_corrupted_json_propagates(memory_dir):
    """Corrupt ``self_model.json`` propagates ``json.JSONDecodeError``.

    Fail-loud (CLAUDE.md → "Fail loud"): silently replacing a corrupt
    self-model with a fresh one would mask data loss; the loop's
    iteration handler now classifies the failure for root-cause analysis.
    """
    (memory_dir / "self_model.json").write_text("{{{{NOT JSON")
    consciousness = Consciousness(memory_dir)
    with pytest.raises(json.JSONDecodeError):
        consciousness.load_self_model()


def test_plan_status_transitions(memory_dir):
    memory = Memory(memory_dir)
    write_evolution_plan(memory_dir, plan_id="trans001", title="Transition test",
                         status="pending_review")
    now = datetime.now(UTC).isoformat()
    memory.update_plan_status("trans001", "approved", reviewed_at=now)
    plan = memory.get_plan("trans001")
    assert plan is not None
    assert plan.status == "approved"
    assert plan.reviewed_at == now

    memory.update_plan_status("trans001", "implemented",
                              pr_url="https://github.com/t/r/pull/1")
    plan = memory.get_plan("trans001")
    assert plan is not None
    assert plan.status == "implemented"
    assert plan.pr_url == "https://github.com/t/r/pull/1"


def test_plan_rejection_with_reason(memory_dir):
    memory = Memory(memory_dir)
    write_evolution_plan(memory_dir, plan_id="rej001", title="Rejected plan",
                         status="pending_review")
    now = datetime.now(UTC).isoformat()
    memory.update_plan_status("rej001", "rejected",
                              rejection_reason="Too risky", reviewed_at=now)
    plan = memory.get_plan("rej001")
    assert plan is not None
    assert plan.status == "rejected"
    assert plan.rejection_reason == "Too risky"
    assert plan.reviewed_at == now


def test_task_success_rate_calculation(memory_dir):
    memory = Memory(memory_dir)
    write_task_record(memory_dir, task_id="s1", goal="Task 1", outcome="success")
    write_task_record(memory_dir, task_id="s2", goal="Task 2", outcome="success")
    write_task_record(memory_dir, task_id="s3", goal="Task 3", outcome="failure")
    write_task_record(memory_dir, task_id="s4", goal="Task 4", outcome="partial")
    rate = memory.get_success_rate()
    assert rate == pytest.approx(0.5)  # 2 out of 4


def test_journal_search(memory_dir):
    journal = Journal(memory_dir)
    journal.write_thought(content="I discovered a bug in the executor",
                          entry_type="reflection", triggered_by="agent_tool")
    journal.write_thought(content="The planner works well",
                          entry_type="reflection", triggered_by="agent_tool")
    journal.write_thought(content="A new bug was found in parsing",
                          entry_type="realization", triggered_by="agent_tool")
    results = journal.search("bug")
    assert len(results) == 2
    assert all("bug" in r.content.lower() for r in results)


def test_journal_read_by_type(memory_dir):
    journal = Journal(memory_dir)
    journal.write_thought(content="Reflection 1",
                          entry_type="reflection", triggered_by="agent_tool")
    journal.write_thought(content="A realization",
                          entry_type="realization", triggered_by="agent_tool")
    journal.write_thought(content="Reflection 2",
                          entry_type="reflection", triggered_by="agent_tool")
    journal.write_thought(content="A milestone",
                          entry_type="milestone", triggered_by="agent_tool")
    reflections = journal.read_by_type("reflection")
    assert len(reflections) == 2
    realizations = journal.read_by_type("realization")
    assert len(realizations) == 1
    milestones = journal.read_by_type("milestone")
    assert len(milestones) == 1
