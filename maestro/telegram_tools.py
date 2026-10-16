"""Telegram tools for the Claude Agent SDK.

Creates an in-process MCP server with telegram_send and telegram_read_messages
tools that the agent can call directly during task execution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from maestro.inproc_mcp import create_inproc_mcp_server, tool

if TYPE_CHECKING:
    from maestro.messaging import Message, MessageStore
    from maestro.notifications.base import Notifier

logger = logging.getLogger("maestro")

# MCP server key used when registering in cli.py and the executor.
SERVER_KEY = "maestro_telegram"

# Qualified tool names the SDK exposes once the server is registered.
TOOL_NAMES: list[str] = [
    f"mcp__{SERVER_KEY}__telegram_send",
    f"mcp__{SERVER_KEY}__telegram_read_messages",
]

_SEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "The message text to send via Telegram"},
    },
    "required": ["message"],
}

# Bounds on a telegram_read_messages result. A tool result must fit the SDK
# transport's 1 MiB message frame (see the Tool-result frame cap invariant and
# tests/test_source_file_size.py). The worst-case wire expansion of one Python
# char is 12 bytes: an astral code point JSON-escaped with ensure_ascii as a
# surrogate pair (``\ud83d\ude00``). 60 000 chars * 12 B = 720 000 B, leaving
# ~300 KiB of headroom for the message envelope. The per-message cap keeps one
# huge stored message from crowding every other entry out of the page.
TELEGRAM_READ_MAX_LIMIT = 50
TELEGRAM_READ_RESULT_CHAR_CAP = 60_000
TELEGRAM_READ_MESSAGE_CHAR_CAP = 20_000
# Header fields are capped too, so one entry always fits the page budget and
# at least one message is delivered per call.
TELEGRAM_READ_HEADER_FIELD_CHAR_CAP = 200

_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "description": (
                f"Max messages to return (default 10, max {TELEGRAM_READ_MAX_LIMIT})"
            ),
            "default": 10,
            "maximum": TELEGRAM_READ_MAX_LIMIT,
        },
        "unread_only": {
            "type": "boolean",
            "description": "Only return unread messages from Thomas (default true)",
            "default": True,
        },
    },
}

# Advertised contract for telegram_read_messages. Recent-mode (unread_only=false)
# returns the tail of the full log via MessageStore.get_recent, which does not
# filter by sender, so Maestro's own outbound messages appear alongside Thomas's.
# The description names both senders so the agent is not misled about authorship.
_READ_DESCRIPTION = (
    "Read recent Telegram messages. With unread_only=true (default) returns only "
    "unread messages from Thomas; with unread_only=false returns recent messages "
    "from both Maestro and Thomas. Results are size-capped; when messages are "
    "left out the result says so. Reading does not consume messages: unread "
    "messages stay pending until the loop's message-processing phase ingests them, "
    "which queues instructions automatically; do not act on them from this read."
)


async def _handle_send(notifier: Notifier, args: dict[str, Any]) -> dict[str, Any]:
    message = args.get("message", "")
    text = message.strip() if isinstance(message, str) else ""
    if not text:
        return {
            "type": "text",
            "text": "Message must not be empty.",
            "is_error": True,
        }
    try:
        await notifier.notify(text)
    except Exception as exc:
        # Surface the transient send failure to the agent as a typed is_error
        # response instead of letting it propagate as an opaque tool failure.
        # Mirrors the empty-message rejection above and _handle_read's limit
        # rejection. No warning/error-level log here: the failure is reported
        # to the caller via the return, so this is fail-loud, not silencing.
        return {
            "type": "text",
            "text": f"Failed to send Telegram message: {exc}",
            "is_error": True,
        }
    return {"type": "text", "text": f"Message sent via Telegram: {text[:100]}"}


async def _handle_read(
    message_store: MessageStore, args: dict[str, Any],
) -> dict[str, Any]:
    limit = args.get("limit", 10)
    unread_only = args.get("unread_only", True)

    # Validate the model-supplied limit at the tool boundary: the input schema
    # reaches the SDK only as advisory, so a non-positive value would otherwise
    # slice with raw Python semantics (limit=0 -> [], limit=-1 -> all-but-newest).
    # Exclude bool explicitly since it is an int subclass. Mirrors _handle_send's
    # empty-message rejection.
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return {
            "type": "text",
            "text": "limit must be a positive integer.",
            "is_error": True,
        }

    # Clamp rather than reject an over-max limit, keeping the schema's
    # "max messages" contract while bounding the page size.
    limit = min(limit, TELEGRAM_READ_MAX_LIMIT)

    if unread_only:
        messages = message_store.get_unread()[:limit]
    else:
        messages = message_store.get_recent(limit)

    if not messages:
        label = "unread " if unread_only else ""
        return {"type": "text", "text": f"No {label}messages found."}

    # Display only: ``read`` means loop-ingested, so the flag is left for
    # ``MaestroAgent._process_messages`` to write once it has enqueued (#4020).
    text = _render_bounded(messages, newest_first=not unread_only)

    return {"type": "text", "text": text}


def _render_entry(m: Message) -> str:
    content = m.content
    if len(content) > TELEGRAM_READ_MESSAGE_CHAR_CAP:
        dropped = len(content) - TELEGRAM_READ_MESSAGE_CHAR_CAP
        content = (
            content[:TELEGRAM_READ_MESSAGE_CHAR_CAP]
            + f"\n[truncated — {dropped} more chars]"
        )
    read_marker = " " if m.read else "*"
    timestamp = m.timestamp[:TELEGRAM_READ_HEADER_FIELD_CHAR_CAP]
    sender = m.sender[:TELEGRAM_READ_HEADER_FIELD_CHAR_CAP]
    return f"[{read_marker}] {timestamp} | {sender}\n    {content}"


def _render_bounded(
    messages: list[Message], *, newest_first: bool,
) -> str:
    """Join whole message entries within ``TELEGRAM_READ_RESULT_CHAR_CAP``.

    Unread mode fills oldest-first so a follow-up call resumes where this one
    stopped; recent mode fills newest-first so the tail is kept. Emitted
    entries are always returned in chronological order.
    """
    # Reserve room for the omission footer up front so adding it can never
    # push the result over the cap.
    footer_reserve = 80
    budget = TELEGRAM_READ_RESULT_CHAR_CAP - footer_reserve
    order = list(reversed(messages)) if newest_first else list(messages)
    kept: list[tuple[Message, str]] = []
    used = 0
    for m in order:
        entry = _render_entry(m)
        cost = len(entry) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append((m, entry))
        used += cost
    if newest_first:
        kept.reverse()
    text = "\n".join(entry for _, entry in kept)
    omitted = len(messages) - len(kept)
    if omitted:
        footer = (
            f"[{omitted} older message(s) not shown]"
            if newest_first
            else f"[{omitted} newer message(s) not shown]"
        )
        # A single entry is bounded by the per-message and header-field caps,
        # well under the budget, so at least one entry is always kept and text is non-empty.
        text = f"{text}\n{footer}"
    assert len(text) <= TELEGRAM_READ_RESULT_CHAR_CAP, len(text)
    return text


def create_telegram_mcp_server(
    notifier: Notifier, message_store: MessageStore,
) -> Any:
    """Create an in-process MCP server with Telegram tools.

    The *notifier* and *message_store* instances are captured in closures
    so each tool handler can call their methods directly.
    """

    @tool(
        name="telegram_send",
        description="Send a message via Telegram",
        input_schema=_SEND_SCHEMA,
    )
    async def telegram_send(args: dict[str, Any]) -> dict[str, Any]:
        return await _handle_send(notifier, args)

    @tool(
        name="telegram_read_messages",
        description=_READ_DESCRIPTION,
        input_schema=_READ_SCHEMA,
    )
    async def telegram_read_messages(args: dict[str, Any]) -> dict[str, Any]:
        return await _handle_read(message_store, args)

    return create_inproc_mcp_server(
        name="maestro_telegram", tools=[telegram_send, telegram_read_messages],
    )
