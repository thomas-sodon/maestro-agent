"""Tests for custom agent tools (maestro.tools)."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from maestro.consciousness import SelfModel
from maestro.diagnostics import DiagnosticSnapshot
from maestro.issues import Issue
from maestro.journal import Journal, JournalEntry
from maestro.memory import EvolutionPlan, TaskRecord
from maestro.messaging import Message
from maestro.notifications.base import NullNotifier
from maestro.vcs import IssueMeta

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _TrackingNotifier(NullNotifier):
    """Notifier that records calls for assertions."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.fail_next = False

    async def notify(
        self, message: str, parse_mode: str | None = None,
    ) -> None:
        if self.fail_next:
            raise ConnectionError("Telegram API unavailable")
        self.messages.append(message)


def _find_tool(tools: list, name_fragment: str):  # type: ignore[type-arg]
    """Find a tool by name attribute in a list of InprocTool objects."""
    return next(t for t in tools if hasattr(t, "name") and name_fragment in t.name)


def _capture_tools(notifier, mock_store):  # type: ignore[no-untyped-def]
    """Create messaging server and capture the InprocTool objects."""
    captured: list = []
    with patch(
        "maestro.tools.create_inproc_mcp_server",
        side_effect=lambda **kwargs: captured.extend(kwargs.get("tools", [])),
    ):
        from maestro.tools import create_messaging_server

        create_messaging_server(notifier, mock_store)
    return captured


def _capture_server_tools(factory_fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    """Generic helper to capture tool objects from any create_*_server factory."""
    captured: list = []
    with patch(
        "maestro.tools.create_inproc_mcp_server",
        side_effect=lambda **kw: captured.extend(kw.get("tools", [])),
    ):
        factory_fn(*args, **kwargs)
    return captured


# ---------------------------------------------------------------------------
# send_message_to_thomas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_to_thomas_success() -> None:
    """send_message_to_thomas should save message and notify."""
    notifier = _TrackingNotifier()
    mock_store = MagicMock()
    tools = _capture_tools(notifier, mock_store)
    send_tool = _find_tool(tools, "send")

    result = await send_tool.handler({"message": "Hello Thomas!"})
    assert "Hello Thomas!" in result["text"]
    mock_store.save_message.assert_called_once()
    saved_msg = mock_store.save_message.call_args[0][0]
    assert saved_msg.sender == "maestro"
    assert saved_msg.content == "Hello Thomas!"
    assert saved_msg.message_type == "info"
    assert len(notifier.messages) == 1
    assert "Hello Thomas!" in notifier.messages[0]


@pytest.mark.asyncio
async def test_send_message_to_thomas_empty_message() -> None:
    """Tool should raise on empty message."""
    notifier = _TrackingNotifier()
    mock_store = MagicMock()
    tools = _capture_tools(notifier, mock_store)
    send_tool = _find_tool(tools, "send")

    with pytest.raises(ValueError, match="empty"):
        await send_tool.handler({"message": ""})

    with pytest.raises(ValueError, match="empty"):
        await send_tool.handler({"message": "   "})

    mock_store.save_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_to_thomas_delivery_failure() -> None:
    """Tool should raise RuntimeError when Telegram delivery fails, but still save."""
    notifier = _TrackingNotifier()
    notifier.fail_next = True
    mock_store = MagicMock()
    tools = _capture_tools(notifier, mock_store)
    send_tool = _find_tool(tools, "send")

    with pytest.raises(RuntimeError, match="delivery to Telegram failed"):
        await send_tool.handler({"message": "Important update"})

    # Message should still be saved even though delivery failed
    mock_store.save_message.assert_called_once()


# ---------------------------------------------------------------------------
# read_messages_from_thomas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_messages_returns_unread() -> None:
    """Tool should return unread messages and mark them read."""
    mock_store = MagicMock()
    mock_store.get_unread.return_value = [
        Message(
            message_id="m1",
            timestamp="2026-04-01T00:00:00",
            sender="thomas",
            content="Please check the logs",
            message_type="instruction",
        ),
        Message(
            message_id="m2",
            timestamp="2026-04-01T00:01:00",
            sender="thomas",
            content="The answer is 42",
            message_type="answer",
        ),
    ]
    notifier = _TrackingNotifier()
    tools = _capture_tools(notifier, mock_store)
    read_tool = _find_tool(tools, "read")

    result = await read_tool.handler({})
    assert "Please check the logs" in result["text"]
    assert "The answer is 42" in result["text"]
    assert "[Instruction @ 2026-04-01T00:00:00]" in result["text"]
    assert "[Answer @ 2026-04-01T00:01:00]" in result["text"]
    mock_store.mark_read_many.assert_not_called()
    mock_store.mark_read.assert_not_called()


@pytest.mark.asyncio
async def test_read_messages_includes_timestamp() -> None:
    """Each rendered line should carry the message's timestamp (regression: #308)."""
    mock_store = MagicMock()
    mock_store.get_unread.return_value = [
        Message(
            message_id="m1",
            timestamp="2026-04-01T08:15:00",
            sender="thomas",
            content="Please check the logs",
            message_type="instruction",
        ),
        Message(
            message_id="m2",
            timestamp="2026-04-01T11:47:00",
            sender="thomas",
            content="The answer is 42",
            message_type="answer",
        ),
    ]
    notifier = _TrackingNotifier()
    tools = _capture_tools(notifier, mock_store)
    read_tool = _find_tool(tools, "read")

    result = await read_tool.handler({})
    assert "[Instruction @ 2026-04-01T08:15:00]" in result["text"]
    assert "[Answer @ 2026-04-01T11:47:00]" in result["text"]


@pytest.mark.asyncio
async def test_read_messages_no_unread() -> None:
    """Tool should return 'no messages' when nothing is unread."""
    mock_store = MagicMock()
    mock_store.get_unread.return_value = []
    notifier = _TrackingNotifier()
    tools = _capture_tools(notifier, mock_store)
    read_tool = _find_tool(tools, "read")

    result = await read_tool.handler({})
    assert "No new messages" in result["text"]


# ---------------------------------------------------------------------------
# create_messaging_server
# ---------------------------------------------------------------------------


def test_create_messaging_server_returns_server() -> None:
    """Factory should call create_inproc_mcp_server with correct args."""
    notifier = _TrackingNotifier()
    mock_store = MagicMock()

    with patch("maestro.tools.create_inproc_mcp_server", return_value="mock_server") as mock_create:
        from maestro.tools import create_messaging_server

        result = create_messaging_server(notifier, mock_store)

    assert result == "mock_server"
    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["name"] == "messaging"
    assert len(kwargs["tools"]) == 2


# ===========================================================================
# Journal Server
# ===========================================================================


def _make_journal_tools():  # type: ignore[no-untyped-def]
    """Build journal tools with mock dependencies."""
    mock_journal = MagicMock()
    # Render entry headers through the real renderer.
    mock_journal.format_entry_header.side_effect = functools.partial(
        Journal.format_entry_header, SimpleNamespace(),
    )
    from maestro.tools import create_journal_server

    tools = _capture_server_tools(create_journal_server, mock_journal)
    return tools, mock_journal


@pytest.mark.asyncio
async def test_write_journal_entry_success() -> None:
    tools, mock_journal = _make_journal_tools()
    write_tool = _find_tool(tools, "write_journal_entry")

    mock_journal.write_thought.return_value = JournalEntry(
        entry_id="e1", timestamp="2026-01-01",
        entry_type="reflection", content="test insight",
        triggered_by="agent_tool",
    )

    result = await write_tool.handler({"content": "test insight"})
    assert "test insight" in result["text"]
    mock_journal.write_thought.assert_called_once_with(
        content="test insight",
        entry_type="reflection",
        triggered_by="agent_tool",
    )


@pytest.mark.asyncio
async def test_write_journal_entry_with_type() -> None:
    tools, mock_journal = _make_journal_tools()
    write_tool = _find_tool(tools, "write_journal_entry")
    mock_journal.write_thought.return_value = JournalEntry(
        entry_id="e2", timestamp="2026-01-01",
        entry_type="realization", content="aha",
    )

    await write_tool.handler({"content": "aha", "entry_type": "realization"})
    mock_journal.write_thought.assert_called_once_with(
        content="aha",
        entry_type="realization",
        triggered_by="agent_tool",
    )


@pytest.mark.asyncio
async def test_write_journal_entry_empty_content() -> None:
    tools, _ = _make_journal_tools()
    write_tool = _find_tool(tools, "write_journal_entry")
    with pytest.raises(ValueError, match="empty"):
        await write_tool.handler({"content": ""})


@pytest.mark.asyncio
async def test_write_journal_entry_invalid_type() -> None:
    tools, _ = _make_journal_tools()
    write_tool = _find_tool(tools, "write_journal_entry")
    with pytest.raises(ValueError, match="Invalid entry_type"):
        await write_tool.handler({"content": "test", "entry_type": "bogus"})


@pytest.mark.asyncio
async def test_read_journal_with_entries() -> None:
    tools, mock_journal = _make_journal_tools()
    read_tool = _find_tool(tools, "read_journal")
    mock_journal.read_recent.return_value = [
        JournalEntry(
            entry_id="e1", timestamp="2026-01-01",
            entry_type="reflection", content="First thought",
        ),
    ]

    result = await read_tool.handler({"count": 5})
    assert "First thought" in result["text"]
    mock_journal.read_recent.assert_called_once_with(5)


@pytest.mark.asyncio
async def test_read_journal_invalid_count_defaults_to_ten() -> None:
    tools, mock_journal = _make_journal_tools()
    read_tool = _find_tool(tools, "read_journal")
    mock_journal.read_recent.return_value = []

    await read_tool.handler({"count": "bad"})
    mock_journal.read_recent.assert_called_once_with(10)


@pytest.mark.asyncio
async def test_read_journal_empty() -> None:
    tools, mock_journal = _make_journal_tools()
    read_tool = _find_tool(tools, "read_journal")
    mock_journal.read_recent.return_value = []

    result = await read_tool.handler({})
    assert "No journal entries" in result["text"]


@pytest.mark.asyncio
async def test_read_journal_by_type() -> None:
    tools, mock_journal = _make_journal_tools()
    read_tool = _find_tool(tools, "read_journal")
    mock_journal.read_by_type.return_value = [
        JournalEntry(
            entry_id="e1", timestamp="2026-01-01",
            entry_type="realization", content="Eureka",
        ),
    ]

    result = await read_tool.handler({"entry_type": "realization"})
    assert "Eureka" in result["text"]
    mock_journal.read_by_type.assert_called_once_with("realization")


@pytest.mark.asyncio
async def test_read_journal_by_type_respects_count() -> None:
    tools, mock_journal = _make_journal_tools()
    read_tool = _find_tool(tools, "read_journal")
    mock_journal.read_by_type.return_value = [
        JournalEntry(
            entry_id=f"e{i}", timestamp="2026-01-01",
            entry_type="realization", content=f"entry-{i}",
        )
        for i in range(30)
    ]

    result = await read_tool.handler({"count": 5, "entry_type": "realization"})

    text = result["text"]
    # Only the last 5 of the 30 entries should be rendered.
    for i in range(25, 30):
        assert f"entry-{i}" in text
    assert "entry-24" not in text
    assert text.count("\n\n---\n\n") == 4
    mock_journal.read_by_type.assert_called_once_with("realization")


@pytest.mark.asyncio
async def test_read_journal_invalid_type() -> None:
    tools, _ = _make_journal_tools()
    read_tool = _find_tool(tools, "read_journal")
    with pytest.raises(ValueError, match="Invalid entry_type"):
        await read_tool.handler({"entry_type": "bogus"})


@pytest.mark.asyncio
async def test_search_journal_found() -> None:
    tools, mock_journal = _make_journal_tools()
    search_tool = _find_tool(tools, "search_journal")
    mock_journal.search.return_value = [
        JournalEntry(
            entry_id="e1", timestamp="2026-01-01",
            entry_type="reflection", content="Found the pattern",
        ),
    ]

    result = await search_tool.handler({"query": "pattern"})
    assert "Found the pattern" in result["text"]
    mock_journal.search.assert_called_once_with("pattern")


@pytest.mark.asyncio
async def test_search_journal_empty_query() -> None:
    tools, _ = _make_journal_tools()
    search_tool = _find_tool(tools, "search_journal")
    with pytest.raises(ValueError, match="empty"):
        await search_tool.handler({"query": ""})


@pytest.mark.asyncio
async def test_search_journal_no_results() -> None:
    tools, mock_journal = _make_journal_tools()
    search_tool = _find_tool(tools, "search_journal")
    mock_journal.search.return_value = []

    result = await search_tool.handler({"query": "nonexistent"})
    assert "No journal entries matching" in result["text"]


@pytest.mark.asyncio
async def test_search_journal_applies_head_cap() -> None:
    """When Journal.search returns more than 30 entries, the wrapper
    takes the first 30 (top relevance), not the last 30 (newest)."""
    tools, mock_journal = _make_journal_tools()
    search_tool = _find_tool(tools, "search_journal")
    mock_journal.search.return_value = [
        JournalEntry(
            entry_id=f"e{i}", timestamp="2026-01-01",
            entry_type="reflection", content=f"entry-marker-{i}",
        )
        for i in range(50)
    ]

    result = await search_tool.handler({"query": "marker"})
    # Top 30 (indices 0..29) should be present.
    for i in range(30):
        assert f"entry-marker-{i}" in result["text"]
    # Tail (indices 30..49) should be clipped.
    for i in range(30, 50):
        assert f"entry-marker-{i}" not in result["text"]


def test_create_journal_server_returns_server() -> None:
    mock_journal = MagicMock()
    with patch("maestro.tools.create_inproc_mcp_server", return_value="mock") as mock_create:
        from maestro.tools import create_journal_server

        result = create_journal_server(mock_journal)
    assert result == "mock"
    kwargs = mock_create.call_args.kwargs
    assert kwargs["name"] == "journal"
    assert len(kwargs["tools"]) == 3


# ===========================================================================
# Self-Awareness Server
# ===========================================================================


def _make_self_awareness_tools(conscious_episodes=None):  # type: ignore[no-untyped-def]
    mock_consciousness = MagicMock()
    mock_consciousness.load_self_model.return_value = SelfModel(
        identity_statements=["I am Maestro"],
    )

    from maestro.tools import create_self_awareness_server

    tools = _capture_server_tools(
        create_self_awareness_server, mock_consciousness,
        conscious_episodes,
    )
    return tools, mock_consciousness


def _conscious_item(**overrides: Any):  # type: ignore[no-untyped-def]
    """Build one real WorkspaceItem for the conscious-moment handler tests."""
    from maestro.conscious_workspace import build_workspace_item

    kw: dict[str, Any] = dict(
        item_id="i1", source_kind="audit_finding", source_ref="ref",
        observed_at="2026-01-01T00:00:00+00:00",
        trust="mechanical_observation", confidence=0.5, expires_at=None,
        summary="a bounded summary",
    )
    kw.update(overrides)
    return build_workspace_item(**kw)


def _conscious_episode(items):  # type: ignore[no-untyped-def]
    """Wrap *items* in a real ConsciousEpisode (no MagicMock in the record path)."""
    from maestro.conscious_workspace import (
        MAX_MOMENT_CHARS,
        MAX_MOMENT_ITEMS,
        AttentionSchema,
        ConsciousEpisode,
        ConsciousMoment,
    )

    schema = AttentionSchema(
        focus="deliver PR #42", uncertainty=0.3, capacity_used=len(items),
        omitted_count=0, permission_summary="read-only",
        blind_spots=("no audit snapshot",),
    )
    moment = ConsciousMoment(
        moment_id="cm-1", predecessor_id=None, continuity="genesis",
        assembled_at="2026-01-01T00:00:00+00:00", policy_version="v1",
        items=tuple(items), excluded=(), schema=schema,
        candidate_hash="ch", selection_hash="sh",
        max_items=MAX_MOMENT_ITEMS, max_chars=MAX_MOMENT_CHARS,
    )
    return ConsciousEpisode(
        moment=moment, receipts=(), cited_task_ids=(), cited_plan_ids=(),
        recorded_at="2026-01-01T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_read_self_model() -> None:
    tools, _mock_consciousness = _make_self_awareness_tools()
    read_tool = _find_tool(tools, "read_self_model")

    result = await read_tool.handler({})
    assert "I am Maestro" in result["text"]


def test_create_self_awareness_server_returns_server() -> None:
    mock_c = MagicMock()
    with patch("maestro.tools.create_inproc_mcp_server", return_value="mock") as mock_create:
        from maestro.tools import create_self_awareness_server

        result = create_self_awareness_server(mock_c)
    assert result == "mock"
    kwargs = mock_create.call_args.kwargs
    assert kwargs["name"] == "self_awareness"
    assert len(kwargs["tools"]) == 3


# --- conscious-workspace inspection tools (proposal 130, step 8) ------------


@pytest.mark.asyncio
async def test_read_conscious_moment_not_configured() -> None:
    """With no backend wired, the moment tool reports the workspace is off."""
    tools, _ = _make_self_awareness_tools()  # conscious_episodes=None
    read_tool = _find_tool(tools, "read_conscious_moment")
    result = await read_tool.handler({})
    assert result["text"] == "Conscious workspace is not configured."


@pytest.mark.asyncio
async def test_read_conscious_access_profile_not_configured() -> None:
    tools, _ = _make_self_awareness_tools()
    profile_tool = _find_tool(tools, "read_conscious_access_profile")
    result = await profile_tool.handler({})
    assert result["text"] == "Conscious workspace is not configured."


@pytest.mark.asyncio
async def test_read_conscious_moment_empty_ledger() -> None:
    """A configured but empty ledger returns the not-recorded message."""
    backend = MagicMock()
    backend.read_recent.return_value = []
    tools, _ = _make_self_awareness_tools(backend)
    read_tool = _find_tool(tools, "read_conscious_moment")
    result = await read_tool.handler({})
    assert result["text"] == "No conscious moment has been recorded yet."
    backend.read_recent.assert_called_once_with(1)
    backend.append.assert_not_called()


@pytest.mark.asyncio
async def test_read_conscious_access_profile_empty_ledger() -> None:
    backend = MagicMock()
    backend.read_recent.return_value = []
    tools, _ = _make_self_awareness_tools(backend)
    profile_tool = _find_tool(tools, "read_conscious_access_profile")
    result = await profile_tool.handler({})
    assert result["text"] == "No conscious moment has been recorded yet."
    backend.read_recent.assert_called_once_with(1)
    backend.append.assert_not_called()


@pytest.mark.asyncio
async def test_read_conscious_moment_renders_visible_item_and_focus() -> None:
    backend = MagicMock()
    backend.read_recent.return_value = [
        _conscious_episode([_conscious_item(item_id="visible-1")]),
    ]
    tools, _ = _make_self_awareness_tools(backend)
    read_tool = _find_tool(tools, "read_conscious_moment")
    result = await read_tool.handler({})
    assert "visible-1" in result["text"]
    assert "deliver PR #42" in result["text"]
    # The read-only surface labels itself honestly as self-inspection rather than
    # claiming a single broadcast consumer (it may be hosted by the task-execution
    # or the grounded-contemplation session).
    assert "self_inspection" in result["text"]
    assert "task_execution" not in result["text"]
    backend.read_recent.assert_called_once_with(1)
    backend.append.assert_not_called()


@pytest.mark.asyncio
async def test_read_conscious_moment_withholds_untrusted_external_item() -> None:
    """An untrusted-external item is absent from the block and counted withheld."""
    backend = MagicMock()
    backend.read_recent.return_value = [
        _conscious_episode(
            [
                _conscious_item(item_id="trusted-1"),
                _conscious_item(item_id="untrusted-1", trust="untrusted_external"),
            ],
        ),
    ]
    tools, _ = _make_self_awareness_tools(backend)
    read_tool = _find_tool(tools, "read_conscious_moment")
    result = await read_tool.handler({})
    assert "trusted-1" in result["text"]
    assert "untrusted-1" not in result["text"]
    assert "Withheld from this consumer: 1 item(s)" in result["text"]


@pytest.mark.asyncio
async def test_read_conscious_access_profile_carries_counts_no_summary() -> None:
    backend = MagicMock()
    backend.read_recent.return_value = [
        _conscious_episode(
            [_conscious_item(item_id="i-secret", summary="LEAK ME")],
        ),
    ]
    tools, _ = _make_self_awareness_tools(backend)
    profile_tool = _find_tool(tools, "read_conscious_access_profile")
    result = await profile_tool.handler({})
    payload = json.loads(result["text"])
    assert "excluded_counts" in payload
    assert payload["blind_spots"] == ["no audit snapshot"]
    assert payload["withheld_from_consumer"] == 0
    assert payload["visible_item_count"] == 1
    # The profile names its own read-only surface, not a broadcast consumer.
    assert payload["consumer"] == "self_inspection"
    assert "LEAK ME" not in result["text"]
    assert "i-secret" not in result["text"]
    backend.read_recent.assert_called_once_with(1)
    backend.append.assert_not_called()


# ===========================================================================
# Memory Server
# ===========================================================================


def _make_memory_tools():  # type: ignore[no-untyped-def]
    mock_memory = MagicMock()

    from maestro.tools import create_memory_server

    tools = _capture_server_tools(create_memory_server, mock_memory)
    return tools, mock_memory


@pytest.mark.asyncio
async def test_read_task_history_success() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_history")
    mock_memory.get_recent_tasks.return_value = [
        TaskRecord(
            task_id="t1", goal="Fix bug", plan=[], outcome="success",
            result_summary="Fixed", errors=[], duration_secs=10.0,
            timestamp="2026-01-01", key_findings="Found the root cause",
        ),
    ]

    result = await read_tool.handler({"count": 5})
    assert "Fix bug" in result["text"]
    assert "success" in result["text"]
    mock_memory.get_recent_tasks.assert_called_once_with(5)


@pytest.mark.asyncio
async def test_read_task_history_empty() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_history")
    mock_memory.get_recent_tasks.return_value = []

    result = await read_tool.handler({})
    assert "No task history" in result["text"]


@pytest.mark.asyncio
async def test_read_task_history_clamps_count() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_history")
    mock_memory.get_recent_tasks.return_value = []

    await read_tool.handler({"count": 100})
    mock_memory.get_recent_tasks.assert_called_once_with(30)


@pytest.mark.asyncio
async def test_read_task_history_invalid_count_defaults_to_ten() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_history")
    mock_memory.get_recent_tasks.return_value = []

    await read_tool.handler({"count": "bad"})
    mock_memory.get_recent_tasks.assert_called_once_with(10)


@pytest.mark.asyncio
async def test_read_task_history_includes_artifacts_when_present() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_history")
    mock_memory.get_recent_tasks.return_value = [
        TaskRecord(
            task_id="t1", goal="Investigate", plan=[], outcome="success",
            result_summary="done", errors=[], duration_secs=5.0,
            timestamp="2026-01-01", artifacts_dir="/work/artifacts/t1",
        ),
    ]
    with patch(
        "maestro.tools.list_dir_safe",
        return_value=["report.md", "data.json"],
    ):
        result = await read_tool.handler({})
    assert "Artifacts: /work/artifacts/t1" in result["text"]
    assert "report.md" in result["text"]
    assert "data.json" in result["text"]
    assert "Task ID: t1" in result["text"]


@pytest.mark.asyncio
async def test_read_task_history_omits_artifacts_when_empty() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_history")
    mock_memory.get_recent_tasks.return_value = [
        TaskRecord(
            task_id="t1", goal="Investigate", plan=[], outcome="success",
            result_summary="done", errors=[], duration_secs=5.0,
            timestamp="2026-01-01", artifacts_dir=None,
        ),
    ]
    with patch("maestro.tools.list_dir_safe", return_value=[]):
        result = await read_tool.handler({})
    assert "Artifacts:" not in result["text"]


def _big_task(task_id: str, goal: str) -> TaskRecord:
    return TaskRecord(
        task_id=task_id, goal=goal, plan=[], outcome="failure",
        result_summary="r" * 5000, errors=["e" * 5000] * 3,
        duration_secs=1.0, timestamp="2026-01-01",
        artifacts_dir=f"/w/artifacts/{task_id}",
    )


@pytest.mark.asyncio
async def test_read_task_history_caps_goal_bytes() -> None:
    from maestro.prompt_caps import MAX_TASK_GOAL_BYTES, TRUNCATED_SENTINEL

    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_history")
    mock_memory.get_recent_tasks.return_value = [
        _big_task("t1", "a" * 40_000),
        _big_task("t2", "é" * 20_000),
    ]
    with patch("maestro.tools.list_dir_safe", return_value=[]):
        result = await read_tool.handler({})
    text = result["text"]
    assert "\ufffd" not in text
    for marker, char in (("t1", "a"), ("t2", "é")):
        start = text.index(char * 10)
        end = text.index(f"  Task ID: {marker}")
        goal = text[start:end].rstrip("\n")
        assert goal.endswith(TRUNCATED_SENTINEL)
        assert len(goal.encode("utf-8")) <= MAX_TASK_GOAL_BYTES


@pytest.mark.asyncio
async def test_read_task_history_caps_errors_and_artifacts() -> None:
    from maestro.tools import (
        _TASK_HISTORY_ARTIFACTS_BYTES,
        _TASK_HISTORY_ERRORS_BYTES,
        _TASK_HISTORY_FINDINGS_BYTES,
    )

    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_history")
    mock_memory.get_recent_tasks.return_value = [_big_task("t1", "g")]
    names = [f"artifact_{i:04d}_" + "n" * 60 + ".md" for i in range(500)]
    with patch("maestro.tools.list_dir_safe", return_value=names):
        result = await read_tool.handler({})
    text = result["text"]
    errors = text[text.index("  Errors: ") + 10:text.index("\n  Artifacts: ")]
    assert len(errors.encode("utf-8")) <= _TASK_HISTORY_ERRORS_BYTES
    prefix = "  Artifacts: /w/artifacts/t1 ("
    artifacts = text[text.index(prefix) + len(prefix):].removesuffix(")")
    assert len(artifacts.encode("utf-8")) <= _TASK_HISTORY_ARTIFACTS_BYTES
    findings = text[text.index("  Findings: ") + 12:text.index("\n  Errors: ")]
    assert len(findings.encode("utf-8")) <= _TASK_HISTORY_FINDINGS_BYTES
    assert "omitted" not in text


@pytest.mark.asyncio
async def test_read_task_history_total_response_bounded() -> None:
    from maestro.tools import _TOOL_REPLY_MAX_BYTES

    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_history")
    mock_memory.get_recent_tasks.return_value = [
        _big_task(f"t{i}", "x" * 40_000) for i in range(30)
    ]
    names = ["n" * 200 for _ in range(100)]
    with patch("maestro.tools.list_dir_safe", return_value=names):
        result = await read_tool.handler({"count": 30})
    text = result["text"]
    assert len(text.encode("utf-8")) <= _TOOL_REPLY_MAX_BYTES
    kept = text.count("  Task ID: ")
    assert kept > 0
    assert text.startswith(f"[{30 - kept} older task(s) omitted")
    assert "Task ID: t29\n" in text
    assert "Task ID: t0\n" not in text
    assert text.index("Task ID: t28\n") < text.index("Task ID: t29\n")


def _make_artifact_task(
    task_id: str = "t1", artifacts_dir: str | None = "/w/artifacts/t1",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id, goal="g", plan=[], outcome="success",
        result_summary="s", errors=[], duration_secs=1.0,
        timestamp="2026-01-01", artifacts_dir=artifacts_dir,
    )


@pytest.mark.asyncio
async def test_read_task_artifact_success() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_artifact")
    mock_memory.get_recent_tasks.return_value = [_make_artifact_task()]
    with patch.object(Path, "resolve", lambda self, strict=False: self), \
            patch.object(Path, "is_file", lambda self: True), \
            patch.object(Path, "read_text", lambda self, **kw: "hello world"):
        result = await read_tool.handler(
            {"task_id": "t1", "filename": "report.md"},
        )
    assert result["text"] == "hello world"


@pytest.mark.asyncio
async def test_read_task_artifact_truncates() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_artifact")
    mock_memory.get_recent_tasks.return_value = [_make_artifact_task()]
    big = "x" * 5000
    with patch.object(Path, "resolve", lambda self, strict=False: self), \
            patch.object(Path, "is_file", lambda self: True), \
            patch.object(Path, "read_text", lambda self, **kw: big):
        result = await read_tool.handler(
            {"task_id": "t1", "filename": "big.txt"},
        )
    assert result["text"].startswith("x" * 4000)
    assert "[truncated — 1000 more chars]" in result["text"]


@pytest.mark.asyncio
async def test_read_task_artifact_missing_task() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_artifact")
    mock_memory.get_recent_tasks.return_value = []
    result = await read_tool.handler({"task_id": "nope", "filename": "x.txt"})
    assert "Unknown task_id" in result["text"]


@pytest.mark.asyncio
async def test_read_task_artifact_no_artifacts_dir() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_artifact")
    mock_memory.get_recent_tasks.return_value = [
        _make_artifact_task(artifacts_dir=None),
    ]
    result = await read_tool.handler({"task_id": "t1", "filename": "x.txt"})
    assert "no artifacts directory" in result["text"]


@pytest.mark.asyncio
async def test_read_task_artifact_file_missing() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_artifact")
    mock_memory.get_recent_tasks.return_value = [_make_artifact_task()]
    with patch.object(Path, "resolve", lambda self, strict=False: self), \
            patch.object(Path, "is_file", lambda self: False):
        result = await read_tool.handler(
            {"task_id": "t1", "filename": "ghost.txt"},
        )
    assert "not found" in result["text"]


@pytest.mark.asyncio
async def test_read_task_artifact_path_traversal_rejected() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_artifact")
    mock_memory.get_recent_tasks.return_value = [_make_artifact_task()]

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        # Simulate a separator-free ".." escaping the base via the parent
        # dir — the residual vector the containment guard still blocks now
        # that the separator check rejects any "/"- or "\\"-bearing name.
        s = str(self)
        if "etc/passwd" in s or ".." in s:
            return Path("/etc/passwd")
        return self

    with patch.object(Path, "resolve", fake_resolve):
        result = await read_tool.handler(
            {"task_id": "t1", "filename": ".."},
        )
    assert "Refusing to read path outside" in result["text"]


@pytest.mark.asyncio
async def test_read_task_artifact_rejects_path_separator() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_artifact")
    mock_memory.get_recent_tasks.return_value = [_make_artifact_task()]

    def forbidden_read(self: Path, **kw: Any) -> str:
        raise AssertionError("read_text must not be reached")

    with patch.object(Path, "read_text", forbidden_read):
        posix = await read_tool.handler(
            {"task_id": "t1", "filename": "subdir/report.md"},
        )
        backslash = await read_tool.handler(
            {"task_id": "t1", "filename": "sub\\file.txt"},
        )
    assert posix["text"] == "filename must be a bare name, not a path."
    assert backslash["text"] == "filename must be a bare name, not a path."


@pytest.mark.asyncio
async def test_read_task_artifact_resolve_error() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_artifact")
    mock_memory.get_recent_tasks.return_value = [_make_artifact_task()]

    def boom(self: Path, strict: bool = False) -> Path:
        raise OSError("bad path")

    with patch.object(Path, "resolve", boom):
        result = await read_tool.handler(
            {"task_id": "t1", "filename": "x.txt"},
        )
    assert "Could not resolve" in result["text"]


@pytest.mark.asyncio
async def test_read_task_artifact_read_error() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_artifact")
    mock_memory.get_recent_tasks.return_value = [_make_artifact_task()]

    def bad_read(self: Path, **kw: Any) -> str:
        raise OSError("io fail")

    with patch.object(Path, "resolve", lambda self, strict=False: self), \
            patch.object(Path, "is_file", lambda self: True), \
            patch.object(Path, "read_text", bad_read):
        result = await read_tool.handler(
            {"task_id": "t1", "filename": "x.txt"},
        )
    assert "Could not read artifact" in result["text"]


@pytest.mark.asyncio
async def test_read_task_artifact_missing_inputs() -> None:
    tools, _ = _make_memory_tools()
    read_tool = _find_tool(tools, "read_task_artifact")
    r1 = await read_tool.handler({"filename": "x.txt"})
    assert "task_id is required" in r1["text"]
    r2 = await read_tool.handler({"task_id": "t1"})
    assert "filename is required" in r2["text"]
    r3 = await read_tool.handler("not a dict")
    assert "task_id is required" in r3["text"]


@pytest.mark.asyncio
async def test_read_evolution_plans_success() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_evolution_plans")
    mock_memory.get_all_plans.return_value = [
        EvolutionPlan(
            plan_id="p1", title="Add feature X",
            category="capability", rationale="Needed for autonomy",
            proposed_changes=["change.py"], files_affected=["change.py"],
            expected_impact="medium", priority="capability",
            status="pending_review",
        ),
    ]

    result = await read_tool.handler({})
    assert "Add feature X" in result["text"]
    assert "pending_review" in result["text"]


@pytest.mark.asyncio
async def test_read_evolution_plans_filtered() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_evolution_plans")
    mock_memory.get_all_plans.return_value = [
        EvolutionPlan(
            plan_id="p1", title="Plan A",
            category="capability", rationale="r", proposed_changes=[],
            files_affected=[], expected_impact="low",
            status="approved",
        ),
        EvolutionPlan(
            plan_id="p2", title="Plan B",
            category="capability", rationale="r", proposed_changes=[],
            files_affected=[], expected_impact="low",
            status="rejected",
        ),
    ]

    result = await read_tool.handler({"status": "approved"})
    assert "Plan A" in result["text"]
    assert "Plan B" not in result["text"]


def _numbered_plans(n: int, status_for=lambda i: "approved"):  # type: ignore[no-untyped-def]
    return [
        EvolutionPlan(
            plan_id=f"p{i:03d}", title=f"Plan {i}",
            category="capability", rationale="r", proposed_changes=[],
            files_affected=[], expected_impact="low",
            status=status_for(i),
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_read_evolution_plans_caps_default_count_newest_first() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_evolution_plans")
    mock_memory.get_all_plans.return_value = _numbered_plans(100)

    text = (await read_tool.handler({}))["text"]
    assert text.count("[p") == 10
    assert text.index("Plan 99") < text.index("Plan 90")
    assert "Plan 89" not in text
    assert "90 more plans" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "expected"), [(500, 30), (0, 10), (-3, 10), ("x", 10)],
)
async def test_read_evolution_plans_count_clamped(
    count: object, expected: int,
) -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_evolution_plans")
    mock_memory.get_all_plans.return_value = _numbered_plans(100)

    text = (await read_tool.handler({"count": count}))["text"]
    assert text.count("[p") == expected


@pytest.mark.asyncio
async def test_read_evolution_plans_cap_applies_after_status_filter() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_evolution_plans")
    mock_memory.get_all_plans.return_value = _numbered_plans(
        100, lambda i: "approved" if i % 2 == 0 else "rejected",
    )

    text = (await read_tool.handler({"status": "approved"}))["text"]
    assert text.count("[p") == 10
    assert text.count("(approved)") == 10
    assert "rejected" not in text
    assert "40 more plans" in text


@pytest.mark.asyncio
async def test_read_evolution_plans_no_note_when_under_cap() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_evolution_plans")
    mock_memory.get_all_plans.return_value = _numbered_plans(3)

    text = (await read_tool.handler({}))["text"]
    assert text.count("[p") == 3
    assert "more plans" not in text


@pytest.mark.asyncio
async def test_read_evolution_plans_caps_title_length() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_evolution_plans")
    plans = _numbered_plans(1)
    plans[0].title = "T" * 500
    mock_memory.get_all_plans.return_value = plans

    text = (await read_tool.handler({}))["text"]
    assert "T" * 200 in text
    assert "T" * 201 not in text


@pytest.mark.asyncio
async def test_read_evolution_plans_empty() -> None:
    tools, mock_memory = _make_memory_tools()
    read_tool = _find_tool(tools, "read_evolution_plans")
    mock_memory.get_all_plans.return_value = []

    result = await read_tool.handler({})
    assert "No evolution plans" in result["text"]


@pytest.mark.asyncio
async def test_search_evolution_plans_found() -> None:
    tools, mock_memory = _make_memory_tools()
    search_tool = _find_tool(tools, "search_evolution_plans")
    mock_memory.search_plans.return_value = [
        EvolutionPlan(
            plan_id="p1", title="Refactor planner",
            category="capability", rationale="The planner needs work",
            proposed_changes=["tweak planner"], files_affected=["planner.py"],
            expected_impact="medium", priority="capability",
            status="pending_review",
        ),
        EvolutionPlan(
            plan_id="p2", title="Rework planner pipeline",
            category="capability",
            rationale="Pipeline refactor", proposed_changes=[],
            files_affected=[], expected_impact="low",
            status="merged",
        ),
    ]

    result = await search_tool.handler({"query": "planner"})
    assert "Refactor planner" in result["text"]
    assert "Rework planner pipeline" in result["text"]
    mock_memory.search_plans.assert_called_once_with("planner")


@pytest.mark.asyncio
async def test_search_evolution_plans_empty_query() -> None:
    tools, _ = _make_memory_tools()
    search_tool = _find_tool(tools, "search_evolution_plans")
    with pytest.raises(ValueError, match="empty"):
        await search_tool.handler({"query": ""})


@pytest.mark.asyncio
async def test_search_evolution_plans_whitespace_query() -> None:
    tools, _ = _make_memory_tools()
    search_tool = _find_tool(tools, "search_evolution_plans")
    with pytest.raises(ValueError, match="empty"):
        await search_tool.handler({"query": "   "})


@pytest.mark.asyncio
async def test_search_evolution_plans_no_results() -> None:
    tools, mock_memory = _make_memory_tools()
    search_tool = _find_tool(tools, "search_evolution_plans")
    mock_memory.search_plans.return_value = []

    result = await search_tool.handler({"query": "nonexistent"})
    assert "No evolution plans matching" in result["text"]


@pytest.mark.asyncio
async def test_search_evolution_plans_status_filter() -> None:
    """Status filter removes plans that don't match."""
    tools, mock_memory = _make_memory_tools()
    search_tool = _find_tool(tools, "search_evolution_plans")
    mock_memory.search_plans.return_value = [
        EvolutionPlan(
            plan_id="p1", title="Plan A",
            category="capability", rationale="r",
            proposed_changes=[], files_affected=[],
            expected_impact="low", status="approved",
        ),
        EvolutionPlan(
            plan_id="p2", title="Plan B",
            category="capability", rationale="r",
            proposed_changes=[], files_affected=[],
            expected_impact="low", status="rejected",
        ),
    ]

    result = await search_tool.handler(
        {"query": "plan", "status": "approved"}
    )
    assert "Plan A" in result["text"]
    assert "Plan B" not in result["text"]


@pytest.mark.asyncio
async def test_search_evolution_plans_applies_head_cap() -> None:
    """When search returns more than 20 plans, the wrapper takes the
    top 20 from the head (highest relevance), not the tail."""
    tools, mock_memory = _make_memory_tools()
    search_tool = _find_tool(tools, "search_evolution_plans")
    mock_memory.search_plans.return_value = [
        EvolutionPlan(
            plan_id=f"p{i}", title=f"Plan {i}",
            category="capability", rationale="r",
            proposed_changes=[], files_affected=[],
            expected_impact="low", status="pending_review",
        )
        for i in range(50)
    ]

    result = await search_tool.handler({"query": "plan"})
    # First 20 plans from the ranked results should appear.
    for i in range(20):
        assert f"[p{i}]" in result["text"]
    # Plans 20..49 should be clipped.
    for i in range(20, 50):
        assert f"[p{i}]" not in result["text"]


@pytest.mark.asyncio
async def test_search_evolution_plans_non_string_status_ignored() -> None:
    """A non-string status value is ignored (not used as a filter)."""
    tools, mock_memory = _make_memory_tools()
    search_tool = _find_tool(tools, "search_evolution_plans")
    mock_memory.search_plans.return_value = [
        EvolutionPlan(
            plan_id="p1", title="Only plan",
            category="capability", rationale="r",
            proposed_changes=[], files_affected=[],
            expected_impact="low", status="approved",
        ),
    ]

    result = await search_tool.handler({"query": "plan", "status": 123})
    assert "Only plan" in result["text"]


@pytest.mark.asyncio
async def test_search_evolution_plans_non_dict_input() -> None:
    """Non-dict input should be coerced to empty dict and raise empty query."""
    tools, _ = _make_memory_tools()
    search_tool = _find_tool(tools, "search_evolution_plans")
    with pytest.raises(ValueError, match="empty"):
        await search_tool.handler("not a dict")


def test_create_memory_server_returns_server() -> None:
    mock_m = MagicMock()
    with patch("maestro.tools.create_inproc_mcp_server", return_value="mock") as mock_create:
        from maestro.tools import create_memory_server

        result = create_memory_server(mock_m)
    assert result == "mock"
    kwargs = mock_create.call_args.kwargs
    assert kwargs["name"] == "memory"
    assert len(kwargs["tools"]) == 4


# ===========================================================================
# Issues Server
# ===========================================================================


def _make_issues_tools():  # type: ignore[no-untyped-def]
    mock_tracker = MagicMock()

    from maestro.tools import create_issues_server

    tools = _capture_server_tools(create_issues_server, mock_tracker)
    return tools, mock_tracker


@pytest.mark.asyncio
async def test_list_open_issues_success() -> None:
    tools, mock_tracker = _make_issues_tools()
    list_tool = _find_tool(tools, "list_open_issues")
    mock_tracker.list_open.return_value = [
        Issue(number=1, title="Bug in parser", labels=["bug"], body="", created_at="2026-01-01"),
        Issue(number=2, title="Add feature", labels=["enhancement"], body="", created_at="2026-01-01"),
    ]

    result = await list_tool.handler({})
    assert "#1: Bug in parser" in result["text"]
    assert "#2: Add feature" in result["text"]


@pytest.mark.asyncio
async def test_list_open_issues_with_labels() -> None:
    tools, mock_tracker = _make_issues_tools()
    list_tool = _find_tool(tools, "list_open_issues")
    mock_tracker.list_open.return_value = []

    await list_tool.handler({"labels": ["bug"]})
    mock_tracker.list_open.assert_called_once_with(
        labels=["bug"], exclude_labels=["crash"], page_size=None,
    )


@pytest.mark.asyncio
async def test_list_open_issues_empty() -> None:
    tools, mock_tracker = _make_issues_tools()
    list_tool = _find_tool(tools, "list_open_issues")
    mock_tracker.list_open.return_value = []

    result = await list_tool.handler({})
    assert "No open issues" in result["text"]


@pytest.mark.asyncio
async def test_list_open_issues_shows_created_at() -> None:
    """The MCP issue list tool should surface created_at so the agent
    can see issue age and prioritize oldest-first."""
    tools, mock_tracker = _make_issues_tools()
    list_tool = _find_tool(tools, "list_open_issues")
    mock_tracker.list_open.return_value = [
        Issue(number=1, title="Old bug", labels=["bug"], body="", created_at="2026-01-15T00:00:00Z"),
        Issue(number=5, title="New bug", labels=["bug"], body="", created_at="2026-05-20T10:30:00Z"),
    ]

    result = await list_tool.handler({})
    text = result["text"]
    assert "(created: 2026-01-15T00:00:00Z)" in text
    assert "(created: 2026-05-20T10:30:00Z)" in text


@pytest.mark.asyncio
async def test_list_open_issues_omits_created_at_when_empty() -> None:
    """Issues with no created_at should not show an empty suffix."""
    tools, mock_tracker = _make_issues_tools()
    list_tool = _find_tool(tools, "list_open_issues")
    mock_tracker.list_open.return_value = [
        Issue(number=3, title="No date", labels=[], body="", created_at=""),
    ]

    result = await list_tool.handler({})
    text = result["text"]
    assert "(created:" not in text
    assert "#3: No date [none]" in text


@pytest.mark.asyncio
async def test_list_open_issues_non_list_labels_ignored() -> None:
    tools, mock_tracker = _make_issues_tools()
    list_tool = _find_tool(tools, "list_open_issues")
    mock_tracker.list_open.return_value = []

    await list_tool.handler({"labels": "not-a-list"})
    mock_tracker.list_open.assert_called_once_with(
        labels=None, exclude_labels=["crash"], page_size=None,
    )


@pytest.mark.asyncio
async def test_create_issue_success() -> None:
    tools, mock_tracker = _make_issues_tools()
    create_tool = _find_tool(tools, "create_issue")
    mock_tracker.create.return_value = 42

    result = await create_tool.handler({
        "title": "New bug",
        "body": "Description of the bug",
        "labels": ["bug"],
    })
    assert "#42" in result["text"]
    mock_tracker.create.assert_called_once_with(
        "New bug", "Description of the bug", labels=["bug"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["gap:maestro", " Gap:Phase"])
async def test_create_issue_refuses_gap_label(label: str) -> None:
    """Gap issues open only through the evolution-fuel door (proposal 155)."""
    tools, mock_tracker = _make_issues_tools()
    create_tool = _find_tool(tools, "create_issue")

    result = await create_tool.handler({
        "title": "x", "body": "y", "labels": ["bug", label],
    })
    assert result["is_error"] is True
    assert "Refused" in result["text"]
    mock_tracker.create.assert_not_called()


@pytest.mark.asyncio
async def test_create_issue_empty_title() -> None:
    tools, _ = _make_issues_tools()
    create_tool = _find_tool(tools, "create_issue")
    with pytest.raises(ValueError, match="empty"):
        await create_tool.handler({"title": "", "body": "test"})


@pytest.mark.asyncio
async def test_create_issue_non_string_body_defaults_empty() -> None:
    tools, mock_tracker = _make_issues_tools()
    create_tool = _find_tool(tools, "create_issue")
    mock_tracker.create.return_value = 10

    await create_tool.handler({"title": "Test", "body": 123})
    mock_tracker.create.assert_called_once_with("Test", "", labels=None)


@pytest.mark.asyncio
async def test_create_issue_non_list_labels_ignored() -> None:
    tools, mock_tracker = _make_issues_tools()
    create_tool = _find_tool(tools, "create_issue")
    mock_tracker.create.return_value = 11

    await create_tool.handler({"title": "Test", "body": "desc", "labels": "bug"})
    mock_tracker.create.assert_called_once_with("Test", "desc", labels=None)


@pytest.mark.asyncio
async def test_create_issue_no_repo() -> None:
    tools, mock_tracker = _make_issues_tools()
    create_tool = _find_tool(tools, "create_issue")
    mock_tracker.create.return_value = None

    result = await create_tool.handler({"title": "test", "body": "test"})
    assert "Failed" in result["text"]


@pytest.mark.asyncio
async def test_close_issue_success() -> None:
    tools, mock_tracker = _make_issues_tools()
    close_tool = _find_tool(tools, "close_issue")
    mock_tracker.meta.return_value = IssueMeta("open", "bug", ("bug",))
    mock_tracker.close.return_value = True

    result = await close_tool.handler({"number": 5})
    assert "#5 closed" in result["text"]
    mock_tracker.close.assert_called_once_with(5)


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", [None, IssueMeta("open", "t", ("gap:maestro",))])
async def test_close_issue_refuses_gap_or_unreadable(meta: IssueMeta | None) -> None:
    tools, mock_tracker = _make_issues_tools()
    close_tool = _find_tool(tools, "close_issue")
    mock_tracker.meta.return_value = meta
    result = await close_tool.handler({"number": 5})
    assert result["is_error"] is True
    assert result["text"].startswith("Refused")
    mock_tracker.close.assert_not_called()


@pytest.mark.asyncio
async def test_close_issue_reports_failure() -> None:
    tools, mock_tracker = _make_issues_tools()
    close_tool = _find_tool(tools, "close_issue")
    mock_tracker.meta.return_value = IssueMeta("open", "bug", ())
    mock_tracker.close.return_value = False
    result = await close_tool.handler({"number": 5})
    assert result["is_error"] is True
    assert result["text"] == "Failed to close issue #5."


@pytest.mark.asyncio
async def test_close_issue_invalid_number() -> None:
    tools, _ = _make_issues_tools()
    close_tool = _find_tool(tools, "close_issue")
    with pytest.raises(ValueError, match="positive integer"):
        await close_tool.handler({"number": -1})

    with pytest.raises(ValueError, match="positive integer"):
        await close_tool.handler({"number": 0})

    with pytest.raises(ValueError, match="positive integer"):
        await close_tool.handler({"number": True})


@pytest.mark.asyncio
async def test_list_open_issues_respects_page_size() -> None:
    mock_tracker = MagicMock()

    from maestro.tools import create_issues_server

    tools = _capture_server_tools(create_issues_server, mock_tracker, page_size=2)
    list_tool = _find_tool(tools, "list_open_issues")
    mock_tracker.list_open.return_value = [
        Issue(number=1, title="Bug 1", labels=[], body="", created_at=""),
        Issue(number=2, title="Bug 2", labels=[], body="", created_at=""),
        Issue(number=3, title="Bug 3", labels=[], body="", created_at=""),
    ]

    result = await list_tool.handler({})
    mock_tracker.list_open.assert_called_once_with(
        labels=None, exclude_labels=["crash"], page_size=2,
    )
    assert "#1" in result["text"]
    assert "#2" in result["text"]
    assert "#3" not in result["text"]


def test_create_issues_server_returns_server() -> None:
    mock_t = MagicMock()
    with patch("maestro.tools.create_inproc_mcp_server", return_value="mock") as mock_create:
        from maestro.tools import create_issues_server

        result = create_issues_server(mock_t)
    assert result == "mock"
    kwargs = mock_create.call_args.kwargs
    assert kwargs["name"] == "issues"
    assert len(kwargs["tools"]) == 3


# ===========================================================================
# Executor agent_tools wiring
# ===========================================================================


def test_executor_build_agent_tools_options_empty() -> None:
    """When no agent_tools are provided, returns empty lists."""
    from maestro.executor import Executor

    executor = Executor()
    tools, mcp = executor._build_agent_tools_options()
    assert tools == []
    assert mcp == {}


def test_executor_build_agent_tools_options_with_tools() -> None:
    """Registry-shaped agent_tools materialise into qualified names + wire-shape servers."""
    from mcp.server import Server

    from maestro.executor import Executor
    from maestro.inproc_mcp import InprocTool

    async def _noop(_: Any) -> dict[str, Any]:
        return {"type": "text", "text": ""}

    synthetic = InprocTool(
        name="write_journal_entry",
        description="synthetic",
        input_schema={"type": "object", "properties": {}},
        handler=_noop,
    )
    agent_tools = {"registry": {"journal": [synthetic]}}
    executor = Executor(agent_tools=agent_tools)
    tools, mcp = executor._build_agent_tools_options()
    assert tools == ["mcp__journal__write_journal_entry"]
    assert set(mcp.keys()) == {"journal"}
    assert mcp["journal"]["type"] == "sdk"
    assert mcp["journal"]["name"] == "journal"
    assert isinstance(mcp["journal"]["instance"], Server)


# ---------------------------------------------------------------------------
# diagnostics_tools
# ---------------------------------------------------------------------------


def _snapshot(**overrides: Any) -> DiagnosticSnapshot:
    """Build a DiagnosticSnapshot with sensible defaults for tool tests."""
    fields: dict[str, Any] = {
        "recent_error_count": 3,
        "has_quota_warning": False,
        "has_rate_limit": True,
        "consecutive_failures": 1,
        "summary": "3 recent errors; rate limit hit",
    }
    fields.update(overrides)
    return DiagnosticSnapshot(**fields)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 50),      # missing -> default
        ("nope", 50),    # non-int -> default
        (True, 50),      # bool rejected -> default
        (0, 1),          # below lo -> clamped to lo
        (1000, 500),     # above hi -> clamped to hi
        (25, 25),        # in range -> unchanged
    ],
)
def test_clamp_int(value: Any, expected: int) -> None:
    """_clamp_int coerces/clamps into [lo, hi] with a default fallback."""
    from maestro.tools import _clamp_int

    assert _clamp_int(value, 50, 1, 500) == expected


def test_diagnostics_tools_builds_three_named_tools() -> None:
    """diagnostics_tools exposes exactly the three expected tool names."""
    from maestro.tools import diagnostics_tools

    names = {t.name for t in diagnostics_tools(Path("/log"))}
    assert names == {
        "get_diagnostic_snapshot",
        "get_recent_errors",
        "is_diagnostics_noteworthy",
    }


@pytest.mark.asyncio
async def test_get_diagnostic_snapshot_returns_json() -> None:
    """get_diagnostic_snapshot serialises the snapshot dataclass as JSON."""
    from maestro.tools import diagnostics_tools

    tools = diagnostics_tools(Path("/var/log/maestro.log"))
    snap_tool = _find_tool(tools, "get_diagnostic_snapshot")
    with patch(
        "maestro.tools.build_diagnostic_snapshot", return_value=_snapshot(),
    ) as mock_build:
        result = await snap_tool.handler({"n_entries": 100})

    assert result["type"] == "text"
    payload = json.loads(result["text"])
    assert payload["recent_error_count"] == 3
    assert payload["has_rate_limit"] is True
    assert payload["summary"] == "3 recent errors; rate limit hit"
    mock_build.assert_called_once()
    assert mock_build.call_args.kwargs["n_entries"] == 100


@pytest.mark.asyncio
async def test_get_diagnostic_snapshot_clamps_n_entries() -> None:
    """n_entries above the ceiling is clamped to 500 before the build call."""
    from maestro.tools import diagnostics_tools

    tools = diagnostics_tools(Path("/log"))
    snap_tool = _find_tool(tools, "get_diagnostic_snapshot")
    with patch(
        "maestro.tools.build_diagnostic_snapshot", return_value=_snapshot(),
    ) as mock_build:
        await snap_tool.handler({"n_entries": 100000})

    assert mock_build.call_args.kwargs["n_entries"] == 500


@pytest.mark.asyncio
async def test_get_diagnostic_snapshot_none_log_path() -> None:
    """A None log_path yields the no-log fallback without raising."""
    from maestro.tools import diagnostics_tools

    tools = diagnostics_tools(None)
    snap_tool = _find_tool(tools, "get_diagnostic_snapshot")
    result = await snap_tool.handler({})
    assert result["text"] == "No diagnostic log data available."


@pytest.mark.asyncio
async def test_get_diagnostic_snapshot_threads_memory_dir() -> None:
    """memory_dir is forwarded so cost-trend parity with contemplation holds."""
    from maestro.tools import diagnostics_tools

    mem = Path("/var/lib/maestro")
    tools = diagnostics_tools(Path("/log"), mem)
    snap_tool = _find_tool(tools, "get_diagnostic_snapshot")
    with patch(
        "maestro.tools.build_diagnostic_snapshot", return_value=_snapshot(),
    ) as mock_build:
        await snap_tool.handler({})

    assert mock_build.call_args.kwargs["memory_dir"] == mem


@pytest.mark.asyncio
async def test_is_diagnostics_noteworthy_threads_memory_dir() -> None:
    """The noteworthy gate also forwards memory_dir (cost-spike parity)."""
    from maestro.tools import diagnostics_tools

    mem = Path("/var/lib/maestro")
    tools = diagnostics_tools(Path("/log"), mem)
    gate_tool = _find_tool(tools, "is_diagnostics_noteworthy")
    with patch(
        "maestro.tools.build_diagnostic_snapshot", return_value=_snapshot(),
    ) as mock_build, patch("maestro.tools.is_noteworthy", return_value=False):
        await gate_tool.handler({})

    assert mock_build.call_args.kwargs["memory_dir"] == mem


@pytest.mark.asyncio
async def test_get_recent_errors_formats_and_filters() -> None:
    """get_recent_errors formats entries and requests ERROR-level filtering."""
    from maestro.tools import diagnostics_tools

    entries = [
        {
            "timestamp": "2026-07-17T10:00:00",
            "logger": "maestro",
            "message": "boom",
        },
        {
            "timestamp": "2026-07-17T10:01:00",
            "logger": "maestro.sdk",
            "message": "bang",
        },
    ]
    tools = diagnostics_tools(Path("/log"))
    err_tool = _find_tool(tools, "get_recent_errors")
    with patch(
        "maestro.tools.parse_app_log_tail", return_value=entries,
    ) as mock_parse:
        result = await err_tool.handler({"n_entries": 20, "limit": 5})

    assert mock_parse.call_args.kwargs["level"] == "ERROR"
    assert mock_parse.call_args.kwargs["n"] == 20
    lines = result["text"].splitlines()
    assert lines[0] == "[2026-07-17T10:00:00 | ERROR | maestro] boom"
    assert lines[1] == "[2026-07-17T10:01:00 | ERROR | maestro.sdk] bang"


@pytest.mark.asyncio
async def test_get_recent_errors_applies_limit() -> None:
    """Only the last `limit` ERROR entries are returned."""
    from maestro.tools import diagnostics_tools

    entries = [
        {"timestamp": f"t{i}", "logger": "m", "message": f"msg{i}"}
        for i in range(5)
    ]
    tools = diagnostics_tools(Path("/log"))
    err_tool = _find_tool(tools, "get_recent_errors")
    with patch("maestro.tools.parse_app_log_tail", return_value=entries):
        result = await err_tool.handler({"limit": 2})

    lines = result["text"].splitlines()
    assert len(lines) == 2
    assert "msg3" in lines[0]
    assert "msg4" in lines[1]


@pytest.mark.asyncio
async def test_get_recent_errors_truncates_long_message() -> None:
    """A very long message is truncated to the 500-char cap plus an ellipsis."""
    from maestro.tools import diagnostics_tools

    entries = [{"timestamp": "t", "logger": "m", "message": "x" * 2000}]
    tools = diagnostics_tools(Path("/log"))
    err_tool = _find_tool(tools, "get_recent_errors")
    with patch("maestro.tools.parse_app_log_tail", return_value=entries):
        result = await err_tool.handler({})

    assert "x" * 500 in result["text"]
    assert "x" * 501 not in result["text"]
    assert result["text"].endswith("…")


@pytest.mark.asyncio
async def test_get_recent_errors_aggregate_cap() -> None:
    """Aggregate output is bounded: oldest errors drop, newest are kept."""
    from maestro.tools import diagnostics_tools

    # 50 max-length errors would be ~25 KB; the 4000-char aggregate cap keeps
    # only the newest handful and notes how many were omitted.
    entries = [
        {"timestamp": f"t{i}", "logger": "m", "message": "x" * 500}
        for i in range(50)
    ]
    tools = diagnostics_tools(Path("/log"))
    err_tool = _find_tool(tools, "get_recent_errors")
    with patch("maestro.tools.parse_app_log_tail", return_value=entries):
        result = await err_tool.handler({"limit": 50})

    text = result["text"]
    # Bounded near the 4000-char cap (the short omission notice is prepended
    # after the budget loop, so a small overshoot is expected) — and far below
    # the ~26 KB an uncapped join of 50 max-length errors would produce.
    assert len(text) < 4200
    lines = text.splitlines()
    # First line is the omission notice; the rest are the newest errors.
    assert lines[0].startswith("… ")
    assert "omitted (output capped)" in lines[0]
    # The newest error (t49) survives; the oldest (t0) is dropped.
    assert any("t49" in line for line in lines)
    assert not any("t0 " in line or "] t0" in line for line in lines)


@pytest.mark.asyncio
async def test_get_recent_errors_no_omission_note_when_under_cap() -> None:
    """A small result set emits no omission notice."""
    from maestro.tools import diagnostics_tools

    entries = [
        {"timestamp": f"t{i}", "logger": "m", "message": f"msg{i}"}
        for i in range(3)
    ]
    tools = diagnostics_tools(Path("/log"))
    err_tool = _find_tool(tools, "get_recent_errors")
    with patch("maestro.tools.parse_app_log_tail", return_value=entries):
        result = await err_tool.handler({"limit": 10})

    lines = result["text"].splitlines()
    assert len(lines) == 3
    assert not lines[0].startswith("… ")


@pytest.mark.asyncio
async def test_get_recent_errors_empty() -> None:
    """No ERROR entries yields the empty-log message."""
    from maestro.tools import diagnostics_tools

    tools = diagnostics_tools(Path("/log"))
    err_tool = _find_tool(tools, "get_recent_errors")
    with patch("maestro.tools.parse_app_log_tail", return_value=[]):
        result = await err_tool.handler({})
    assert result["text"] == "No recent errors found."


@pytest.mark.asyncio
async def test_get_recent_errors_none_log_path() -> None:
    """A None log_path yields the fallback without calling the parser."""
    from maestro.tools import diagnostics_tools

    tools = diagnostics_tools(None)
    err_tool = _find_tool(tools, "get_recent_errors")
    with patch("maestro.tools.parse_app_log_tail") as mock_parse:
        result = await err_tool.handler({})
    assert result["text"] == "No diagnostic log data available."
    mock_parse.assert_not_called()


@pytest.mark.asyncio
async def test_is_diagnostics_noteworthy_true() -> None:
    """is_diagnostics_noteworthy returns 'true' when the gate is set."""
    from maestro.tools import diagnostics_tools

    tools = diagnostics_tools(Path("/log"))
    gate_tool = _find_tool(tools, "is_diagnostics_noteworthy")
    with patch(
        "maestro.tools.build_diagnostic_snapshot", return_value=_snapshot(),
    ), patch("maestro.tools.is_noteworthy", return_value=True):
        result = await gate_tool.handler({"n_entries": 10})
    assert result["text"] == "true"


@pytest.mark.asyncio
async def test_is_diagnostics_noteworthy_false() -> None:
    """is_diagnostics_noteworthy returns 'false' when the gate is clear."""
    from maestro.tools import diagnostics_tools

    tools = diagnostics_tools(Path("/log"))
    gate_tool = _find_tool(tools, "is_diagnostics_noteworthy")
    with patch(
        "maestro.tools.build_diagnostic_snapshot", return_value=_snapshot(),
    ), patch("maestro.tools.is_noteworthy", return_value=False):
        result = await gate_tool.handler({})
    assert result["text"] == "false"


@pytest.mark.asyncio
async def test_is_diagnostics_noteworthy_none_log_path() -> None:
    """A None log_path yields the no-log fallback without raising."""
    from maestro.tools import diagnostics_tools

    tools = diagnostics_tools(None)
    gate_tool = _find_tool(tools, "is_diagnostics_noteworthy")
    result = await gate_tool.handler({})
    assert result["text"] == "No diagnostic log data available."


def _journal_entry(i: int, content: str) -> JournalEntry:
    return JournalEntry(
        entry_id=f"e{i}", timestamp=f"2026-01-01T00:00:{i:02d}",
        entry_type="reflection", content=content,
    )


@pytest.mark.asyncio
async def test_read_journal_total_response_bounded() -> None:
    from maestro.tools import _JOURNAL_ENTRY_MAX_BYTES, _TOOL_REPLY_MAX_BYTES

    tools, mock_journal = _make_journal_tools()
    read_tool = _find_tool(tools, "read_journal")
    mock_journal.read_recent.return_value = [
        _journal_entry(i, "x" * 100_000) for i in range(30)
    ]
    result = await read_tool.handler({"count": 30})
    text = result["text"]
    assert len(text.encode("utf-8")) <= _TOOL_REPLY_MAX_BYTES
    blocks = text.split("\n\n---\n\n")
    kept = len(blocks) - 1
    assert kept > 0
    assert blocks[0] == (
        f"[{30 - kept} older journal entries omitted — response byte "
        f"budget reached]"
    )
    assert "2026-01-01T00:00:29" in text
    assert "2026-01-01T00:00:00 " not in text
    assert text.index("00:00:28") < text.index("00:00:29")
    for block in blocks[1:]:
        body = block.split("\n", 1)[1]
        assert len(body.encode("utf-8")) <= _JOURNAL_ENTRY_MAX_BYTES


@pytest.mark.asyncio
async def test_search_journal_total_response_bounded() -> None:
    from maestro.tools import _TOOL_REPLY_MAX_BYTES

    tools, mock_journal = _make_journal_tools()
    search_tool = _find_tool(tools, "search_journal")
    # Rank order deliberately opposite to time order.
    mock_journal.search.return_value = [
        _journal_entry(29 - i, "y" * 100_000) for i in range(30)
    ]
    result = await search_tool.handler({"query": "y"})
    text = result["text"]
    assert len(text.encode("utf-8")) <= _TOOL_REPLY_MAX_BYTES
    blocks = text.split("\n\n---\n\n")
    kept = len(blocks) - 1
    assert blocks[0] == (
        f"[{30 - kept} lower-ranked matches omitted — response byte "
        f"budget reached]"
    )
    assert blocks[1].startswith("[2026-01-01T00:00:29 ")
    assert "2026-01-01T00:00:00 " not in text


@pytest.mark.asyncio
async def test_journal_single_omission_notice_is_singular() -> None:
    from maestro.tools import (
        _JOURNAL_SEPARATOR,
        _TOOL_REPLY_MAX_BYTES,
        _TOOL_REPLY_NOTICE_RESERVE,
    )

    tools, mock_journal = _make_journal_tools()
    big = "z" * 3000
    per_entry = len(f"[2026-01-01T00:00:00 | reflection]\n{big}")
    sep = len(_JOURNAL_SEPARATOR)
    fits = (_TOOL_REPLY_MAX_BYTES - _TOOL_REPLY_NOTICE_RESERVE) // (per_entry + sep)
    entries = [_journal_entry(i, big) for i in range(fits + 1)]
    mock_journal.read_recent.return_value = entries
    mock_journal.search.return_value = entries
    read_text = (await _find_tool(tools, "read_journal").handler({}))["text"]
    search_text = (
        await _find_tool(tools, "search_journal").handler({"query": "z"})
    )["text"]
    assert read_text.startswith("[1 older journal entry omitted")
    assert search_text.startswith("[1 lower-ranked match omitted")


@pytest.mark.asyncio
async def test_read_journal_small_entries_no_notice() -> None:
    tools, mock_journal = _make_journal_tools()
    mock_journal.read_recent.return_value = [
        _journal_entry(i, f"short-{i}") for i in range(3)
    ]
    text = (await _find_tool(tools, "read_journal").handler({}))["text"]
    assert "omitted" not in text
    assert text.split("\n\n---\n\n")[0] == (
        "[2026-01-01T00:00:00 | reflection]\nshort-0"
    )
