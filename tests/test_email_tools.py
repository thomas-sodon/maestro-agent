"""Tests for email_tools — MCP server factory and tool handlers with mocked EmailClient."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from maestro.email import EmailMessage
from maestro.email_tools import (
    _INBOX_SCHEMA,
    EMAIL_RESULT_CHAR_CAP,
    SERVER_KEY,
    TOOL_NAMES,
    _handle_inbox,
    _handle_read,
    _handle_send,
    create_email_mcp_server,
)


def _make_mock_client() -> MagicMock:
    """Create a mock EmailClient with AsyncMock methods."""
    client = MagicMock()
    client.send = AsyncMock(return_value="<test-msg-id@example.com>")
    client.fetch_inbox = AsyncMock(return_value=[])
    client.mark_seen = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def test_create_email_mcp_server_returns_config() -> None:
    client = _make_mock_client()
    config = create_email_mcp_server(client)
    assert config is not None
    assert config["type"] == "sdk"
    assert config["name"] == "maestro_email"


def test_server_key_matches_factory_name() -> None:
    assert SERVER_KEY == "maestro_email"


def test_tool_names_use_mcp_prefix() -> None:
    for name in TOOL_NAMES:
        assert name.startswith(f"mcp__{SERVER_KEY}__")
    assert len(TOOL_NAMES) == 3


# ---------------------------------------------------------------------------
# email_send handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_send_basic() -> None:
    client = _make_mock_client()
    result = await _handle_send(client, {
        "to": "bob@test.com", "subject": "Hi", "body": "Hello Bob",
    })

    assert result["type"] == "text"
    assert "bob@test.com" in result["text"]
    assert "<test-msg-id@example.com>" in result["text"]
    client.send.assert_called_once_with(
        "bob@test.com", "Hi", "Hello Bob", reply_to_message_id=None,
    )


@pytest.mark.asyncio
async def test_handle_send_with_reply_to() -> None:
    client = _make_mock_client()
    await _handle_send(client, {
        "to": "bob@test.com",
        "subject": "Re: Hi",
        "body": "Reply",
        "reply_to_message_id": "<orig@test.com>",
    })

    client.send.assert_called_once_with(
        "bob@test.com", "Re: Hi", "Reply", reply_to_message_id="<orig@test.com>",
    )


# ---------------------------------------------------------------------------
# email_inbox handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_inbox_empty() -> None:
    client = _make_mock_client()
    result = await _handle_inbox(client, {})

    assert "No emails found" in result["text"]
    client.fetch_inbox.assert_called_once_with(limit=20, unseen_only=True)


@pytest.mark.asyncio
async def test_handle_inbox_with_messages() -> None:
    client = _make_mock_client()
    client.fetch_inbox = AsyncMock(return_value=[
        EmailMessage(
            uid="1", from_addr="alice@test.com", subject="Hello",
            body="body", date="2026-04-01", message_id="<a@t>", seen=False,
        ),
        EmailMessage(
            uid="2", from_addr="bob@test.com", subject="World",
            body="body", date="2026-04-02", message_id="<b@t>", seen=True,
        ),
    ])
    result = await _handle_inbox(client, {"limit": 10, "unseen_only": False})

    text = result["text"]
    assert "alice@test.com" in text
    assert "bob@test.com" in text
    assert "Hello" in text
    assert "World" in text
    client.fetch_inbox.assert_called_once_with(limit=10, unseen_only=False)


@pytest.mark.asyncio
async def test_handle_inbox_defaults() -> None:
    client = _make_mock_client()
    await _handle_inbox(client, {})
    client.fetch_inbox.assert_called_once_with(limit=20, unseen_only=True)


@pytest.mark.asyncio
async def test_handle_inbox_zero_limit_rejected() -> None:
    """limit=0 must be refused: uids[-0:] == uids[0:] fetches the whole mailbox."""
    client = _make_mock_client()
    result = await _handle_inbox(client, {"limit": 0})

    assert result.get("is_error") is True
    assert "positive integer" in result["text"]
    client.fetch_inbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_inbox_negative_limit_rejected() -> None:
    """limit=-5 inverts the slice to all-but-the-5-oldest; reject before fetching."""
    client = _make_mock_client()
    result = await _handle_inbox(client, {"limit": -5})

    assert result.get("is_error") is True
    assert "positive integer" in result["text"]
    client.fetch_inbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_inbox_bool_limit_rejected() -> None:
    """A bool limit (int subclass) is rejected rather than coerced to 1/0."""
    client = _make_mock_client()
    result = await _handle_inbox(client, {"limit": True})

    assert result.get("is_error") is True
    assert "positive integer" in result["text"]
    client.fetch_inbox.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_limit", ["5", 20.0, 3.5, None])
async def test_handle_inbox_non_int_limit_rejected(bad_limit: object) -> None:
    """A non-int limit (string/float/None) is rejected by the isinstance guard.

    Exercises the ``not isinstance(limit, int)`` clause, which is first
    specifically to stop ``limit <= 0`` raising TypeError on a non-comparable
    type; ``fetch_inbox`` is never awaited.
    """
    client = _make_mock_client()
    result = await _handle_inbox(client, {"limit": bad_limit})

    assert result.get("is_error") is True
    assert "positive integer" in result["text"]
    client.fetch_inbox.assert_not_awaited()


def test_inbox_schema_declares_minimum_one() -> None:
    """Defence-in-depth: the advisory schema pins limit >= 1 so a well-behaved
    validator rejects non-positive values early. The handler guard above stays
    load-bearing; this constraint must not be silently dropped."""
    assert _INBOX_SCHEMA["properties"]["limit"]["minimum"] == 1


# ---------------------------------------------------------------------------
# email_read handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_read_found() -> None:
    client = _make_mock_client()
    client.fetch_by_uid = AsyncMock(return_value=EmailMessage(
        uid="42", from_addr="sender@test.com", subject="Read Me",
        body="Email body text", date="2026-04-01",
        message_id="<read@test>", seen=False,
    ))
    result = await _handle_read(client, {"uid": "42"})

    text = result["text"]
    assert "sender@test.com" in text
    assert "Read Me" in text
    assert "Email body text" in text
    client.fetch_by_uid.assert_called_once_with("42", mark_seen=True)


@pytest.mark.asyncio
async def test_handle_read_already_seen() -> None:
    client = _make_mock_client()
    client.fetch_by_uid = AsyncMock(return_value=EmailMessage(
        uid="42", from_addr="s@t.com", subject="Test",
        body="body", date="2026-04-01",
        message_id="<t@t>", seen=True,
    ))
    result = await _handle_read(client, {"uid": "42"})
    assert "is_error" not in result
    client.fetch_by_uid.assert_called_once_with("42", mark_seen=True)


@pytest.mark.asyncio
async def test_handle_read_not_found() -> None:
    client = _make_mock_client()
    client.fetch_by_uid = AsyncMock(return_value=None)
    result = await _handle_read(client, {"uid": "999"})

    assert result.get("is_error") is True
    assert "not found" in result["text"]


def _msg(*, body: str = "b", subject: str = "S", from_addr: str = "f@t.com") -> EmailMessage:
    return EmailMessage(
        uid="7", from_addr=from_addr, subject=subject, body=body,
        date="2026-04-01", message_id="<m@t>", seen=False,
    )


_MARKER = re.compile(r"\n\n\[truncated — (\d+) more chars\]$")


@pytest.mark.asyncio
async def test_handle_read_truncates_oversized_body() -> None:
    client = _make_mock_client()
    body = "x" * (2 * 1024 * 1024)
    client.fetch_by_uid = AsyncMock(return_value=_msg(body=body))
    result = await _handle_read(client, {"uid": "7"})

    text = result["text"]
    assert len(text) <= EMAIL_RESULT_CHAR_CAP
    match = _MARKER.search(text)
    assert match is not None
    full = "From: f@t.com\nSubject: S\nDate: 2026-04-01\nMessage-ID: <m@t>\n\n" + body
    kept = len(text) - len(match.group(0))
    assert int(match.group(1)) == len(full) - kept
    assert "is_error" not in result


@pytest.mark.asyncio
async def test_handle_read_truncates_oversized_headers() -> None:
    client = _make_mock_client()
    huge = "h" * (EMAIL_RESULT_CHAR_CAP * 2)
    client.fetch_by_uid = AsyncMock(return_value=_msg(subject=huge, from_addr=huge))
    result = await _handle_read(client, {"uid": "7"})

    assert len(result["text"]) <= EMAIL_RESULT_CHAR_CAP
    assert _MARKER.search(result["text"]) is not None


@pytest.mark.asyncio
async def test_handle_read_multibyte_body_fits_frame() -> None:
    client = _make_mock_client()
    body = ("\U0001F600\n\"" * 400_000)
    client.fetch_by_uid = AsyncMock(return_value=_msg(body=body))
    result = await _handle_read(client, {"uid": "7"})

    encoded = json.dumps(result["text"]).encode("utf-8")
    assert len(encoded) < 800_000


@pytest.mark.asyncio
async def test_handle_read_small_message_unchanged() -> None:
    client = _make_mock_client()
    client.fetch_by_uid = AsyncMock(return_value=_msg(body="hello"))
    result = await _handle_read(client, {"uid": "7"})

    expected = "From: f@t.com\nSubject: S\nDate: 2026-04-01\nMessage-ID: <m@t>\n\nhello"
    assert result == {"type": "text", "text": expected}


@pytest.mark.asyncio
async def test_handle_inbox_truncates_oversized_rows() -> None:
    client = _make_mock_client()
    client.fetch_inbox = AsyncMock(return_value=[_msg(subject="s" * (EMAIL_RESULT_CHAR_CAP * 2))])
    result = await _handle_inbox(client, {"limit": 1})

    assert len(result["text"]) <= EMAIL_RESULT_CHAR_CAP
    assert _MARKER.search(result["text"]) is not None
