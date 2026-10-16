"""Tests for the dashboard CSRF middleware (proposal 022).

Double-submit contract: cookie-authenticated mutating requests must echo
the JS-readable ``maestro_csrf`` cookie in the ``X-CSRF-Token`` header.
Bearer-header-authenticated requests are exempt (header credentials are
attached explicitly per-request, never ambiently, so they cannot be
forged cross-site); an *invalid* bearer header does not exempt.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.dashboard.auth import COOKIE_NAME, CSRF_COOKIE_NAME
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner

FAKE_DIR = Path("/fake/csrf")
SECRET = "csrf-test-secret-token"
CSRF_TOKEN = "csrf-challenge-value-123"


def _make_app(mem_fs: dict[str, str], *, token: str | None = SECRET):  # type: ignore[no-untyped-def]
    from maestro.agent import MaestroAgent
    from maestro.dashboard.app import create_app

    config = Config(memory_dir=FAKE_DIR, workspace=FAKE_DIR, dashboard_token=token)
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
    return create_app(agent), agent


def _seed_pending_plan(agent, plan_id: str = "pcsrf") -> str:  # type: ignore[no-untyped-def]
    """Seed a pending plan and return its 021 content hash.

    Tests that expect to reach the approve route with a 200 must echo
    the hash in the request body (the route verifies it before acting).
    """
    agent.memory.save_evolution_plan(EvolutionPlan(
        plan_id=plan_id, title="CSRF Test Plan",
        category="capability", rationale="test", proposed_changes=[],
        files_affected=[], expected_impact="",
        created_at="2025-01-01T00:00:00Z",
    ))
    saved = agent.memory.get_plan(plan_id)
    assert saved is not None
    return saved.plan_content_hash


# ---------------------------------------------------------------------------
# Cookie-authenticated mutations require the double-submit header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cookie_post_without_csrf_header_is_403(mem_fs: dict[str, str]) -> None:
    """Valid session cookie but no X-CSRF-Token header → 403."""
    app, agent = _make_app(mem_fs)
    _seed_pending_plan(agent)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pcsrf/approve",
            cookies={COOKIE_NAME: SECRET, CSRF_COOKIE_NAME: CSRF_TOKEN},
        )
    assert r.status_code == 403
    assert r.json() == {"error": "CSRF token missing or invalid"}
    plan = agent.memory.get_plan("pcsrf")
    assert plan is not None
    assert plan.status == "pending_review"


@pytest.mark.asyncio
async def test_cookie_post_with_matching_csrf_succeeds(mem_fs: dict[str, str]) -> None:
    """Matching header + cookie passes the middleware and reaches the route."""
    app, agent = _make_app(mem_fs)
    content_hash = _seed_pending_plan(agent)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pcsrf/approve",
            cookies={COOKIE_NAME: SECRET, CSRF_COOKIE_NAME: CSRF_TOKEN},
            headers={"X-CSRF-Token": CSRF_TOKEN},
            json={"content_hash": content_hash},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_cookie_post_with_mismatched_csrf_is_403(mem_fs: dict[str, str]) -> None:
    app, agent = _make_app(mem_fs)
    _seed_pending_plan(agent)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pcsrf/approve",
            cookies={COOKIE_NAME: SECRET, CSRF_COOKIE_NAME: CSRF_TOKEN},
            headers={"X-CSRF-Token": "some-other-value"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_header_without_csrf_cookie_is_403(mem_fs: dict[str, str]) -> None:
    """The header alone is not enough — the cookie half must exist too."""
    app, agent = _make_app(mem_fs)
    _seed_pending_plan(agent)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pcsrf/approve",
            cookies={COOKIE_NAME: SECRET},
            headers={"X-CSRF-Token": CSRF_TOKEN},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_messages_post_requires_csrf(mem_fs: dict[str, str]) -> None:
    """The CSRF guard covers every mutating route, not just approvals."""
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/messages",
            cookies={COOKIE_NAME: SECRET},
            json={"content": "hello"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_income_post_requires_csrf(mem_fs: dict[str, str]) -> None:
    """The operator income write sits behind the same CSRF guard."""
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/income",
            cookies={COOKIE_NAME: SECRET},
            json={"amount_usd": 1.0, "kind": "donation", "source_label": "x"},
        )
    assert r.status_code == 403



@pytest.mark.asyncio
async def test_intake_post_requires_csrf(mem_fs: dict[str, str]) -> None:
    """The operator intake write sits behind the same CSRF guard."""
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/intake",
            cookies={COOKIE_NAME: SECRET},
            json={"offering": "audit", "contact": "a@b.c"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_intake_status_post_requires_csrf(mem_fs: dict[str, str]) -> None:
    """The intake status move sits behind the same CSRF guard."""
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/intake/abc123/status",
            cookies={COOKIE_NAME: SECRET},
            json={"status": "quoted"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_quote_decision_requires_csrf(
    mem_fs: dict[str, str], decision: str,
) -> None:
    """The quote decision routes sit behind the same CSRF guard (052)."""
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            f"/api/quotes/q-x/{decision}",
            cookies={COOKIE_NAME: SECRET},
            json={},
        )
    assert r.status_code == 403

# ---------------------------------------------------------------------------
# Bearer-header exemption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_bearer_header_is_exempt_from_csrf(mem_fs: dict[str, str]) -> None:
    """API clients authenticating per-request cannot be CSRF'd — no check."""
    app, agent = _make_app(mem_fs)
    content_hash = _seed_pending_plan(agent)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pcsrf/approve",
            headers={"Authorization": f"Bearer {SECRET}"},
            json={"content_hash": content_hash},
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_invalid_bearer_with_valid_cookie_still_requires_csrf(
    mem_fs: dict[str, str],
) -> None:
    """A bogus bearer header must not bypass CSRF for a cookie-backed session."""
    app, agent = _make_app(mem_fs)
    _seed_pending_plan(agent)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pcsrf/approve",
            cookies={COOKIE_NAME: SECRET},
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Exemptions: safe methods, public paths, /logout, static prefix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_requests_skip_csrf(mem_fs: dict[str, str]) -> None:
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/status", cookies={COOKIE_NAME: SECRET})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_login_post_is_exempt_from_csrf(mem_fs: dict[str, str]) -> None:
    """No CSRF cookie exists before login — the login POST must not require one."""
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.post("/login", data={"token": SECRET})
    assert r.status_code == 303
    assert r.headers["location"] == "/account"


@pytest.mark.asyncio
async def test_logout_post_is_exempt_from_csrf(mem_fs: dict[str, str]) -> None:
    """Plain HTML forms cannot attach headers; a forged logout is a nuisance."""
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.post("/logout", cookies={COOKIE_NAME: SECRET})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_static_prefix_is_exempt_from_csrf(mem_fs: dict[str, str]) -> None:
    """/static/* POSTs fall through to StaticFiles (405), never a CSRF 403."""
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/static/index.js", cookies={COOKIE_NAME: SECRET})
    assert r.status_code == 405


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_ascii_csrf_header_returns_403_not_500(mem_fs: dict[str, str]) -> None:
    """A non-ASCII X-CSRF-Token must yield 403, not a TypeError/500."""
    app, agent = _make_app(mem_fs)
    _seed_pending_plan(agent)

    response_data: dict[str, object] = {}

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            response_data["status"] = message["status"]

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/plans/pcsrf/approve",
        "raw_path": b"/api/plans/pcsrf/approve",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
        "headers": [
            (
                b"cookie",
                f"{COOKIE_NAME}={SECRET}; {CSRF_COOKIE_NAME}=abc".encode("latin-1"),
            ),
            # latin-1 'café' — str.compare_digest raises TypeError on non-ASCII
            (b"x-csrf-token", b"caf\xe9"),
            (b"host", b"testserver"),
        ],
        "app": app,
    }

    await app(scope, receive, send)
    assert response_data.get("status") == 403


def test_csrf_middleware_rejects_empty_token() -> None:
    from maestro.dashboard.auth import CSRFMiddleware

    with pytest.raises(ValueError, match="must not be empty"):
        CSRFMiddleware(MagicMock(), token="   ")


@pytest.mark.asyncio
async def test_no_csrf_enforcement_when_auth_disabled(mem_fs: dict[str, str]) -> None:
    """Without a dashboard token there is no session for CSRF to ride."""
    app, agent = _make_app(mem_fs, token=None)
    content_hash = _seed_pending_plan(agent)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/plans/pcsrf/approve", json={"content_hash": content_hash},
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_crafted_host_header_does_not_exempt_from_csrf(
    mem_fs: dict[str, str],
) -> None:
    """#4305: ``Host: x/login?`` must not make a mutation look like ``/login``."""
    app, _agent = _make_app(mem_fs)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/messages",
            cookies={COOKIE_NAME: SECRET},
            headers={"Host": "x/login?"},
            json={"content": "hello"},
        )
    assert r.status_code == 403
