"""Tests for Notification base classes."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from maestro.notifications.base import NullNotifier


@pytest.mark.asyncio
async def test_null_notifier() -> None:
    """NullNotifier.notify should not raise."""
    notifier = NullNotifier()
    await notifier.notify("test message")


@pytest.mark.asyncio
async def test_null_notifier_none_message_no_crash() -> None:
    """NullNotifier.notify must handle None without crashing.

    message[:100] in the log call raises TypeError when message is None.
    """
    notifier = NullNotifier()
    await notifier.notify(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_null_notifier_ask_thomas_falls_back_to_notify() -> None:
    """Default ask_thomas should call notify with question prefix."""
    notifier = NullNotifier()
    await notifier.ask_thomas("What should I do?", "msg123")


@pytest.mark.asyncio
async def test_null_notifier_message_polling_noop() -> None:
    """start/stop_message_polling should be no-ops."""
    notifier = NullNotifier()
    await notifier.start_message_polling(lambda text: None)  # type: ignore[arg-type, return-value]
    await notifier.stop_message_polling()


@pytest.mark.asyncio
async def test_null_notifier_publication_for_review_falls_back_to_notify() -> None:
    """Default notify_publication_for_review forwards to notify()."""
    notifier = NullNotifier()
    # Plain str body renders through the base-class fallback to self.notify.
    await notifier.notify_publication_for_review(
        "prop-1", "A publication proposal is ready",
    )


@pytest.mark.asyncio
async def test_null_notifier_resolve_publication_noop() -> None:
    """resolve_publication default is a safe no-op."""
    notifier = NullNotifier()
    await notifier.resolve_publication("prop-42", "Approved")


@pytest.mark.asyncio
async def test_notify_plan_refined_default_delegates_to_notify() -> None:
    """Default notify_plan_refined forwards the message to notify()."""
    notifier = NullNotifier()
    notifier.notify = AsyncMock()  # type: ignore[method-assign]
    await notifier.notify_plan_refined("plan-7", "Refined this plan", "f00dfeed" * 8)
    notifier.notify.assert_awaited_once_with("Refined this plan")


@pytest.mark.asyncio
async def test_null_notifier_plan_refined_falls_back_to_notify() -> None:
    """NullNotifier inherits the plain-text default and does not crash."""
    notifier = NullNotifier()
    await notifier.notify_plan_refined("plan-9", "A plan was refined", "f00dfeed" * 8)
