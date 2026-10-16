"""Email tools for the Claude Agent SDK.

Creates an in-process MCP server with email_send, email_inbox, and email_read
tools that the agent can call directly during task execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from maestro.inproc_mcp import create_inproc_mcp_server, tool

if TYPE_CHECKING:
    from maestro.email import EmailClient

logger = logging.getLogger("maestro")

# MCP server key used when registering in cli.py and the executor.
SERVER_KEY = "maestro_email"

# Qualified tool names the SDK exposes once the server is registered.
TOOL_NAMES: list[str] = [
    f"mcp__{SERVER_KEY}__email_send",
    f"mcp__{SERVER_KEY}__email_inbox",
    f"mcp__{SERVER_KEY}__email_read",
]

# Receive-side output budget for email_read / email_inbox, distinct from the
# outbound ``email_max_length``. A tool result must stay under the SDK
# transport's 1 MiB stream-json frame. The worst case per Python character is a
# non-BMP code point, which ``json.dumps`` escapes to a 12-byte surrogate pair
# (``\ud83d\ude00``); 64 000 chars x 12 bytes = 768 000 bytes, leaving
# headroom for the MCP/stream-json envelope. The cap covers the whole assembled
# response (headers included), so a pathological header cannot bypass it.
EMAIL_RESULT_CHAR_CAP = 64_000


def _cap_text(text: str, cap: int = EMAIL_RESULT_CHAR_CAP) -> str:
    """Truncate *text* to at most *cap* chars, marker included; pass short text through."""
    if len(text) <= cap:
        return text
    # The marker's own length depends on the omitted count; size it against the
    # largest possible count so the final string never exceeds *cap*.
    reserve = len(f"\n\n[truncated — {len(text)} more chars]")
    kept = cap - reserve
    return text[:kept] + f"\n\n[truncated — {len(text) - kept} more chars]"


_SEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Recipient email address"},
        "subject": {"type": "string", "description": "Email subject line"},
        "body": {"type": "string", "description": "Email body text"},
        "reply_to_message_id": {
            "type": "string",
            "description": "Optional Message-ID to reply to (for threading)",
        },
    },
    "required": ["to", "subject", "body"],
}

_INBOX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Max emails to return (default 20)",
            "default": 20,
        },
        "unseen_only": {
            "type": "boolean",
            "description": "Only return unseen emails (default true)",
            "default": True,
        },
    },
}

_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "uid": {"type": "string", "description": "Email UID to read"},
    },
    "required": ["uid"],
}


async def _handle_send(client: EmailClient, args: dict[str, Any]) -> dict[str, Any]:
    to = args["to"]
    subject = args["subject"]
    body = args["body"]
    reply_to = args.get("reply_to_message_id")
    message_id = await client.send(to, subject, body, reply_to_message_id=reply_to)
    return {"type": "text", "text": f"Email sent to {to}. Message-ID: {message_id}"}


async def _handle_inbox(client: EmailClient, args: dict[str, Any]) -> dict[str, Any]:
    limit = args.get("limit", 20)
    unseen_only = args.get("unseen_only", True)

    # Validate the model-supplied limit at the tool boundary: the input schema
    # reaches the SDK only as advisory, so a non-positive value would otherwise
    # slice with raw Python semantics at email.py's uids[-limit:] (limit=0 ->
    # uids[0:], i.e. the whole mailbox; limit=-5 -> all-but-the-5-oldest), one
    # IMAP round-trip per message. Exclude bool explicitly since it is an int
    # subclass, and order the isinstance check first so limit <= 0 cannot raise
    # TypeError on a non-comparable value.
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return {
            "type": "text",
            "text": "limit must be a positive integer.",
            "is_error": True,
        }

    messages = await client.fetch_inbox(limit=limit, unseen_only=unseen_only)
    if not messages:
        return {"type": "text", "text": "No emails found."}
    lines: list[str] = []
    for m in messages:
        seen = " " if m.seen else "*"
        lines.append(f"[{seen}] UID={m.uid} | {m.date} | {m.from_addr}")
        lines.append(f"    Subject: {m.subject}")
    return {"type": "text", "text": _cap_text("\n".join(lines))}


async def _handle_read(client: EmailClient, args: dict[str, Any]) -> dict[str, Any]:
    uid = args["uid"]
    found = await client.fetch_by_uid(uid, mark_seen=True)
    if found is None:
        return {
            "type": "text",
            "text": f"Email UID {uid} not found.",
            "is_error": True,
        }
    text = (
        f"From: {found.from_addr}\n"
        f"Subject: {found.subject}\n"
        f"Date: {found.date}\n"
        f"Message-ID: {found.message_id}\n"
        f"\n{found.body}"
    )
    return {"type": "text", "text": _cap_text(text)}


def create_email_mcp_server(client: EmailClient) -> Any:
    """Create an in-process MCP server with email tools.

    The *client* instance is captured in closures so each tool handler
    can call its async methods directly.
    """

    @tool(name="email_send", description="Send an email to a recipient", input_schema=_SEND_SCHEMA)
    async def email_send(args: dict[str, Any]) -> dict[str, Any]:
        return await _handle_send(client, args)

    @tool(name="email_inbox", description="List emails in the inbox", input_schema=_INBOX_SCHEMA)
    async def email_inbox(args: dict[str, Any]) -> dict[str, Any]:
        return await _handle_inbox(client, args)

    @tool(
        name="email_read",
        description="Read a specific email by UID and mark it as seen",
        input_schema=_READ_SCHEMA,
    )
    async def email_read(args: dict[str, Any]) -> dict[str, Any]:
        return await _handle_read(client, args)

    return create_inproc_mcp_server(
        name="maestro_email", tools=[email_send, email_inbox, email_read],
    )
