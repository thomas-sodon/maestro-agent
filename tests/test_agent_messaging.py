"""Tests for messaging integration in MaestroAgent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.messaging import Message, MessageStore
from tests.conftest import MockQueryResult


@pytest.mark.asyncio
async def test_ask_thomas_saves_message(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.message_store = MessageStore(Path("/fake/agent"))

    msg_id = await agent.ask_thomas("What should I focus on?")

    assert msg_id != ""
    messages = agent.message_store.get_recent()
    assert len(messages) == 1
    assert messages[0].sender == "maestro"
    assert messages[0].message_type == "question"
    assert messages[0].content == "What should I focus on?"
    assert messages[0].message_id == msg_id


@pytest.mark.asyncio
async def test_ask_thomas_without_store(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert agent.message_store is None

    result = await agent.ask_thomas("question")

    assert result == ""


@pytest.mark.asyncio
async def test_process_messages_reads_and_marks(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store

    store.save_message(Message(
        message_id="msg001",
        timestamp="2026-03-31T00:00:00+00:00",
        sender="thomas",
        content="Please focus on testing.",
        message_type="instruction",
    ))

    context = await agent._process_messages()

    assert "Please focus on testing." in context
    # Message should now be marked as read
    unread = store.get_unread()
    assert len(unread) == 0
    all_msgs = store.get_recent()
    assert len(all_msgs) == 1
    assert all_msgs[0].read is True


@pytest.mark.asyncio
async def test_process_messages_empty(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent.message_store = MessageStore(Path("/fake/agent"))

    result = await agent._process_messages()

    assert result == ""


@pytest.mark.asyncio
async def test_process_messages_journals(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store

    store.save_message(Message(
        message_id="msg002",
        timestamp="2026-03-31T00:00:00+00:00",
        sender="thomas",
        content="Great progress today!",
        message_type="info",
    ))

    await agent._process_messages()

    entries = agent.journal.read_recent_events(10)
    assert len(entries) >= 1
    matching = [e for e in entries if e.triggered_by == "messages"]
    assert len(matching) == 1
    assert "Great progress today!" in matching[0].content


@pytest.mark.asyncio
async def test_process_messages_without_store(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    assert agent.message_store is None

    result = await agent._process_messages()

    assert result == ""


@pytest.mark.asyncio
async def test_run_loop_crashes_on_message_processing_failure(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """run_loop propagates _process_messages errors (phase-level suppression removed)."""
    from unittest.mock import AsyncMock, MagicMock

    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="ok")  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]
    agent._process_messages = AsyncMock(side_effect=RuntimeError("broken"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="broken"):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # The phase raised before contemplate could run, so contemplate was never called.
    agent.contemplate.assert_not_called()
    agent.shutdown.assert_called_once_with(crashed=True)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ask_thomas_notifier_failure_still_saves(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """If the notifier fails, the message should still be saved."""
    from unittest.mock import AsyncMock

    agent = make_agent()
    agent.message_store = MessageStore(Path("/fake/agent"))
    agent.notifier.ask_thomas = AsyncMock(side_effect=RuntimeError("send failed"))  # type: ignore[method-assign]

    msg_id = await agent.ask_thomas("What should I do?")

    assert msg_id != ""
    msgs = agent.message_store.get_recent()
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_ask_thomas_empty_question_is_noop(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """An empty question returns "" without saving or pinging the notifier."""
    agent = make_agent()
    agent.message_store = MessageStore(Path("/fake/agent"))
    agent.notifier.ask_thomas = AsyncMock()  # type: ignore[method-assign]

    result = await agent.ask_thomas("")

    assert result == ""
    assert agent.message_store.get_recent() == []
    agent.notifier.ask_thomas.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_thomas_whitespace_question_is_noop(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """A whitespace-only question is a no-op — proves the guard uses .strip()."""
    agent = make_agent()
    agent.message_store = MessageStore(Path("/fake/agent"))
    agent.notifier.ask_thomas = AsyncMock()  # type: ignore[method-assign]

    result = await agent.ask_thomas("   ")

    assert result == ""
    assert agent.message_store.get_recent() == []
    agent.notifier.ask_thomas.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_messages_multiple(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Multiple unread messages should all be processed."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store

    store.save_message(Message(
        message_id="m1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="First", message_type="instruction",
    ))
    store.save_message(Message(
        message_id="m2", timestamp="2026-01-01T00:01:00",
        sender="thomas", content="Second", message_type="answer",
    ))

    context = await agent._process_messages()

    assert "First" in context
    assert "Second" in context
    assert len(store.get_unread()) == 0


@pytest.mark.asyncio
async def test_process_messages_context_bounded(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """A large unread backlog yields a bounded context; side effects still run."""
    from maestro.messaging import MAX_MESSAGE_CONTEXT_CHARS

    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store
    size = agent.config.message_max_length
    for i in range(30):
        store.save_message(Message(
            message_id=f"m{i}", timestamp=f"2026-01-01T00:{i:02d}:00",
            sender="thomas", content=f"{i:02d}" + "x" * (size - 2),
            message_type="instruction" if i % 3 == 0 else "answer",
        ))

    context = await agent._process_messages()

    assert len(context) <= MAX_MESSAGE_CONTEXT_CHARS
    newest = "[Answer from Thomas] 29" + "x" * (size - 2)
    assert context.endswith(newest)
    kept = context.count("from Thomas]")
    assert context.startswith(f"[{30 - kept} earlier message(s) omitted")
    requests = agent.consciousness.load_self_model().pending_user_requests
    assert len(requests) == 10
    assert store.get_unread() == []


@pytest.mark.asyncio
async def test_process_messages_context_under_bound_unchanged(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """A short batch is the plain newline join, with no elision marker."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store
    store.save_message(Message(
        message_id="m1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="First", message_type="instruction",
    ))
    store.save_message(Message(
        message_id="m2", timestamp="2026-01-01T00:01:00",
        sender="thomas", content="Second", message_type="answer",
    ))

    context = await agent._process_messages()

    assert context == "[Instruction from Thomas] First\n[Answer from Thomas] Second"


@pytest.mark.asyncio
async def test_process_messages_pre_deploy_notice_excluded_from_reply_context(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
):
    """A notice-only batch still informs contemplation but leaves nothing to
    reply to."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store
    agent._iter_reply_context = "stale"
    store.save_message(Message(
        message_id="pre-deploy-1700000000000",
        timestamp="2026-03-31T00:00:00+00:00",
        sender="thomas",
        content="Pre-deploy notice: #1 landing",
        message_type="info",
    ))

    context = await agent._process_messages()

    assert context == "[Info from Thomas] Pre-deploy notice: #1 landing"
    assert agent._iter_reply_context == ""
    assert store.get_unread() == []


@pytest.mark.asyncio
async def test_process_messages_mixed_batch_reply_context_has_only_thomas(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
):
    """A mixed batch replies to the typed message only; contemplation sees
    both."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store
    store.save_message(Message(
        message_id="msg-real",
        timestamp="2026-03-31T00:00:00+00:00",
        sender="thomas",
        content="Real note",
        message_type="info",
    ))
    store.save_message(Message(
        message_id="pre-deploy-1700000000000",
        timestamp="2026-03-31T00:00:01+00:00",
        sender="thomas",
        content="Pre-deploy notice",
        message_type="info",
    ))

    context = await agent._process_messages()

    assert context == (
        "[Info from Thomas] Real note\n[Info from Thomas] Pre-deploy notice"
    )
    assert agent._iter_reply_context == "[Info from Thomas] Real note"


@pytest.mark.asyncio
async def test_process_messages_no_store_clears_reply_context(make_agent):  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._iter_reply_context = "stale"

    context = await agent._process_messages()

    assert context == ""
    assert agent._iter_reply_context == ""


def test_respond_to_messages_phase_skipped_for_notice_only_batch(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._iter_message_context = "[Info from Thomas] Pre-deploy notice"
    agent._iter_reply_context = ""
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=False,
    ):
        will_run = agent._phase_will_run(
            "respond_to_messages",
            iteration=1, smoke=False, first_iteration_after_restart=True,
            task_every=3, evolve_every=5, dream_every=10, audit_self_every=10,
        )
    assert will_run is False


def test_respond_to_messages_phase_runs_for_reply_context(make_agent) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    agent._iter_message_context = "[Info from Thomas] Real note"
    agent._iter_reply_context = "[Info from Thomas] Real note"
    with patch(
        "maestro.loop.lifecycle._is_in_peak_throttle_window", return_value=False,
    ):
        will_run = agent._phase_will_run(
            "respond_to_messages",
            iteration=1, smoke=False, first_iteration_after_restart=True,
            task_every=3, evolve_every=5, dream_every=10, audit_self_every=10,
        )
    assert will_run is True


def test_is_pre_deploy_notice_true_and_false_cases() -> None:
    from maestro.messaging import PRE_DEPLOY_MESSAGE_ID_PREFIX, is_pre_deploy_notice
    from maestro.types import MessageSender

    def _msg(message_id: str, sender: MessageSender) -> Message:
        return Message(
            message_id=message_id,
            timestamp="2026-03-31T00:00:00+00:00",
            sender=sender,
            content="x",
            message_type="info",
        )

    assert is_pre_deploy_notice(_msg(f"{PRE_DEPLOY_MESSAGE_ID_PREFIX}1", "thomas")) is True
    assert is_pre_deploy_notice(_msg("msg001", "thomas")) is False
    assert is_pre_deploy_notice(_msg(f"{PRE_DEPLOY_MESSAGE_ID_PREFIX}1", "maestro")) is False


def test_bound_message_context_oversized_newest_truncated() -> None:
    """A lone oversized newest part is tail-truncated, not dropped."""
    from maestro.messaging import bound_message_context

    marker = "[1 earlier message(s) omitted — see messages.jsonl]"

    result = bound_message_context(["old", "N" * 100], 80)

    assert result == marker + "\n" + "N" * (80 - len(marker) - 1)


def test_bound_message_context_single_oversized_part() -> None:
    """A single oversized part carries no marker and keeps its head."""
    from maestro.messaging import bound_message_context

    result = bound_message_context(["abcdefghij"], 4)

    assert result == "abcd"


# --- _respond_to_messages tests ---


@pytest.mark.asyncio
async def test_respond_to_messages_generates_and_sends(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Happy path: generates a response and sends via Telegram."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]

    mock_result = MockQueryResult(response="Got it, Thomas! I'll focus on testing.")
    with patch("maestro.agent.query", AsyncMock(return_value=mock_result)):
        await agent._respond_to_messages(
            "[Instruction from Thomas] Focus on testing.", "I reflected on testing.",
        )

    msgs = store.get_recent()
    assert len(msgs) == 1
    assert msgs[0].sender == "maestro"
    assert msgs[0].message_type == "info"
    assert "focus on testing" in msgs[0].content.lower()
    agent.notifier.notify.assert_called_once()
    call_text = agent.notifier.notify.call_args[0][0]
    assert call_text.startswith("[Maestro]")


@pytest.mark.asyncio
async def test_respond_to_messages_history_omits_pre_deploy_notice(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """The recent-conversation history never shows a notice as a [Thomas] turn."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent_hist"))
    agent.message_store = store
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]
    store.save_message(Message(
        message_id="pre-deploy-1700000000000",
        timestamp="2026-03-31T00:00:00+00:00",
        sender="thomas",
        content="Pre-deploy notice: #1 landing",
        message_type="info",
    ))
    store.save_message(Message(
        message_id="msg-real",
        timestamp="2026-03-31T00:00:01+00:00",
        sender="thomas",
        content="Real note",
        message_type="info",
    ))

    mock_result = MockQueryResult(response="Ack")
    with patch("maestro.agent.query", AsyncMock(return_value=mock_result)) as mock_q:
        await agent._respond_to_messages("[Info from Thomas] Real note", "r")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "[Thomas] Real note" in prompt
    assert "Pre-deploy notice" not in prompt


@pytest.mark.asyncio
async def test_respond_to_messages_passes_chars_complete(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """_respond_to_messages wires chars_complete(200) through both paths."""
    from unittest.mock import MagicMock

    from maestro.sdk import chars_complete

    agent = make_agent()
    agent.message_store = MessageStore(Path("/fake/agent_resp_ct"))
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]

    mock_result = MockQueryResult(response="Reply from Maestro")

    with patch("maestro.agent.query", AsyncMock(return_value=mock_result)) as mock_q:
        await agent._respond_to_messages("ctx", "reflection")
    assert mock_q.call_args.kwargs.get("complete_on") is chars_complete(200)

    with patch(
        "maestro.agent.query_on_session", AsyncMock(return_value=mock_result),
    ) as mock_qos:
        await agent._respond_to_messages("ctx", "reflection", session=MagicMock())
    assert mock_qos.call_args.kwargs.get("complete_on") is chars_complete(200)


@pytest.mark.asyncio
async def test_respond_to_messages_sessionless_is_tool_free(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """The sessionless reply declares ``tools=[]`` — no default shell (#4127)."""
    agent = make_agent()
    agent.message_store = MessageStore(Path("/fake/agent_resp_tools"))
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]

    mock_result = MockQueryResult(response="Reply from Maestro")
    with patch("maestro.agent.query", AsyncMock(return_value=mock_result)) as mock_q:
        await agent._respond_to_messages("ctx", "reflection")
    assert mock_q.call_args.kwargs["options"].tools == []


@pytest.mark.asyncio
async def test_respond_to_messages_prompt_states_configured_max_length(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """The response prompt states the configured message_max_length, not a literal.

    Regression for #392: the prompt's stated cap must track
    config.message_max_length so the guidance and the enforced truncation
    ceiling stay in sync. Using a non-default value (that does not itself
    contain "500") proves the prompt reflects config rather than any baked-in
    literal.
    """
    agent = make_agent()
    agent.message_store = MessageStore(Path("/fake/agent_resp_maxlen"))
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]
    agent.config.message_max_length = 1234

    mock_result = MockQueryResult(response="Reply from Maestro")
    with patch("maestro.agent.query", AsyncMock(return_value=mock_result)) as mock_q:
        await agent._respond_to_messages("ctx", "reflection")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "1234" in prompt
    assert "500 characters" not in prompt


@pytest.mark.asyncio
async def test_respond_to_messages_skips_when_no_context(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Empty message_context should skip without making an LLM call."""
    agent = make_agent()
    agent.message_store = MessageStore(Path("/fake/agent"))

    with patch("maestro.agent.query", AsyncMock()) as mock_query:
        await agent._respond_to_messages("", "some reflection")

    mock_query.assert_not_called()


@pytest.mark.asyncio
async def test_respond_to_messages_skips_when_no_store(make_agent):  # type: ignore[no-untyped-def]
    """No message_store should skip without making an LLM call."""
    agent = make_agent()
    assert agent.message_store is None

    with patch("maestro.agent.query", AsyncMock()) as mock_query:
        await agent._respond_to_messages("some context", "some reflection")

    mock_query.assert_not_called()


@pytest.mark.asyncio
async def test_respond_to_messages_propagates_query_failure(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """If query() raises, the error propagates (phase-level suppression removed)."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store

    with patch("maestro.agent.query", AsyncMock(side_effect=RuntimeError("LLM down"))), \
         pytest.raises(RuntimeError, match="LLM down"):
        await agent._respond_to_messages("some context", "reflection")

    # No message was saved because the query failed before save_message was reached.
    assert len(store.get_recent()) == 0


@pytest.mark.asyncio
async def test_respond_to_messages_propagates_notify_failure(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """A real ``Exception`` from notifier.notify propagates after save.

    The message is persisted before the Telegram call, so the local
    record is intact even when the notifier raises. Best-effort
    notification suppresses only ``CancelledError``; real bugs surface
    for RCA rather than being hidden under WARNING.
    """
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store
    agent.notifier.notify = AsyncMock(side_effect=RuntimeError("Telegram down"))  # type: ignore[method-assign]

    mock_result = MockQueryResult(response="Thanks Thomas!")
    with (
        patch("maestro.agent.query", AsyncMock(return_value=mock_result)),
        pytest.raises(RuntimeError, match="Telegram down"),
    ):
        await agent._respond_to_messages("some context", "reflection")

    msgs = store.get_recent()
    assert len(msgs) == 1
    assert msgs[0].content == "Thanks Thomas!"


@pytest.mark.asyncio
async def test_respond_to_messages_truncates_long_response(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Response should be truncated to config.message_max_length."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store
    agent.notifier.notify = AsyncMock()  # type: ignore[method-assign]
    agent.config.message_max_length = 50

    long_response = "A" * 200
    mock_result = MockQueryResult(response=long_response)
    with patch("maestro.agent.query", AsyncMock(return_value=mock_result)):
        await agent._respond_to_messages("context", "reflection")

    msgs = store.get_recent()
    assert len(msgs) == 1
    assert len(msgs[0].content) == 50


@pytest.mark.asyncio
async def test_respond_to_messages_skips_empty_response(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """If the LLM returns empty text, no message should be saved."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store

    mock_result = MockQueryResult(response="   ")
    with patch("maestro.agent.query", AsyncMock(return_value=mock_result)):
        await agent._respond_to_messages("context", "reflection")

    assert len(store.get_recent()) == 0


@pytest.mark.asyncio
async def test_run_loop_sends_response_after_contemplate(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """Integration: messages in store -> loop processes them -> response sent."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    store.save_message(Message(
        message_id="m1", timestamp="2026-01-01T00:00:00",
        sender="thomas", content="Hello Maestro!", message_type="info",
    ))

    mock_text = MockQueryResult(response="Hello Thomas!")
    # Consciousness's update_self_model now propagates JSON parse failures
    # (fail-loud, CLAUDE.md → "Fail loud"), so its query path needs valid
    # JSON; the agent's response-message path keeps plain text.
    mock_json = MockQueryResult(response="{}")
    with patch("maestro.consciousness.query", AsyncMock(return_value=mock_json)), \
         patch("maestro.agent.query", AsyncMock(return_value=mock_text)):
        await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    # Should have the original message + Maestro's response
    msgs = store.get_recent()
    maestro_msgs = [m for m in msgs if m.sender == "maestro"]
    assert len(maestro_msgs) >= 1
    assert any("Hello Thomas!" in m.content for m in maestro_msgs)


def _save_thomas_instruction(
    store: MessageStore, content: str, reply_to: str | None = None,
) -> None:
    store.save_message(Message(
        message_id="t1",
        timestamp="2026-03-31T00:00:00+00:00",
        sender="thomas",
        content=content,
        message_type="instruction",
        reply_to=reply_to,
    ))


def _read_via_messaging_tool(store: MessageStore):  # type: ignore[no-untyped-def]
    from maestro.notifications.base import NullNotifier
    from maestro.tools import create_messaging_server

    captured: list = []  # type: ignore[type-arg]
    with patch(
        "maestro.tools.create_inproc_mcp_server",
        side_effect=lambda **kwargs: captured.extend(kwargs.get("tools", [])),
    ):
        create_messaging_server(NullNotifier(), store)
    return next(t for t in captured if t.name == "read_messages_from_thomas")


@pytest.mark.asyncio
async def test_tool_read_does_not_preempt_instruction_enqueue(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """#4020: telegram_read_messages shows a message without consuming it, so
    the loop still enqueues the instruction and then marks it read."""
    from maestro.telegram_tools import _handle_read

    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store
    _save_thomas_instruction(store, "Add a unit test for X")

    shown = await _handle_read(store, {})
    assert "Add a unit test for X" in shown["text"]

    await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["Add a unit test for X"]
    assert store.get_unread() == []


@pytest.mark.asyncio
async def test_messaging_tool_read_does_not_preempt_instruction_enqueue(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """#4020: read_messages_from_thomas is display-only too."""
    agent = make_agent()
    store = MessageStore(Path("/fake/agent"))
    agent.message_store = store
    _save_thomas_instruction(store, "Add a unit test for X")

    shown = await _read_via_messaging_tool(store).handler({})
    assert "Add a unit test for X" in shown["text"]

    await agent._process_messages()

    model = agent.consciousness.load_self_model()
    assert model.pending_user_requests == ["Add a unit test for X"]
    assert store.get_unread() == []


@pytest.mark.asyncio
async def test_tool_read_does_not_strand_blocked_plan_reply(make_agent, mem_fs):  # type: ignore[no-untyped-def]
    """#4020: a threaded reply read through the tool still re-admits the
    blocked plan it answers."""
    from maestro.memory import EvolutionPlan
    from maestro.telegram_tools import _handle_read

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    store = MessageStore(agent.config.memory_dir)
    agent.message_store = store
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="blk1",
        title="Blocked One",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="blocked",
        created_at="2025-01-01T00:00:00Z",
        failure_count=2,
        blocked_question_id="q-abc",
    ))
    _save_thomas_instruction(store, "the environment is fixed, retry", reply_to="q-abc")

    await _handle_read(store, {})
    await agent._process_messages()

    stored = agent.memory.get_plan("blk1")
    assert stored is not None
    assert stored.status == "approved"
    assert store.get_unread() == []


@pytest.mark.asyncio
async def test_mis_threaded_reply_logs_info_and_is_not_enqueued(  # type: ignore[no-untyped-def]
    make_agent, mem_fs, caplog,
):
    """#4154: an answer whose ``reply_to`` matches no blocked question is
    logged visibly; being an ``answer`` it is not enqueued as a task."""
    import logging

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    store = MessageStore(agent.config.memory_dir)
    agent.message_store = store
    store.save_message(Message(
        message_id="a1",
        timestamp="2025-01-01T00:00:00Z",
        sender="thomas",
        content="late answer",
        message_type="answer",
        reply_to="q-gone",
    ))

    with caplog.at_level(logging.INFO, logger="maestro"):
        await agent._process_messages()

    assert "unmatched question q-gone (answer)" in caplog.text
    assert agent.consciousness.load_self_model().pending_user_requests == []
    assert store.get_unread() == []


@pytest.mark.asyncio
async def test_run_loop_telegram_reply_readmits_blocked_plan(make_agent) -> None:  # type: ignore[no-untyped-def]
    """#4154: a Telegram reply to a question threads ``reply_to`` through
    ``_on_message`` so ``_process_messages`` re-admits the blocked plan
    instead of enqueuing the answer as a fresh task."""
    from maestro.memory import EvolutionPlan
    from maestro.messaging import MessageStore
    from maestro.notifications.telegram import TelegramNotifier

    agent = make_agent()
    agent.config.attempt_disposition_enabled = True
    agent.message_store = MessageStore(agent.config.memory_dir)

    captured: dict[str, object] = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured["on_message"] = kwargs.get("on_message")

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock(side_effect=fake_start_polling)
    mock_notifier.stop_approval_polling = AsyncMock()
    mock_notifier.resolve_plan = AsyncMock()
    mock_notifier.resolve_brief = AsyncMock()
    mock_notifier.__class__ = TelegramNotifier
    mock_notifier.poll_failure = None
    agent.notifier = mock_notifier

    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._write_pid_file = MagicMock()  # type: ignore[method-assign]
    agent.contributions.check_for_contributions = MagicMock()  # type: ignore[method-assign]
    agent.contemplate = AsyncMock(return_value="reflection")  # type: ignore[method-assign]
    agent._implement_approved_plans = AsyncMock()  # type: ignore[method-assign]
    agent.evolve = AsyncMock()  # type: ignore[method-assign]
    agent.shutdown = AsyncMock()  # type: ignore[method-assign]

    await agent.run_loop(interval=0, max_iters=1, task_every=0, evolve_every=999)

    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id="blk1",
        title="Blocked One",
        category="capability",
        rationale="test",
        proposed_changes=["change"],
        files_affected=["file.py"],
        expected_impact="better",
        status="blocked",
        created_at="2025-01-01T00:00:00Z",
        failure_count=2,
        blocked_question_id="q-abc",
    ))
    on_message = captured.get("on_message")
    assert on_message is not None
    await on_message("runner fixed, retry it", "q-abc")  # type: ignore[operator]

    msgs = agent.message_store.get_recent(10)
    assert len(msgs) == 1
    assert msgs[0].reply_to == "q-abc"
    assert msgs[0].message_type == "answer"

    await agent._process_messages()

    stored = agent.memory.get_plan("blk1")
    assert stored is not None
    assert stored.status == "approved"
    assert stored.blocked_answer == "runner fixed, retry it"
    assert agent.consciousness.load_self_model().pending_user_requests == []


async def _capture_on_message(agent, **run_kwargs):  # type: ignore[no-untyped-def]
    """Run one ``run_loop`` iteration and return the Telegram ``on_message``."""
    from maestro.notifications.telegram import TelegramNotifier

    captured: dict[str, object] = {}

    async def fake_start_polling(on_approve, on_reject, **kwargs):  # type: ignore[no-untyped-def]
        captured["on_message"] = kwargs.get("on_message")

    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_notifier.notify = AsyncMock()
    mock_notifier.start_approval_polling = AsyncMock(side_effect=fake_start_polling)
    mock_notifier.stop_approval_polling = AsyncMock()
    mock_notifier.resolve_plan = AsyncMock()
    mock_notifier.__class__ = TelegramNotifier
    mock_notifier.poll_failure = None
    agent.notifier = mock_notifier

    agent._install_signal_handlers = MagicMock()
    agent._write_pid_file = MagicMock()
    agent.contributions.check_for_contributions = MagicMock()
    agent.contemplate = AsyncMock(return_value="reflection")
    agent._implement_approved_plans = AsyncMock()
    agent.evolve = AsyncMock()
    agent.shutdown = AsyncMock()

    await agent.run_loop(
        interval=0, max_iters=1, task_every=0, evolve_every=999, **run_kwargs,
    )
    return captured["on_message"]


def _pending_plan():  # type: ignore[no-untyped-def]
    from maestro.memory import EvolutionPlan

    return EvolutionPlan(
        plan_id="pf1", title="Wire a sensor",
        category="capability", rationale="r", proposed_changes=["c"],
        files_affected=["maestro/agent.py"], expected_impact="none",
        status="pending_review", plan_content_hash="a" * 64,
    )


@pytest.mark.asyncio
async def test_run_loop_telegram_plan_reply_folds_into_ledger(make_agent) -> None:  # type: ignore[no-untyped-def]
    """Proposal 145: with ``plan_feedback_enabled`` a reply to a review body
    lands once on the plan's ledger, is never enqueued, is acknowledged, and
    arms the off-cadence curate trigger."""
    from maestro.plan_feedback import PlanRef

    agent = make_agent()
    agent.config.loop_plan_feedback_enabled = True
    agent.message_store = MessageStore(agent.config.memory_dir)
    on_message = await _capture_on_message(agent, curate_enabled=True)
    agent.memory.save_evolution_plan(_pending_plan())
    agent._lifecycle._feedback_pending = False
    agent.notifier.notify.reset_mock()

    await on_message("drop step 2", None, plan_ref=PlanRef("pf1", "a" * 16))
    msgs = agent.message_store.get_recent(10)
    assert msgs[0].message_type == "plan_feedback"
    await agent._process_messages()

    plan = agent.memory.get_plan("pf1")
    assert plan is not None
    assert [f.text for f in plan.reviewer_feedback] == ["drop step 2"]
    assert agent.consciousness.load_self_model().pending_user_requests == []
    assert agent._lifecycle._feedback_pending is None
    notices = [c.args[0] for c in agent.notifier.notify.await_args_list]
    assert notices == [
        "Feedback recorded — Maestro will revise plan pf1 (Wire a sensor).",
    ]
    assert agent.message_store.get_unread() == []


@pytest.mark.asyncio
async def test_run_loop_telegram_plan_reply_flag_off_enqueues(make_agent) -> None:  # type: ignore[no-untyped-def]
    """With the flag off the same reply is today's ``instruction`` task."""
    from maestro.plan_feedback import PlanRef

    agent = make_agent()
    agent.message_store = MessageStore(agent.config.memory_dir)
    on_message = await _capture_on_message(agent)
    agent.memory.save_evolution_plan(_pending_plan())

    await on_message("drop step 2", None, plan_ref=PlanRef("pf1", "a" * 16))
    await agent._process_messages()

    plan = agent.memory.get_plan("pf1")
    assert plan is not None
    assert plan.reviewer_feedback == []
    assert agent.consciousness.load_self_model().pending_user_requests == [
        "drop step 2",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("curate_enabled", "focus"), [
    (False, "self_evolution"),
    (True, "external_delivery"),
])
async def test_run_loop_telegram_plan_reply_unserviced_enqueues(  # type: ignore[no-untyped-def]
    make_agent, curate_enabled: bool, focus: str,
) -> None:
    """Flag on but no curate pass will run (curation off, or the
    ``external_delivery`` focus suppresses it): the reply stays an
    instruction rather than vanishing into an unread ledger."""
    from maestro.plan_feedback import PlanRef

    agent = make_agent()
    agent.config.loop_plan_feedback_enabled = True
    agent.config.work_focus_profile = focus
    agent.message_store = MessageStore(agent.config.memory_dir)
    on_message = await _capture_on_message(agent, curate_enabled=curate_enabled)
    agent.memory.save_evolution_plan(_pending_plan())

    await on_message("drop step 2", None, plan_ref=PlanRef("pf1", "a" * 16))
    await agent._process_messages()

    plan = agent.memory.get_plan("pf1")
    assert plan is not None
    assert plan.reviewer_feedback == []
    assert agent.consciousness.load_self_model().pending_user_requests == [
        "drop step 2",
    ]


# --- stuck-gap decision replies (proposal 155 step 5) ----------------------------


def _gap_question(store: MessageStore, number: int = 7) -> None:
    store.save_message(Message(
        message_id="gq1", timestamp="2026-10-14T00:00:00+00:00", sender="maestro",
        content=f"[gap-decision #{number}] Gap decision: gap issue {number}",
        message_type="question",
    ))


def _gap_reply(store: MessageStore, mid: str, content: str) -> None:
    store.save_message(Message(
        message_id=mid, timestamp="2026-10-14T01:00:00+00:00", sender="thomas",
        content=content, message_type="instruction", reply_to="gq1",
    ))


@pytest.mark.asyncio
async def test_gap_decision_reply_removes_label_and_is_not_enqueued(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
):
    agent = make_agent()
    store = MessageStore(agent.config.memory_dir)
    agent.message_store = store
    agent.issues = MagicMock()
    agent.issues.remove_label.return_value = True
    _gap_question(store)
    _gap_reply(store, "g1", "build")

    await agent._process_messages()

    agent.issues.remove_label.assert_called_once_with(7, "needs-decision")
    assert agent.consciousness.load_self_model().pending_user_requests == []
    assert store.get_unread() == []


@pytest.mark.asyncio
async def test_second_gap_decision_reply_is_ignored(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    store = MessageStore(agent.config.memory_dir)
    agent.message_store = store
    agent.issues = MagicMock()
    _gap_question(store)
    _gap_reply(store, "g1", "buy")
    _gap_reply(store, "g2", "build")

    await agent._process_messages()

    agent.issues.remove_label.assert_not_called()
    assert agent.consciousness.load_self_model().pending_user_requests == []


@pytest.mark.asyncio
async def test_ordinary_question_reply_still_enqueued(make_agent, mem_fs) -> None:  # type: ignore[no-untyped-def]
    agent = make_agent()
    store = MessageStore(agent.config.memory_dir)
    agent.message_store = store
    agent.issues = MagicMock()
    store.save_message(Message(
        message_id="gq1", timestamp="2026-10-14T00:00:00+00:00", sender="maestro",
        content="Which stream next?", message_type="question",
    ))
    _gap_reply(store, "g1", "build the scraper")

    await agent._process_messages()

    agent.issues.remove_label.assert_not_called()
    assert agent.consciousness.load_self_model().pending_user_requests == [
        "build the scraper",
    ]
