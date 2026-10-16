"""Tests for telegram_tools — MCP server factory and tool handlers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.messaging import Message
from maestro.telegram_tools import (
    _READ_DESCRIPTION,
    _READ_SCHEMA,
    SERVER_KEY,
    TELEGRAM_READ_HEADER_FIELD_CHAR_CAP,
    TELEGRAM_READ_MAX_LIMIT,
    TELEGRAM_READ_MESSAGE_CHAR_CAP,
    TELEGRAM_READ_RESULT_CHAR_CAP,
    TOOL_NAMES,
    _handle_read,
    _handle_send,
    create_telegram_mcp_server,
)


def _make_mock_notifier() -> MagicMock:
    """Create a mock Notifier with AsyncMock methods."""
    notifier = MagicMock()
    notifier.notify = AsyncMock()
    return notifier


def _make_mock_store() -> MagicMock:
    """Create a mock MessageStore."""
    store = MagicMock()
    store.get_unread = MagicMock(return_value=[])
    store.get_recent = MagicMock(return_value=[])
    store.mark_read = MagicMock()
    return store


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_create_telegram_mcp_server_returns_config() -> None:
    notifier = _make_mock_notifier()
    store = _make_mock_store()
    config = create_telegram_mcp_server(notifier, store)
    assert config is not None
    assert config["type"] == "sdk"
    assert config["name"] == "maestro_telegram"


def test_server_key_matches_factory_name() -> None:
    assert SERVER_KEY == "maestro_telegram"


def test_tool_names_use_mcp_prefix() -> None:
    for name in TOOL_NAMES:
        assert name.startswith(f"mcp__{SERVER_KEY}__")
    assert len(TOOL_NAMES) == 2


def test_read_tool_description_names_both_senders() -> None:
    # Recent-mode (unread_only=false) returns messages from both senders via
    # get_recent, so the advertised description must name both — not just Thomas.
    assert "Maestro" in _READ_DESCRIPTION
    assert "Thomas" in _READ_DESCRIPTION


# ---------------------------------------------------------------------------
# telegram_send handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_send_basic() -> None:
    notifier = _make_mock_notifier()
    result = await _handle_send(notifier, {"message": "Hello Thomas!"})

    assert result["type"] == "text"
    assert "Hello Thomas!" in result["text"]
    notifier.notify.assert_called_once_with("Hello Thomas!")


@pytest.mark.asyncio
async def test_handle_send_empty_message() -> None:
    notifier = _make_mock_notifier()
    result = await _handle_send(notifier, {"message": ""})

    assert result.get("is_error") is True
    assert "empty" in result["text"].lower()
    notifier.notify.assert_not_called()


@pytest.mark.asyncio
async def test_handle_send_whitespace_only() -> None:
    notifier = _make_mock_notifier()
    result = await _handle_send(notifier, {"message": "   "})

    assert result.get("is_error") is True
    notifier.notify.assert_not_called()


@pytest.mark.asyncio
async def test_handle_send_missing_message_key() -> None:
    notifier = _make_mock_notifier()
    result = await _handle_send(notifier, {})

    assert result.get("is_error") is True
    notifier.notify.assert_not_called()


@pytest.mark.asyncio
async def test_handle_send_truncates_in_response() -> None:
    notifier = _make_mock_notifier()
    long_msg = "x" * 200
    result = await _handle_send(notifier, {"message": long_msg})

    # The response text should be truncated to 100 chars preview
    response_text = result["text"]
    assert len(response_text) < 200
    notifier.notify.assert_called_once_with(long_msg)


@pytest.mark.asyncio
async def test_handle_send_notify_failure_returns_is_error() -> None:
    """A raised notify() becomes a typed is_error response, not an opaque failure."""
    notifier = _make_mock_notifier()
    notifier.notify = AsyncMock(side_effect=RuntimeError("telegram down"))

    result = await _handle_send(notifier, {"message": "hi"})

    assert result.get("is_error") is True
    assert "telegram down" in result["text"]
    notifier.notify.assert_called_once_with("hi")


# ---------------------------------------------------------------------------
# telegram_read_messages handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_read_no_messages() -> None:
    store = _make_mock_store()
    result = await _handle_read(store, {})

    assert "No unread messages" in result["text"]
    store.get_unread.assert_called_once()


@pytest.mark.asyncio
async def test_handle_read_with_unread_messages() -> None:
    store = _make_mock_store()
    store.get_unread.return_value = [
        Message(
            message_id="m1", timestamp="2026-04-01T10:00:00",
            sender="thomas", content="Hi Maestro!",
            message_type="info", read=False,
        ),
        Message(
            message_id="m2", timestamp="2026-04-01T10:05:00",
            sender="thomas", content="How are you?",
            message_type="info", read=False,
        ),
    ]
    result = await _handle_read(store, {})

    text = result["text"]
    assert "Hi Maestro!" in text
    assert "How are you?" in text
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
async def test_handle_read_unread_only_false() -> None:
    store = _make_mock_store()
    store.get_recent.return_value = [
        Message(
            message_id="m1", timestamp="2026-04-01T10:00:00",
            sender="thomas", content="Old message",
            message_type="info", read=True,
        ),
    ]
    result = await _handle_read(store, {"unread_only": False, "limit": 5})

    text = result["text"]
    assert "Old message" in text
    store.get_recent.assert_called_once_with(5)
    # Already read, should not mark_read_many
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
async def test_unread_messages_not_marked_read_when_unread_only_false() -> None:
    """When unread_only=False, unread messages are shown but left for loop ingestion (#4020)."""
    store = _make_mock_store()
    unread_msg = Message(
        message_id="m1", timestamp="2026-04-05T00:00:00",
        sender="thomas", content="hello",
        message_type="instruction", read=False,
    )
    read_msg = Message(
        message_id="m2", timestamp="2026-04-05T00:01:00",
        sender="thomas", content="old",
        message_type="instruction", read=True,
    )
    store.get_recent.return_value = [read_msg, unread_msg]

    result = await _handle_read(store, {"unread_only": False, "limit": 10})

    # Display only: _process_messages owns the read flag
    store.mark_read_many.assert_not_called()
    text = result["text"]
    assert "hello" in text
    assert "old" in text


@pytest.mark.asyncio
async def test_handle_read_respects_limit() -> None:
    store = _make_mock_store()
    msgs = [
        Message(
            message_id=f"m{i}", timestamp=f"2026-04-01T10:0{i}:00",
            sender="thomas", content=f"Message {i}",
            message_type="info", read=False,
        )
        for i in range(5)
    ]
    store.get_unread.return_value = msgs
    result = await _handle_read(store, {"limit": 2})

    text = result["text"]
    assert "Message 0" in text
    assert "Message 1" in text
    assert "Message 4" not in text


def _backlog(count: int, content: str, *, read: bool = False) -> list[Message]:
    return [
        Message(
            message_id=f"m{i}", timestamp=f"2026-04-01T10:00:{i:03d}",
            sender="thomas", content=f"#{i}# {content}",
            message_type="info", read=read,
        )
        for i in range(count)
    ]


def _shown_ids(text: str, count: int) -> set[str]:
    return {f"m{i}" for i in range(count) if f"#{i}# " in text}


@pytest.mark.asyncio
async def test_handle_read_large_backlog_bounded_unread_only() -> None:
    store = _make_mock_store()
    store.get_unread.return_value = _backlog(300, "x" * 4096)

    result = await _handle_read(store, {"limit": 300})

    text = result["text"]
    assert len(text) <= TELEGRAM_READ_RESULT_CHAR_CAP
    assert "is_error" not in result
    shown = _shown_ids(text, 300)
    # Unread mode fills oldest-first, so the newer messages are the omitted ones.
    assert "m0" in shown
    omitted = TELEGRAM_READ_MAX_LIMIT - len(shown)
    assert omitted > 0
    assert text.endswith(f"[{omitted} newer message(s) not shown]")
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
async def test_handle_read_large_backlog_bounded_recent() -> None:
    store = _make_mock_store()
    store.get_recent.return_value = _backlog(TELEGRAM_READ_MAX_LIMIT, "y" * 4096)

    result = await _handle_read(store, {"unread_only": False, "limit": 300})

    text = result["text"]
    assert len(text) <= TELEGRAM_READ_RESULT_CHAR_CAP
    shown = _shown_ids(text, TELEGRAM_READ_MAX_LIMIT)
    newest = f"m{TELEGRAM_READ_MAX_LIMIT - 1}"
    assert newest in shown
    assert "m0" not in shown
    # Emitted in chronological order: the newest entry comes last.
    assert text.index("#48# ") < text.index("#49# ")
    # A repeat recent-mode call returns the same tail, so the footer must not
    # promise the omitted older messages are reachable by calling again.
    omitted = TELEGRAM_READ_MAX_LIMIT - len(shown)
    assert text.endswith(f"[{omitted} older message(s) not shown]")
    assert "call again" not in text
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
async def test_handle_read_single_oversized_message_truncated() -> None:
    store = _make_mock_store()
    size = 2 * 1024 * 1024
    store.get_unread.return_value = [
        Message(
            message_id="big", timestamp="2026-04-01T10:00:00",
            sender="thomas", content="z" * size,
            message_type="info", read=False,
        ),
    ]

    result = await _handle_read(store, {})

    text = result["text"]
    dropped = size - TELEGRAM_READ_MESSAGE_CHAR_CAP
    assert f"[truncated — {dropped} more chars]" in text
    assert len(text) <= TELEGRAM_READ_RESULT_CHAR_CAP
    assert "not shown" not in text
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
async def test_handle_read_oversized_header_still_delivered() -> None:
    store = _make_mock_store()
    store.get_unread.return_value = [
        Message(
            message_id="hdr", timestamp="t" * 100_000,
            sender="s" * 100_000, content="z" * 100_000,
            message_type="info", read=False,
        ),
    ]

    result = await _handle_read(store, {})

    text = result["text"]
    assert len(text) <= TELEGRAM_READ_RESULT_CHAR_CAP
    assert "t" * TELEGRAM_READ_HEADER_FIELD_CHAR_CAP in text
    assert "t" * (TELEGRAM_READ_HEADER_FIELD_CHAR_CAP + 1) not in text
    assert "not shown" not in text
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
async def test_handle_read_multibyte_escaping_fits_frame() -> None:
    store = _make_mock_store()
    store.get_unread.return_value = _backlog(300, '😀\n"' * 20_000)

    result = await _handle_read(store, {"limit": 300})

    assert len(json.dumps(result).encode("utf-8")) < 1024 * 1024
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) < 1024 * 1024


@pytest.mark.asyncio
async def test_handle_read_limit_above_max_clamped() -> None:
    store = _make_mock_store()
    store.get_unread.return_value = _backlog(100, "hi")

    result = await _handle_read(store, {"limit": 10_000})

    assert "is_error" not in result
    shown = _shown_ids(result["text"], 100)
    assert len(shown) == TELEGRAM_READ_MAX_LIMIT

    await _handle_read(store, {"unread_only": False, "limit": 10_000})
    store.get_recent.assert_called_once_with(TELEGRAM_READ_MAX_LIMIT)


def test_read_schema_advertises_max_limit() -> None:
    assert _READ_SCHEMA["properties"]["limit"]["maximum"] == TELEGRAM_READ_MAX_LIMIT


@pytest.mark.asyncio
async def test_handle_read_defaults() -> None:
    store = _make_mock_store()
    await _handle_read(store, {})

    store.get_unread.assert_called_once()


@pytest.mark.asyncio
async def test_handle_read_no_messages_all() -> None:
    store = _make_mock_store()
    result = await _handle_read(store, {"unread_only": False})

    assert "No messages found" in result["text"]
    assert "unread" not in result["text"]


@pytest.mark.asyncio
async def test_handle_read_zero_limit_rejected_unread_only() -> None:
    """limit=0 on the unread branch is rejected before slicing; nothing marked read."""
    store = _make_mock_store()
    store.get_unread.return_value = [
        Message(
            message_id="m1", timestamp="2026-04-01T10:00:00",
            sender="thomas", content="Hi Maestro!",
            message_type="instruction", read=False,
        ),
        Message(
            message_id="m2", timestamp="2026-04-01T10:05:00",
            sender="thomas", content="How are you?",
            message_type="instruction", read=False,
        ),
    ]
    result = await _handle_read(store, {"limit": 0})

    assert result.get("is_error") is True
    assert "positive integer" in result["text"]
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
async def test_handle_read_negative_limit_rejected_unread_only() -> None:
    """limit=-1 must not silently drop the newest unread message or consume any."""
    store = _make_mock_store()
    store.get_unread.return_value = [
        Message(
            message_id="m1", timestamp="2026-04-01T10:00:00",
            sender="thomas", content="Older",
            message_type="instruction", read=False,
        ),
        Message(
            message_id="m2", timestamp="2026-04-01T10:05:00",
            sender="thomas", content="Newest instruction",
            message_type="instruction", read=False,
        ),
    ]
    result = await _handle_read(store, {"limit": -1})

    assert result.get("is_error") is True
    assert "positive integer" in result["text"]
    # The newest message must not leak into the response, nor be consumed.
    assert "Newest instruction" not in result["text"]
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
async def test_handle_read_zero_limit_rejected_unread_only_false() -> None:
    """limit=0 is rejected on the get_recent branch too, before the store is queried."""
    store = _make_mock_store()
    result = await _handle_read(store, {"unread_only": False, "limit": 0})

    assert result.get("is_error") is True
    assert "positive integer" in result["text"]
    store.get_recent.assert_not_called()
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
async def test_handle_read_negative_limit_rejected_unread_only_false() -> None:
    """limit=-1 is rejected on the get_recent branch, aligning both branches."""
    store = _make_mock_store()
    result = await _handle_read(store, {"unread_only": False, "limit": -1})

    assert result.get("is_error") is True
    assert "positive integer" in result["text"]
    store.get_recent.assert_not_called()
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
async def test_handle_read_bool_limit_rejected() -> None:
    """A bool limit (int subclass) is rejected rather than coerced to 1/0."""
    store = _make_mock_store()
    store.get_unread.return_value = [
        Message(
            message_id="m1", timestamp="2026-04-01T10:00:00",
            sender="thomas", content="Hi Maestro!",
            message_type="instruction", read=False,
        ),
    ]
    result = await _handle_read(store, {"limit": True})

    assert result.get("is_error") is True
    assert "positive integer" in result["text"]
    store.mark_read_many.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_limit", ["5", 3.5, None])
async def test_handle_read_non_int_limit_rejected(bad_limit: object) -> None:
    """A non-int limit (string/float/None) is rejected by the isinstance guard.

    Exercises the ``not isinstance(limit, int)`` clause, which is first
    specifically to stop ``limit <= 0`` raising TypeError on a non-comparable
    type; the store is never touched.
    """
    store = _make_mock_store()
    store.get_unread.return_value = [
        Message(
            message_id="m1", timestamp="2026-04-01T10:00:00",
            sender="thomas", content="Hi Maestro!",
            message_type="instruction", read=False,
        ),
    ]
    result = await _handle_read(store, {"limit": bad_limit})

    assert result.get("is_error") is True
    assert "positive integer" in result["text"]
    store.get_unread.assert_not_called()
    store.mark_read_many.assert_not_called()
