"""Tests for the extracted ``maestro.loop.task_record.run_post_task_reflection`` stage.

This is ``run``'s ``# 6.`` tail lifted into a named, independently-testable
method: it records the post-task outcome event (``Journal.record_event`` into
``events.jsonl``; one of three outcome branches, guarded so a write failure
never blocks the notice) and fires the best-effort completion notification.
These tests pin the event content, the notification message variants, and the
write-failure guard.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from maestro.loop.task_record import run_post_task_reflection
from maestro.memory import PlanStep


def _steps(n: int) -> list[PlanStep]:
    return [PlanStep(step_id=f"s{i}", description=f"step {i}") for i in range(n)]


@pytest.mark.asyncio
async def test_failure_branch_writes_failure_reflection(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch.object(agent, "_notify_best_effort", new=AsyncMock()) as notify:
        await run_post_task_reflection(
            agent,
            goal="do the thing",
            task_id="task123",
            outcome="failure",
            steps=_steps(2),
            errors=["boom"],
            failed=2,
            key_findings=None,
            summary="a summary",
            linked_question=None,
            source="external",
        )

    entries = agent.journal.read_recent_events()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.entry_type == "reflection"
    assert entry.triggered_by == "task:task123"
    assert "Task failed: do the thing" in entry.content
    assert "Errors: ['boom']" in entry.content
    # No linked question → no targeting prefix.
    assert "[Targeting:" not in entry.content
    # Failure notice, base form.
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "Task failed: do the thing"


@pytest.mark.asyncio
async def test_partial_branch_writes_partial_reflection(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch.object(agent, "_notify_best_effort", new=AsyncMock()) as notify:
        await run_post_task_reflection(
            agent,
            goal="partial goal",
            task_id="taskP",
            outcome="partial",
            steps=_steps(3),
            errors=["e1"],
            failed=1,
            key_findings="the finding",
            summary="the summary",
            linked_question=None,
            source="external",
        )

    entry = agent.journal.read_recent_events()[0]
    assert entry.entry_type == "reflection"
    assert entry.triggered_by == "task:taskP"
    assert "Partial success: partial goal" in entry.content
    assert "1/3 steps failed" in entry.content
    # key_findings wins over summary for the findings text.
    assert "Findings: the finding" in entry.content
    # partial is not "failure" → completion-style notice.
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "Task completed (partial): partial goal"


@pytest.mark.asyncio
async def test_partial_branch_falls_back_to_summary_findings(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch.object(agent, "_notify_best_effort", new=AsyncMock()):
        await run_post_task_reflection(
            agent,
            goal="g",
            task_id="t",
            outcome="partial",
            steps=_steps(2),
            errors=[],
            failed=1,
            key_findings=None,
            summary="summary-findings",
            linked_question=None,
            source="external",
        )

    entry = agent.journal.read_recent_events()[0]
    # With no key_findings, the summary supplies the findings text.
    assert "Findings: summary-findings" in entry.content


@pytest.mark.asyncio
async def test_success_branch_writes_success_reflection(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch.object(agent, "_notify_best_effort", new=AsyncMock()) as notify:
        await run_post_task_reflection(
            agent,
            goal="ship it",
            task_id="taskS",
            outcome="success",
            steps=_steps(1),
            errors=[],
            failed=0,
            key_findings="landed cleanly",
            summary="ignored",
            linked_question=None,
            source="external",
        )

    entry = agent.journal.read_recent_events()[0]
    assert entry.entry_type == "reflection"
    assert entry.triggered_by == "task:taskS"
    assert "Completed: ship it" in entry.content
    assert "Findings: landed cleanly" in entry.content
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "Task completed (success): ship it"


@pytest.mark.asyncio
async def test_linked_question_adds_targeting_prefix(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch.object(agent, "_notify_best_effort", new=AsyncMock()):
        await run_post_task_reflection(
            agent,
            goal="probe goal",
            task_id="tQ",
            outcome="success",
            steps=_steps(1),
            errors=[],
            failed=0,
            key_findings="found",
            summary="s",
            linked_question="Am I improving?",
            source="external",
        )

    entry = agent.journal.read_recent_events()[0]
    assert entry.content.startswith("[Targeting: Am I improving?] ")


@pytest.mark.asyncio
async def test_self_directed_notice_appends_targeting_and_finding(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch.object(agent, "_notify_best_effort", new=AsyncMock()) as notify:
        await run_post_task_reflection(
            agent,
            goal="self goal",
            task_id="tSD",
            outcome="success",
            steps=_steps(1),
            errors=[],
            failed=0,
            key_findings="a key finding",
            summary="s",
            linked_question="What matters?",
            source="self_directed",
        )

    message = notify.await_args.args[0]
    assert message == (
        "Task completed (success): self goal"
        "\nTargeting: What matters?"
        "\nFinding: a key finding"
    )


@pytest.mark.asyncio
async def test_self_directed_notice_without_key_findings_omits_finding(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch.object(agent, "_notify_best_effort", new=AsyncMock()) as notify:
        await run_post_task_reflection(
            agent,
            goal="self goal",
            task_id="tSD2",
            outcome="success",
            steps=_steps(1),
            errors=[],
            failed=0,
            key_findings=None,
            summary="s",
            linked_question="What matters?",
            source="self_directed",
        )

    message = notify.await_args.args[0]
    assert message == (
        "Task completed (success): self goal\nTargeting: What matters?"
    )
    assert "Finding:" not in message


@pytest.mark.asyncio
async def test_self_directed_without_linked_question_uses_base_notice(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch.object(agent, "_notify_best_effort", new=AsyncMock()) as notify:
        await run_post_task_reflection(
            agent,
            goal="self goal",
            task_id="tSD3",
            outcome="success",
            steps=_steps(1),
            errors=[],
            failed=0,
            key_findings="k",
            summary="s",
            linked_question=None,
            source="self_directed",
        )

    # source is self_directed but there is no linked_question → base notice only.
    assert notify.await_args.args[0] == "Task completed (success): self goal"


@pytest.mark.asyncio
async def test_journal_failure_is_swallowed_and_notice_still_fires(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    with (
        patch.object(
            agent.journal, "record_event", side_effect=OSError("disk full")
        ),
        patch.object(agent, "_notify_best_effort", new=AsyncMock()) as notify,
    ):
        # Must not raise despite the journal write blowing up.
        await run_post_task_reflection(
            agent,
            goal="g",
            task_id="t",
            outcome="failure",
            steps=_steps(1),
            errors=["x"],
            failed=1,
            key_findings=None,
            summary="s",
            linked_question=None,
            source="external",
        )

    # The event never persisted, but the completion notice still went out.
    assert agent.journal.read_recent_events() == []
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "Task failed: g"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "partial", "success"])
async def test_remediation_goal_is_bounded_in_journal_entry(make_agent, outcome):  # type: ignore[no-untyped-def]
    """#4372: a remediation directive must not blank the narrative window."""
    from maestro.loop.pr_remediation import _compose_pr_remediation_prompt

    goal = _compose_pr_remediation_prompt(
        pr_number=42,
        pr_title="Fix thing",
        plan=None,
        plan_id="p1",
        pr_body="body",
        pr_url="https://example/pr/42",
        problems=["x"],
    )
    assert len(goal) > 3000
    agent = make_agent()
    agent.journal.write_thought(
        content="an earlier inner thought",
        triggered_by="agent_tool",
    )
    with patch.object(agent, "_notify_best_effort", new=AsyncMock()):
        await run_post_task_reflection(
            agent,
            goal=goal,
            task_id="taskR",
            outcome=outcome,
            steps=_steps(2),
            errors=["e" * 500 for _ in range(20)],
            failed=1,
            key_findings="k" * 1000,
            summary="s",
            linked_question=None,
            source="external",
        )

    entry = agent.journal.read_recent_events()[-1]
    assert entry.triggered_by == "task:taskR"
    assert len(entry.content) < 1000
    assert "Remediate PR #42: Fix thing" in entry.content
    assert "git merge-base --is-ancestor" not in entry.content
    narrative = agent.journal.get_recent_narrative(max_chars=3000)
    assert "an earlier inner thought" in narrative


@pytest.mark.asyncio
async def test_blank_goal_is_kept_verbatim(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    with patch.object(agent, "_notify_best_effort", new=AsyncMock()):
        await run_post_task_reflection(
            agent,
            goal="",
            task_id="taskB",
            outcome="success",
            steps=_steps(1),
            errors=[],
            failed=0,
            key_findings="kf",
            summary="s",
            linked_question=None,
            source="external",
        )

    entry = agent.journal.read_recent_events()[0]
    assert entry.content == "Completed: . Findings: kf"
