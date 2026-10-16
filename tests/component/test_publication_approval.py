"""Component tests for the publication approval callbacks (proposal 032).

The callbacks live inside ``run_loop`` so constructing them in isolation
would require reaching into the run loop's local closure. Instead these
tests use the same surface Telegram uses: call the mutators on ``Memory``
directly and assert the resulting state transitions, focusing on the
TOCTOU + double-resolve guards that the callbacks (and the matching
dashboard action functions) rely on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from maestro.dashboard.routes import (
    PublicationNotFoundError,
    PublicationNotPendingError,
    approve_publication_action,
    reject_publication_action,
)
from maestro.memory import Memory, PublicationProposal


def _seed_proposal(
    memory: Memory,
    *,
    proposal_id: str,
    status: str = "pending_approval",
) -> PublicationProposal:
    proposal = PublicationProposal(
        proposal_id=proposal_id,
        task_id="t-1",
        review_id="r-1",
        proposed_name=f"repo-{proposal_id}",
        description="demo",
        proposed_readme="# demo",
        publishable_files=["README.md"],
        status=status,  # type: ignore[arg-type]
        created_at=datetime(2026, 4, 24, tzinfo=UTC).isoformat(),
    )
    memory.write_publication_proposal(proposal)
    return proposal


class _FakeAgent:
    """Minimal agent stub surfacing only ``memory`` — all the action
    functions touch."""

    def __init__(self, memory: Memory) -> None:
        self.memory = memory


def test_approve_publication_action_flips_status_and_stamps_reviewed_at(
    tmp_path: Path,
) -> None:
    memory = Memory(tmp_path)
    _seed_proposal(memory, proposal_id="p-ok")
    agent = _FakeAgent(memory)

    result = approve_publication_action(agent, "p-ok")  # type: ignore[arg-type]
    assert result == "approved"

    updated = memory.get_proposal("p-ok")
    assert updated is not None
    assert updated.status == "approved"
    assert updated.reviewed_at is not None


def test_double_approval_raises_not_pending(tmp_path: Path) -> None:
    memory = Memory(tmp_path)
    _seed_proposal(memory, proposal_id="p-dup")
    agent = _FakeAgent(memory)

    approve_publication_action(agent, "p-dup")  # type: ignore[arg-type]
    with pytest.raises(PublicationNotPendingError):
        approve_publication_action(agent, "p-dup")  # type: ignore[arg-type]


def test_approve_after_telegram_reject_raises_not_pending(
    tmp_path: Path,
) -> None:
    """Cross-surface idempotency: Telegram rejected → dashboard approve fails."""
    memory = Memory(tmp_path)
    _seed_proposal(memory, proposal_id="p-xsurface")
    agent = _FakeAgent(memory)

    # Simulate Telegram ``_on_publication_reject`` having already run.
    memory.update_proposal_status(
        "p-xsurface", "rejected",
        rejection_reason="Rejected via Telegram",
        reviewed_at=datetime(2026, 4, 24, 0, 0, 1, tzinfo=UTC).isoformat(),
    )

    with pytest.raises(PublicationNotPendingError):
        approve_publication_action(agent, "p-xsurface")  # type: ignore[arg-type]


def test_reject_publication_action_records_reason(tmp_path: Path) -> None:
    memory = Memory(tmp_path)
    _seed_proposal(memory, proposal_id="p-rej")
    agent = _FakeAgent(memory)

    result = reject_publication_action(
        agent, "p-rej", "not ready",  # type: ignore[arg-type]
    )
    assert result == "rejected"
    updated = memory.get_proposal("p-rej")
    assert updated is not None
    assert updated.status == "rejected"
    assert updated.rejection_reason == "not ready"
    assert updated.reviewed_at is not None


def test_approve_missing_proposal_raises_not_found(tmp_path: Path) -> None:
    memory = Memory(tmp_path)
    agent = _FakeAgent(memory)
    with pytest.raises(PublicationNotFoundError):
        approve_publication_action(agent, "does-not-exist")  # type: ignore[arg-type]


def test_reject_missing_proposal_raises_not_found(tmp_path: Path) -> None:
    memory = Memory(tmp_path)
    agent = _FakeAgent(memory)
    with pytest.raises(PublicationNotFoundError):
        reject_publication_action(agent, "missing", "x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# MaestroAgent._on_publication_approve / _on_publication_reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_on_publication_approve_success(
    make_agent, memory_dir: Path,
) -> None:
    agent, _, _ = make_agent()
    _seed_proposal(agent.memory, proposal_id="p-agent-ok")

    result = await agent._on_publication_approve("p-agent-ok")
    assert result == "approved"

    updated = agent.memory.get_proposal("p-agent-ok")
    assert updated is not None and updated.status == "approved"
    assert updated.reviewed_at is not None

    milestones = [
        e for e in agent.journal._load_events()
        if e.entry_type == "milestone"
        and e.triggered_by == "publication:p-agent-ok"
    ]
    assert len(milestones) == 1


@pytest.mark.asyncio
async def test_agent_on_publication_approve_missing_raises(
    make_agent,
) -> None:
    agent, _, _ = make_agent()
    with pytest.raises(ValueError, match="not pending_approval"):
        await agent._on_publication_approve("does-not-exist")


@pytest.mark.asyncio
async def test_agent_on_publication_approve_not_pending_raises(
    make_agent,
) -> None:
    agent, _, _ = make_agent()
    _seed_proposal(
        agent.memory, proposal_id="p-resolved", status="approved",
    )
    with pytest.raises(ValueError, match="not pending_approval"):
        await agent._on_publication_approve("p-resolved")


@pytest.mark.asyncio
async def test_agent_on_publication_approve_toctou_returns_ignored(
    make_agent,
) -> None:
    """If the update returns False (proposal vanished), return 'ignored'."""
    from unittest.mock import patch

    agent, _, _ = make_agent()
    _seed_proposal(agent.memory, proposal_id="p-race")
    with patch.object(
        agent.memory, "update_proposal_status", return_value=False,
    ):
        result = await agent._on_publication_approve("p-race")
    assert result == "ignored"


@pytest.mark.asyncio
async def test_agent_on_publication_approve_journal_failure_swallowed(
    make_agent,
) -> None:
    from unittest.mock import patch

    agent, _, _ = make_agent()
    _seed_proposal(agent.memory, proposal_id="p-jnl")
    with patch.object(
        agent.journal, "record_event",
        side_effect=OSError("journal boom"),
    ):
        result = await agent._on_publication_approve("p-jnl")
    # Status still flipped successfully.
    assert result == "approved"
    updated = agent.memory.get_proposal("p-jnl")
    assert updated is not None and updated.status == "approved"


@pytest.mark.asyncio
async def test_agent_on_publication_reject_success(
    make_agent,
) -> None:
    agent, _, _ = make_agent()
    _seed_proposal(agent.memory, proposal_id="p-rej")
    result = await agent._on_publication_reject("p-rej")
    assert result == "rejected"
    updated = agent.memory.get_proposal("p-rej")
    assert updated is not None
    assert updated.status == "rejected"
    assert updated.rejection_reason == "Rejected via Telegram"

    realizations = [
        e for e in agent.journal._load_events()
        if e.entry_type == "realization"
        and e.triggered_by == "publication:p-rej"
    ]
    assert len(realizations) == 1


@pytest.mark.asyncio
async def test_agent_on_publication_reject_missing_raises(
    make_agent,
) -> None:
    agent, _, _ = make_agent()
    with pytest.raises(ValueError, match="not pending_approval"):
        await agent._on_publication_reject("never-existed")


@pytest.mark.asyncio
async def test_agent_on_publication_reject_toctou_returns_ignored(
    make_agent,
) -> None:
    from unittest.mock import patch

    agent, _, _ = make_agent()
    _seed_proposal(agent.memory, proposal_id="p-rejrace")
    with patch.object(
        agent.memory, "update_proposal_status", return_value=False,
    ):
        result = await agent._on_publication_reject("p-rejrace")
    assert result == "ignored"


@pytest.mark.asyncio
async def test_agent_on_publication_approve_loses_lock_checked_race(
    make_agent,
) -> None:
    """Genuine race: another surface flips the row between status-check
    and update; the lock-checked update returns False without overwriting
    the race winner's state."""
    from typing import Any
    from unittest.mock import patch

    agent, _, _ = make_agent()
    _seed_proposal(agent.memory, proposal_id="p-real-race")

    real_update = agent.memory.update_proposal_status
    call_count = {"n": 0}

    def racy_update(*args: Any, **kwargs: Any) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            real_update(
                "p-real-race", "rejected",
                rejection_reason="Rejected via dashboard",
                reviewed_at="2026-04-25T00:00:00+00:00",
            )
        return real_update(*args, **kwargs)

    with patch.object(
        agent.memory, "update_proposal_status", side_effect=racy_update,
    ):
        result = await agent._on_publication_approve("p-real-race")
    assert result == "ignored"
    # Race winner's verdict survives; approver's mutation never landed.
    p = agent.memory.get_proposal("p-real-race")
    assert p is not None
    assert p.status == "rejected"
    assert p.rejection_reason == "Rejected via dashboard"


@pytest.mark.asyncio
async def test_agent_on_publication_reject_journal_failure_swallowed(
    make_agent,
) -> None:
    from unittest.mock import patch

    agent, _, _ = make_agent()
    _seed_proposal(agent.memory, proposal_id="p-rej-jnl")
    with patch.object(
        agent.journal, "record_event",
        side_effect=OSError("journal boom"),
    ):
        result = await agent._on_publication_reject("p-rej-jnl")
    assert result == "rejected"
