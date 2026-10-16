"""Tests for the Identity (consciousness) section of the dashboard."""

from __future__ import annotations

from typing import Any


def test_identity_statements_rendered(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["consciousness"]["identity_statements"] = [
        "a learning system",
        "an evolving agent",
    ]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    section = page.locator("#identity")
    assert section.locator(".card-title", has_text="I am...").is_visible()
    items = section.locator(".identity-list li").all_text_contents()
    assert "a learning system" in items
    assert "an evolving agent" in items


def test_values_rendered(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["consciousness"]["values"] = ["honesty", "curiosity"]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    section = page.locator("#identity")
    assert section.locator(".card-title", has_text="Values").is_visible()
    items = section.locator(".identity-list").nth(0).locator("li").all_text_contents()
    assert "honesty" in items
    assert "curiosity" in items


def test_open_questions_rendered(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["consciousness"]["open_questions"] = ["Which revenue stream pays first?"]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    section = page.locator("#identity")
    assert section.locator(".card-title", has_text="Open Questions").is_visible()
    assert "Which revenue stream pays first?" in (section.text_content() or "")


def test_capabilities_rendered(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["consciousness"]["capabilities_known"] = ["code generation", "self-reflection"]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    section = page.locator("#identity")
    assert section.locator(".card-title", has_text="Capabilities").is_visible()
    assert "code generation" in (section.text_content() or "")
    assert "self-reflection" in (section.text_content() or "")


def test_limitations_rendered(
    page: Any,
    dashboard_server: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> None:
    url, data, _ = dashboard_server
    data["consciousness"]["limitations_known"] = ["no persistent memory between sessions"]
    page.goto(url)
    page.wait_for_load_state("networkidle")

    section = page.locator("#identity")
    assert section.locator(".card-title", has_text="Limitations").is_visible()
    assert "no persistent memory between sessions" in (section.text_content() or "")


def test_empty_identity(dashboard_page: tuple[Any, str, dict[str, Any], list[dict[str, Any]]]) -> None:
    page, *_ = dashboard_page
    assert page.locator("#identity .empty", has_text="No identity data yet").is_visible()
