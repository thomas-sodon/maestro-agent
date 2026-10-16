"""Email client for sending and receiving emails."""

from __future__ import annotations

import email.utils
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage as StdEmailMessage
from email.policy import default as default_policy
from typing import Any

try:
    import aioimaplib
    import aiosmtplib
except ImportError:  # pragma: no cover — the optional `email` extra is not installed
    aioimaplib = None  # type: ignore[assignment]
    aiosmtplib = None  # type: ignore[assignment]

logger = logging.getLogger("maestro")

_MISSING_EMAIL_DEPS_MSG = (
    "Email support requires the optional 'email' extra, which is not installed. "
    "Install it with: pip install -e '.[email]'"
)


@dataclass
class EmailMessage:
    """A received email message."""

    uid: str
    from_addr: str
    subject: str
    body: str
    date: str
    message_id: str
    seen: bool


class EmailClient:
    """Async email client for SMTP sending and IMAP reading."""

    def __init__(
        self,
        address: str,
        password: str,
        smtp_host: str = "smtp.zoho.com",
        smtp_port: int = 587,
        imap_host: str = "imap.zoho.com",
        imap_port: int = 993,
        max_length: int = 50000,
    ) -> None:
        if aiosmtplib is None or aioimaplib is None:
            raise RuntimeError(_MISSING_EMAIL_DEPS_MSG)
        if not address or not address.strip():
            raise ValueError("address must not be empty")
        if not password or not password.strip():
            raise ValueError("password must not be empty")
        self._address = address
        self._password = password
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._max_length = max(1, max_length)

    def _truncate(self, text: str) -> str:
        """Truncate text to fit within max_length."""
        if len(text) <= self._max_length:
            return text
        suffix = "\n\n(truncated)"
        truncate_at = self._max_length - len(suffix)
        if truncate_at > 0:
            return text[:truncate_at] + suffix
        return text[: self._max_length]

    def _build_message(
        self,
        to: str,
        subject: str,
        body: str,
        reply_to_message_id: str | None = None,
    ) -> StdEmailMessage:
        """Build an EmailMessage object with proper headers."""
        msg = StdEmailMessage(policy=default_policy)
        msg["From"] = self._address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = email.utils.make_msgid(
            idstring=uuid.uuid4().hex[:12], domain=self._address.split("@")[-1],
        )
        msg["Date"] = email.utils.formatdate(localtime=True)
        if reply_to_message_id:
            msg["In-Reply-To"] = reply_to_message_id
            msg["References"] = reply_to_message_id
        msg.set_content(self._truncate(body))
        return msg

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        reply_to_message_id: str | None = None,
    ) -> str:
        """Send an email via SMTP. Returns the Message-ID of the sent email."""
        if not to or not to.strip():
            raise ValueError("recipient address must not be empty")
        if not subject or not subject.strip():
            raise ValueError("subject must not be empty")
        msg = self._build_message(to, subject, body, reply_to_message_id)
        message_id = msg["Message-ID"] or ""
        await aiosmtplib.send(
            msg,
            hostname=self._smtp_host,
            port=self._smtp_port,
            username=self._address,
            password=self._password,
            start_tls=True,
        )
        logger.info("Email sent to %s: %s", to, subject)
        return message_id

    async def fetch_inbox(
        self,
        limit: int = 20,
        unseen_only: bool = True,
    ) -> list[EmailMessage]:
        """Fetch emails from the inbox via IMAP."""
        imap = aioimaplib.IMAP4_SSL(host=self._imap_host, port=self._imap_port)
        await imap.wait_hello_from_server()
        await imap.login(self._address, self._password)
        try:
            await imap.select("INBOX")
            criteria = "UNSEEN" if unseen_only else "ALL"
            resp = await imap.uid_search(criteria)
            if resp.result != "OK":
                logger.warning("IMAP search failed: %s", resp.result)
                return []
            uids = _parse_uid_list(resp.lines)
            if not uids:
                return []
            # Take the most recent emails (last N UIDs)
            uids = uids[-limit:]
            messages: list[EmailMessage] = []
            for uid in uids:
                msg = await self._fetch_one(imap, uid)
                if msg is not None:
                    messages.append(msg)
            return messages
        finally:
            try:
                await imap.logout()
            except Exception:
                logger.debug("IMAP logout error", exc_info=True)

    async def fetch_by_uid(
        self, uid: str, mark_seen: bool = False,
    ) -> EmailMessage | None:
        """Fetch a single email by UID in one IMAP connection.

        When *mark_seen* is True the ``\\Seen`` flag is set on the same
        connection, avoiding a second round-trip.
        """
        imap = aioimaplib.IMAP4_SSL(host=self._imap_host, port=self._imap_port)
        await imap.wait_hello_from_server()
        await imap.login(self._address, self._password)
        try:
            await imap.select("INBOX")
            msg = await self._fetch_one(imap, uid)
            if msg is not None and mark_seen and not msg.seen:
                await imap.uid("store", uid, "+FLAGS", r"(\Seen)")
                msg.seen = True
            return msg
        finally:
            try:
                await imap.logout()
            except Exception:
                logger.debug("IMAP logout error", exc_info=True)

    async def mark_seen(self, uid: str) -> None:
        """Mark an email as seen via IMAP."""
        imap = aioimaplib.IMAP4_SSL(host=self._imap_host, port=self._imap_port)
        await imap.wait_hello_from_server()
        await imap.login(self._address, self._password)
        try:
            await imap.select("INBOX")
            await imap.uid("store", uid, "+FLAGS", r"(\Seen)")
        finally:
            try:
                await imap.logout()
            except Exception:
                logger.debug("IMAP logout error", exc_info=True)

    async def _fetch_one(
        self,
        imap: Any,
        uid: str,
    ) -> EmailMessage | None:
        """Fetch a single email by UID."""
        resp = await imap.uid("fetch", uid, "(BODY.PEEK[] FLAGS)")
        if resp.result != "OK":
            return None
        return _parse_fetch_response(uid, resp.lines)


def _line_text(line: str | bytes | bytearray) -> str:
    """Decode one IMAP response line to text (aioimaplib yields bytes)."""
    if isinstance(line, str):
        return line
    return bytes(line).decode("utf-8", errors="replace")


def _parse_uid_list(lines: Sequence[str | bytes | bytearray]) -> list[str]:
    """Extract UIDs from an IMAP search response."""
    uids: list[str] = []
    for line in lines:
        stripped = _line_text(line).strip()
        if stripped:
            uids.extend(t for t in stripped.split() if t.isascii() and t.isdigit())
    return uids


def _decode_text(raw: bytes, charset: str) -> str:
    """Decode *raw* as *charset*, falling back to lossy UTF-8 on a bad label."""
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeError):
        return raw.decode("utf-8", errors="replace")


def _part_content(uid: str, part: StdEmailMessage) -> object:
    """Return a part's content, treating its declared charset as untrusted.

    ``get_content()`` raises on a charset label Python cannot decode text with
    (``unknown-8bit``, ``base64``, ``idna``, a non-ASCII label, ...), so a text
    part is decoded here directly -- mirroring ``get_content()``'s
    ``errors="replace"`` policy -- falling back to lossy UTF-8, the same policy
    ``_line_text`` applies to IMAP lines, so it degrades only its own body.
    """
    if part.get_content_maintype() != "text":
        return part.get_content()
    charset = part.get_content_charset() or "us-ascii"
    raw = part.get_payload(decode=True)
    data = raw if isinstance(raw, bytes) else b""
    logger.debug("Email uid %s: decoding text part as %r", uid, charset)
    return _decode_text(data, charset)


def _parse_fetch_response(
    uid: str,
    lines: Sequence[str | bytes | bytearray],
) -> EmailMessage | None:
    """Parse a raw IMAP FETCH response into an EmailMessage.

    aioimaplib frames the message literal as a ``bytearray`` and every
    control/status line as ``bytes``; the literal is the ``bytearray`` (or,
    absent one, the largest ``bytes`` line that parses with headers — so a
    no-match ``Fetch completed.`` status line is not mistaken for a message)
    and all other lines are decoded to text for the ``FLAGS`` scan.
    """
    literal_idx: int | None = None
    raw_bytes: bytes | None = None
    for idx, line in enumerate(lines):
        if isinstance(line, bytearray):
            literal_idx, raw_bytes = idx, bytes(line)
            break
    from_bytearray = raw_bytes is not None
    if raw_bytes is None:
        for idx, line in enumerate(lines):
            if isinstance(line, bytes) and (raw_bytes is None or len(line) > len(raw_bytes)):
                literal_idx, raw_bytes = idx, line
    flags_str = " ".join(
        text
        for idx, line in enumerate(lines)
        if idx != literal_idx and "FLAGS" in (text := _line_text(line)).upper()
    )
    if raw_bytes is None:
        return None
    import email as email_mod

    parsed = email_mod.message_from_bytes(raw_bytes, policy=default_policy)
    if not from_bytearray and not parsed.keys():
        return None
    body = ""
    if parsed.is_multipart():
        for part in parsed.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = _part_content(uid, part)
                if isinstance(payload, str):
                    body = payload
                    break
    else:
        payload = _part_content(uid, parsed)
        if isinstance(payload, str):
            body = payload
    seen = r"\seen" in flags_str.lower()
    return EmailMessage(
        uid=uid,
        from_addr=str(parsed.get("From", "")),
        subject=str(parsed.get("Subject", "")),
        body=body,
        date=str(parsed.get("Date", "")),
        message_id=str(parsed.get("Message-ID", "")),
        seen=seen,
    )
