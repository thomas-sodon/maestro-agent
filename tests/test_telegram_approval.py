"""Tests for Telegram approval polling — all telegram.Bot calls are mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import Conflict


def _make_notifier(bot: MagicMock) -> object:
    with patch("maestro.notifications.telegram.Bot", return_value=bot):
        from maestro.notifications.telegram import TelegramNotifier

        return TelegramNotifier(
            bot_token="fake-token",
            chat_id="123",
        )


def _make_callback_update(
    update_id: int, action: str, plan_id: str,
) -> MagicMock:
    """Create a mock Telegram Update with a callback_query."""
    update = MagicMock()
    update.update_id = update_id
    update.callback_query = MagicMock()
    update.callback_query.id = f"cq_{update_id}"
    update.callback_query.data = f"{action}:{plan_id}"
    update.callback_query.message = MagicMock()
    update.callback_query.message.text = "Original plan message"
    update.callback_query.message.message_id = 42
    # Authorized-chat press: matches the _make_notifier chat_id ("123") so the
    # _poll_loop authorization gate dispatches it.
    update.callback_query.message.chat.id = 123
    return update


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_approve_callback(_claim: AsyncMock) -> None:
    """Pressing Approve calls on_approve and edits the message."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)

    update = _make_callback_update(1, "approve", "plan_abc")

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [update]
        # Stop polling after first batch
        notifier._polling = False
        return []

    bot.get_updates.side_effect = fake_get_updates

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    notifier._polling = True
    await notifier._poll_loop(on_approve, on_reject)

    on_approve.assert_called_once_with("plan_abc", rendered_hash=None)
    on_reject.assert_not_called()
    bot.answer_callback_query.assert_called()
    bot.edit_message_text.assert_called_once()
    edit_kwargs = bot.edit_message_text.call_args.kwargs
    assert "Approved via Telegram" in edit_kwargs["text"]


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_reject_callback(_claim: AsyncMock) -> None:
    """Pressing Reject calls on_reject and edits the message."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)

    update = _make_callback_update(1, "reject", "plan_xyz")
    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [update]
        notifier._polling = False
        return []

    bot.get_updates.side_effect = fake_get_updates

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    notifier._polling = True
    await notifier._poll_loop(on_approve, on_reject)

    on_reject.assert_called_once_with("plan_xyz", rendered_hash=None)
    on_approve.assert_not_called()
    bot.edit_message_text.assert_called_once()
    edit_kwargs = bot.edit_message_text.call_args.kwargs
    assert "Rejected via Telegram" in edit_kwargs["text"]


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_ignores_non_callback_updates(_claim: AsyncMock) -> None:
    """Updates without callback_query are silently ignored."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)

    update = MagicMock()
    update.update_id = 1
    update.callback_query = None  # Not a callback

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [update]
        notifier._polling = False
        return []

    bot.get_updates.side_effect = fake_get_updates

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    notifier._polling = True
    await notifier._poll_loop(on_approve, on_reject)

    on_approve.assert_not_called()
    on_reject.assert_not_called()


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_handles_approve_error(_claim: AsyncMock) -> None:
    """When on_approve raises, the loop continues without crashing."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)

    update = _make_callback_update(1, "approve", "bad_plan")
    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [update]
        notifier._polling = False
        return []

    bot.get_updates.side_effect = fake_get_updates

    on_approve = AsyncMock(side_effect=ValueError("Plan not found"))
    on_reject = AsyncMock(return_value="rejected")

    notifier._polling = True
    await notifier._poll_loop(on_approve, on_reject)

    on_approve.assert_called_once_with("bad_plan", rendered_hash=None)
    # Message SHOULD be updated with failure text so the user sees the error
    bot.edit_message_text.assert_called_once()
    edit_kwargs = bot.edit_message_text.call_args.kwargs
    assert "fail" in edit_kwargs.get("text", "").lower()


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_handles_network_error(_claim: AsyncMock) -> None:
    """``httpx.HTTPError`` from get_updates is tolerated — the loop retries."""
    import httpx

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("Network down")
        # Stop after retry
        notifier._polling = False
        return []

    bot.get_updates = AsyncMock(side_effect=fake_get_updates)
    notifier._bot = bot

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    # Run _poll_loop directly to avoid task scheduling issues
    notifier._polling = True
    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await notifier._poll_loop(on_approve, on_reject)

    assert call_count >= 2
    # Verify sleep was called after error (retry backoff)
    mock_sleep.assert_called_with(5)


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_handles_retry_after(_claim: AsyncMock) -> None:
    """A 429 ``RetryAfter`` backs off the server-specified interval (issue #4155)."""
    from telegram.error import RetryAfter

    bot = MagicMock()
    bot.get_updates = AsyncMock()
    notifier = _make_notifier(bot)

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RetryAfter(7)
        notifier._polling = False
        return []

    bot.get_updates = AsyncMock(side_effect=fake_get_updates)
    notifier._bot = bot

    notifier._polling = True
    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await notifier._poll_loop(AsyncMock(), AsyncMock())

    assert call_count == 2
    mock_sleep.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_propagates_unexpected_exception(_claim: AsyncMock) -> None:
    """Non-network exceptions from get_updates propagate out of the polling loop.

    Fail-loud: programming errors, ``OSError``, ``AssertionError``, etc.
    are not transient — they escape so the main loop sees the failure
    when it ``await``s the polling task.
    """
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock(side_effect=RuntimeError("not a network blip"))
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)
    notifier._bot = bot

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    notifier._polling = True
    with pytest.raises(RuntimeError, match="not a network blip"):
        await notifier._poll_loop(on_approve, on_reject)


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_start_stop_polling(_claim: AsyncMock) -> None:
    """start_approval_polling creates a task, stop cancels it."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock(return_value=[])
    bot.answer_callback_query = AsyncMock()

    notifier = _make_notifier(bot)

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    await notifier.start_approval_polling(on_approve, on_reject)
    assert notifier._polling is True
    assert notifier._poll_task is not None
    assert not notifier._poll_task.done()

    await notifier.stop_approval_polling()
    assert notifier._polling is False
    assert notifier._poll_task is None


def _make_message_update(update_id: int, text: str, chat_id: int = 123) -> MagicMock:
    """Create a mock Telegram Update carrying a text message.

    ``chat_id`` defaults to the ``_make_notifier`` chat ("123") so the
    authorization gate forwards it to ``_on_message``; pass a different id to
    exercise the unauthorized-chat drop branch.
    """
    update = MagicMock()
    update.update_id = update_id
    update.callback_query = None
    update.message = MagicMock()
    update.message.text = text
    update.message.chat.id = chat_id
    return update


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_task_failure_is_observed_during_run(_claim: AsyncMock) -> None:
    """A handler crash on the fire-and-forget poll task is surfaced as state.

    Regression for the silent-death bad state: an exception escaping the poll
    loop used to sit unretrieved on the task with no crash, notification, or
    health flag. The done-callback observer must now record it on
    ``notifier.poll_failure`` — surfaced through the observer, not merely left
    retrievable via ``task.exception()`` — so the main loop can re-raise it.
    """
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)
    # An OSError from the message handler — e.g. ``message_store.save_message``
    # failing to persist — is a non-transient defect that kills the channel.
    notifier._on_message = AsyncMock(side_effect=OSError("disk gone"))

    message = _make_message_update(900, "hello")
    bot.get_updates = AsyncMock(return_value=[message])

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    # Healthy channel starts with no recorded failure.
    assert notifier.poll_failure is None

    await notifier.start_approval_polling(on_approve, on_reject, on_message=notifier._on_message)
    # Drive the fire-and-forget task to completion; its done-callback (added
    # before this await, so it runs first) records the failure before the
    # exception is re-raised to us.
    with pytest.raises(OSError, match="disk gone"):
        await notifier._poll_task

    assert notifier.poll_failure is not None
    assert isinstance(notifier.poll_failure, OSError)
    assert str(notifier.poll_failure) == "disk gone"


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_offset_advances_only_after_handler_success(_claim: AsyncMock) -> None:
    """A crashing handler leaves the offset at the crashing update, not past it.

    Regression: the offset advanced *before* dispatch, so an update whose
    handler raised was confirmed-and-skipped — the failing input was silently
    dropped rather than re-delivered on restart. After the fix the offset stays
    at the crashing ``update_id`` (not ``update_id + 1``).
    """
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)
    notifier._on_message = AsyncMock(side_effect=RuntimeError("handler boom"))

    crashing = _make_message_update(800, "boom")
    bot.get_updates = AsyncMock(return_value=[crashing])

    on_approve = AsyncMock()
    on_reject = AsyncMock()

    # Poll starting from offset 800 — update 800 is the next unconfirmed one.
    notifier._poll_offset = 800
    notifier._polling = True
    with pytest.raises(RuntimeError, match="handler boom"):
        await notifier._poll_loop(on_approve, on_reject)

    # Offset stays at the crashing update (800), NOT update_id + 1 (801): a
    # restart re-delivers update 800 rather than confirming-and-skipping it.
    assert notifier._poll_offset == 800


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_offset_advances_after_success_and_authorized_drop(_claim: AsyncMock) -> None:
    """A successful dispatch and an unauthorized-chat drop both advance the offset.

    Companion to the crash-pin: good updates and intentionally-discarded ones
    must still be confirmed so they are not re-polled forever — only a *raising*
    handler leaves the offset unadvanced. (The non-integer ``update_id`` and
    unauthorized-callback drop branches are pinned by
    ``test_poll_loop_skips_update_with_none_update_id`` /
    ``test_poll_loop_rejects_callback_from_unauthorized_chat``.)
    """
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)
    on_message = AsyncMock()
    notifier._on_message = on_message

    handled = _make_message_update(810, "authorized", chat_id=123)
    intruder = _make_message_update(820, "unauthorized", chat_id=666)

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [handled, intruder]
        notifier._polling = False
        return []

    bot.get_updates = AsyncMock(side_effect=fake_get_updates)

    on_approve = AsyncMock()
    on_reject = AsyncMock()

    notifier._polling = True
    await notifier._poll_loop(on_approve, on_reject)

    # Authorized message dispatched once; the intruder never reaches the handler.
    on_message.assert_awaited_once_with("authorized", None, plan_ref=None)
    # Both updates confirmed: offset advances past the last (820 -> 821).
    assert notifier._poll_offset == 821


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_task_cancellation_is_not_recorded_as_failure(_claim: AsyncMock) -> None:
    """A graceful ``stop_approval_polling`` cancellation must not set ``poll_failure``."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock(return_value=[])
    bot.answer_callback_query = AsyncMock()

    notifier = _make_notifier(bot)
    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    await notifier.start_approval_polling(on_approve, on_reject)
    await notifier.stop_approval_polling()

    assert notifier.poll_failure is None


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_task_natural_completion_records_no_failure(_claim: AsyncMock) -> None:
    """A poll task that ends cleanly (no cancel, no exception) leaves ``poll_failure`` None.

    Covers the done-callback's natural-completion branch: the loop exits on its
    own (``_polling`` flips False, ``get_updates`` returns []), so the task
    finishes not cancelled and with ``exception()`` None — the observer records
    nothing. Distinct from the cancellation path (early return) and the
    handler-crash path (failure stored): here the ``if exc is not None`` guard is
    False and the callback falls through, which neither sibling test exercises.
    """
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.answer_callback_query = AsyncMock()

    notifier = _make_notifier(bot)

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        # Exit the loop on the first poll: no updates, stop polling. The task
        # then completes normally with no exception.
        notifier._polling = False
        return []

    bot.get_updates = AsyncMock(side_effect=fake_get_updates)

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    await notifier.start_approval_polling(on_approve, on_reject)
    # Drive the fire-and-forget task to natural completion; its done-callback
    # (added before this await, so it runs first) sees ``cancelled()`` False and
    # ``exception()`` None and records nothing.
    await notifier._poll_task

    assert notifier._poll_task.cancelled() is False
    assert notifier._poll_task.exception() is None
    assert notifier.poll_failure is None


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_ignores_unknown_action(_claim: AsyncMock) -> None:
    """Callback data with unknown action (not approve/reject) is ignored."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)

    update = _make_callback_update(1, "delete", "plan_x")
    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [update]
        notifier._polling = False
        return []

    bot.get_updates.side_effect = fake_get_updates

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    notifier._polling = True
    await notifier._poll_loop(on_approve, on_reject)

    on_approve.assert_not_called()
    on_reject.assert_not_called()


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_offset_persists_across_restart(_claim: AsyncMock) -> None:
    """After a stop/restart cycle the offset must continue from last_update_id+1.

    Regression test for: _poll_loop used a local `offset = 0` variable that
    reset on every invocation, causing Telegram to re-deliver all previously-
    seen updates on restart.
    """
    offsets_called: list[int] = []
    call_count = [0]

    bot = MagicMock()
    notifier = _make_notifier(bot)

    async def fake_get_updates(offset: int = 0, timeout: int = 30, allowed_updates: object = None) -> list[MagicMock]:
        offsets_called.append(offset)
        call_count[0] += 1
        if call_count[0] == 1:
            update = MagicMock()
            update.update_id = 500
            update.callback_query = None
            return [update]
        notifier._polling = False
        return []

    notifier._bot.get_updates = fake_get_updates

    on_approve: AsyncMock = AsyncMock(return_value="approved")
    on_reject: AsyncMock = AsyncMock(return_value="rejected")

    # --- First session ---
    notifier._polling = True
    await notifier._poll_loop(on_approve, on_reject)

    # After processing update_id=500, the offset should have advanced to 501
    assert 501 in offsets_called, f"Expected 501 in first-session offsets: {offsets_called}"

    # --- Second session ---
    second_session_offsets: list[int] = []
    call_count[0] = 0

    async def tracking_get_updates(offset: int = 0, timeout: int = 30, allowed_updates: object = None) -> list[MagicMock]:
        second_session_offsets.append(offset)
        call_count[0] += 1
        notifier._polling = False
        return []

    notifier._bot.get_updates = tracking_get_updates

    notifier._polling = True
    await notifier._poll_loop(on_approve, on_reject)

    first_offset_second_session = second_session_offsets[0]
    assert first_offset_second_session == 501, (
        f"After restart, get_updates should start at offset=501 "
        f"(to skip already-seen update_id=500), but started at {first_offset_second_session}. "
        f"The offset is not persisted across poll-loop restarts."
    )


@pytest.mark.asyncio
async def test_stale_approve_does_not_edit_message_to_approved() -> None:
    """When on_approve raises (plan not in pending_review), the Telegram message
    must NOT be edited to 'Approved via Telegram'."""
    bot = MagicMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)

    async def stale_on_approve(plan_id: str, *, rendered_hash: str | None) -> str:
        raise ValueError(f"plan {plan_id} not in pending_review")

    async def stale_on_reject(plan_id: str, *, rendered_hash: str | None) -> str:
        raise ValueError(f"plan {plan_id} not in pending_review")

    cq = MagicMock()
    cq.id = "cq_stale"
    cq.data = "approve:already_approved_plan"
    cq.message = MagicMock()
    cq.message.text = "Some plan text"
    cq.message.message_id = 99

    await notifier._handle_callback(cq, stale_on_approve, stale_on_reject)

    for call in bot.edit_message_text.call_args_list:
        text = call.kwargs.get("text", "") or ""
        assert "Approved via Telegram" not in text, (
            f"Stale approve must not edit message to 'Approved via Telegram', got: {text!r}"
        )


@pytest.mark.asyncio
async def test_stale_reject_does_not_edit_message_to_rejected() -> None:
    """When on_reject raises (plan not in pending_review), the Telegram message
    must NOT be edited to 'Rejected via Telegram'."""
    bot = MagicMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()

    notifier = _make_notifier(bot)

    async def stale_on_approve(plan_id: str, *, rendered_hash: str | None) -> str:
        raise ValueError(f"plan {plan_id} not in pending_review")

    async def stale_on_reject(plan_id: str, *, rendered_hash: str | None) -> str:
        raise ValueError(f"plan {plan_id} not in pending_review")

    cq = MagicMock()
    cq.id = "cq_stale_reject"
    cq.data = "reject:already_rejected_plan"
    cq.message = MagicMock()
    cq.message.text = "Some plan text"
    cq.message.message_id = 100

    await notifier._handle_callback(cq, stale_on_approve, stale_on_reject)

    for call in bot.edit_message_text.call_args_list:
        text = call.kwargs.get("text", "") or ""
        assert "Rejected via Telegram" not in text, (
            f"Stale reject must not edit message to 'Rejected via Telegram', got: {text!r}"
        )


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_handles_conflict_with_backoff(_claim: AsyncMock) -> None:
    """Conflict errors use exponential backoff instead of fixed 5s retry."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock()

    notifier = _make_notifier(bot)

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise Conflict("terminated by other getUpdates request")
        notifier._polling = False
        return []

    bot.get_updates = AsyncMock(side_effect=fake_get_updates)
    notifier._bot = bot

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    notifier._polling = True
    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await notifier._poll_loop(on_approve, on_reject)

    assert call_count == 3
    sleep_values = [c.args[0] for c in mock_sleep.call_args_list]
    assert sleep_values == [5.0, 10.0]


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_conflict_backoff_resets_on_success(_claim: AsyncMock) -> None:
    """After a successful poll, conflict backoff resets to the initial value."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock()

    notifier = _make_notifier(bot)

    call_count = 0

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Conflict("terminated by other getUpdates request")
        if call_count == 2:
            return []  # success — resets backoff
        if call_count == 3:
            raise Conflict("terminated by other getUpdates request")
        notifier._polling = False
        return []

    bot.get_updates = AsyncMock(side_effect=fake_get_updates)
    notifier._bot = bot

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    notifier._polling = True
    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await notifier._poll_loop(on_approve, on_reject)

    sleep_values = [c.args[0] for c in mock_sleep.call_args_list]
    # Both conflict sleeps should be 5.0 because the success in between resets backoff
    assert sleep_values == [5.0, 5.0]


@pytest.mark.asyncio
@patch("maestro.notifications.telegram.TelegramNotifier._claim_polling_session", new_callable=AsyncMock)
async def test_poll_loop_conflict_backoff_caps_at_max(_claim: AsyncMock) -> None:
    """Conflict backoff is capped at _CONFLICT_BACKOFF_MAX (60s)."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock()

    notifier = _make_notifier(bot)

    call_count = 0
    # Need 5 conflicts to reach cap: 5, 10, 20, 40, 60 (capped from 80)
    total_conflicts = 5

    async def fake_get_updates(**kwargs: object) -> list[MagicMock]:
        nonlocal call_count
        call_count += 1
        if call_count <= total_conflicts:
            raise Conflict("terminated by other getUpdates request")
        notifier._polling = False
        return []

    bot.get_updates = AsyncMock(side_effect=fake_get_updates)
    notifier._bot = bot

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    notifier._polling = True
    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await notifier._poll_loop(on_approve, on_reject)

    sleep_values = [c.args[0] for c in mock_sleep.call_args_list]
    assert sleep_values == [5.0, 10.0, 20.0, 40.0, 60.0]


# --- _claim_polling_session tests ---


@pytest.mark.asyncio
async def test_claim_session_succeeds_first_attempt() -> None:
    """Claim call succeeds immediately — no retries, no sleep."""
    bot = MagicMock()
    bot.get_updates = AsyncMock(return_value=[])

    notifier = _make_notifier(bot)

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await notifier._claim_polling_session()

    # Claim uses the current poll offset (0 at startup), not a negative one,
    # so pending updates are not confirmed/discarded (#344).
    bot.get_updates.assert_called_once_with(offset=0, timeout=0)
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_claim_session_retries_on_conflict() -> None:
    """Claim retries once after a 409, then succeeds."""
    bot = MagicMock()
    bot.get_updates = AsyncMock(
        side_effect=[Conflict("terminated"), []],
    )

    notifier = _make_notifier(bot)

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await notifier._claim_polling_session()

    assert bot.get_updates.call_count == 2
    mock_sleep.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_claim_session_exhausts_retries() -> None:
    """After 3 conflicts the claim gives up without raising."""
    bot = MagicMock()
    bot.get_updates = AsyncMock(
        side_effect=Conflict("terminated"),
    )

    notifier = _make_notifier(bot)

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await notifier._claim_polling_session()  # should not raise

    assert bot.get_updates.call_count == 3
    # Two sleeps between the three attempts (not after the last)
    assert mock_sleep.call_count == 2
    for call in mock_sleep.call_args_list:
        assert call.args[0] == 1


@pytest.mark.asyncio
async def test_claim_session_handles_unexpected_error() -> None:
    """Unexpected errors abort the claim immediately without retrying."""
    bot = MagicMock()
    bot.get_updates = AsyncMock(side_effect=RuntimeError("boom"))

    notifier = _make_notifier(bot)

    with patch("maestro.notifications.telegram.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await notifier._claim_polling_session()  # should not raise

    bot.get_updates.assert_called_once()
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_poll_loop_calls_claim_before_polling() -> None:
    """The first get_updates call in _poll_loop is the claim (offset=0, timeout=0)."""
    bot = MagicMock()
    bot.get_updates = AsyncMock()

    notifier = _make_notifier(bot)

    calls: list[dict[str, object]] = []

    async def tracking_get_updates(**kwargs: object) -> list[MagicMock]:
        calls.append(kwargs)
        if len(calls) == 1:
            # This is the claim call — return empty
            return []
        # Stop after one real poll iteration
        notifier._polling = False
        return []

    bot.get_updates.side_effect = tracking_get_updates

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")

    notifier._polling = True
    await notifier._poll_loop(on_approve, on_reject)

    assert len(calls) >= 2
    # First call is the claim — uses the current poll offset (0), not -1, so
    # queued updates are preserved for the real poll to consume (#344).
    assert calls[0]["offset"] == 0
    assert calls[0]["timeout"] == 0
    # Second call is the normal poll
    assert calls[1]["timeout"] == 30


# ---------------------------------------------------------------------------
# Proposal 032 — publication approval callbacks
# ---------------------------------------------------------------------------


def _make_pub_callback_update(
    update_id: int, action: str, proposal_id: str,
) -> MagicMock:
    """Telegram callback_query update for a ``pub_approve:`` / ``pub_reject:`` press."""
    update = MagicMock()
    update.update_id = update_id
    update.callback_query = MagicMock()
    update.callback_query.id = f"cq_{update_id}"
    update.callback_query.data = f"{action}:{proposal_id}"
    update.callback_query.message = MagicMock()
    update.callback_query.message.text = "Publication proposal text"
    update.callback_query.message.message_id = 7
    return update


@pytest.mark.asyncio
async def test_pub_approve_routes_to_publication_callback() -> None:
    bot = MagicMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()
    notifier = _make_notifier(bot)

    plan_approve = AsyncMock(return_value="approved")
    plan_reject = AsyncMock(return_value="rejected")
    pub_approve = AsyncMock(return_value="approved")
    pub_reject = AsyncMock(return_value="rejected")
    notifier._on_publication_approve = pub_approve
    notifier._on_publication_reject = pub_reject

    cq = _make_pub_callback_update(1, "pub_approve", "prop-42").callback_query

    await notifier._handle_callback(cq, plan_approve, plan_reject)

    pub_approve.assert_awaited_once_with("prop-42")
    plan_approve.assert_not_called()
    plan_reject.assert_not_called()
    # Message edited to Approved via Telegram.
    assert bot.edit_message_text.await_count == 1
    edit_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "Approved via Telegram" in edit_text


@pytest.mark.asyncio
async def test_pub_reject_routes_to_publication_callback() -> None:
    bot = MagicMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()
    notifier = _make_notifier(bot)

    plan_approve = AsyncMock(return_value="approved")
    plan_reject = AsyncMock(return_value="rejected")
    pub_approve = AsyncMock(return_value="approved")
    pub_reject = AsyncMock(return_value="rejected")
    notifier._on_publication_approve = pub_approve
    notifier._on_publication_reject = pub_reject

    cq = _make_pub_callback_update(1, "pub_reject", "prop-99").callback_query
    await notifier._handle_callback(cq, plan_approve, plan_reject)

    pub_reject.assert_awaited_once_with("prop-99")
    plan_approve.assert_not_called()
    plan_reject.assert_not_called()
    edit_text = bot.edit_message_text.await_args.kwargs["text"]
    assert "Rejected via Telegram" in edit_text


@pytest.mark.asyncio
async def test_legacy_approve_still_routes_to_plan_callback() -> None:
    """A ``pub_*`` handler registration must not hijack legacy ``approve:`` presses."""
    bot = MagicMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()
    notifier = _make_notifier(bot)

    plan_approve = AsyncMock(return_value="approved")
    plan_reject = AsyncMock(return_value="rejected")
    pub_approve = AsyncMock(return_value="pub-approved")
    notifier._on_publication_approve = pub_approve

    cq = _make_callback_update(1, "approve", "plan-123").callback_query
    await notifier._handle_callback(cq, plan_approve, plan_reject)

    plan_approve.assert_awaited_once_with("plan-123", rendered_hash=None)
    pub_approve.assert_not_called()


@pytest.mark.asyncio
async def test_pub_callback_without_handler_logs_warning() -> None:
    """``pub_approve:`` press with no registered handler logs and ignores."""
    import logging
    bot = MagicMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()
    notifier = _make_notifier(bot)
    # No publication callbacks wired.
    assert notifier._on_publication_approve is None

    plan_approve = AsyncMock(return_value="approved")
    plan_reject = AsyncMock(return_value="rejected")

    cq = _make_pub_callback_update(
        1, "pub_approve", "prop-noop",
    ).callback_query

    with patch.object(
        logging.getLogger("maestro"), "warning",
    ) as mock_warn:
        await notifier._handle_callback(cq, plan_approve, plan_reject)

    plan_approve.assert_not_called()
    bot.edit_message_text.assert_not_called()
    # At least one warning mentioning the missing handler.
    assert any(
        "no handler registered" in str(c.args[0])
        for c in mock_warn.call_args_list
    )


@pytest.mark.asyncio
async def test_unknown_prefix_logs_warning_and_noop() -> None:
    """Unknown action prefix (e.g. ``publish:``) logs a warning instead of crashing."""
    import logging
    bot = MagicMock()
    bot.answer_callback_query = AsyncMock()
    bot.edit_message_text = AsyncMock()
    notifier = _make_notifier(bot)

    plan_approve = AsyncMock(return_value="approved")
    plan_reject = AsyncMock(return_value="rejected")

    cq = MagicMock()
    cq.id = "cq_weird"
    cq.data = "publish:proposal-x"
    cq.message = MagicMock()
    cq.message.text = "whatever"
    cq.message.message_id = 1

    with patch.object(
        logging.getLogger("maestro"), "warning",
    ) as mock_warn:
        await notifier._handle_callback(cq, plan_approve, plan_reject)

    plan_approve.assert_not_called()
    plan_reject.assert_not_called()
    bot.edit_message_text.assert_not_called()
    assert any(
        "unknown action prefix" in str(c.args[0])
        for c in mock_warn.call_args_list
    )


@pytest.mark.asyncio
async def test_start_approval_polling_positional_backwards_compatible() -> None:
    """Legacy 3-positional signature still works after 032's keyword args."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_updates = AsyncMock(return_value=[])
    bot.answer_callback_query = AsyncMock()
    notifier = _make_notifier(bot)

    on_approve = AsyncMock(return_value="approved")
    on_reject = AsyncMock(return_value="rejected")
    on_message = AsyncMock()

    await notifier.start_approval_polling(on_approve, on_reject, on_message)
    try:
        assert notifier._polling is True
        # Publication callbacks are None because they weren't passed.
        assert notifier._on_publication_approve is None
        assert notifier._on_publication_reject is None
        assert notifier._on_message is on_message
    finally:
        await notifier.stop_approval_polling()


@pytest.mark.asyncio
async def test_notify_publication_for_review_empty_message_skipped() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    notifier = _make_notifier(bot)

    await notifier.notify_publication_for_review("p-empty", "   ")

    bot.send_message.assert_not_called()
    assert "p-empty" not in notifier._publication_messages


@pytest.mark.asyncio
async def test_notify_publication_for_review_send_error_propagates() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))
    notifier = _make_notifier(bot)

    with pytest.raises(RuntimeError, match="boom"):
        await notifier.notify_publication_for_review("p-fail", "hello")

    # Message id not stored when send failed.
    assert "p-fail" not in notifier._publication_messages


@pytest.mark.asyncio
async def test_resolve_publication_unknown_id_noop() -> None:
    """resolve_publication for an unknown id must not touch Telegram."""
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    notifier = _make_notifier(bot)

    await notifier.resolve_publication("never-sent", "Approved")

    bot.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_publication_edit_error_swallowed() -> None:
    """A failure to edit the message is logged but does not raise."""
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=RuntimeError("edit boom"))
    notifier = _make_notifier(bot)
    notifier._publication_messages["p-resolve"] = 42

    await notifier.resolve_publication("p-resolve", "Approved")

    bot.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_publication_for_review_uses_pub_prefixes() -> None:
    """Button callback_data must carry the ``pub_*`` prefixes, not ``approve:``."""
    bot = MagicMock()
    send_result = MagicMock()
    send_result.message_id = 501
    bot.send_message = AsyncMock(return_value=send_result)
    notifier = _make_notifier(bot)

    await notifier.notify_publication_for_review(
        "prop-77", "Publication proposal ready",
    )

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    markup = kwargs["reply_markup"]
    # Extract callback_data strings from the inline keyboard.
    rows = markup.inline_keyboard
    buttons = [btn for row in rows for btn in row]
    datas = {btn.callback_data for btn in buttons}
    assert datas == {"pub_approve:prop-77", "pub_reject:prop-77"}
    assert notifier._publication_messages["prop-77"] == 501
