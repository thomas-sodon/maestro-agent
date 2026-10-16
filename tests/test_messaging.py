"""Unit tests for maestro.messaging module."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from maestro.messaging import Message, MessageStore, build_telegram_inbound_message
from maestro.plan_feedback import PlanRef


def _msg(
    mid: str,
    sender: str = "maestro",
    content: str = "hello",
    mtype: str = "info",
    reply_to: str | None = None,
    read: bool = False,
) -> Message:
    return Message(
        message_id=mid,
        timestamp="2026-03-31T00:00:00Z",
        sender=sender,  # type: ignore[arg-type]
        content=content,
        message_type=mtype,  # type: ignore[arg-type]
        reply_to=reply_to,
        read=read,
    )


class TestMessageStore:
    def test_save_and_get_recent(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        m1 = _msg("a1", content="first")
        m2 = _msg("a2", content="second")
        m3 = _msg("a3", content="third")
        store.save_message(m1)
        store.save_message(m2)
        store.save_message(m3)

        recent = store.get_recent()
        assert len(recent) == 3
        assert recent[0].message_id == "a1"
        assert recent[1].message_id == "a2"
        assert recent[2].message_id == "a3"

    def test_get_recent_zero(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("a1"))
        assert store.get_recent(0) == []

    def test_get_recent_limits(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("a1", content="first"))
        store.save_message(_msg("a2", content="second"))
        store.save_message(_msg("a3", content="third"))

        recent = store.get_recent(2)
        assert len(recent) == 2
        assert recent[0].message_id == "a2"
        assert recent[1].message_id == "a3"

    def test_get_unread(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("t1", sender="thomas", read=False))
        store.save_message(_msg("t2", sender="thomas", read=True))
        store.save_message(_msg("m1", sender="maestro", read=False))

        unread = store.get_unread()
        assert len(unread) == 1
        assert unread[0].message_id == "t1"

    def test_mark_read(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("t1", sender="thomas", read=False))
        store.save_message(_msg("t2", sender="thomas", read=False))

        result = store.mark_read("t1")
        assert result is True

        msgs = store.get_recent()
        by_id = {m.message_id: m for m in msgs}
        assert by_id["t1"].read is True
        assert by_id["t2"].read is False

    def test_mark_read_not_found(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("t1"))

        result = store.mark_read("nonexistent")
        assert result is False

    def test_mark_read_many_multi_flip(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("t1", sender="thomas", read=False))
        store.save_message(_msg("t2", sender="thomas", read=False))
        store.save_message(_msg("t3", sender="thomas", read=False))

        count = store.mark_read_many({"t1", "t2"})
        assert count == 2

        by_id = {m.message_id: m for m in store.get_recent()}
        assert by_id["t1"].read is True
        assert by_id["t2"].read is True
        assert by_id["t3"].read is False

    def test_mark_read_many_partial_and_no_match(
        self, mem_fs: dict[str, str],
    ) -> None:
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("t1", sender="thomas", read=False))
        store.save_message(_msg("t2", sender="thomas", read=True))

        # One unread flip, plus an already-read id and an absent id that
        # both must be ignored: only t1 is newly flipped.
        count = store.mark_read_many({"t1", "t2", "absent"})
        assert count == 1

        by_id = {m.message_id: m for m in store.get_recent()}
        assert by_id["t1"].read is True
        assert by_id["t2"].read is True

        # All-already-read / absent ids: nothing newly flipped.
        assert store.mark_read_many({"t2", "absent"}) == 0

    def test_mark_read_many_empty_set(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("t1", sender="thomas", read=False))

        assert store.mark_read_many(set()) == 0

        # File untouched: t1 still unread.
        by_id = {m.message_id: m for m in store.get_recent()}
        assert by_id["t1"].read is False

    def test_get_thread(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        question = _msg("q1", sender="maestro", mtype="question", content="Why?")
        reply = _msg("r1", sender="thomas", mtype="answer", content="Because.", reply_to="q1")
        unrelated = _msg("u1", sender="thomas", content="Unrelated")

        store.save_message(question)
        store.save_message(reply)
        store.save_message(unrelated)

        thread = store.get_thread("q1")
        assert len(thread) == 2
        ids = {m.message_id for m in thread}
        assert ids == {"q1", "r1"}

    def test_get_unanswered_questions(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        q1 = _msg("q1", sender="maestro", mtype="question", content="Question 1?")
        q2 = _msg("q2", sender="maestro", mtype="question", content="Question 2?")
        answer = _msg("a1", sender="thomas", mtype="answer", content="Answer", reply_to="q1")

        store.save_message(q1)
        store.save_message(q2)
        store.save_message(answer)

        unanswered = store.get_unanswered_questions()
        assert len(unanswered) == 1
        assert unanswered[0].message_id == "q2"

    def test_save_empty_content_raises(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        with pytest.raises(ValueError, match="content must not be empty"):
            store.save_message(_msg("x1", content=""))

        with pytest.raises(ValueError, match="content must not be empty"):
            store.save_message(_msg("x2", content="   "))

    def test_save_empty_id_raises(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        with pytest.raises(ValueError, match="message_id must not be empty"):
            store.save_message(_msg("", content="valid"))

    def test_corrupted_lines_skipped(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        # Save a valid message first
        store.save_message(_msg("valid1", content="good"))

        # Inject a corrupted line into the file
        path = str(store._messages_path)
        mem_fs[path] = "NOT VALID JSON\n" + mem_fs[path]

        msgs = store.get_recent()
        assert len(msgs) == 1
        assert msgs[0].message_id == "valid1"

    def test_save_after_unterminated_tail_keeps_new_message(
        self, mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A torn tail from a crashed append must not swallow the next save."""
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("before", sender="thomas"))
        path = str(store._messages_path)
        mem_fs[path] += '{"message_id":"interrupted'

        with caplog.at_level(logging.WARNING, logger="maestro.fileutil"):
            store.save_message(_msg("after", sender="thomas"))

        assert [m.message_id for m in store.get_recent()] == ["before", "after"]
        assert [m.message_id for m in store.get_unread()] == ["before", "after"]
        assert store.latest_inbound_message_id() == "after"
        warnings = [
            r for r in caplog.records
            if r.name == "maestro.fileutil" and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert path in warnings[0].getMessage()

    def test_save_after_fragment_only_file(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        mem_fs[str(store._messages_path)] = '{"message_id":"interrupted'

        store.save_message(_msg("after"))

        assert [m.message_id for m in store.get_recent()] == ["after"]

    def test_blank_lines_in_jsonl_skipped(self, mem_fs: dict[str, str]) -> None:
        """Blank lines embedded in the JSONL file are silently skipped."""
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("v1", content="valid"))

        # Inject blank lines around the valid record
        path = str(store._messages_path)
        mem_fs[path] = "\n" + mem_fs[path] + "\n\n"

        msgs = store.get_recent()
        assert len(msgs) == 1
        assert msgs[0].message_id == "v1"

    def test_non_dict_json_lines_skipped(self, mem_fs: dict[str, str]) -> None:
        """Valid-JSON non-dict lines are skipped like unparseable ones (#3186)."""
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("valid1", sender="thomas", content="good"))

        # Inject valid-JSON lines that parse to non-dict shapes around the
        # record: before the fix these reach ``raw.items()`` and raise
        # AttributeError, escaping the corruption-skip handler.
        path = str(store._messages_path)
        mem_fs[path] = "42\n" + '"x"\n' + mem_fs[path] + "[1]\n"

        # The whole reader surface degrades gracefully instead of wedging.
        msgs = store.get_recent()
        assert len(msgs) == 1
        assert msgs[0].message_id == "valid1"

        unread = store.get_unread()
        assert len(unread) == 1
        assert unread[0].message_id == "valid1"

        assert store.latest_inbound_message_id() == "valid1"

    def test_empty_store(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))

        assert store.get_recent() == []
        assert store.get_unread() == []
        assert store.get_thread("anything") == []
        assert store.get_unanswered_questions() == []

    def test_latest_inbound_message_id_empty(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        assert store.latest_inbound_message_id() is None

    def test_latest_inbound_message_id_only_maestro(self, mem_fs: dict[str, str]) -> None:
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("m1", sender="maestro"))
        store.save_message(_msg("m2", sender="maestro"))
        assert store.latest_inbound_message_id() is None

    def test_latest_inbound_message_id_returns_most_recent_thomas(
        self, mem_fs: dict[str, str]
    ) -> None:
        store = MessageStore(Path("/fake/messaging"))
        store.save_message(_msg("t1", sender="thomas", content="first"))
        store.save_message(_msg("m1", sender="maestro", content="reply"))
        store.save_message(_msg("t2", sender="thomas", content="second"))
        store.save_message(_msg("m2", sender="maestro", content="reply2"))
        assert store.latest_inbound_message_id() == "t2"


# ── build_telegram_inbound_message (proposal 145) ────────────────────

_REF = PlanRef("p1", "0123456789abcdef")


def test_inbound_plan_reply_flag_on_is_plan_feedback() -> None:
    msg = build_telegram_inbound_message(
        "narrow it", None, _REF, max_length=4, feedback_serviceable=True,
    )
    assert msg.message_type == "plan_feedback"
    assert msg.reply_to == "p1@0123456789abcdef"
    assert msg.content == "narr"
    assert msg.sender == "thomas"


def test_inbound_plan_reply_flag_off_is_instruction() -> None:
    msg = build_telegram_inbound_message(
        "narrow it", None, _REF, max_length=100, feedback_serviceable=False,
    )
    assert msg.message_type == "instruction"
    assert msg.reply_to is None


def test_inbound_question_reply_is_answer() -> None:
    msg = build_telegram_inbound_message(
        "yes", "q1", None, max_length=100, feedback_serviceable=True,
    )
    assert msg.message_type == "answer"
    assert msg.reply_to == "q1"
