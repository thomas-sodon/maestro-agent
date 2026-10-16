"""Tests for TelegramNotifier — all telegram.Bot calls are mocked."""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, TimedOut

from maestro.memory import EvolutionPlan, Memory, compute_plan_content_hash
from maestro.notifications.base import CALLBACK_HASH_PREFIX_LEN, NotifierTransportError
from maestro.notifications.telegram import PlanDecisionHandler
from maestro.plan_decision import telegram_plan_handlers
from maestro.plan_feedback import PlanRef, parse_plan_ref
from maestro.plan_review_message import compose_review_message


@pytest.fixture
def mock_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


def _make_notifier(
    bot: MagicMock, max_message_length: int = 4096
) -> object:
    with patch("maestro.notifications.telegram.Bot", return_value=bot):
        from maestro.notifications.telegram import TelegramNotifier

        return TelegramNotifier(
            bot_token="fake-token",
            chat_id="123",
            max_message_length=max_message_length,
        )


def test_construction(mock_bot: MagicMock) -> None:
    notifier = _make_notifier(mock_bot)
    assert notifier._chat_id == "123"


@pytest.mark.asyncio
async def test_notify_sends_message(mock_bot: MagicMock) -> None:
    notifier = _make_notifier(mock_bot)
    await notifier.notify("Hello Thomas")

    mock_bot.send_message.assert_called_once_with(
        chat_id="123",
        text="Hello Thomas",
        parse_mode=None,
    )


@pytest.mark.asyncio
async def test_notify_forwards_parse_mode_html(mock_bot: MagicMock) -> None:
    """parse_mode='HTML' is forwarded to Bot.send_message unchanged."""
    notifier = _make_notifier(mock_bot)
    await notifier.notify("<b>Hello</b>", parse_mode="HTML")

    mock_bot.send_message.assert_called_once_with(
        chat_id="123",
        text="<b>Hello</b>",
        parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_notify_truncates_long_message(mock_bot: MagicMock) -> None:
    notifier = _make_notifier(mock_bot)
    long_msg = "x" * 5000
    await notifier.notify(long_msg)

    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert len(sent_text) <= 4096
    assert sent_text.endswith("(truncated)")


@pytest.mark.asyncio
async def test_notify_truncation_respects_very_small_limit(
    mock_bot: MagicMock,
) -> None:
    notifier = _make_notifier(mock_bot, max_message_length=10)
    await notifier.notify("x" * 50)

    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert len(sent_text) <= 10


@pytest.mark.asyncio
async def test_notify_truncation_no_partial_suffix(
    mock_bot: MagicMock,
) -> None:
    """When max_length is too small for the full suffix, the truncated message
    must NOT contain a misleading partial suffix like '(trunca'.

    The old code's second truncation ``message[:max_length]`` sliced through
    the '(truncated)' suffix, producing broken text.  The message should
    either include the full suffix or omit it entirely.
    """
    # max_length=10 is smaller than the suffix "\n\n(truncated)" (14 chars),
    # so the suffix cannot fit.
    notifier = _make_notifier(mock_bot, max_message_length=10)
    await notifier.notify("x" * 50)

    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert len(sent_text) <= 10
    # Must NOT contain a broken partial suffix
    assert "(trunca" not in sent_text, f"Message contains partial suffix: {sent_text!r}"


@pytest.mark.asyncio
async def test_notify_raises_on_failure(mock_bot: MagicMock) -> None:
    mock_bot.send_message.side_effect = RuntimeError("Network error")
    notifier = _make_notifier(mock_bot)

    with pytest.raises(RuntimeError, match="Network error"):
        await notifier.notify("test")


@pytest.mark.asyncio
async def test_notify_translates_transport_timeout(mock_bot: MagicMock) -> None:
    """A Telegram transport timeout is translated into ``NotifierTransportError``.

    Mirrors ``_poll_loop``'s tolerance set so a best-effort caller can
    absorb a brief unreachability without the loop crashing (issue #3476).
    """
    mock_bot.send_message.side_effect = TimedOut("Timed out")
    notifier = _make_notifier(mock_bot)

    with pytest.raises(NotifierTransportError):
        await notifier.notify("test")


@pytest.mark.asyncio
async def test_notify_translates_retry_after(mock_bot: MagicMock) -> None:
    """A 429 ``RetryAfter`` is a transport transient, not a crash (issue #4155)."""
    from telegram.error import RetryAfter

    mock_bot.send_message.side_effect = RetryAfter(5)
    notifier = _make_notifier(mock_bot)

    with pytest.raises(NotifierTransportError):
        await notifier.notify("test")


def test_transient_set_includes_retry_after_not_bad_request() -> None:
    """The tolerance set cannot drift back to the ``NetworkError`` family only."""
    from telegram.error import BadRequest, RetryAfter

    from maestro.notifications.telegram import _TRANSIENT_TELEGRAM_ERRORS

    assert RetryAfter in _TRANSIENT_TELEGRAM_ERRORS
    assert BadRequest not in _TRANSIENT_TELEGRAM_ERRORS


def test_retry_after_seconds_normalises_int_float_and_timedelta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RetryAfter.retry_after`` may be ``int``, ``float`` or ``timedelta``."""
    import datetime

    from telegram.error import RetryAfter

    from maestro.notifications.telegram import _retry_after_seconds

    assert _retry_after_seconds(RetryAfter(7)) == 7.0
    # A fractional interval surfaces as ``float`` without ``PTB_TIMEDELTA``.
    assert _retry_after_seconds(RetryAfter(datetime.timedelta(seconds=2.5))) == 2.5
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    assert _retry_after_seconds(RetryAfter(datetime.timedelta(seconds=3))) == 3.0


@pytest.mark.asyncio
async def test_notify_propagates_programming_error(mock_bot: MagicMock) -> None:
    """A programming error is not translated — it propagates unchanged for RCA."""
    mock_bot.send_message.side_effect = TypeError("bug")
    notifier = _make_notifier(mock_bot)

    with pytest.raises(TypeError, match="bug"):
        await notifier.notify("test")


@pytest.mark.asyncio
async def test_notify_propagates_bad_request(mock_bot: MagicMock) -> None:
    """A ``BadRequest`` is NOT translated into ``NotifierTransportError``.

    ``BadRequest`` subclasses ``NetworkError`` in python-telegram-bot, so a
    naive transport-arm catch would absorb it as a transient. It actually
    flags a content/data defect in the message (malformed ``parse_mode``
    entities, chat not found, message too long) — a real bug that must
    surface for RCA, not be swallowed by the best-effort status ping
    (issue #3476). Pins the ``except BadRequest`` arm ordering.
    """
    mock_bot.send_message.side_effect = BadRequest("Can't parse entities")
    notifier = _make_notifier(mock_bot)

    with pytest.raises(BadRequest, match="parse entities"):
        await notifier.notify("<b>broken", parse_mode="HTML")


@pytest.mark.asyncio
async def test_notify_empty_message_does_not_raise(mock_bot: MagicMock) -> None:
    """notify() with an empty string should return silently, not raise ValueError."""
    notifier = _make_notifier(mock_bot)
    # Should not raise
    await notifier.notify("")


@pytest.mark.asyncio
async def test_notify_whitespace_message_does_not_raise(mock_bot: MagicMock) -> None:
    """notify() with a whitespace-only string should return silently, not raise ValueError."""
    notifier = _make_notifier(mock_bot)
    # Should not raise
    await notifier.notify("   ")


@pytest.mark.asyncio
async def test_notify_empty_message_does_not_call_send(mock_bot: MagicMock) -> None:
    """notify() with an empty message should not attempt to call Bot.send_message."""
    notifier = _make_notifier(mock_bot)
    await notifier.notify("")
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_notify_whitespace_message_does_not_call_send(mock_bot: MagicMock) -> None:
    """notify() with a whitespace-only message should not attempt to call Bot.send_message."""
    notifier = _make_notifier(mock_bot)
    await notifier.notify("   ")
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_notify_plan_for_review_sends_inline_keyboard(
    mock_bot: MagicMock,
) -> None:
    """notify_plan_for_review should send a message with approve/reject buttons."""
    mock_result = MagicMock()
    mock_result.message_id = 42
    mock_bot.send_message.return_value = mock_result

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_for_review("plan123", "Review this plan", "f00dfeed" * 8)

    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert call_kwargs["text"] == "Review this plan"
    assert call_kwargs["chat_id"] == "123"

    reply_markup = call_kwargs["reply_markup"]
    # InlineKeyboardMarkup has an inline_keyboard attribute (list of rows)
    buttons = reply_markup.inline_keyboard
    assert len(buttons) == 1  # one row
    assert len(buttons[0]) == 2  # two buttons
    assert buttons[0][0].text == "Approve"
    assert buttons[0][0].callback_data == "approve:plan123:f00dfeedf00dfeed"
    assert buttons[0][1].text == "Reject"
    assert buttons[0][1].callback_data == "reject:plan123:f00dfeedf00dfeed"


@pytest.mark.asyncio
async def test_notify_plan_for_review_stores_message_id(
    mock_bot: MagicMock,
) -> None:
    """After sending, the plan_id→message_id mapping should be stored."""
    mock_result = MagicMock()
    mock_result.message_id = 99
    mock_bot.send_message.return_value = mock_result

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_for_review("pid_abc", "Test plan", "f00dfeed" * 8)

    assert notifier._plan_messages["pid_abc"] == 99


@pytest.mark.asyncio
async def test_notify_plan_for_review_chunks_long_message(
    mock_bot: MagicMock,
) -> None:
    """A long single-line body is hard-split, never cut under a keyboard (#4589)."""
    mock_result = MagicMock()
    mock_result.message_id = 1
    mock_bot.send_message.return_value = mock_result

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_for_review("p1", "x" * 5000, "f00dfeed" * 8)

    calls = mock_bot.send_message.call_args_list
    assert len(calls) == 2
    for call in calls:
        assert len(call.kwargs["text"]) <= 4096
        assert not call.kwargs["text"].endswith("(truncated)")
    assert "reply_markup" not in calls[0].kwargs
    assert calls[-1].kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_notify_plan_for_review_empty_message_does_not_raise(
    mock_bot: MagicMock,
) -> None:
    """notify_plan_for_review with empty message should return silently."""
    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_for_review("p1", "", "f00dfeed" * 8)
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_notify_plan_for_review_fallback_on_null_notifier() -> None:
    """NullNotifier.notify_plan_for_review should delegate to notify (no crash)."""
    from maestro.notifications.base import NullNotifier

    notifier = NullNotifier()
    await notifier.notify_plan_for_review("p1", "test message", "f00dfeed" * 8)


@pytest.mark.asyncio
async def test_notify_plan_refined_sends_inline_keyboard(
    mock_bot: MagicMock,
) -> None:
    """notify_plan_refined sends a tagged message with approve/reject buttons."""
    mock_result = MagicMock()
    mock_result.message_id = 7
    mock_bot.send_message.return_value = mock_result

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_refined("planXYZ", "I refined this plan", "f00dfeed" * 8)

    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert call_kwargs["text"] == "I refined this plan"
    assert call_kwargs["chat_id"] == "123"

    buttons = call_kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == 1
    assert len(buttons[0]) == 2
    assert buttons[0][0].callback_data == "approve:planXYZ:f00dfeedf00dfeed"
    assert buttons[0][1].callback_data == "reject:planXYZ:f00dfeedf00dfeed"


@pytest.mark.asyncio
async def test_notify_plan_refined_stores_message_id(
    mock_bot: MagicMock,
) -> None:
    """The refined message id is tracked so resolve_plan can clean it up."""
    mock_result = MagicMock()
    mock_result.message_id = 55
    mock_bot.send_message.return_value = mock_result

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_refined("pid_ref", "Refined", "f00dfeed" * 8)

    assert notifier._plan_messages["pid_ref"] == 55


@pytest.mark.asyncio
async def test_notify_plan_refined_chunks_long_message(
    mock_bot: MagicMock,
) -> None:
    """A long single-line body is hard-split, never cut under a keyboard (#4589)."""
    mock_result = MagicMock()
    mock_result.message_id = 1
    mock_bot.send_message.return_value = mock_result

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_refined("p1", "x" * 5000, "f00dfeed" * 8)

    calls = mock_bot.send_message.call_args_list
    assert len(calls) == 2
    for call in calls:
        assert len(call.kwargs["text"]) <= 4096
        assert not call.kwargs["text"].endswith("(truncated)")
    assert "reply_markup" not in calls[0].kwargs
    assert calls[-1].kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_notify_plan_refined_empty_message_does_not_send(
    mock_bot: MagicMock,
) -> None:
    """notify_plan_refined with an empty message returns silently."""
    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_refined("p1", "", "f00dfeed" * 8)
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_notify_plan_refined_raises_on_send_failure(
    mock_bot: MagicMock,
) -> None:
    """A Bot.send_message failure propagates (fail-loud)."""
    mock_bot.send_message.side_effect = RuntimeError("Network error")
    notifier = _make_notifier(mock_bot)
    with pytest.raises(RuntimeError):
        await notifier.notify_plan_refined("p1", "Refined", "f00dfeed" * 8)


@pytest.mark.asyncio
async def test_notify_plan_refined_fallback_on_null_notifier() -> None:
    """NullNotifier.notify_plan_refined delegates to notify (no crash)."""
    from maestro.notifications.base import NullNotifier

    notifier = NullNotifier()
    await notifier.notify_plan_refined("p1", "test message", "f00dfeed" * 8)


@pytest.mark.asyncio
async def test_notify_plan_refined_retires_prior_review_message(
    mock_bot: MagicMock,
) -> None:
    """A refinement for an already-tracked plan sends a *new* message (so the
    operator gets a push alert) and retires the original review message's
    buttons so they don't orphan when the plan later resolves."""
    mock_bot.edit_message_text = AsyncMock()
    review = MagicMock()
    review.message_id = 100
    refined = MagicMock()
    refined.message_id = 200
    mock_bot.send_message = AsyncMock(side_effect=[review, refined])

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_for_review("p1", "original plan", "f00dfeed" * 8)
    await notifier.notify_plan_refined("p1", "refined plan", "f00dfeed" * 8)

    # A fresh message was sent for the refinement (push alert preserved).
    assert mock_bot.send_message.call_count == 2
    # The original review message's controls were retired...
    mock_bot.edit_message_text.assert_called_once()
    assert mock_bot.edit_message_text.call_args.kwargs["message_id"] == 100
    # ...and only the refined message remains tracked for resolve_plan.
    assert notifier._plan_messages["p1"] == 200


@pytest.mark.asyncio
async def test_notify_plan_for_review_first_send_does_not_retire(
    mock_bot: MagicMock,
) -> None:
    """The first plan message has no predecessor, so nothing is retired."""
    mock_bot.edit_message_text = AsyncMock()
    result = MagicMock()
    result.message_id = 7
    mock_bot.send_message.return_value = result

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_for_review("p1", "first plan", "f00dfeed" * 8)

    mock_bot.edit_message_text.assert_not_called()
    assert notifier._plan_messages["p1"] == 7


@pytest.mark.asyncio
async def test_notify_plan_for_review_retires_prior_message_on_resend(
    mock_bot: MagicMock,
) -> None:
    """A second notify_plan_for_review for the same plan_id retires the first
    message so its Approve/Reject buttons do not outlive the replacement.

    The shared _send_plan_with_controls helper retires the superseded message
    regardless of caller; this pins that contract for notify_plan_for_review so
    a future guard restricting retirement to notify_plan_refined — or an
    accidental drop of the retire block — is caught for this caller too."""
    mock_bot.edit_message_text = AsyncMock()
    first = MagicMock()
    first.message_id = 10
    second = MagicMock()
    second.message_id = 20
    mock_bot.send_message = AsyncMock(side_effect=[first, second])

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_for_review("p1", "first version", "f00dfeed" * 8)
    await notifier.notify_plan_for_review("p1", "second version", "f00dfeed" * 8)

    # A fresh message was sent for the re-send (push alert preserved)...
    assert mock_bot.send_message.call_count == 2
    # ...the first message's controls were retired...
    mock_bot.edit_message_text.assert_called_once()
    assert mock_bot.edit_message_text.call_args.kwargs["message_id"] == 10
    # ...and only the replacement remains tracked for resolve_plan.
    assert notifier._plan_messages["p1"] == 20


@pytest.mark.asyncio
async def test_notify_plan_refined_retire_swallows_edit_failure(
    mock_bot: MagicMock,
) -> None:
    """If retiring the superseded message fails, the refinement still succeeds
    and tracks the new message id (best-effort, mirroring resolve_plan)."""
    mock_bot.edit_message_text = AsyncMock(side_effect=RuntimeError("edit boom"))
    review = MagicMock()
    review.message_id = 100
    refined = MagicMock()
    refined.message_id = 200
    mock_bot.send_message = AsyncMock(side_effect=[review, refined])

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_for_review("p1", "original plan", "f00dfeed" * 8)
    # Must not raise even though the retire edit fails.
    await notifier.notify_plan_refined("p1", "refined plan", "f00dfeed" * 8)

    assert notifier._plan_messages["p1"] == 200


def _make_callback_query(action: str, plan_id: str) -> MagicMock:
    cq = MagicMock()
    cq.id = "cq_test_123"
    cq.data = f"{action}:{plan_id}"
    cq.message = MagicMock()
    cq.message.text = "Original plan text"
    cq.message.message_id = 42
    return cq


@pytest.mark.asyncio
async def test_error_feedback_reaches_user_when_action_fails() -> None:
    """When an action raises, the user should see error feedback via edit_message_text
    rather than a second answer_callback_query (which Telegram rejects)."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()

    answer_call_count = 0

    async def fake_answer_callback_query(query_id: str, text: str | None = None, show_alert: bool = False) -> None:
        nonlocal answer_call_count
        answer_call_count += 1
        if answer_call_count > 1:
            raise Exception(
                "Bad Request: query is too old and response timeout expired or query ID is invalid"
            )

    bot.answer_callback_query = AsyncMock(side_effect=fake_answer_callback_query)

    notifier = _make_notifier(bot)

    on_approve = AsyncMock(side_effect=RuntimeError("Approval process failed"))
    on_reject = AsyncMock(return_value="rejected")

    callback_query = _make_callback_query("approve", "plan_xyz")

    await notifier._handle_callback(callback_query, on_approve, on_reject)

    assert answer_call_count >= 1

    # The user should see visible error feedback via edit_message_text
    # (since answer_callback_query can only be called once per query)
    bot.edit_message_text.assert_called_once()

    edit_kwargs = bot.edit_message_text.call_args.kwargs
    assert "fail" in edit_kwargs.get("text", "").lower()


# ---------------------------------------------------------------------------
# resolve_plan — dashboard-triggered cleanup
# ---------------------------------------------------------------------------

def _make_notifier_with_mock_bot() -> tuple[object, MagicMock]:
    """Create a TelegramNotifier with all bot calls mocked."""
    mock_bot = MagicMock()
    send_result = MagicMock()
    send_result.message_id = 99
    mock_bot.send_message = AsyncMock(return_value=send_result)
    mock_bot.edit_message_text = AsyncMock(return_value=MagicMock())
    with patch("maestro.notifications.telegram.Bot", return_value=mock_bot):
        from maestro.notifications.telegram import TelegramNotifier
        notifier = TelegramNotifier(
            bot_token="fake-token",
            chat_id="12345",
        )
    return notifier, mock_bot


@pytest.mark.asyncio
async def test_plan_messages_cleared_after_dashboard_approve() -> None:
    """After resolve_plan, _plan_messages must be empty and edit_message_text called."""
    notifier, mock_bot = _make_notifier_with_mock_bot()
    plan_id = "plan-dashboard-001"

    # Simulate plan being sent for review (stores plan_id in _plan_messages)
    await notifier.notify_plan_for_review(plan_id, "Please review this plan.", "f00dfeed" * 8)
    assert plan_id in notifier._plan_messages

    # Simulate dashboard approval resolving the plan
    await notifier.resolve_plan(plan_id, "Approved via Dashboard")

    # _plan_messages must be cleaned up so buttons are no longer stale
    assert plan_id not in notifier._plan_messages, \
        "After resolve_plan, _plan_messages should not contain the plan_id. " \
        "Stale entry means buttons remain active indefinitely."
    # edit_message_text must have been called to remove the inline buttons
    mock_bot.edit_message_text.assert_called_once()


@pytest.mark.asyncio
async def test_plan_messages_cleared_after_dashboard_reject() -> None:
    """resolve_plan with 'rejected' label clears state and edits message."""
    notifier, mock_bot = _make_notifier_with_mock_bot()
    plan_id = "plan-dashboard-002"

    await notifier.notify_plan_for_review(plan_id, "Please review this plan.", "f00dfeed" * 8)
    assert plan_id in notifier._plan_messages

    await notifier.resolve_plan(plan_id, "Rejected via Dashboard")

    assert plan_id not in notifier._plan_messages
    mock_bot.edit_message_text.assert_called_once()
    edit_kwargs = mock_bot.edit_message_text.call_args.kwargs
    assert "Rejected via Dashboard" in edit_kwargs.get("text", "")


@pytest.mark.asyncio
async def test_resolve_plan_noop_when_plan_not_tracked() -> None:
    """resolve_plan is a no-op (no error) if plan_id was never stored."""
    notifier, mock_bot = _make_notifier_with_mock_bot()

    # Should not raise even if plan was never registered
    await notifier.resolve_plan("unknown-plan-id", "Approved via Dashboard")

    mock_bot.edit_message_text.assert_not_called()


def test_update_message_after_action_has_no_status_parameter() -> None:
    """_update_message_after_action must not accept a 'status' parameter.

    The parameter was dead code (never read inside the method body),
    so it was removed to reduce noise in the signature and at call sites.
    Proposal 032 renamed the first parameter from ``plan_id`` to
    ``entity_id`` because the method now handles both evolution plans
    and publication proposals — the parameter must be present under the
    new name.
    """
    from maestro.notifications.telegram import TelegramNotifier

    sig = inspect.signature(TelegramNotifier._update_message_after_action)
    assert "status" not in sig.parameters, \
        "Dead 'status' parameter should have been removed from " \
        "_update_message_after_action signature."
    # Verify the remaining expected parameters are still present
    param_names = list(sig.parameters.keys())
    assert "entity_id" in param_names
    assert "message" in param_names
    assert "action" in param_names


# ---------------------------------------------------------------------------
# base_url constructor kwarg
# ---------------------------------------------------------------------------

def test_construction_with_base_url(mock_bot: MagicMock) -> None:
    """Passing base_url should forward it to Bot constructor."""
    with patch("maestro.notifications.telegram.Bot", return_value=mock_bot) as MockBot:
        from maestro.notifications.telegram import TelegramNotifier

        TelegramNotifier(
            bot_token="fake-token",
            chat_id="123",
            base_url="https://custom.bot.server/bot",
        )

    MockBot.assert_called_once_with(
        token="fake-token",
        base_url="https://custom.bot.server/bot",
    )


def test_construction_without_base_url_omits_kwarg(mock_bot: MagicMock) -> None:
    """When base_url is None, Bot must be called without base_url kwarg."""
    with patch("maestro.notifications.telegram.Bot", return_value=mock_bot) as MockBot:
        from maestro.notifications.telegram import TelegramNotifier

        TelegramNotifier(bot_token="fake-token", chat_id="123")

    call_kwargs = MockBot.call_args.kwargs
    assert "base_url" not in call_kwargs


# ---------------------------------------------------------------------------
# notify_plan_for_review exception path (lines 91-93)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_plan_for_review_raises_on_failure(mock_bot: MagicMock) -> None:
    """When send_message raises, notify_plan_for_review should propagate the exception."""
    mock_bot.send_message.side_effect = RuntimeError("Network error")
    notifier = _make_notifier(mock_bot)

    with pytest.raises(RuntimeError, match="Network error"):
        await notifier.notify_plan_for_review("plan1", "Review this", "f00dfeed" * 8)


# ---------------------------------------------------------------------------
# resolve_plan exception path (lines 111-112)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_plan_swallows_edit_exception(mock_bot: MagicMock) -> None:
    """resolve_plan should log a warning but not raise if edit_message_text fails."""
    mock_result = MagicMock()
    mock_result.message_id = 77
    mock_bot.send_message = AsyncMock(return_value=mock_result)
    mock_bot.edit_message_text = AsyncMock(side_effect=RuntimeError("Telegram error"))

    notifier = _make_notifier(mock_bot)
    await notifier.notify_plan_for_review("plan_err", "Review this plan", "f00dfeed" * 8)

    # Should not raise even though edit_message_text fails
    await notifier.resolve_plan("plan_err", "Approved via Dashboard")

    # plan_id should be removed from _plan_messages regardless of the edit failure
    assert "plan_err" not in notifier._plan_messages


# ---------------------------------------------------------------------------
# _handle_callback: empty plan_id (line 187)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_callback_empty_plan_id_is_ignored(mock_bot: MagicMock) -> None:
    """_handle_callback should return early when plan_id is empty or whitespace."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    cq = MagicMock()
    cq.id = "cq_empty"
    cq.data = "approve:   "  # whitespace-only plan_id
    cq.message = MagicMock()

    on_approve = AsyncMock()
    on_reject = AsyncMock()

    await notifier._handle_callback(cq, on_approve, on_reject)

    on_approve.assert_not_called()
    on_reject.assert_not_called()
    mock_bot.answer_callback_query.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_callback: answer_callback_query exception (lines 193-194)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_callback_answer_exception_is_swallowed(mock_bot: MagicMock) -> None:
    """If answer_callback_query raises, _handle_callback should continue processing."""
    mock_bot.answer_callback_query = AsyncMock(side_effect=RuntimeError("Query expired"))
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    cq = MagicMock()
    cq.id = "cq_expired"
    cq.data = "approve:plan_x"
    cq.message = MagicMock()
    cq.message.text = "Original text"
    cq.message.message_id = 10

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock()

    # Should not raise
    await notifier._handle_callback(cq, on_approve, on_reject)

    # Action should still be called despite answer failing
    on_approve.assert_called_once_with("plan_x", rendered_hash=None)


# ---------------------------------------------------------------------------
# _handle_callback error handler: edit_message_text exception (lines 217-218)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_callback_error_edit_exception_is_swallowed(mock_bot: MagicMock) -> None:
    """When the action fails AND edit_message_text in the error handler also fails,
    _handle_callback should swallow both exceptions without raising."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock(side_effect=RuntimeError("Edit failed too"))

    notifier = _make_notifier(mock_bot)

    cq = MagicMock()
    cq.id = "cq_double_fail"
    cq.data = "approve:plan_y"
    cq.message = MagicMock()
    cq.message.text = "Original text"
    cq.message.message_id = 20

    on_approve = AsyncMock(side_effect=ValueError("Action failed"))
    on_reject = AsyncMock()

    # Should not raise even though both action and error-edit fail
    await notifier._handle_callback(cq, on_approve, on_reject)

    on_approve.assert_called_once_with("plan_y", rendered_hash=None)
    mock_bot.edit_message_text.assert_called_once()


# ---------------------------------------------------------------------------
# _update_message_after_action: None message (line 228)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_message_after_action_none_message(mock_bot: MagicMock) -> None:
    """_update_message_after_action with message=None should return immediately."""
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    # Should not raise or call edit_message_text
    await notifier._update_message_after_action("plan_none", None, "approve")

    mock_bot.edit_message_text.assert_not_called()


# ---------------------------------------------------------------------------
# _update_message_after_action: edit exception (lines 239-240)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_message_after_action_edit_exception_is_swallowed(
    mock_bot: MagicMock,
) -> None:
    """If edit_message_text raises in _update_message_after_action, it should be swallowed."""
    mock_bot.edit_message_text = AsyncMock(side_effect=RuntimeError("Network error"))

    notifier = _make_notifier(mock_bot)
    notifier._plan_messages["plan_edit_fail"] = 55

    message = MagicMock()
    message.text = "Some plan text"
    message.message_id = 55

    # Should not raise
    await notifier._update_message_after_action("plan_edit_fail", message, "approve")

    mock_bot.edit_message_text.assert_called_once()
    # plan_messages should still be cleaned up
    assert "plan_edit_fail" not in notifier._plan_messages


# ---------------------------------------------------------------------------
# Constructor validation: empty bot_token / chat_id (lines 27, 29)
# ---------------------------------------------------------------------------

def test_construction_empty_bot_token_raises() -> None:
    """Empty bot_token should raise ValueError."""
    with patch("maestro.notifications.telegram.Bot"):
        from maestro.notifications.telegram import TelegramNotifier

        with pytest.raises(ValueError, match="bot_token must not be empty"):
            TelegramNotifier(bot_token="", chat_id="123")


def test_construction_whitespace_bot_token_raises() -> None:
    """Whitespace-only bot_token should raise ValueError."""
    with patch("maestro.notifications.telegram.Bot"):
        from maestro.notifications.telegram import TelegramNotifier

        with pytest.raises(ValueError, match="bot_token must not be empty"):
            TelegramNotifier(bot_token="   ", chat_id="123")


def test_construction_empty_chat_id_raises() -> None:
    """Empty chat_id should raise ValueError."""
    with patch("maestro.notifications.telegram.Bot"):
        from maestro.notifications.telegram import TelegramNotifier

        with pytest.raises(ValueError, match="chat_id must not be empty"):
            TelegramNotifier(bot_token="real-token", chat_id="")


def test_construction_whitespace_chat_id_raises() -> None:
    """Whitespace-only chat_id should raise ValueError."""
    with patch("maestro.notifications.telegram.Bot"):
        from maestro.notifications.telegram import TelegramNotifier

        with pytest.raises(ValueError, match="chat_id must not be empty"):
            TelegramNotifier(bot_token="real-token", chat_id="  ")


# ---------------------------------------------------------------------------
# start_approval_polling when task already running (line 125)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_approval_polling_stops_existing_task_first(
    mock_bot: MagicMock,
) -> None:
    """If start_approval_polling is called while a poll task is already running,
    it should stop the old task before starting a new one."""
    mock_bot.get_updates = AsyncMock(return_value=[])

    notifier = _make_notifier(mock_bot)

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    # Start polling for the first time
    await notifier.start_approval_polling(on_approve, on_reject)
    first_task = notifier._poll_task
    assert first_task is not None
    assert not first_task.done()

    # Start again — should stop the first task and create a new one
    await notifier.start_approval_polling(on_approve, on_reject)
    second_task = notifier._poll_task

    assert second_task is not None
    assert second_task is not first_task
    assert first_task.done()  # old task was cancelled

    # Cleanup
    await notifier.stop_approval_polling()


# ---------------------------------------------------------------------------
# _poll_loop: non-integer update_id warning (lines 156-160)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_skips_update_with_non_integer_update_id(
    _claim: AsyncMock,
    mock_bot: MagicMock,
) -> None:
    """Updates with a non-integer update_id should emit a warning and be skipped."""

    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    bad_update = MagicMock()
    bad_update.update_id = "not-an-int"  # non-integer
    bad_update.callback_query = MagicMock()

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [bad_update]
        notifier._polling = False
        return []

    mock_bot.get_updates = AsyncMock(side_effect=fake_get_updates)

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock):
        notifier._polling = True
        await notifier._poll_loop(on_approve, on_reject)

    # callback should NOT have been processed since update_id was non-integer
    on_approve.assert_not_called()
    on_reject.assert_not_called()


# ---------------------------------------------------------------------------
# _claim_polling_session: preserves pending updates (regression, #344)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_polling_session_preserves_pending_updates(
    mock_bot: MagicMock,
) -> None:
    """The claim must use the current poll offset, not a negative one.

    A negative offset confirms and permanently drops every pending update,
    discarding callbacks/messages queued while the bot was offline.  Claiming
    with the current offset (0 at startup) terminates the stale long-poll
    without confirming anything, so those updates survive for the main loop.
    """
    mock_bot.get_updates = AsyncMock(return_value=[])

    notifier = _make_notifier(mock_bot)
    # Default startup offset — nothing consumed yet.
    assert notifier._poll_offset == 0

    await notifier._claim_polling_session()

    mock_bot.get_updates.assert_awaited_once_with(offset=0, timeout=0)
    # Pin the intent: a non-negative offset never confirms pending updates.
    assert mock_bot.get_updates.await_args.kwargs["offset"] >= 0


# ---------------------------------------------------------------------------
# _handle_callback: ":" not in data early return (line 181)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_callback_no_colon_in_data_is_ignored(
    mock_bot: MagicMock,
) -> None:
    """_handle_callback should return early when callback data has no ':'."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    cq = MagicMock()
    cq.id = "cq_no_colon"
    cq.data = "approveplan123"  # no colon separator
    cq.message = MagicMock()

    on_approve = AsyncMock()
    on_reject = AsyncMock()

    await notifier._handle_callback(cq, on_approve, on_reject)

    on_approve.assert_not_called()
    on_reject.assert_not_called()
    mock_bot.answer_callback_query.assert_not_called()


# ---------------------------------------------------------------------------
# _poll_loop: None update_id handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_skips_update_with_none_update_id(
    _claim: AsyncMock,
    mock_bot: MagicMock,
) -> None:
    """Updates with a None update_id should be skipped without crashing.

    Previously, ``update.update_id + 1`` raised TypeError when update_id
    was None, and _poll_offset was never advanced, causing the same
    malformed update to be re-fetched indefinitely.
    """
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    bad_update = MagicMock()
    bad_update.update_id = None
    bad_update.callback_query = None

    good_update = MagicMock()
    good_update.update_id = 100
    good_update.callback_query = None

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [bad_update, good_update]
        notifier._polling = False
        return []

    mock_bot.get_updates = AsyncMock(side_effect=fake_get_updates)

    on_approve = AsyncMock()
    on_reject = AsyncMock()

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock):
        notifier._polling = True
        await notifier._poll_loop(on_approve, on_reject)

    # _poll_offset should have advanced past the good update
    assert notifier._poll_offset == 101
    # Should NOT have retried due to TypeError — only 2 calls
    assert call_count == 2


# ---------------------------------------------------------------------------
# _truncate: edge cases for small max_message_length values
# ---------------------------------------------------------------------------


def test_truncate_zero_content_still_has_original_text(mock_bot: MagicMock) -> None:
    """When max_message_length is just barely larger than the suffix,
    _truncate must include at least some original content, not just suffix."""
    notifier = _make_notifier(mock_bot, max_message_length=15)

    result = notifier._truncate("Hello, this is a long message")
    assert len(result) <= 15
    # Result should contain at least some original content, not just suffix
    assert not result.startswith("\n\n")


@pytest.mark.asyncio
async def test_truncate_when_max_length_equals_suffix_length(
    mock_bot: MagicMock,
) -> None:
    """When max_message_length equals the suffix length, truncation should
    not crash and the result must respect the limit."""
    suffix = "\n\n(truncated)"
    max_len = len(suffix)  # 14

    notifier = _make_notifier(mock_bot, max_message_length=max_len)
    long_msg = "A" * 100
    await notifier.notify(long_msg)

    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert len(sent_text) <= max_len


def test_zero_max_message_length_clamped(mock_bot: MagicMock) -> None:
    """max_message_length=0 should be clamped to a safe minimum."""
    notifier = _make_notifier(mock_bot, max_message_length=0)
    assert notifier._max_length >= 1


def test_negative_max_message_length_clamped(mock_bot: MagicMock) -> None:
    """Negative max_message_length should be clamped to a safe minimum."""
    notifier = _make_notifier(mock_bot, max_message_length=-10)
    assert notifier._max_length >= 1


# ---------------------------------------------------------------------------
# ask_thomas / message polling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_thomas_sends_formatted_message(mock_bot: MagicMock) -> None:
    """ask_thomas should send 'Maestro has a question:\\n\\n{question}' via notify."""
    notifier = _make_notifier(mock_bot)
    await notifier.ask_thomas("Which revenue stream pays first?", message_id="msg1")

    mock_bot.send_message.assert_called_once_with(
        chat_id="123",
        text="Maestro has a question [ref msg1]:\n\nWhich revenue stream pays first?",
        parse_mode=None,
    )


@pytest.mark.asyncio
async def test_start_message_polling_is_noop_on_telegram_notifier(
    mock_bot: MagicMock,
) -> None:
    """start_message_polling is a no-op on TelegramNotifier.

    The on_message callback is registered via start_approval_polling(on_message=…)
    instead; the separate start_message_polling path was removed to avoid
    overwriting the same _on_message slot a second time.
    """
    notifier = _make_notifier(mock_bot)
    assert notifier._on_message is None

    callback = AsyncMock()
    await notifier.start_message_polling(callback)

    # Base-class no-op: _on_message is NOT set by this method
    assert notifier._on_message is None


@pytest.mark.asyncio
async def test_stop_message_polling_is_noop_on_telegram_notifier(
    mock_bot: MagicMock,
) -> None:
    """stop_message_polling is a no-op on TelegramNotifier.

    The _on_message lifecycle is managed entirely by start/stop_approval_polling;
    stop_message_polling no longer clears the field.
    """
    notifier = _make_notifier(mock_bot)
    sentinel = AsyncMock()
    notifier._on_message = sentinel

    await notifier.stop_message_polling()

    # Base-class no-op: _on_message is NOT cleared by this method
    assert notifier._on_message is sentinel


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_handles_text_message(_claim: AsyncMock, mock_bot: MagicMock) -> None:
    """_poll_loop should call on_message when a text message update arrives."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    on_message = AsyncMock()
    notifier._on_message = on_message

    text_update = MagicMock()
    text_update.update_id = 200
    text_update.callback_query = None
    text_update.message = MagicMock()
    text_update.message.text = "Hello Maestro"
    text_update.message.chat.id = 123

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [text_update]
        notifier._polling = False
        return []

    mock_bot.get_updates = AsyncMock(side_effect=fake_get_updates)

    on_approve = AsyncMock()
    on_reject = AsyncMock()

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock):
        notifier._polling = True
        await notifier._poll_loop(on_approve, on_reject)

    on_message.assert_called_once_with("Hello Maestro", None, plan_ref=None)


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_ignores_text_when_no_callback(_claim: AsyncMock, mock_bot: MagicMock) -> None:
    """_poll_loop should not error when a text message arrives but _on_message is None."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)
    assert notifier._on_message is None

    text_update = MagicMock()
    text_update.update_id = 300
    text_update.callback_query = None
    text_update.message = MagicMock()
    text_update.message.text = "Ignored message"

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [text_update]
        notifier._polling = False
        return []

    mock_bot.get_updates = AsyncMock(side_effect=fake_get_updates)

    on_approve = AsyncMock()
    on_reject = AsyncMock()

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock):
        notifier._polling = True
        # Should not raise
        await notifier._poll_loop(on_approve, on_reject)

    # offset should still advance
    assert notifier._poll_offset == 301


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_text_message_error_propagates(
    _claim: AsyncMock, mock_bot: MagicMock,
) -> None:
    """If ``on_message`` raises, the polling loop propagates the failure.

    Fail-loud: a callback bug is a real defect in message handling —
    silently swallowing it hid agent-side regressions. The exception
    escapes the polling loop so the main loop sees it when it
    ``await``s the polling task.
    """
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    on_message = AsyncMock(side_effect=RuntimeError("handler error"))
    notifier._on_message = on_message

    text_update = MagicMock()
    text_update.update_id = 400
    text_update.callback_query = None
    text_update.message = MagicMock()
    text_update.message.text = "Boom"
    text_update.message.chat.id = 123

    mock_bot.get_updates = AsyncMock(return_value=[text_update])

    on_approve = AsyncMock()
    on_reject = AsyncMock()

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock):
        notifier._polling = True
        with pytest.raises(RuntimeError, match="handler error"):
            await notifier._poll_loop(on_approve, on_reject)

    on_message.assert_called_once_with("Boom", None, plan_ref=None)


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_propagates_cancelled_error(
    _claim: AsyncMock,
    mock_bot: MagicMock,
) -> None:
    """CancelledError from get_updates must propagate — not be swallowed by the
    generic except clause — so task cancellation can unwind the polling loop.
    """
    mock_bot.get_updates = AsyncMock(side_effect=asyncio.CancelledError)

    notifier = _make_notifier(mock_bot)
    on_approve = AsyncMock()
    on_reject = AsyncMock()

    notifier._polling = True
    with pytest.raises(asyncio.CancelledError):
        await notifier._poll_loop(on_approve, on_reject)


@pytest.mark.asyncio
async def test_start_approval_polling_with_on_message(mock_bot: MagicMock) -> None:
    """start_approval_polling should store the optional on_message callback."""
    mock_bot.get_updates = AsyncMock(return_value=[])

    notifier = _make_notifier(mock_bot)

    on_approve = AsyncMock()
    on_reject = AsyncMock()
    on_message = AsyncMock()

    await notifier.start_approval_polling(on_approve, on_reject, on_message=on_message)

    assert notifier._on_message is on_message

    # Cleanup
    await notifier.stop_approval_polling()


# ---------------------------------------------------------------------------
# _handle_callback: handler returns 'ignored' (cross-surface idempotency)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_callback_ignored_shows_already_resolved(
    mock_bot: MagicMock,
) -> None:
    """When handler returns 'ignored', the message should show 'Already resolved'
    instead of the misleading 'Approved via Telegram' label."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    cq = _make_callback_query("approve", "plan_race")

    on_approve = AsyncMock(return_value="ignored")
    on_reject = AsyncMock()

    await notifier._handle_callback(cq, on_approve, on_reject)

    on_approve.assert_called_once_with("plan_race", rendered_hash=None)
    mock_bot.edit_message_text.assert_called_once()
    edit_kwargs = mock_bot.edit_message_text.call_args.kwargs
    assert "Already resolved" in edit_kwargs["text"]
    # Must NOT say "Approved via Telegram"
    assert "via Telegram" not in edit_kwargs["text"]


@pytest.mark.asyncio
async def test_handle_callback_approved_calls_update_message(
    mock_bot: MagicMock,
) -> None:
    """When handler returns 'approved', _update_message_after_action should be
    called as before (regression guard)."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    cq = _make_callback_query("approve", "plan_ok")

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock()

    await notifier._handle_callback(cq, on_approve, on_reject)

    on_approve.assert_called_once_with("plan_ok", rendered_hash=None)
    mock_bot.edit_message_text.assert_called_once()
    edit_kwargs = mock_bot.edit_message_text.call_args.kwargs
    assert "Approved via Telegram" in edit_kwargs["text"]


@pytest.mark.asyncio
async def test_handle_callback_ignored_with_none_message(
    mock_bot: MagicMock,
) -> None:
    """When handler returns 'ignored' and message is None, no crash should occur."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    cq = MagicMock()
    cq.id = "cq_null_msg"
    cq.data = "reject:plan_null"
    cq.message = None

    on_approve = AsyncMock()
    on_reject = AsyncMock(return_value="ignored")

    # Should not raise
    await notifier._handle_callback(cq, on_approve, on_reject)

    on_reject.assert_called_once_with("plan_null", rendered_hash=None)
    mock_bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_handle_callback_ignored_edit_failure_is_swallowed(
    mock_bot: MagicMock,
) -> None:
    """When handler returns 'ignored' and the 'Already resolved' edit fails,
    the error should be logged and swallowed, not propagated."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock(
        side_effect=RuntimeError("Telegram edit failed"),
    )

    notifier = _make_notifier(mock_bot)

    cq = _make_callback_query("pub_approve", "prop_race")
    notifier._on_publication_approve = AsyncMock(return_value="ignored")

    on_approve = AsyncMock()
    on_reject = AsyncMock()

    # Should not raise despite edit failure
    await notifier._handle_callback(cq, on_approve, on_reject)

    notifier._on_publication_approve.assert_called_once_with("prop_race")
    mock_bot.edit_message_text.assert_called_once()


# ---------------------------------------------------------------------------
# Inbound chat authorization (security): only the configured chat_id may
# inject messages / button presses. get_updates returns traffic from any
# user who contacts the bot, so this is the sole authorization boundary.
# ---------------------------------------------------------------------------


def test_authorized_chat_matches_int_chat_id(mock_bot: MagicMock) -> None:
    """An int chat.id equal to the configured (string) value is authorized."""
    notifier = _make_notifier(mock_bot)
    message = MagicMock()
    message.chat.id = 123
    assert notifier._authorized_chat(message) is True


def test_authorized_chat_matches_string_chat_id(mock_bot: MagicMock) -> None:
    """A string chat.id equal to the configured value is authorized."""
    notifier = _make_notifier(mock_bot)
    message = MagicMock()
    message.chat.id = "123"
    assert notifier._authorized_chat(message) is True


def test_authorized_chat_rejects_other_chat_id(mock_bot: MagicMock) -> None:
    """A chat.id different from the configured one is rejected."""
    notifier = _make_notifier(mock_bot)
    message = MagicMock()
    message.chat.id = 999
    assert notifier._authorized_chat(message) is False


def test_authorized_chat_rejects_missing_chat_id(mock_bot: MagicMock) -> None:
    """A message with no chat id (e.g. callback Telegram could not echo back,
    passed here as ``None``) fails closed."""
    notifier = _make_notifier(mock_bot)
    assert notifier._authorized_chat(None) is False


def test_authorized_chat_reads_chat_id_from_inaccessible_message(
    mock_bot: MagicMock,
) -> None:
    """A callback whose original message has become inaccessible (date == 0,
    e.g. too old or deleted) arrives as a real ``InaccessibleMessage`` — which
    exposes ``chat`` (and thus ``chat.id``) but has no ``chat_id`` property.
    Authorization must read ``chat.id`` so a legitimate button press from the
    configured user is not silently rejected. Regression test: reading
    ``chat_id`` instead returns ``None`` and fails closed on a valid press."""
    from telegram import Chat, InaccessibleMessage

    notifier = _make_notifier(mock_bot)
    message = InaccessibleMessage(
        chat=Chat(id=123, type=Chat.PRIVATE), message_id=1,
    )
    # The trap the old code fell into: InaccessibleMessage has no chat_id.
    assert getattr(message, "chat_id", None) is None
    assert notifier._authorized_chat(message) is True


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_rejects_message_from_unauthorized_chat(
    _claim: AsyncMock, mock_bot: MagicMock,
) -> None:
    """A text message from a chat other than the configured one must not be
    forwarded to ``_on_message`` — otherwise any Telegram user could inject
    instructions stored as if they came from Thomas."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)
    on_message = AsyncMock()
    notifier._on_message = on_message

    intruder = MagicMock()
    intruder.update_id = 500
    intruder.callback_query = None
    intruder.message = MagicMock()
    intruder.message.text = "rm -rf everything"
    intruder.message.chat.id = 666  # not the configured chat

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [intruder]
        notifier._polling = False
        return []

    mock_bot.get_updates = AsyncMock(side_effect=fake_get_updates)

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock):
        notifier._polling = True
        await notifier._poll_loop(AsyncMock(), AsyncMock())

    on_message.assert_not_called()
    # The update is still consumed so it is not re-fetched.
    assert notifier._poll_offset == 501


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_rejects_callback_from_unauthorized_chat(
    _claim: AsyncMock, mock_bot: MagicMock,
) -> None:
    """A callback_query whose message lives in a chat other than the
    configured one must not invoke the approve/reject handlers."""
    mock_bot.answer_callback_query = AsyncMock()
    mock_bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(mock_bot)

    intruder = MagicMock()
    intruder.update_id = 700
    intruder.callback_query = MagicMock()
    intruder.callback_query.data = "approve:plan_abc"
    intruder.callback_query.message = MagicMock()
    intruder.callback_query.message.chat.id = 666  # not the configured chat

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [intruder]
        notifier._polling = False
        return []

    mock_bot.get_updates = AsyncMock(side_effect=fake_get_updates)

    on_approve = AsyncMock()
    on_reject = AsyncMock()

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock):
        notifier._polling = True
        await notifier._poll_loop(on_approve, on_reject)

    on_approve.assert_not_called()
    on_reject.assert_not_called()
    mock_bot.answer_callback_query.assert_not_called()
    assert notifier._poll_offset == 701




# ------------------------------------------------------------------
# Stale plan buttons after a curate refine (#4146)
# ------------------------------------------------------------------


def _stale_button_bot() -> MagicMock:
    bot = MagicMock()
    ids = iter(range(100, 200))
    bot.send_message = AsyncMock(
        side_effect=lambda **_: MagicMock(message_id=next(ids)),
    )
    bot.edit_message_text = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    return bot


def _seed_pending(plan_id: str) -> Memory:
    memory = Memory(Path("/fake/stale_button"))
    memory.save_evolution_plan(EvolutionPlan(
        plan_id=plan_id, title="Stale",
        category="capability", rationale="v1", proposed_changes=["Change A"],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z", status="pending_review",
    ))
    saved = memory.get_plan(plan_id)
    assert saved is not None
    memory.update_plan_status(
        plan_id, "pending_review",
        notified_content_hash=saved.plan_content_hash,
    )
    return memory


def _decision_handlers(
    memory: Memory,
) -> tuple[PlanDecisionHandler, PlanDecisionHandler]:
    return telegram_plan_handlers(memory)


def _press(data: str, message_id: int) -> MagicMock:
    cq = MagicMock()
    cq.id = "cq"
    cq.data = data
    cq.message.message_id = message_id
    cq.message.text = "Plan v1"
    return cq


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", [0, 1])
@pytest.mark.parametrize("setup", ["restart", "refined_send_fails", "retire_fails"])
async def test_stale_button_refused_after_refine(
    mem_fs: dict[str, str], setup: str, arm: int,
) -> None:
    """Pressing message A's button after a refine never lands on v2."""
    plan_id = f"stale_{setup}_{arm}"
    memory = _seed_pending(plan_id)
    bot = _stale_button_bot()
    notifier = _make_notifier(bot)
    v1 = memory.get_plan(plan_id)
    assert v1 is not None
    await notifier.notify_plan_for_review(plan_id, "Plan v1", v1.plan_content_hash)
    keyboard = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    stale_data = keyboard[0][arm].callback_data
    message_a = 100

    memory.refine_plan(plan_id, proposed_changes=["Change B"])
    v2 = memory.get_plan(plan_id)
    assert v2 is not None
    memory.update_plan_status(
        plan_id, "pending_review", notified_content_hash=v2.plan_content_hash,
    )
    if setup == "restart":
        await notifier.notify_plan_refined(plan_id, "Plan v2", v2.plan_content_hash)
        notifier = _make_notifier(bot)
    elif setup == "refined_send_fails":
        bot.send_message.side_effect = TimedOut()
        with pytest.raises(TimedOut):
            await notifier.notify_plan_refined(plan_id, "Plan v2", v2.plan_content_hash)
    else:
        bot.edit_message_text.side_effect = [RuntimeError("edit failed"), None]
        await notifier.notify_plan_refined(plan_id, "Plan v2", v2.plan_content_hash)
    bot.edit_message_text.reset_mock(side_effect=True)

    on_approve, on_reject = _decision_handlers(memory)
    await notifier._handle_callback(_press(stale_data, message_a), on_approve, on_reject)

    plan = memory.get_plan(plan_id)
    assert plan is not None
    assert plan.status == "pending_review"
    assert plan.reviewed_at is None
    assert plan.rejection_reason is None
    edited = bot.edit_message_text.call_args.kwargs
    assert edited["message_id"] == message_a
    assert "Plan changed since this message" in edited["text"]
    assert "--- Failed to" not in edited["text"]
    assert "via Telegram ---" not in edited["text"]


@pytest.mark.asyncio
async def test_refined_button_approves_refined_body(mem_fs: dict[str, str]) -> None:
    """Message B's own Approve button lands on v2."""
    memory = _seed_pending("stale_b")
    bot = _stale_button_bot()
    notifier = _make_notifier(bot)
    memory.refine_plan("stale_b", proposed_changes=["Change B"])
    v2 = memory.get_plan("stale_b")
    assert v2 is not None
    await notifier.notify_plan_refined("stale_b", "Plan v2", v2.plan_content_hash)
    data = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    on_approve, on_reject = _decision_handlers(memory)
    await notifier._handle_callback(_press(data, 100), on_approve, on_reject)

    approved = memory.get_plan("stale_b")
    assert approved is not None
    assert approved.status == "approved"


@pytest.mark.asyncio
async def test_empty_hash_segment_is_treated_as_legacy() -> None:
    """``approve:pid:`` must not become a match-anything prefix."""
    bot = _stale_button_bot()
    notifier = _make_notifier(bot)
    on_approve = AsyncMock(return_value="approved")

    await notifier._handle_callback(_press("approve:pid:", 1), on_approve, AsyncMock())

    on_approve.assert_awaited_once_with("pid", rendered_hash=None)


@pytest.mark.asyncio
async def test_oversized_callback_payload_raises() -> None:
    """A plan id too long for Telegram's 64-byte callback cap fails loud."""
    bot = _stale_button_bot()
    notifier = _make_notifier(bot)

    with pytest.raises(ValueError, match="64-byte"):
        await notifier.notify_plan_for_review("p" * 60, "msg", "f00dfeed" * 8)

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_callback_guard_measures_longer_approve_payload() -> None:
    """At the boundary where only ``approve:`` exceeds 64 bytes, still fail loud."""
    bot = _stale_button_bot()
    notifier = _make_notifier(bot)
    plan_id = "p" * 40

    with pytest.raises(ValueError, match="64-byte"):
        await notifier.notify_plan_for_review(plan_id, "msg", "f00dfeed" * 8)

    bot.send_message.assert_not_called()


def test_parse_question_ref_round_trips_format_question() -> None:
    """#4154: the marker ``ask_thomas`` emits parses back to its message_id."""
    from maestro.notifications.telegram import format_question, parse_question_ref

    assert parse_question_ref(format_question("Retry?", "q1")) == "q1"
    assert parse_question_ref("Maestro has a question:\n\nRetry?") is None
    assert parse_question_ref("just chatting [ref q1]:") is None
    assert parse_question_ref(None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply_to_message", "expected"),
    [
        ("question", "q1"),
        ("plain", None),
        ("absent", None),
    ],
)
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_threads_reply_to_question(
    _claim: AsyncMock,
    mock_bot: MagicMock,
    reply_to_message: str,
    expected: str | None,
) -> None:
    """#4154: a reply to an ``ask_thomas`` message carries its question id;
    a reply to anything else, or no reply, carries ``None``."""
    from maestro.notifications.telegram import format_question

    notifier = _make_notifier(mock_bot)
    on_message = AsyncMock()
    notifier._on_message = on_message

    update = MagicMock()
    update.update_id = 300
    update.callback_query = None
    update.message = MagicMock()
    update.message.text = "runner fixed, retry it"
    update.message.chat.id = 123
    if reply_to_message == "absent":
        update.message.reply_to_message = None
    else:
        update.message.reply_to_message = MagicMock()
        update.message.reply_to_message.text = (
            format_question("Retry?", "q1")
            if reply_to_message == "question"
            else "Plan approved."
        )

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        notifier._polling = False
        return [update]

    mock_bot.get_updates = AsyncMock(side_effect=fake_get_updates)

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock):
        notifier._polling = True
        await notifier._poll_loop(AsyncMock(), AsyncMock())

    on_message.assert_called_once_with(
        "runner fixed, retry it", expected, plan_ref=None,
    )


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_threads_plan_ref_from_review_reply(
    _claim: AsyncMock, mock_bot: MagicMock,
) -> None:
    """Proposal 145: a reply to a plan review body carries its parsed
    ``PlanRef``, even after ``_truncate`` cut the body's tail."""
    from maestro.plan_feedback import PlanRef

    notifier = _make_notifier(mock_bot)
    on_message = AsyncMock()
    notifier._on_message = on_message
    review = notifier._truncate(
        "New Evolution Plan [plan p1@0123456789abcdef]: T\n" + "x" * 10_000,
    )

    update = MagicMock()
    update.update_id = 301
    update.callback_query = None
    update.message = MagicMock()
    update.message.text = "narrow the scope"
    update.message.chat.id = 123
    update.message.reply_to_message = MagicMock()
    update.message.reply_to_message.text = review

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        notifier._polling = False
        return [update]

    mock_bot.get_updates = AsyncMock(side_effect=fake_get_updates)

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock):
        notifier._polling = True
        await notifier._poll_loop(AsyncMock(), AsyncMock())

    on_message.assert_called_once_with(
        "narrow the scope", None,
        plan_ref=PlanRef("p1", "0123456789abcdef"),
    )


# --- #4589: plan buttons bind only a fully delivered body -------------------

_CONT_RE = re.compile(r"^\[plan [^\]]+\] \(cont\. \d+/\d+\)\n")


def _long_plan() -> EvolutionPlan:
    return EvolutionPlan(
        plan_id="plan-4589",
        title="T" * 120,
        category="capability",
        rationale="r" * 1500,
        proposed_changes=[f"change {i} " + "c" * 450 for i in range(5)],
        files_affected=["maestro/alpha.py", "maestro/beta.py"],
        expected_impact="i" * 300,
        acceptance_criteria=[f"criterion {i} " + "a" * 150 for i in range(4)],
    )


def _counting_bot(mock_bot: MagicMock) -> MagicMock:
    counter = iter(range(100, 200))

    async def _send(**_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.message_id = next(counter)
        return result

    mock_bot.send_message = AsyncMock(side_effect=_send)
    mock_bot.edit_message_text = AsyncMock()
    return mock_bot


def _stamped_long_plan() -> tuple[EvolutionPlan, str]:
    plan = _long_plan()
    plan.plan_content_hash = compute_plan_content_hash(plan)
    msg = compose_review_message(plan)
    assert len(msg) > 4096
    return plan, msg


def _assert_whole_delivery(mock_bot: MagicMock, expected: str) -> None:
    calls = mock_bot.send_message.call_args_list
    assert len(calls) > 1
    texts = [call.kwargs["text"] for call in calls]
    for text in texts:
        assert len(text) <= 4096
        assert not text.endswith("(truncated)")
    keyed = [i for i, call in enumerate(calls) if call.kwargs.get("reply_markup")]
    assert keyed == [len(calls) - 1]
    rebuilt = texts[0] + "".join(_CONT_RE.sub("", t, count=1) for t in texts[1:])
    assert rebuilt == expected
    assert "Files:" in rebuilt
    assert "Acceptance criteria:" in rebuilt


@pytest.mark.asyncio
async def test_plan_review_buttons_bind_only_fully_delivered_body(
    mock_bot: MagicMock,
) -> None:
    bot = _counting_bot(mock_bot)
    plan, msg = _stamped_long_plan()
    notifier = _make_notifier(bot, max_message_length=4096)
    await notifier.notify_plan_for_review(plan.plan_id, msg, plan.plan_content_hash)

    _assert_whole_delivery(bot, msg)
    last_id = 100 + bot.send_message.await_count - 1
    assert notifier._plan_messages[plan.plan_id] == last_id


@pytest.mark.asyncio
async def test_plan_continuation_chunks_carry_plan_ref(mock_bot: MagicMock) -> None:
    bot = _counting_bot(mock_bot)
    plan, msg = _stamped_long_plan()
    notifier = _make_notifier(bot, max_message_length=4096)
    await notifier.notify_plan_for_review(plan.plan_id, msg, plan.plan_content_hash)

    expected = PlanRef(plan.plan_id, plan.plan_content_hash[:CALLBACK_HASH_PREFIX_LEN])
    for call in bot.send_message.call_args_list[1:]:
        assert parse_plan_ref(call.kwargs["text"]) == expected


@pytest.mark.asyncio
async def test_plan_refined_notice_chunks_keep_buttons_on_full_body(
    mock_bot: MagicMock,
) -> None:
    bot = _counting_bot(mock_bot)
    plan, msg = _stamped_long_plan()
    refined = "Refined — updated fields: rationale\n\n" + msg
    notifier = _make_notifier(bot, max_message_length=4096)
    await notifier.notify_plan_refined(plan.plan_id, refined, plan.plan_content_hash)

    _assert_whole_delivery(bot, refined)


@pytest.mark.asyncio
async def test_plan_body_over_chunk_cap_sends_no_keyboard(
    mock_bot: MagicMock,
) -> None:
    bot = _counting_bot(mock_bot)
    notifier = _make_notifier(bot, max_message_length=200)
    notifier._plan_messages["p1"] = 7
    body = "\n".join("line " + "z" * 60 for _ in range(40))
    await notifier.notify_plan_for_review("p1", body, "f00dfeed" * 8)

    calls = bot.send_message.call_args_list
    assert len(calls) == 1
    assert calls[0].kwargs.get("reply_markup") is None
    text = calls[0].kwargs["text"]
    assert len(text) <= 200
    assert text.endswith("review and decide on the dashboard.)")
    assert notifier._plan_messages["p1"] == 100
    bot.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_plan_body_no_budget_sends_no_keyboard(mock_bot: MagicMock) -> None:
    """A max length too small for the continuation prefix fails safe."""
    bot = _counting_bot(mock_bot)
    notifier = _make_notifier(bot, max_message_length=20)
    await notifier.notify_plan_for_review("p1", "y" * 50, "f00dfeed" * 8)

    calls = bot.send_message.call_args_list
    assert len(calls) == 1
    assert calls[0].kwargs.get("reply_markup") is None
    assert calls[0].kwargs["text"] == "y" * 20


def test_split_plan_message_hard_splits_overlong_line() -> None:
    from maestro.notifications.telegram import _split_for_controls

    marker = "[plan p1@f00dfeed]"
    message = "short\n" + "w" * 250 + "\ntail"
    chunks = _split_for_controls(message, marker, 120)
    assert chunks is not None
    for chunk in chunks:
        assert len(chunk) <= 120
    rebuilt = chunks[0] + "".join(_CONT_RE.sub("", c, count=1) for c in chunks[1:])
    assert rebuilt == message
