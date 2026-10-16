"""Tests for auto-refresh and dynamic update behavior."""

from __future__ import annotations

from typing import Any

from tests.browser.conftest import make_journal_entry, make_log_entry, make_task_record


def test_refresh_function_exists(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    assert page.evaluate("typeof refresh === 'function'") is True
    assert page.evaluate("typeof refreshLogs === 'function'") is True


def test_manual_refresh_updates_journal(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Initially no journal entries
    assert page.locator("#journal .empty").is_visible()

    # Add a journal entry to mock data
    data["journal"].append(make_journal_entry("A new thought", entry_type="reflection"))

    # Trigger manual refresh
    page.evaluate("refresh()")
    page.wait_for_timeout(500)

    # Journal should now show the entry
    assert page.locator("#journal .journal-entry").count() == 1
    assert "A new thought" in (page.locator("#journal").text_content() or "")


def test_manual_refresh_updates_tasks(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    page.goto(url)
    page.wait_for_load_state("networkidle")

    assert page.locator("#tasks .empty").is_visible()

    data["tasks"].append(make_task_record("Execute the plan", outcome="success"))

    page.evaluate("refresh()")
    page.wait_for_timeout(500)

    assert page.locator("#tasks .task-row").count() == 1
    assert "Execute the plan" in (page.locator("#tasks").text_content() or "")


def test_manual_refresh_logs_updates(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Add log entries
    data["logs"].append(make_log_entry("new log message", level="INFO"))

    # Trigger log refresh
    page.evaluate("refreshLogs()")
    page.wait_for_timeout(500)

    assert page.locator("#logs .log-entry").count() == 1
    assert "new log message" in (page.locator("#logs").text_content() or "")


def test_status_updates_on_refresh(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    page.goto(url)
    page.wait_for_load_state("networkidle")

    # Initially idle
    assert "Idle" in (page.locator("#status").text_content() or "")

    # Change status to running
    data["status"]["running"] = True
    data["status"]["current_task"] = "evolving"

    page.evaluate("refresh()")
    page.wait_for_timeout(500)

    status_text = page.locator("#status").text_content() or ""
    assert "Running" in status_text
    assert "evolving" in status_text


def test_journal_entry_type_badge(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["journal"] = [
        make_journal_entry("Thought 1", entry_type="reflection"),
        make_journal_entry("Thought 2", entry_type="question"),
        make_journal_entry("Thought 3", entry_type="milestone"),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    assert page.locator(".journal-type.type-reflection").count() >= 1
    assert page.locator(".journal-type.type-question").count() >= 1
    assert page.locator(".journal-type.type-milestone").count() >= 1


def test_journal_source_toggle_shows_events(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    """The Events toggle swaps the inner-thought journal for operational records."""
    url, data, _ = dashboard_server
    data["journal"] = [make_journal_entry("An inner thought", triggered_by="contemplation")]
    data["journal_events"] = [
        make_journal_entry("Pre-deploy notice", entry_type="milestone", triggered_by="pre_deploy"),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.locator('.nav-link[data-nav="journal"]').click()

    journal = page.locator("#journal")
    assert "An inner thought" in (journal.text_content() or "")
    assert "Pre-deploy notice" not in (journal.text_content() or "")

    page.locator('[data-journal-source="events"]').click()
    page.wait_for_function(
        "document.getElementById('journal').textContent.includes('Pre-deploy notice')",
    )
    assert "An inner thought" not in (journal.text_content() or "")
    events_btn_class = page.locator('[data-journal-source="events"]').get_attribute("class")
    assert "active" in (events_btn_class or "")

    data["journal_events"] = []
    page.locator('[data-journal-source="thoughts"]').click()
    page.wait_for_function(
        "document.getElementById('journal').textContent.includes('An inner thought')",
    )
    page.locator('[data-journal-source="events"]').click()
    page.wait_for_function(
        "document.getElementById('journal').textContent.includes('No journal events recorded yet.')",
    )


def test_task_outcome_styling(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["tasks"] = [
        make_task_record("Success task", outcome="success"),
        make_task_record("Failed task", outcome="failure"),
        make_task_record("Partial task", outcome="partial"),
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    assert page.locator(".outcome-success").count() >= 1
    assert page.locator(".outcome-failure").count() >= 1
    assert page.locator(".outcome-partial").count() >= 1
