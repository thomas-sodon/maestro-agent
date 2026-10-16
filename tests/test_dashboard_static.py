"""Tests for ``/static/*`` serving and the auth exemption for static assets."""

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

FAKE_DIR = Path("/fake/static")


def _make_app(mem_fs: dict[str, str], *, token: str | None = None) -> Any:
    from maestro.agent import MaestroAgent
    from maestro.dashboard.app import create_app

    config = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        dashboard_token=token,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_type"),
    [
        ("/static/public.js", "javascript"),
        ("/static/public.css", "css"),
        ("/static/index.js", "javascript"),
        ("/static/index.css", "css"),
        ("/static/login.js", "javascript"),
        ("/static/login.css", "css"),
    ],
)
async def test_static_assets_served_without_auth(
    mem_fs: dict[str, str], path: str, expected_type: str,
) -> None:
    """Static assets bypass auth via _PUBLIC_PREFIXES even with a token set."""
    app = _make_app(mem_fs, token="test-secret-token")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get(path)
    assert r.status_code == 200, f"{path}: {r.status_code}"
    assert expected_type in r.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_static_path_traversal_rejected(mem_fs: dict[str, str]) -> None:
    """FastAPI StaticFiles refuses ``..`` escape (sent percent-encoded so httpx does not normalize it away)."""
    app = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/static/%2e%2e/auth.py")
    # Result may be 404, 403, or a redirect — just not a 200.
    assert r.status_code != 200


@pytest.mark.asyncio
async def test_missing_static_asset_returns_404(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/static/nonexistent.js")
    assert r.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/static/public.js",
        "/static/public.css",
        "/static/index.js",
        "/static/login.css",
    ],
)
async def test_static_assets_set_no_cache(
    mem_fs: dict[str, str], path: str,
) -> None:
    # Cloudflare's default rules cache CSS/JS aggressively when the
    # origin is silent on Cache-Control, which pins clients to old
    # assets across deploys. The matching HTML shell already sends
    # ``no-cache``; assets must too or a deploy is invisible until
    # the edge cache expires.
    app = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get(path)
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
