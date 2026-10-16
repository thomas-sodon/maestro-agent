"""Tests for known-state context injection into task generation.

Covers the pure formatter ``format_known_state_summary`` in
``maestro/audit_checks.py`` and its integration into
``MaestroAgent.generate_task()``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro.audit_checks import format_known_state_summary
from tests.conftest import MockQueryResult

# ---------------------------------------------------------------------------
# Pure-function tests for format_known_state_summary
# ---------------------------------------------------------------------------


def test_happy_path() -> None:
    """Gate states and recent goals produce output containing all names and goals."""
    gates = {
        "compound_scope_gate_enabled": False,
        "stale_entry_enabled": True,
        "stall_block_enabled": False,
    }
    goals = [
        "Fix the stale-entry rule",
        "Add compound-scope threshold",
        "Run audit on task queue",
        "Refactor consciousness module",
        "Measure cost per task",
    ]
    result = format_known_state_summary(gates, goals)

    # All gate names present
    assert "compound_scope_gate_enabled" in result
    assert "stale_entry_enabled" in result
    assert "stall_block_enabled" in result

    # Gate values present
    assert "disabled" in result
    assert "enabled" in result

    # All goal substrings present
    for goal in goals:
        assert goal in result

    # Section header present
    assert "## Already Known State" in result


def test_empty_goals() -> None:
    """Empty goals list produces gate-states-only block without error."""
    gates = {
        "compound_scope_gate_enabled": True,
        "stall_block_enabled": False,
    }
    result = format_known_state_summary(gates, [])

    assert "## Already Known State" in result
    assert "compound_scope_gate_enabled" in result
    assert "stall_block_enabled" in result
    # No recent-tasks header when goals are empty
    assert "Recent task goals" not in result


def test_long_goal_truncation() -> None:
    """A 120-char goal is truncated to 80 chars plus ellipsis."""
    long_goal = "A" * 120
    result = format_known_state_summary({}, [long_goal])

    # The truncated goal should appear (80 chars + ellipsis)
    assert "A" * 80 + "…" in result
    # The full 120-char goal should NOT appear
    assert "A" * 120 not in result


def test_empty_inputs_returns_empty_string() -> None:
    """Both empty gates and empty goals returns empty string."""
    assert format_known_state_summary({}, []) == ""


def test_pr_lap_context_appears_in_output() -> None:
    """A non-empty pr_lap_context renders as a PR-LAPS line in the section."""
    result = format_known_state_summary(
        {}, [], pr_lap_context="PR-LAPS: #770 (3/6 laps)",
    )
    assert "## Already Known State" in result
    assert "PR-LAPS: #770 (3/6 laps)" in result


def test_pr_lap_context_only_still_renders_section() -> None:
    """pr_lap_context alone defeats the all-empty guard and renders the block."""
    result = format_known_state_summary({}, [], pr_lap_context="PR-LAPS: x")
    assert result.startswith("## Already Known State")
    assert "PR-LAPS: x" in result


def test_max_goals_cap() -> None:
    """Only up to max_goals are included in the output."""
    goals = [f"goal_{i}" for i in range(10)]
    result = format_known_state_summary({}, goals, max_goals=3)

    assert "goal_0" in result
    assert "goal_1" in result
    assert "goal_2" in result
    assert "goal_3" not in result


def test_custom_max_goal_len() -> None:
    """Custom max_goal_len truncates at the specified length."""
    goal = "B" * 50
    result = format_known_state_summary({}, [goal], max_goal_len=30)

    assert "B" * 30 + "…" in result
    assert "B" * 50 not in result


def test_gates_sorted_alphabetically() -> None:
    """Gate states are listed in sorted order."""
    gates = {
        "z_gate": True,
        "a_gate": False,
        "m_gate": True,
    }
    result = format_known_state_summary(gates, [])
    lines = result.splitlines()
    gate_lines = [ln for ln in lines if ln.strip().startswith("- ") and ":" in ln]
    gate_names = [ln.strip().lstrip("- ").split(":")[0] for ln in gate_lines]
    assert gate_names == ["a_gate", "m_gate", "z_gate"]


# ---------------------------------------------------------------------------
# LIVE-PLAN-COLLISION surfacing (proposal 100) — the duplicate-live-concern line
# ---------------------------------------------------------------------------


def _seed_collision_plans(
    mem_fs: dict[str, str],
    data_dir: Path,
    plans: list[dict[str, object]],
) -> None:
    """Write *plans* as ``evolution_plans.jsonl`` under *data_dir* via ``mem_fs``."""
    body = "\n".join(json.dumps(plan) for plan in plans)
    mem_fs[str(data_dir / "evolution_plans.jsonl")] = body + ("\n" if body else "")


def test_live_plan_collision_line_present_when_colliding(
    mem_fs: dict[str, str],
) -> None:
    """A colliding live-plan corpus surfaces the LIVE-PLAN-COLLISION line."""
    data_dir = Path("/fake/known")
    plans: list[dict[str, object]] = [
        {"plan_id": "a", "status": "pending_review",
         "files_affected": ["shared1.py", "shared2.py"]},
        {"plan_id": "b", "status": "approved",
         "files_affected": ["shared1.py", "shared2.py"]},
    ]
    plans += [
        {"plan_id": f"f{i}", "status": "implemented",
         "files_affected": [f"solo{i}a.py", f"solo{i}b.py"]}
        for i in range(4)
    ]
    _seed_collision_plans(mem_fs, data_dir, plans)
    section = format_known_state_summary({}, [], data_dir=data_dir)
    assert "LIVE-PLAN-COLLISION:" in section
    # Flush-left, so the gate-line filters in this module never capture it.
    assert "\nLIVE-PLAN-COLLISION:" in section


def test_live_plan_collision_line_absent_when_disjoint(
    mem_fs: dict[str, str],
) -> None:
    """A disjoint live-plan corpus self-suppresses the line (zero is healthy)."""
    data_dir = Path("/fake/known")
    plans: list[dict[str, object]] = [
        {"plan_id": f"p{i}", "status": "pending_review",
         "files_affected": [f"u{i}a.py", f"u{i}b.py"]}
        for i in range(6)
    ]
    _seed_collision_plans(mem_fs, data_dir, plans)
    section = format_known_state_summary({}, [], data_dir=data_dir)
    assert "LIVE-PLAN-COLLISION" not in section


def test_live_plan_collision_line_absent_when_data_dir_none() -> None:
    """With no data_dir the function stays pure and emits no collision line."""
    section = format_known_state_summary({}, [])
    assert "LIVE-PLAN-COLLISION" not in section


# ---------------------------------------------------------------------------
# Integration test — generate_task injects known-state context into prompt
# ---------------------------------------------------------------------------


def _enqueue_payload(goal: str) -> str:
    """Build a JSON response for the prune/edit/enqueue schema."""
    return json.dumps({
        "prune": [],
        "edit": [],
        "enqueue": [{"goal": goal}],
    })


@pytest.mark.asyncio
async def test_generate_task_injection(make_agent) -> None:  # type: ignore[no-untyped-def]
    """generate_task() injects known-state context into the prompt."""
    agent = make_agent()

    captured_prompt: list[str] = []

    async def capturing_query(**kwargs):  # type: ignore[no-untyped-def]
        captured_prompt.append(kwargs.get("prompt", ""))
        return MockQueryResult(response=_enqueue_payload("some new task"))

    with patch("maestro.loop.task_generation.query", side_effect=capturing_query):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]

    # The known-state section header must be present
    assert "Already Known State" in prompt

    # Gate states from the default config must appear
    assert "stale_entry_enabled" in prompt
    assert "stall_block_enabled" in prompt


@pytest.mark.asyncio
async def test_generate_task_injects_prediction_accuracy(
    make_agent,
    mem_fs,
) -> None:  # type: ignore[no-untyped-def]
    """generate_task() surfaces prediction accuracy when 5+ matched pairs exist."""
    from maestro.audit_checks_calibration import (
        record_prediction,
    )
    from maestro.memory import TaskRecord

    agent = make_agent()

    # Seed 5 predictions and matching task history
    for i in range(5):
        tid = f"pred_test_{i}"
        record_prediction(agent.predictions, tid, "success")
        agent.memory.record_task(TaskRecord(
            task_id=tid,
            goal=f"fix #{100 + i}",
            plan=[],
            outcome="success",
            result_summary="done",
            errors="",
            duration_secs=10.0,
            timestamp="2025-01-01T00:00:00Z",
            source="self_directed",
        ))

    captured_prompt: list[str] = []

    async def capturing_query(**kwargs):  # type: ignore[no-untyped-def]
        captured_prompt.append(kwargs.get("prompt", ""))
        return MockQueryResult(response=_enqueue_payload("some new task"))

    with patch("maestro.loop.task_generation.query", side_effect=capturing_query):
        await agent.generate_task()

    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]

    # Prediction accuracy line must appear
    assert "Prediction accuracy" in prompt


@pytest.mark.asyncio
async def test_generate_task_passes_settled_prs_to_churn_summary(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """generate_task() hands the settlement ledger's settled set to the churn
    formatter, so a merged PR's stale PR-READY/PR-STUCK line is suppressed."""
    agent = make_agent()

    async def fake_query(**kwargs):  # type: ignore[no-untyped-def]
        return MockQueryResult(response=_enqueue_payload("some new task"))

    seen: list[frozenset[int]] = []

    def _seen_settled(*args, settled_prs, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(settled_prs())
        return ""

    with patch("maestro.loop.task_generation.query", side_effect=fake_query), \
         patch(
             "maestro.audit_checks_settlement.load_settled_prs",
             return_value=frozenset({77}),
         ), \
         patch(
             "maestro.loop.task_generation.format_pr_churn_summary",
             side_effect=_seen_settled,
         ) as churn:
        await agent.generate_task()

    assert churn.call_count == 1
    assert seen == [frozenset({77})]
