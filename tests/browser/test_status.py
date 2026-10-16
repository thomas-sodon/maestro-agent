"""Tests for the Status and Metrics sections of the dashboard."""

from __future__ import annotations

from typing import Any


def test_status_dot_idle(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    dot = page.locator(".status-dot")
    assert dot.is_visible()
    assert "idle" in (dot.get_attribute("class") or "")


def test_status_dot_active(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["status"]["running"] = True
    data["status"]["current_task"] = "thinking deeply"
    page.goto(url)
    page.wait_for_load_state("networkidle")

    dot = page.locator(".status-dot")
    assert "active" in (dot.get_attribute("class") or "")
    assert "Running" in (page.locator("#status").text_content() or "")
    assert "thinking deeply" in (page.locator("#status").text_content() or "")


def test_no_stage_badge(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    assert page.locator(".stage-badge").count() == 0


def test_evolution_count_displayed(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    status_text = page.locator("#status").text_content() or ""
    assert "Evolved 0 times" in status_text


def test_awakened_date_displayed(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    status_text = page.locator("#status").text_content() or ""
    assert "Alive since 2025-01-01" in status_text


def test_metrics_success_rate(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["metrics"]["success_rate"] = 0.85
    data["metrics"]["total_tasks"] = 20
    data["metrics"]["evolution_count"] = 3
    data["metrics"]["pending_plans"] = 1
    page.goto(url)
    page.wait_for_load_state("networkidle")

    metrics_text = page.locator("#metrics").text_content() or ""
    assert "85%" in metrics_text
    assert "20" in metrics_text
    assert "3" in metrics_text
    assert "1" in metrics_text


def test_metrics_stat_labels(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    labels = page.locator(".stat-label").all_text_contents()
    assert "Success Rate" in labels
    assert "Tasks" in labels
    assert "Evolutions" in labels
    assert "Pending" in labels
