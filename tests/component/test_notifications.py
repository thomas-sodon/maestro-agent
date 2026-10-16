"""Component tests for notifications."""

from __future__ import annotations

import json
import re
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from maestro.evolution import QualityGateResult
from maestro.notifications.base import Notifier
from maestro.sdk import QueryResult
from tests.component.conftest import (
    make_quality_gates_mock,
    write_evolution_plan,
    write_self_model,
)
from tests.component.sdk_mock import SDKMock
from tests.support.gap_fuel import fuel_patch

_OK_REVIEW = QualityGateResult(
    passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True,
    raw_output="Ruff: clean\n\nMypy: clean\n\nTests: clean",
)

_telegram_available = True
TelegramNotifier = None  # type: ignore[assignment, misc]
try:
    from maestro.notifications.telegram import TelegramNotifier
except BaseException:
    _telegram_available = False

pytestmark = pytest.mark.skipif(not _telegram_available, reason="telegram lib not importable")


def _parse_telegram_body(request: httpx.Request) -> dict[str, Any]:
    """Parse a Telegram Bot API request body (JSON or form-encoded)."""
    try:
        return cast(dict[str, Any], json.loads(request.content))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raw = parse_qs(request.content.decode())
        return {k: v[0] if len(v) == 1 else v for k, v in raw.items()}


# ---- Telegram message delivery ----


@pytest.mark.asyncio
async def test_telegram_message_delivery(telegram_mock):
    notifier = TelegramNotifier(bot_token="test-bot-token", chat_id="12345")
    await notifier.notify("Evolution plan proposed: Improve error handling")
    assert telegram_mock.called
    body = _parse_telegram_body(telegram_mock.calls.last.request)
    assert "Improve error handling" in body["text"]


@pytest.mark.asyncio
async def test_telegram_truncation(telegram_mock):
    notifier = TelegramNotifier(bot_token="test-bot-token", chat_id="12345")
    await notifier.notify("A" * 5000)
    assert telegram_mock.called
    assert len(telegram_mock.calls) == 1
    body = _parse_telegram_body(telegram_mock.calls.last.request)
    assert len(body["text"]) <= 4096
    assert body["text"].endswith("(truncated)")


@pytest.mark.asyncio
async def test_telegram_failure_propagation():
    with respx.mock:
        respx.post(re.compile(r".*/bot.*/getMe")).mock(
            return_value=httpx.Response(200, json={
                "ok": True,
                "result": {
                    "id": 123456789, "is_bot": True,
                    "first_name": "MockBot", "username": "mock_bot",
                },
            }),
        )
        respx.post(re.compile(r".*/bot.*/sendMessage")).mock(
            return_value=httpx.Response(400, json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: chat not found",
            }),
        )
        respx.route(url__regex=r".*/bot.*").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True}),
        )
        notifier = TelegramNotifier(bot_token="test-bot-token", chat_id="12345")
        with pytest.raises(Exception):  # noqa: B017
            await notifier.notify("This should fail")


# ---- Notification failures (graceful degradation) ----


class FailingNotifier(Notifier):
    """Notifier that always raises."""

    async def notify(
        self, message: str, parse_mode: str | None = None,
    ) -> None:
        raise RuntimeError("notify failed")


@pytest.mark.asyncio
async def test_evolve_notification_failure_does_not_crash(make_agent, memory_dir):
    """A notifier failure during ``evolve()`` is swallowed by ``submit_for_review``.

    ``submit_for_review`` wraps its ``notify_plan_for_review`` call in
    its own ``try/except Exception`` (evolution.py) — that older catch
    is out of scope for the recent narrowing of the centralised
    ``_notify_best_effort`` helper, so ``evolve()`` keeps the plan
    persisted even when the Telegram call raises.
    """
    write_self_model(memory_dir, evolution_count=2,
                     identity_statements=["I am Maestro"],
                     capabilities_known=["code generation"],
                     limitations_known=["slow"])
    agent, _code_host, _vcs = make_agent(notifier=FailingNotifier())
    sdk = SDKMock()
    sdk.then(
        json.dumps({"title": "Improve notifications", "category": "infrastructure",
                     "priority": "capability", "rationale": "Better alerts",
                     "proposed_changes": ["Add retry logic"],
                     "files_affected": ["maestro/notifications/base.py"],
                     "expected_impact": "More reliable notifications"}),
    )
    with (
        fuel_patch(),
        sdk.patch("maestro.consciousness", "maestro.evolution", "maestro.evolution_quality"),
        patch(
            "maestro.evolution.subprocess.run",
            side_effect=make_quality_gates_mock(),
        ),
    ):
        await agent.evolve()
    sdk.assert_all_consumed()
    plans = [json.loads(line) for line in
             (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    assert any(p["title"] == "Improve notifications" for p in plans)


@pytest.mark.asyncio
async def test_run_propagates_notifier_failure(make_agent, memory_dir):
    """A real notifier ``Exception`` during ``agent.run()`` propagates after persistence.

    The task record is appended to ``task_history.jsonl`` before the
    final Telegram notification, so the record is committed before
    the notifier crashes; the exception still surfaces so the loop
    iteration handler sees it.
    """
    write_self_model(memory_dir)
    plan_response = json.dumps([{"description": "Do the thing"}])
    agent, _code_host, _vcs = make_agent(notifier=FailingNotifier())
    sdk = SDKMock()
    sdk.then(plan_response, "Thing done", "Distilled findings.")
    with (
        sdk.patch("maestro.planner", "maestro.executor", "maestro.agent"),
        patch.object(agent, "_install_signal_handlers"),
        pytest.raises(RuntimeError, match="notify failed"),
    ):
        await agent.run("Do something important")
    sdk.assert_all_consumed()
    lines = (memory_dir / "task_history.jsonl").read_text().strip().splitlines()
    assert len(lines) >= 1
    saved = json.loads(lines[0])
    assert saved["outcome"] == "success"


@pytest.mark.asyncio
async def test_implement_propagates_notifier_failure(
    make_agent, memory_dir, workspace_dir,
):
    """A real notifier ``Exception`` during ``implement_plan()`` propagates,
    but the persisted record stays at ``implemented``.

    ``create_pr`` updates the plan status to ``implemented`` with a real
    ``pr_url`` before the PR-creation Telegram call. With the narrowed
    catch the notify failure surfaces — a broken Telegram client must
    not be hidden under a WARNING log. But the persisted record must
    *not* be demoted to ``failed`` once the PR exists on GitHub:
    otherwise the next iteration's ``_implement_approved_plans`` selector
    would re-pick the plan and create a duplicate PR. The choke-point
    gate inside ``_demote_plan_to_failed`` short-circuits the demotion
    when the on-disk record is already ``implemented`` with a ``pr_url``.
    """
    write_self_model(memory_dir, evolution_count=0,
                     identity_statements=["I am Maestro"])
    write_evolution_plan(memory_dir, plan_id="notif001", title="Notif test",
                         status="approved", priority="capability",
                         proposed_changes=["Add file"], files_affected=["test.py"])

    async def _mock_query(**kwargs):
        (workspace_dir / "new_feature.py").write_text("# notif test\n")
        return QueryResult(response="Implemented")

    agent, _code_host, _vcs = make_agent(notifier=FailingNotifier())
    impl_sdk = SDKMock()
    # Under gate convergence (proposal 110, default-on) self_review must reach
    # LGTM or implement_plan demotes before create_pr — approve it here so the
    # notify-during-create_pr path is still reached.
    impl_sdk.on("reviewing an evolution implementation", "LGTM")
    impl_sdk.on("", _mock_query)
    with (
        impl_sdk.patch("maestro.evolution", "maestro.evolution_quality"),
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        pytest.raises(RuntimeError, match="notify failed"),
    ):
        await agent.implement_plan("notif001")
    plans = [json.loads(line) for line in
             (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    impl_plan = next((p for p in plans if p["plan_id"] == "notif001"), None)
    assert impl_plan is not None
    assert impl_plan["status"] == "implemented"
    assert impl_plan["pr_url"]
    assert impl_plan.get("failure_count", 0) == 0


@pytest.mark.asyncio
async def test_implement_does_not_re_create_pr_on_retry_after_notifier_failure(
    make_agent, memory_dir, workspace_dir,
):
    """A second loop iteration must not re-create the PR after a notify failure.

    With the choke-point gate in place, the first attempt persists the
    plan as ``implemented`` with a real ``pr_url`` (the PR exists on
    GitHub) even though the notify call raised. The second iteration's
    ``_implement_approved_plans`` selector — keyed on
    ``status == "approved"`` or ``status == "failed"`` — must see no
    eligible plan and return cleanly, leaving the original PR-creation
    count unchanged. This is the direct complement of the duplicate-PR
    regression: without the gate, the demotion to ``failed`` would
    re-qualify the plan and a second ``gh pr create`` would fire.
    """
    write_self_model(memory_dir, evolution_count=0,
                     identity_statements=["I am Maestro"])
    write_evolution_plan(memory_dir, plan_id="notif001", title="Notif test",
                         status="approved", priority="capability",
                         proposed_changes=["Add file"], files_affected=["test.py"])

    async def _mock_query(**kwargs):
        (workspace_dir / "new_feature.py").write_text("# notif test\n")
        return QueryResult(response="Implemented")

    agent, code_host, _vcs = make_agent(notifier=FailingNotifier())
    impl_sdk = SDKMock()
    # Under gate convergence (proposal 110, default-on) self_review must reach
    # LGTM or implement_plan demotes before create_pr — approve it here so the
    # notify-during-create_pr path is still reached.
    impl_sdk.on("reviewing an evolution implementation", "LGTM")
    impl_sdk.on("", _mock_query)
    with (
        impl_sdk.patch("maestro.evolution", "maestro.evolution_quality"),
        patch.object(agent.evolution, "run_quality_gates", return_value=_OK_REVIEW),
        pytest.raises(RuntimeError, match="notify failed"),
    ):
        await agent.implement_plan("notif001")

    pr_count_after_first = len(code_host.created_prs)
    assert pr_count_after_first == 1
    plans = [json.loads(line) for line in
             (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    impl_plan = next((p for p in plans if p["plan_id"] == "notif001"), None)
    assert impl_plan is not None
    original_pr_url = impl_plan["pr_url"]
    assert original_pr_url

    # Second iteration: the eligibility selector should skip the plan
    # because it is now ``implemented``, not ``approved`` or ``failed``.
    await agent._implement_approved_plans()

    assert len(code_host.created_prs) == pr_count_after_first
    plans = [json.loads(line) for line in
             (memory_dir / "evolution_plans.jsonl").read_text().strip().splitlines()]
    impl_plan = next((p for p in plans if p["plan_id"] == "notif001"), None)
    assert impl_plan is not None
    assert impl_plan["status"] == "implemented"
    assert impl_plan["pr_url"] == original_pr_url
    assert impl_plan.get("failure_count", 0) == 0


# ---- Telegram inline approval ----


@pytest.mark.asyncio
async def test_notify_plan_for_review_sends_inline_keyboard(
    telegram_mock: Any,
) -> None:
    """notify_plan_for_review should send a message with reply_markup."""
    notifier = TelegramNotifier(
        bot_token="test-bot-token", chat_id="12345",
    )
    await notifier.notify_plan_for_review(
        "plan_abc", "Review this evolution plan", "f00dfeed" * 8,
    )
    assert telegram_mock.called
    body = _parse_telegram_body(telegram_mock.calls.last.request)
    assert "Review this evolution plan" in body["text"]
    assert "reply_markup" in body


@pytest.mark.asyncio
async def test_plan_review_message_contains_buttons() -> None:
    """The inline keyboard should have Approve and Reject buttons."""
    with respx.mock:
        respx.post(re.compile(r".*/bot.*/getMe")).mock(
            return_value=httpx.Response(200, json={
                "ok": True,
                "result": {
                    "id": 123456789, "is_bot": True,
                    "first_name": "MockBot", "username": "mock_bot",
                },
            }),
        )
        send_route = respx.post(re.compile(r".*/bot.*/sendMessage")).mock(
            return_value=httpx.Response(200, json={
                "ok": True,
                "result": {
                    "message_id": 42,
                    "chat": {"id": "12345", "type": "private"},
                    "text": "", "date": 1700000000,
                },
            }),
        )
        respx.route(url__regex=r".*/bot.*").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": True},
            ),
        )

        notifier = TelegramNotifier(
            bot_token="test-bot-token", chat_id="12345",
        )
        await notifier.notify_plan_for_review(
            "plan_xyz", "Plan: Improve logging", "f00dfeed" * 8,
        )

        assert send_route.called
        body = _parse_telegram_body(send_route.calls.last.request)
        markup_str = body.get("reply_markup", "")
        if isinstance(markup_str, str):
            markup = json.loads(markup_str)
        else:
            markup = markup_str

        keyboard = markup["inline_keyboard"]
        assert len(keyboard) == 1  # one row
        buttons = keyboard[0]
        assert len(buttons) == 2
        assert buttons[0]["text"] == "Approve"
        assert buttons[0]["callback_data"] == "approve:plan_xyz:f00dfeedf00dfeed"
        assert buttons[1]["text"] == "Reject"
        assert buttons[1]["callback_data"] == "reject:plan_xyz:f00dfeedf00dfeed"

        assert notifier._plan_messages["plan_xyz"] == 42
