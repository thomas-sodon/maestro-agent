"""Tests for ``SecurityHeadersMiddleware``.

Headers must appear on every response regardless of status or route — and
must vanish entirely when the feature is opted out via config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner

FAKE_DIR = Path("/fake/sec")


REQUIRED_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
    # Permissions-Policy and CSP existence-checked (full string elsewhere).
}


def _make_app(
    mem_fs: dict[str, str],
    *,
    token: str | None = None,
    enabled: bool = True,
) -> Any:
    from maestro.agent import MaestroAgent
    from maestro.dashboard.app import create_app

    config = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        dashboard_token=token,
        security_headers_enabled=enabled,
    )
    memory = Memory(FAKE_DIR)
    consciousness = Consciousness(FAKE_DIR)
    journal = Journal(FAKE_DIR)
    notifier = NullNotifier()
    executor = Executor()
    planner = Planner()
    evolution = EvolutionEngine(
        memory, consciousness, journal, notifier, config=config,
    )
    agent = MaestroAgent(
        config=config, consciousness=consciousness, journal=journal,
        memory=memory, executor=executor, planner=planner,
        evolution=evolution, notifier=notifier,
    )
    return create_app(agent)


def _assert_headers(resp: Any) -> None:
    for key, value in REQUIRED_HEADERS.items():
        assert resp.headers.get(key) == value, f"{key} header wrong/missing"
    assert "interest-cohort=()" in resp.headers.get("permissions-policy", "")
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" in csp  # style-src keeps inline styles working
    assert "frame-ancestors 'none'" in csp


@pytest.mark.asyncio
async def test_headers_on_public_200(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.status_code == 200
    _assert_headers(r)


@pytest.mark.asyncio
async def test_headers_on_authed_200(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token="test-tok")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get(
            "/api/status", headers={"Authorization": "Bearer test-tok"},
        )
    assert r.status_code == 200
    _assert_headers(r)


@pytest.mark.asyncio
async def test_headers_on_401(mem_fs: dict[str, str]) -> None:
    """Even unauthenticated responses need the headers — this is the
    outermost-middleware invariant from the plan."""
    app = _make_app(mem_fs, token="test-tok")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/status")
    assert r.status_code == 401
    _assert_headers(r)


@pytest.mark.asyncio
async def test_headers_on_login_page(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token="test-tok")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/login")
    assert r.status_code == 200
    _assert_headers(r)


@pytest.mark.asyncio
async def test_headers_on_static_asset(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/static/public.js")
    assert r.status_code == 200
    _assert_headers(r)


@pytest.mark.asyncio
async def test_disabled_config_removes_headers(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, enabled=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.status_code == 200
    assert "content-security-policy" not in {k.lower() for k in r.headers}
    assert "permissions-policy" not in {k.lower() for k in r.headers}
