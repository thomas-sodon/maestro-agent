"""Bidirectional messaging between Maestro and Thomas."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from maestro.fileutil import append_jsonl_row, atomic_write, file_lock
from maestro.plan_feedback import truncate_hash_prefix
from maestro.types import MessageSender, MessageType

if TYPE_CHECKING:
    from maestro.plan_feedback import PlanRef

logger = logging.getLogger("maestro")

# Bound on the joined unread-message context that feeds contemplation, the
# reply prompt, the journal, and lifecycle resume (context bloat caps). Sized
# so one maximum-size writer — the 4000-char ``notify-pre-deploy`` notice —
# survives intact alongside its label and the elision marker.
MAX_MESSAGE_CONTEXT_CHARS = 4500

# ``message_id`` prefix the ``notify-pre-deploy`` writer stamps on its notice;
# shared with the reader so the drain can tell the machine notice apart from a
# message Thomas typed.
PRE_DEPLOY_MESSAGE_ID_PREFIX = "pre-deploy-"

# ``message_id`` prefix a dashboard "Request changes" submission stamps on its
# ``plan_feedback`` row; the fold reads it back as the entry's ``source``.
DASHBOARD_FEEDBACK_MESSAGE_ID_PREFIX = "dashboard-feedback-"


def is_pre_deploy_notice(msg: Message) -> bool:
    """True iff ``msg`` is a ``notify-pre-deploy`` notice, not a typed message.

    The notice informs contemplation and the journal but is never something
    to reply to.
    """
    return msg.sender == "thomas" and msg.message_id.startswith(
        PRE_DEPLOY_MESSAGE_ID_PREFIX
    )


def bound_message_context(parts: list[str], limit: int) -> str:
    """Join ``parts`` newline-separated within ``limit`` chars, newest-first.

    Keeps the newest parts that fit behind an elision marker naming how many
    older parts were omitted; a lone oversized newest part is truncated at its
    tail rather than dropped. Under the limit the plain join is returned.
    """
    joined = "\n".join(parts)
    if len(joined) <= limit:
        return joined
    kept: list[str] = []
    size = 0
    for part in reversed(parts):
        marker = _elision_marker(len(parts) - len(kept) - 1)
        if size + len(part) + 1 + len(marker) > limit:
            break
        kept.append(part)
        size += len(part) + 1
    if not kept:
        omitted = len(parts) - 1
        head = _elision_marker(omitted) + "\n" if omitted else ""
        return (head + parts[-1])[:limit]
    marker = _elision_marker(len(parts) - len(kept))
    return marker + "\n" + "\n".join(reversed(kept))


def _elision_marker(omitted: int) -> str:
    return f"[{omitted} earlier message(s) omitted — see messages.jsonl]"


@dataclass
class Message:
    message_id: str
    timestamp: str
    sender: MessageSender
    content: str
    message_type: MessageType
    reply_to: str | None = None
    read: bool = False


def build_telegram_inbound_message(
    text: str,
    reply_to: str | None,
    plan_ref: PlanRef | None,
    *,
    max_length: int,
    feedback_serviceable: bool,
) -> Message:
    """Build the ``Message`` row for one authorised inbound Telegram text.

    A reply to an ``ask_thomas`` question threads back as an ``answer``
    (#4154), anything else is an ``instruction``. When *feedback_serviceable*
    (``[loop] plan_feedback_enabled`` with curation running and not
    focus-suppressed) a reply to a plan review body is a ``plan_feedback`` row
    threaded to ``<plan_id>@<hash prefix>`` (proposal 145), which the drain
    folds into the plan's reviewer ledger; otherwise it stays an instruction
    so nothing is swallowed into a ledger no curator reads.
    """
    message_type: MessageType = "answer" if reply_to else "instruction"
    if feedback_serviceable and plan_ref is not None:
        message_type = "plan_feedback"
        reply_to = f"{plan_ref.plan_id}@{plan_ref.hash_prefix}"
    return Message(
        message_id=uuid.uuid4().hex[:12],
        timestamp=datetime.now(UTC).isoformat(),
        sender="thomas",
        content=text[:max_length],
        message_type=message_type,
        reply_to=reply_to,
    )


def build_dashboard_feedback_message(
    plan_id: str, content_hash: str, text: str, *, max_length: int,
) -> Message:
    """Build the ``plan_feedback`` row for one dashboard "Request changes".

    Threaded to ``<plan_id>@<hash prefix>`` exactly like a Telegram review
    reply (proposal 145), so the loop's fold is the only writer of the plan
    row; the id prefix records that the dashboard was the source.
    """
    return Message(
        message_id=DASHBOARD_FEEDBACK_MESSAGE_ID_PREFIX + uuid.uuid4().hex[:12],
        timestamp=datetime.now(UTC).isoformat(),
        sender="thomas",
        content=text[:max_length],
        message_type="plan_feedback",
        reply_to=f"{plan_id}@{truncate_hash_prefix(content_hash)}",
    )


class MessageStore:
    """Persistent message storage backed by a JSONL file."""

    def __init__(self, memory_dir: Path) -> None:
        self._dir = memory_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._messages_path = self._dir / "messages.jsonl"

    def save_message(self, msg: Message) -> None:
        if not msg.message_id:
            raise ValueError("message_id must not be empty")
        if not msg.content or not msg.content.strip():
            raise ValueError("content must not be empty")
        append_jsonl_row(self._messages_path, asdict(msg))

    def _load_all(self) -> list[Message]:
        if not self._messages_path.exists():
            return []
        messages: list[Message] = []
        known = {f.name for f in fields(Message)}
        for line in self._messages_path.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    logger.warning("Skipping corrupted message record in %s", self._messages_path)
                    continue
                filtered = {k: v for k, v in raw.items() if k in known}
                messages.append(Message(**filtered))
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("Skipping corrupted message record in %s", self._messages_path)
                continue
        return messages

    def get_recent(self, n: int = 50) -> list[Message]:
        if n <= 0:
            return []
        return self._load_all()[-n:]

    def get_unread(self) -> list[Message]:
        return [
            m for m in self._load_all()
            if m.sender == "thomas" and not m.read
        ]

    def latest_inbound_message_id(self) -> str | None:
        """Return the message_id of the most recent sender=thomas message.

        Returns ``None`` when no inbound messages have ever been received.
        Used by the burn-rate standby gate to anchor "any strictly-later
        inbound message releases standby" without relying on timestamp
        arithmetic.
        """
        for m in reversed(self._load_all()):
            if m.sender == "thomas":
                return m.message_id
        return None

    def mark_read(self, message_id: str) -> bool:
        with file_lock(self._messages_path):
            messages = self._load_all()
            found = False
            for m in messages:
                if m.message_id == message_id:
                    m.read = True
                    found = True
                    break
            if found:
                atomic_write(
                    self._messages_path,
                    "".join(json.dumps(asdict(m)) + "\n" for m in messages),
                )
            return found

    def mark_read_many(self, message_ids: set[str]) -> int:
        """Mark every message in ``message_ids`` as read in one pass.

        Acquires the lock once, loads once, and rewrites the file at most
        once, returning the number of messages newly flipped to read.
        Marking N messages this way is one load+write, not N.
        """
        if not message_ids:
            return 0
        with file_lock(self._messages_path):
            messages = self._load_all()
            count = 0
            for m in messages:
                if m.message_id in message_ids and not m.read:
                    m.read = True
                    count += 1
            if count:
                atomic_write(
                    self._messages_path,
                    "".join(json.dumps(asdict(m)) + "\n" for m in messages),
                )
            return count

    def get_thread(self, message_id: str) -> list[Message]:
        all_msgs = self._load_all()
        return [
            m for m in all_msgs
            if m.message_id == message_id or m.reply_to == message_id
        ]

    def get_unanswered_questions(self) -> list[Message]:
        all_msgs = self._load_all()
        reply_targets = {m.reply_to for m in all_msgs if m.reply_to}
        return [
            m for m in all_msgs
            if m.sender == "maestro"
            and m.message_type == "question"
            and m.message_id not in reply_targets
        ]
