"""Tests for tab-style navigation: clicking a nav link shows only one section."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "maestro" / "dashboard"
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _create_unmodified_app() -> FastAPI:
    """Mock app that serves the dashboard HTML *without* the test-only style override.

    The shared ``conftest._create_mock_app`` injects ``.tab-panel { display: block }``
    so existing tests that target multiple sections still work. These tests need
    the production CSS to actually verify show/hide behaviour, so we serve the
    HTML untouched here.
    """
    app = FastAPI()

    # Mount the dashboard HTML at both / and /account so these tab-nav tests
    # can navigate directly with ``page.goto(tabs_server)`` while /account
    # stays available for any future test that wants the production path.
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(TEMPLATES_DIR.joinpath("index.html").read_text())

    @app.get("/account", response_class=HTMLResponse)
    async def account() -> HTMLResponse:
        return HTMLResponse(TEMPLATES_DIR.joinpath("index.html").read_text())

    empty_payloads: dict[str, Any] = {
        "/api/status": {
            "running": False,
            "current_task": "",
            "stage": "awakening",
            "evolution_count": 0,
            "awakened_at": "2025-01-01T00:00:00Z",
        },
        "/api/metrics": {
            "success_rate": 0.0,
            "total_tasks": 0,
            "evolution_count": 0,
            "stage": "awakening",
            "total_plans": 0,
            "pending_plans": 0,
        },
        "/api/consciousness": {
            "identity_statements": [],
            "values": [],
            "open_questions": [],
            "capabilities_known": [],
            "limitations_known": [],
            "stage": "awakening",
            "evolution_count": 0,
            "awakened_at": "2025-01-01T00:00:00Z",
        },
        "/api/diagnostics": {
            "recent_error_count": 0,
            "has_quota_warning": False,
            "has_rate_limit": False,
            "consecutive_failures": 0,
            "summary": "",
            "sdk_usage": None,
        },
    }
    for path, payload in empty_payloads.items():
        app.add_api_route(path, lambda payload=payload: JSONResponse(payload))

    for path in (
        "/api/plans", "/api/journal", "/api/tasks", "/api/logs",
        "/api/messages", "/api/versions",
    ):
        app.add_api_route(path, lambda: JSONResponse([]))
    app.add_api_route("/api/logs/sdk-sessions", lambda: JSONResponse([]))
    app.add_api_route("/api/journal/narrative", lambda: JSONResponse({"narrative": ""}))
    app.add_api_route("/api/cost-rates", lambda: JSONResponse({
        "window_start": "2025-06-01T11:00:00+00:00",
        "window_end": "2025-06-01T12:00:00+00:00",
        "window_seconds": 3600.0,
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_calls": 0,
        "cost_per_hour": 0.0,
        "cost_per_day": 0.0,
        "cost_per_month": 0.0,
        "tokens_in_per_hour": 0.0,
        "tokens_out_per_hour": 0.0,
        "by_caller": {},
    }))
    app.add_api_route("/api/budget", lambda: JSONResponse({
        "window_start": "2025-06-01T11:00:00+00:00",
        "window_end": "2025-06-01T12:00:00+00:00",
        "window_hours": 1.0,
        "total_cost_usd": 0.0,
        "reported_cost_usd": 0.0,
        "estimated_cost_usd": 0.0,
        "total_calls": 0,
        "by_caller": [],
        "by_group": [],
    }))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


@pytest.fixture()
def tabs_server() -> Generator[str]:
    app = _create_unmodified_app()
    port = _find_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


_TAB_NAMES = ["diagnostics", "identity", "plans", "journal", "tasks", "messages", "budget", "versions", "sessions", "logs"]


def _visible_tabs(page: Any) -> list[str]:
    visible: list[str] = []
    for name in _TAB_NAMES:
        panel = page.locator(f'.tab-panel[data-tab="{name}"]')
        if panel.is_visible():
            visible.append(name)
    return visible


def test_default_tab_is_identity(page: Any, tabs_server: str) -> None:
    page.goto(tabs_server)
    page.wait_for_load_state("networkidle")
    assert _visible_tabs(page) == ["identity"]


def test_clicking_logs_shows_only_logs(page: Any, tabs_server: str) -> None:
    page.goto(tabs_server)
    page.wait_for_load_state("networkidle")
    page.locator('.nav-link[data-nav="logs"]').click()
    page.wait_for_timeout(100)
    assert _visible_tabs(page) == ["logs"]


def test_clicking_each_tab_shows_only_that_tab(page: Any, tabs_server: str) -> None:
    page.goto(tabs_server)
    page.wait_for_load_state("networkidle")
    for name in _TAB_NAMES:
        page.locator(f'.nav-link[data-nav="{name}"]').click()
        page.wait_for_timeout(100)
        assert _visible_tabs(page) == [name], f"expected only {name} visible"
        active = page.locator(".nav-link.active")
        assert active.count() == 1
        assert active.get_attribute("data-nav") == name


def test_loading_with_section_hash_shows_that_tab(page: Any, tabs_server: str) -> None:
    page.goto(f"{tabs_server}#section-journal")
    page.wait_for_load_state("networkidle")
    assert _visible_tabs(page) == ["journal"]


def test_invalid_hash_falls_back_to_default(page: Any, tabs_server: str) -> None:
    page.goto(f"{tabs_server}#section-bogus")
    page.wait_for_load_state("networkidle")
    assert _visible_tabs(page) == ["identity"]
