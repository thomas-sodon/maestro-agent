"""Tests for folding Telegram review replies into the plan ledger (proposal 145)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from maestro.memory import EvolutionPlan, Memory
from maestro.messaging import Message
from maestro.notifications.base import NotifierTransportError
from maestro.plan_feedback import FEEDBACK_TEXT_MAX_CHARS, MAX_UNADDRESSED_FEEDBACK
from maestro.plan_feedback_fold import fold_feedback_messages

FAKE_DIR = Path("/fake/mem")
PREFIX = "0123456789abcdef"


def _plan(status: str = "pending_review") -> EvolutionPlan:
    return EvolutionPlan(
        plan_id="p1", title="Wire a sensor",
        category="capability", rationale="r", proposed_changes=["c"],
        files_affected=["maestro/agent.py"], expected_impact="none",
        status=status,  # type: ignore[arg-type]
    )


def _msg(
    message_id: str = "m1",
    content: str = "narrow the scope",
    reply_to: str | None = f"p1@{PREFIX}",
    *,
    sender: str = "thomas",
    message_type: str = "plan_feedback",
) -> Message:
    return Message(
        message_id=message_id, timestamp="2026-10-04T00:00:00+00:00",
        sender=sender,  # type: ignore[arg-type]
        content=content,
        message_type=message_type,  # type: ignore[arg-type]
        reply_to=reply_to,
    )


def _memory(mem_fs: dict[str, str], *plans: EvolutionPlan) -> Memory:
    m = Memory(FAKE_DIR)
    for p in plans:
        m.save_evolution_plan(p)
    return m


async def _fold(m: Memory, notifier: AsyncMock, *msgs: Message) -> int:
    return await fold_feedback_messages(
        list(msgs), memory=m, notifier=notifier, max_length=2000,
    )


@pytest.mark.asyncio
async def test_fold_records_and_acknowledges(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    notifier = AsyncMock()

    assert await _fold(m, notifier, _msg()) == 1

    plan = m.get_plan("p1")
    assert plan is not None
    assert len(plan.reviewer_feedback) == 1
    entry = plan.reviewer_feedback[0]
    assert entry.feedback_id == "m1"
    assert entry.source == "telegram"
    assert entry.content_hash_seen == PREFIX
    notifier.notify.assert_awaited_once_with(
        "Feedback recorded — Maestro will revise plan p1 (Wire a sensor).",
    )


@pytest.mark.asyncio
async def test_fold_redrain_lands_once(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    await _fold(m, AsyncMock(), _msg())
    m.update_plan_status("p1", "approved")
    notifier = AsyncMock()
    assert await _fold(m, notifier, _msg()) == 0
    notifier.notify.assert_not_awaited()
    plan = m.get_plan("p1")
    assert plan is not None
    assert len(plan.reviewer_feedback) == 1


@pytest.mark.asyncio
async def test_fold_refuses_non_pending_plan(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan("approved"))
    notifier = AsyncMock()
    assert await _fold(m, notifier, _msg()) == 0
    sent = notifier.notify.await_args.args[0]
    assert "no longer pending review (status: approved)" in sent


@pytest.mark.asyncio
async def test_fold_refuses_missing_plan(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs)
    notifier = AsyncMock()
    assert await _fold(m, notifier, _msg()) == 0
    assert "plan p1 was not found" in notifier.notify.await_args.args[0]


@pytest.mark.asyncio
async def test_fold_refuses_past_cap(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    notifier = AsyncMock()
    msgs = [_msg(f"m{i}") for i in range(MAX_UNADDRESSED_FEEDBACK + 1)]
    assert await _fold(m, notifier, *msgs) == MAX_UNADDRESSED_FEEDBACK
    last = notifier.notify.await_args.args[0]
    assert f"already holds {MAX_UNADDRESSED_FEEDBACK} unaddressed entries" in last


@pytest.mark.asyncio
async def test_fold_truncates_overlong_text(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    assert await _fold(m, AsyncMock(), _msg(content="y" * 5000)) == 1
    plan = m.get_plan("p1")
    assert plan is not None
    assert len(plan.reviewer_feedback[0].text) == FEEDBACK_TEXT_MAX_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "reply_to"), [("   ", f"p1@{PREFIX}"), ("text", None), ("text", "@x")],
)
async def test_fold_skips_empty_or_unthreaded(
    mem_fs: dict[str, str], content: str, reply_to: str | None,
) -> None:
    m = _memory(mem_fs, _plan())
    notifier = AsyncMock()
    assert await _fold(m, notifier, _msg(content=content, reply_to=reply_to)) == 0
    assert "empty or unthreaded" in notifier.notify.await_args.args[0]
    plan = m.get_plan("p1")
    assert plan is not None
    assert plan.reviewer_feedback == []


@pytest.mark.asyncio
async def test_fold_ignores_other_messages(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    notifier = AsyncMock()
    others = (_msg(sender="maestro"), _msg(message_type="instruction"))
    assert await _fold(m, notifier, *others) == 0
    notifier.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_fold_absorbs_transport_failure(mem_fs: dict[str, str]) -> None:
    m = _memory(mem_fs, _plan())
    notifier = AsyncMock()
    notifier.notify.side_effect = NotifierTransportError("down")
    assert await _fold(m, notifier, _msg()) == 1


@pytest.mark.asyncio
async def test_fold_records_dashboard_source(mem_fs: dict[str, str]) -> None:
    from maestro.messaging import DASHBOARD_FEEDBACK_MESSAGE_ID_PREFIX

    m = _memory(mem_fs, _plan())
    dash_id = DASHBOARD_FEEDBACK_MESSAGE_ID_PREFIX + "abc"

    assert await _fold(m, AsyncMock(), _msg(dash_id), _msg("m2", "also this")) == 2

    plan = m.get_plan("p1")
    assert plan is not None
    sources = {f.feedback_id: f.source for f in plan.reviewer_feedback}
    assert sources == {dash_id: "dashboard", "m2": "telegram"}
