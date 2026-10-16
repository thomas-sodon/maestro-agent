"""Tests for EmailClient — all SMTP/IMAP calls are mocked."""

from __future__ import annotations

import tomllib
from email.message import EmailMessage as StdEmailMessage
from email.policy import default as default_policy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.email import (
    EmailClient,
    EmailMessage,
    _parse_fetch_response,
    _parse_uid_list,
)

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_construction() -> None:
    client = EmailClient(address="a@b.com", password="secret")
    assert client._address == "a@b.com"
    assert client._smtp_host == "smtp.zoho.com"


def test_construction_empty_address_raises() -> None:
    with pytest.raises(ValueError, match="address must not be empty"):
        EmailClient(address="", password="secret")


def test_construction_empty_password_raises() -> None:
    with pytest.raises(ValueError, match="password must not be empty"):
        EmailClient(address="a@b.com", password="")


def test_construction_whitespace_only_raises() -> None:
    with pytest.raises(ValueError, match="address must not be empty"):
        EmailClient(address="  ", password="secret")


def test_max_length_floor() -> None:
    client = EmailClient(address="a@b.com", password="s", max_length=0)
    assert client._max_length == 1


_PRESENT = object()  # stand-in for an installed module (not None)


@pytest.mark.parametrize(
    ("smtp", "imap"),
    [(None, _PRESENT), (_PRESENT, None), (None, None)],
)
def test_email_client_raises_clear_error_when_deps_missing(
    smtp: object, imap: object,
) -> None:
    """Constructing EmailClient without the `email` extra fails loudly and early.

    When either optional dep is unresolved (``None`` after the guarded
    module-level import), construction raises a descriptive ``RuntimeError``
    naming the ``email`` extra and the ``pip install`` remedy — instead of a
    cryptic ``AttributeError`` deep inside a later send/fetch call.
    """
    with (
        patch("maestro.email.aiosmtplib", smtp),
        patch("maestro.email.aioimaplib", imap),
        pytest.raises(RuntimeError, match="email") as exc_info,
    ):
        EmailClient(address="a@b.com", password="pw")
    assert "pip install" in str(exc_info.value)


def test_pyproject_email_deps_are_optional_extra_not_core() -> None:
    """The email-only libs live in the `email` extra, not core dependencies.

    Guards against a regression that re-adds ``aiosmtplib`` / ``aioimaplib`` to
    the mandatory ``[project] dependencies`` block, which would make every
    install pay the email-only network-protocol dependency surface.
    """
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    core_deps = data["project"]["dependencies"]
    email_extra = data["project"]["optional-dependencies"]["email"]

    assert any(dep.startswith("aiosmtplib") for dep in email_extra)
    assert any(dep.startswith("aioimaplib") for dep in email_extra)
    assert not any(dep.startswith("aiosmtplib") for dep in core_deps)
    assert not any(dep.startswith("aioimaplib") for dep in core_deps)


def test_readme_dev_setup_installs_email_extra() -> None:
    """README's Development section installs the `email` extra before pytest.

    That section runs the full ``pytest tests/`` suite, which constructs real
    ``EmailClient`` instances; without the email extra the module-level
    ``aiosmtplib`` / ``aioimaplib`` are ``None`` and every such test raises the
    missing-deps ``RuntimeError``. Guards against README drifting back to a bare
    ``[dev]`` install that breaks the documented test workflow (issue #240 PR).
    """
    readme = Path(__file__).resolve().parents[1] / "README.md"
    text = readme.read_text(encoding="utf-8")
    dev_start = text.index("## Development")
    # Bound the section at the next top-level heading so a `pytest tests/` in a
    # later section can't satisfy the assertion for us.
    next_heading = text.find("\n## ", dev_start + 1)
    dev_section = text[dev_start:] if next_heading == -1 else text[dev_start:next_heading]

    assert "pytest tests/" in dev_section
    assert 'pip install -e ".[dev,email]"' in dev_section


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

def test_truncate_short_text() -> None:
    client = EmailClient(address="a@b.com", password="s", max_length=100)
    assert client._truncate("hello") == "hello"


def test_truncate_long_text() -> None:
    client = EmailClient(address="a@b.com", password="s", max_length=50)
    result = client._truncate("x" * 100)
    assert len(result) <= 50
    assert result.endswith("(truncated)")


def test_truncate_very_small_limit() -> None:
    client = EmailClient(address="a@b.com", password="s", max_length=5)
    result = client._truncate("x" * 100)
    assert len(result) <= 5


# ---------------------------------------------------------------------------
# Build message
# ---------------------------------------------------------------------------

def test_build_message_basic() -> None:
    client = EmailClient(address="a@b.com", password="s")
    msg = client._build_message("to@b.com", "Test Subject", "Hello body")
    assert msg["From"] == "a@b.com"
    assert msg["To"] == "to@b.com"
    assert msg["Subject"] == "Test Subject"
    assert msg["Message-ID"] is not None
    assert msg["Date"] is not None
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None
    assert "Hello body" in msg.get_content()


def test_build_message_with_reply_to() -> None:
    client = EmailClient(address="a@b.com", password="s")
    msg = client._build_message(
        "to@b.com", "Re: Test", "Reply body",
        reply_to_message_id="<orig@b.com>",
    )
    assert msg["In-Reply-To"] == "<orig@b.com>"
    assert msg["References"] == "<orig@b.com>"


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_calls_aiosmtplib() -> None:
    client = EmailClient(
        address="a@b.com", password="s",
        smtp_host="smtp.test.com", smtp_port=587,
    )
    with patch("maestro.email.aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        message_id = await client.send("to@b.com", "Subject", "Body text")

    mock_send.assert_called_once()
    call_kwargs = mock_send.call_args
    sent_msg = call_kwargs.args[0]
    assert isinstance(sent_msg, StdEmailMessage)
    assert sent_msg["To"] == "to@b.com"
    assert call_kwargs.kwargs["hostname"] == "smtp.test.com"
    assert call_kwargs.kwargs["port"] == 587
    assert call_kwargs.kwargs["start_tls"] is True
    assert message_id  # non-empty


@pytest.mark.asyncio
async def test_send_empty_recipient_raises() -> None:
    client = EmailClient(address="a@b.com", password="s")
    with pytest.raises(ValueError, match="recipient address must not be empty"):
        await client.send("", "Subject", "Body")


@pytest.mark.asyncio
async def test_send_empty_subject_raises() -> None:
    client = EmailClient(address="a@b.com", password="s")
    with pytest.raises(ValueError, match="subject must not be empty"):
        await client.send("to@b.com", "", "Body")


@pytest.mark.asyncio
async def test_send_with_reply_to() -> None:
    client = EmailClient(address="a@b.com", password="s")
    with patch("maestro.email.aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        await client.send(
            "to@b.com", "Re: Test", "Reply",
            reply_to_message_id="<orig@test.com>",
        )

    sent_msg = mock_send.call_args.args[0]
    assert sent_msg["In-Reply-To"] == "<orig@test.com>"


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def test_parse_uid_list_basic() -> None:
    assert _parse_uid_list(["1 2 3"]) == ["1", "2", "3"]


def test_parse_uid_list_multiple_lines() -> None:
    assert _parse_uid_list(["1 2", "3 4"]) == ["1", "2", "3", "4"]


def test_parse_uid_list_empty() -> None:
    assert _parse_uid_list([""]) == []
    assert _parse_uid_list([]) == []


def test_parse_uid_list_filters_non_numeric() -> None:
    assert _parse_uid_list(["SEARCH 1 2 3"]) == ["1", "2", "3"]
    assert _parse_uid_list(["1 2 FOO 3"]) == ["1", "2", "3"]


def test_parse_uid_list_filters_non_ascii_digits() -> None:
    # str.isdigit() accepts superscripts and other Unicode digits ("²", "٤")
    # that are not valid ASCII IMAP UIDs and would break imap.uid("fetch", ...).
    assert _parse_uid_list(["1 ² 2 ٤ 3"]) == ["1", "2", "3"]


def test_parse_fetch_response_basic() -> None:
    # Build a raw email
    msg = StdEmailMessage(policy=default_policy)
    msg["From"] = "sender@test.com"
    msg["Subject"] = "Test"
    msg["Message-ID"] = "<test123@test.com>"
    msg["Date"] = "Mon, 01 Apr 2026 12:00:00 +0000"
    msg.set_content("Hello world")
    raw = msg.as_bytes()

    result = _parse_fetch_response("42", [raw, r'FLAGS (\Seen)'])
    assert result is not None
    assert result.uid == "42"
    assert result.from_addr == "sender@test.com"
    assert result.subject == "Test"
    assert "Hello world" in result.body
    assert result.seen is True


def test_parse_fetch_response_unseen() -> None:
    msg = StdEmailMessage(policy=default_policy)
    msg["From"] = "x@y.com"
    msg["Subject"] = "Hi"
    msg.set_content("text")
    raw = msg.as_bytes()

    result = _parse_fetch_response("10", [raw, "FLAGS ()"])
    assert result is not None
    assert result.seen is False


def test_parse_fetch_response_no_bytes() -> None:
    result = _parse_fetch_response("10", ["just a string"])
    assert result is None


# ---------------------------------------------------------------------------
# Fetch inbox
# ---------------------------------------------------------------------------

def _real_fetch_lines(raw: bytes, flags: str) -> list[bytes | bytearray]:
    """FETCH lines in aioimaplib's real framing (bytes control, bytearray literal)."""
    return [
        f"1 FETCH (UID 42 BODY[] {{{len(raw)}}}".encode(),
        bytearray(raw),
        f" FLAGS {flags})".encode(),
        b"FETCH completed.",
    ]


def test_parse_uid_list_decodes_byte_lines() -> None:
    uids = _parse_uid_list([b"SEARCH 7 42", bytearray(b"99"), b"SEARCH completed"])
    assert uids == ["7", "42", "99"]
    assert all(type(u) is str for u in uids)


def test_parse_fetch_response_handles_bytearray_literal() -> None:
    msg = StdEmailMessage(policy=default_policy)
    msg["From"] = "real@example.com"
    msg["Subject"] = "Real frame"
    msg.set_content("Real body")
    result = _parse_fetch_response("42", _real_fetch_lines(msg.as_bytes(), r"(\Seen)"))
    assert result is not None
    assert result.from_addr == "real@example.com"
    assert result.subject == "Real frame"
    assert result.body.strip() == "Real body"
    assert result.seen is True


def test_parse_fetch_response_status_only_is_none() -> None:
    # A UID FETCH for a vanished UID returns OK with only the status line.
    assert _parse_fetch_response("999", [b"Fetch completed."]) is None


@pytest.mark.asyncio
async def test_fetch_by_uid_status_only_is_none() -> None:
    mock_imap = _make_mock_imap(fetch_lines=[b"Fetch completed."])
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        assert await client.fetch_by_uid("999") is None


def test_parse_fetch_response_bytes_only_picks_largest() -> None:
    msg = StdEmailMessage(policy=default_policy)
    msg["Subject"] = "Largest"
    msg.set_content("x" * 50)
    result = _parse_fetch_response("1", [b"FLAGS ()", msg.as_bytes(), b"done"])
    assert result is not None
    assert result.subject == "Largest"
    assert result.seen is False


@pytest.mark.asyncio
async def test_fetch_inbox_passes_str_uid_to_fetch() -> None:
    mock_imap = _make_mock_imap(search_uids="42")
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        messages = await client.fetch_inbox(unseen_only=False)

    assert len(messages) == 1
    mock_imap.uid.assert_called_once_with("fetch", "42", "(BODY.PEEK[] FLAGS)")


def _make_mock_imap(
    search_uids: str = "1 2 3",
    fetch_lines: list | None = None,
) -> MagicMock:
    """Create a mock IMAP client."""
    imap = MagicMock()
    imap.wait_hello_from_server = AsyncMock()
    imap.login = AsyncMock(return_value=MagicMock(result="OK"))
    imap.select = AsyncMock(return_value=MagicMock(result="OK"))
    imap.logout = AsyncMock()

    # uid_search returns Response(result, lines)
    search_resp = MagicMock()
    search_resp.result = "OK"
    search_resp.lines = [f"SEARCH {search_uids}".encode(), b"SEARCH completed"]
    imap.uid_search = AsyncMock(return_value=search_resp)

    # uid fetch
    if fetch_lines is None:
        msg = StdEmailMessage(policy=default_policy)
        msg["From"] = "test@test.com"
        msg["Subject"] = "Test Email"
        msg["Message-ID"] = "<uid@test.com>"
        msg.set_content("Test body")
        fetch_lines = _real_fetch_lines(msg.as_bytes(), "()")

    fetch_resp = MagicMock()
    fetch_resp.result = "OK"
    fetch_resp.lines = fetch_lines
    imap.uid = AsyncMock(return_value=fetch_resp)

    return imap


@pytest.mark.asyncio
async def test_fetch_inbox_returns_messages() -> None:
    mock_imap = _make_mock_imap(search_uids="10 20")
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        messages = await client.fetch_inbox(limit=10, unseen_only=True)

    assert len(messages) == 2
    mock_imap.uid_search.assert_called_once_with("UNSEEN")
    mock_imap.login.assert_called_once_with("a@b.com", "s")


@pytest.mark.asyncio
async def test_fetch_inbox_all() -> None:
    mock_imap = _make_mock_imap(search_uids="1")
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        await client.fetch_inbox(unseen_only=False)

    mock_imap.uid_search.assert_called_once_with("ALL")


@pytest.mark.asyncio
async def test_fetch_inbox_all_preserves_unseen() -> None:
    # Regression for #238: a full-inbox fetch (unseen_only=False) must not
    # force seen=True on genuinely-unread mail. The default mock FETCH response
    # carries "FLAGS ()" (no \Seen flag), so the message must stay unseen.
    mock_imap = _make_mock_imap(search_uids="1")
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        messages = await client.fetch_inbox(unseen_only=False)

    assert len(messages) == 1
    assert messages[0].seen is False


@pytest.mark.asyncio
async def test_fetch_inbox_empty() -> None:
    mock_imap = _make_mock_imap(search_uids="")
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        messages = await client.fetch_inbox()

    assert messages == []


@pytest.mark.asyncio
async def test_fetch_inbox_search_failure() -> None:
    mock_imap = _make_mock_imap()
    search_resp = MagicMock()
    search_resp.result = "NO"
    search_resp.lines = []
    mock_imap.uid_search = AsyncMock(return_value=search_resp)
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        messages = await client.fetch_inbox()

    assert messages == []


@pytest.mark.asyncio
async def test_fetch_inbox_limits_results() -> None:
    mock_imap = _make_mock_imap(search_uids="1 2 3 4 5")
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        messages = await client.fetch_inbox(limit=2)

    # Should only fetch the last 2 UIDs (4, 5)
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_fetch_inbox_logout_error_suppressed() -> None:
    mock_imap = _make_mock_imap(search_uids="1")
    mock_imap.logout = AsyncMock(side_effect=Exception("logout failed"))
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        messages = await client.fetch_inbox()

    # Should still return results despite logout error
    assert len(messages) == 1


# ---------------------------------------------------------------------------
# Fetch by UID
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_by_uid_found() -> None:
    mock_imap = _make_mock_imap()
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        msg = await client.fetch_by_uid("42")

    assert msg is not None
    assert msg.uid == "42"
    mock_imap.uid.assert_called_once_with("fetch", "42", "(BODY.PEEK[] FLAGS)")


@pytest.mark.asyncio
async def test_fetch_by_uid_not_found() -> None:
    mock_imap = _make_mock_imap()
    fetch_resp = MagicMock()
    fetch_resp.result = "NO"
    fetch_resp.lines = []
    mock_imap.uid = AsyncMock(return_value=fetch_resp)
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        msg = await client.fetch_by_uid("999")

    assert msg is None


@pytest.mark.asyncio
async def test_fetch_by_uid_mark_seen() -> None:
    mock_imap = _make_mock_imap()
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        msg = await client.fetch_by_uid("42", mark_seen=True)

    assert msg is not None
    assert msg.seen is True
    # First call: fetch, second call: store flags
    assert mock_imap.uid.call_count == 2
    mock_imap.uid.assert_any_call("store", "42", "+FLAGS", r"(\Seen)")


@pytest.mark.asyncio
async def test_fetch_by_uid_already_seen_no_store() -> None:
    """When the message is already seen, no STORE command is issued."""
    from email.message import EmailMessage as StdEmailMessage
    msg_obj = StdEmailMessage(policy=default_policy)
    msg_obj["From"] = "x@y.com"
    msg_obj["Subject"] = "Already seen"
    msg_obj.set_content("body")
    fetch_resp = MagicMock()
    fetch_resp.result = "OK"
    fetch_resp.lines = [msg_obj.as_bytes(), r"FLAGS (\Seen)"]
    mock_imap = _make_mock_imap()
    mock_imap.uid = AsyncMock(return_value=fetch_resp)
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        result = await client.fetch_by_uid("42", mark_seen=True)

    assert result is not None
    assert result.seen is True
    # Only one uid call (fetch), no store since already seen
    mock_imap.uid.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_by_uid_logout_error_suppressed() -> None:
    mock_imap = _make_mock_imap()
    mock_imap.logout = AsyncMock(side_effect=Exception("logout failed"))
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        msg = await client.fetch_by_uid("42")

    assert msg is not None


# ---------------------------------------------------------------------------
# Mark seen
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mark_seen() -> None:
    mock_imap = _make_mock_imap()
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        await client.mark_seen("42")

    mock_imap.uid.assert_called_once_with("store", "42", "+FLAGS", r"(\Seen)")


@pytest.mark.asyncio
async def test_mark_seen_logout_error_suppressed() -> None:
    mock_imap = _make_mock_imap()
    mock_imap.logout = AsyncMock(side_effect=Exception("logout failed"))
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        await client.mark_seen("42")

    mock_imap.uid.assert_called_once()


# ---------------------------------------------------------------------------
# Fetch one — edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_one_non_ok_returns_none() -> None:
    """When uid fetch returns non-OK, the message is skipped."""
    mock_imap = _make_mock_imap(search_uids="1")
    fetch_resp = MagicMock()
    fetch_resp.result = "NO"
    fetch_resp.lines = []
    mock_imap.uid = AsyncMock(return_value=fetch_resp)
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        messages = await client.fetch_inbox()

    assert messages == []


# ---------------------------------------------------------------------------
# Multipart email parsing
# ---------------------------------------------------------------------------

def test_parse_fetch_response_multipart() -> None:
    """Multipart email should extract text/plain body."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart()
    msg["From"] = "sender@test.com"
    msg["Subject"] = "Multipart Test"
    msg["Message-ID"] = "<multi@test.com>"
    msg.attach(MIMEText("<html>rich</html>", "html"))
    msg.attach(MIMEText("plain text body", "plain"))
    raw = msg.as_bytes()

    result = _parse_fetch_response("50", [raw, "FLAGS ()"])
    assert result is not None
    assert "plain text body" in result.body


# ---------------------------------------------------------------------------
# EmailMessage dataclass
# ---------------------------------------------------------------------------

def test_email_message_dataclass() -> None:
    msg = EmailMessage(
        uid="1", from_addr="a@b.com", subject="Hi",
        body="Hello", date="2026-04-01", message_id="<x@y>", seen=False,
    )
    assert msg.uid == "1"
    assert msg.seen is False


# ---------------------------------------------------------------------------
# Unknown declared charset (#4074)
# ---------------------------------------------------------------------------

_UNKNOWN_CHARSET_SINGLE = (
    b"From: bad@test.com\r\n"
    b"Subject: Mislabelled\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: text/plain; charset="unknown-8bit"\r\n'
    b"Content-Transfer-Encoding: 8bit\r\n"
    b"\r\n"
    b"Hello \xff world\r\n"
)


def test_parse_fetch_response_unknown_charset_single_part() -> None:
    result = _parse_fetch_response("7", [_UNKNOWN_CHARSET_SINGLE, "FLAGS ()"])
    assert result is not None
    assert result.from_addr == "bad@test.com"
    assert result.subject == "Mislabelled"
    assert "Hello � world" in result.body


def test_parse_fetch_response_unknown_charset_multipart() -> None:
    raw = (
        b"From: bad@test.com\r\n"
        b"Subject: Multi\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b'Content-Type: text/plain; charset="x-user-defined"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n"
        b"Plain \xfe part\r\n"
        b"--B--\r\n"
    )
    result = _parse_fetch_response("8", [raw, "FLAGS ()"])
    assert result is not None
    assert "Plain � part" in result.body


def test_parse_fetch_response_unknown_charset_non_text_part_keeps_empty_body() -> None:
    raw = (
        b"From: bad@test.com\r\n"
        b"Subject: Blob\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: application/octet-stream; charset="unknown-8bit"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"AAEC\r\n"
    )
    result = _parse_fetch_response("9", [raw, "FLAGS ()"])
    assert result is not None
    assert result.body == ""


def _fetch_resp(raw: bytes) -> MagicMock:
    resp = MagicMock()
    resp.result = "OK"
    resp.lines = [raw, "FLAGS ()"]
    return resp


@pytest.mark.asyncio
async def test_fetch_inbox_lists_neighbour_of_unknown_charset_message() -> None:
    good = StdEmailMessage(policy=default_policy)
    good["From"] = "ok@test.com"
    good["Subject"] = "Fine"
    good.set_content("fine body")
    mock_imap = _make_mock_imap(search_uids="1 2")
    mock_imap.uid = AsyncMock(
        side_effect=[_fetch_resp(_UNKNOWN_CHARSET_SINGLE), _fetch_resp(good.as_bytes())],
    )
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        messages = await client.fetch_inbox(limit=10, unseen_only=True)

    assert [m.subject for m in messages] == ["Mislabelled", "Fine"]


@pytest.mark.asyncio
async def test_fetch_by_uid_mark_seen_stores_seen_for_unknown_charset() -> None:
    mock_imap = _make_mock_imap()
    mock_imap.uid = AsyncMock(return_value=_fetch_resp(_UNKNOWN_CHARSET_SINGLE))
    client = EmailClient(address="a@b.com", password="s")

    with patch("maestro.email.aioimaplib.IMAP4_SSL", return_value=mock_imap):
        msg = await client.fetch_by_uid("42", mark_seen=True)

    assert msg is not None
    assert msg.seen is True
    mock_imap.uid.assert_any_call("store", "42", "+FLAGS", r"(\Seen)")


@pytest.mark.parametrize(
    "label",
    [b"base64", b"hex", b"rot13", b"zlib", b"undefined", b"idna", b"unknown-8bit", "café".encode()],
)
def test_parse_fetch_response_undecodable_charset_labels(label: bytes) -> None:
    raw = (
        b"From: bad@test.com\r\nSubject: Label\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: text/plain; charset="' + label + b'"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n\r\nHi \xff there\r\n"
    )
    result = _parse_fetch_response("10", [raw, "FLAGS ()"])
    assert result is not None
    assert result.subject == "Label"
    assert "Hi" in result.body


def test_parse_fetch_response_known_charset_decodes_natively() -> None:
    raw = (
        b"From: ok@test.com\r\nSubject: Latin\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: text/plain; charset="iso-8859-1"\r\n'
        b"Content-Transfer-Encoding: 8bit\r\n\r\ncaf\xe9\r\n"
    )
    result = _parse_fetch_response("11", [raw, "FLAGS ()"])
    assert result is not None
    assert "café" in result.body
