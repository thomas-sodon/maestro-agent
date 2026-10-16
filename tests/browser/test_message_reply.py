"""Dashboard Reply action threads an answer back to Maestro's question (#4154)."""

from __future__ import annotations

from typing import Any


def _question(message_id: str) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "timestamp": "2025-06-01T11:00:00Z",
        "sender": "maestro",
        "content": "Should I retry the plan?",
        "message_type": "question",
        "reply_to": None,
        "read": False,
    }


def _open_messages(page: Any, url: str) -> None:
    page.goto(f"{url}/account#messages")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("#messages .message-bubble")


def test_question_bubble_reply_posts_reply_to(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, post_log = dashboard_server
    data["messages"] = [_question("q-abc")]
    _open_messages(page, url)

    page.locator('.message-reply[data-reply-to="q-abc"]').click()
    assert page.locator("#compose-reply").is_visible()
    page.fill("#compose-content", "runner fixed, retry it")
    page.click("#compose-send")
    page.wait_for_function("() => document.getElementById('compose-reply').hidden")

    sends = [e for e in post_log if e["action"] == "send_message"]
    assert len(sends) == 1
    assert sends[0]["reply_to"] == "q-abc"
    assert sends[0]["message_type"] == "answer"


def test_plain_send_and_cancelled_reply_post_instruction(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, post_log = dashboard_server
    thomas = dict(_question("m0"), sender="thomas", message_type="instruction")
    data["messages"] = [_question("q-abc"), thomas]
    _open_messages(page, url)

    # Only Maestro's question bubble offers Reply.
    assert page.locator(".message-reply").count() == 1
    page.locator(".message-reply").click()
    page.click("#compose-reply-clear")
    assert page.locator("#compose-reply").is_hidden()
    page.fill("#compose-content", "hello")
    page.click("#compose-send")
    page.wait_for_function("() => document.getElementById('compose-content').value === ''")

    sends = [e for e in post_log if e["action"] == "send_message"]
    assert len(sends) == 1
    assert sends[0]["reply_to"] is None
    assert sends[0]["message_type"] == "instruction"
