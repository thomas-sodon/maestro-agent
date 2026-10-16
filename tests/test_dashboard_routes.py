"""Tests for dashboard login route secure cookie flag behavior.

Covers three cases from the evolution plan (fixes #272, #273):

1. Direct HTTPS connection — ``request.url.scheme == "https"`` → Secure flag set.
2. HTTP behind a TLS-terminating proxy with ``trust_forwarded_proto=True`` and
   ``X-Forwarded-Proto: https`` → Secure flag set.
3. HTTP behind a proxy with ``trust_forwarded_proto=False`` (default safe value)
   and ``X-Forwarded-Proto: https`` → Secure flag NOT set (header ignored).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.config import Config

FAKE_DIR = Path("/fake/routes")
SECRET = "route-test-secret-token"


def _make_app(
    *,
    token: str | None = SECRET,
    trust_forwarded_proto: bool = False,
) -> object:
    """Build a full dashboard ASGI app wired to an in-memory agent."""
    from maestro.agent import MaestroAgent
    from maestro.consciousness import Consciousness
    from maestro.dashboard.app import create_app
    from maestro.evolution import EvolutionEngine
    from maestro.executor import Executor
    from maestro.journal import Journal
    from maestro.memory import Memory
    from maestro.notifications.base import NullNotifier
    from maestro.planner import Planner

    config = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        dashboard_token=token,
        trust_forwarded_proto=trust_forwarded_proto,
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
        config=config,
        consciousness=consciousness,
        journal=journal,
        memory=memory,
        executor=executor,
        planner=planner,
        evolution=evolution,
        notifier=notifier,
    )
    return create_app(agent)


# ---------------------------------------------------------------------------
# Secure cookie flag — direct connections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_secure_flag_set_over_direct_https(mem_fs: dict[str, str]) -> None:
    """Cookie has Secure flag when the client connects directly over HTTPS."""
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        r = await client.post(
            "/login", data={"token": SECRET}, follow_redirects=False
        )
    assert r.status_code == 303
    set_cookie = r.headers.get("set-cookie", "")
    assert "secure" in set_cookie.lower(), (
        f"Expected Secure flag in Set-Cookie header, got: {set_cookie!r}"
    )


@pytest.mark.asyncio
async def test_login_no_secure_flag_over_direct_http(mem_fs: dict[str, str]) -> None:
    """Cookie does NOT have Secure flag when the client connects over plain HTTP."""
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login", data={"token": SECRET}, follow_redirects=False
        )
    assert r.status_code == 303
    set_cookie = r.headers.get("set-cookie", "")
    assert "secure" not in set_cookie.lower(), (
        f"Expected no Secure flag in Set-Cookie header, got: {set_cookie!r}"
    )


# ---------------------------------------------------------------------------
# Secure cookie flag — proxy-forwarded HTTPS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_secure_flag_via_proxy_trust_enabled(mem_fs: dict[str, str]) -> None:
    """Cookie has Secure flag when trust_forwarded_proto=True and X-Forwarded-Proto: https."""
    app = _make_app(trust_forwarded_proto=True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login",
            data={"token": SECRET},
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    set_cookie = r.headers.get("set-cookie", "")
    assert "secure" in set_cookie.lower(), (
        f"Expected Secure flag when trust_forwarded_proto=True and "
        f"X-Forwarded-Proto: https, got: {set_cookie!r}"
    )


@pytest.mark.asyncio
async def test_login_no_secure_flag_via_proxy_trust_disabled(mem_fs: dict[str, str]) -> None:
    """Cookie does NOT have Secure flag when trust_forwarded_proto=False (default).

    X-Forwarded-Proto: https must be ignored when trust is not enabled — this
    prevents header injection from untrusted clients from upgrading cookie security
    incorrectly (or from bypassing the secure=False signal in test environments).
    """
    app = _make_app(trust_forwarded_proto=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login",
            data={"token": SECRET},
            headers={"X-Forwarded-Proto": "https"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    set_cookie = r.headers.get("set-cookie", "")
    assert "secure" not in set_cookie.lower(), (
        f"Expected no Secure flag when trust_forwarded_proto=False, "
        f"got: {set_cookie!r}"
    )


@pytest.mark.asyncio
async def test_login_no_secure_flag_via_proxy_trust_enabled_but_http_proto(
    mem_fs: dict[str, str],
) -> None:
    """Cookie does NOT have Secure flag when proxy header indicates plain http."""
    app = _make_app(trust_forwarded_proto=True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login",
            data={"token": SECRET},
            headers={"X-Forwarded-Proto": "http"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    set_cookie = r.headers.get("set-cookie", "")
    assert "secure" not in set_cookie.lower(), (
        f"Expected no Secure flag when X-Forwarded-Proto: http, got: {set_cookie!r}"
    )


@pytest.mark.asyncio
async def test_login_secure_flag_via_proxy_multivalue_proto(
    mem_fs: dict[str, str],
) -> None:
    """Cookie has Secure flag when a chained proxy yields a multi-value proto.

    Regression for #321: with more than one proxy in the chain,
    ``X-Forwarded-Proto`` is comma-joined (e.g. ``"https, http"``). The raw
    exact-equality comparison ``proto == "https"`` then failed and the Secure
    flag was silently dropped even though the client connection is HTTPS and
    trust is explicitly enabled. The fix normalizes the header to its first
    segment before comparing.
    """
    app = _make_app(trust_forwarded_proto=True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login",
            data={"token": SECRET},
            headers={"X-Forwarded-Proto": "https, http"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    set_cookie = r.headers.get("set-cookie", "")
    assert "secure" in set_cookie.lower(), (
        f"Expected Secure flag when the first X-Forwarded-Proto segment is "
        f"https, got: {set_cookie!r}"
    )


def test_login_submit_with_stub_config_trust_forwarded_proto() -> None:
    """Stub config including trust_forwarded_proto does not cause AttributeError on login.

    Regression test for the defect where _config SimpleNamespace in the browser
    test fixture omitted trust_forwarded_proto, causing an AttributeError (and
    HTTP 500) instead of the expected 302 redirect after a successful login POST.
    """
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from maestro.consciousness import SelfModel
    from maestro.dashboard.routes import create_router

    _config = SimpleNamespace(
        rejection_reason_max=2000,
        max_page_size=500,
        log_buffer_size=500,
        app_log_path=Path("/tmp/maestro.log"),
        sdk_log_dir=Path("/tmp"),
        session_data_days=10,
        message_max_length=2000,
        trust_forwarded_proto=False,
    )
    _agent: object = SimpleNamespace(
        config=_config,
        message_store=None,
        consciousness=SimpleNamespace(load_self_model=lambda: SelfModel()),
        memory=SimpleNamespace(
            get_plan=lambda plan_id: None,
            update_plan_status=lambda *a, **kw: False,
            get_recent_tasks=lambda n: [],
            get_all_plans=lambda: [],
            get_pending_plans=lambda: [],
            task_count=lambda: 0,
            get_success_rate=lambda: 0.0,
        ),
        journal=SimpleNamespace(
            read_by_type=lambda entry_type: [],
            read_recent=lambda n: [],
            get_full_narrative=lambda: "",
        ),
        purpose=SimpleNamespace(
            get_active_hypotheses=lambda: [],
            get_leading_hypothesis=lambda: None,
        ),
    )
    token = "test-secret-token"
    app = FastAPI()
    router = create_router(_agent, dashboard_token=token)  # type: ignore[arg-type]
    app.include_router(router)
    client = TestClient(app, follow_redirects=False)
    response = client.post("/login", data={"token": token})
    assert response.status_code == 303, (
        f"Expected redirect (303) after valid login, got {response.status_code}. "
        "Likely caused by AttributeError on missing trust_forwarded_proto in _config."
    )


@pytest.mark.asyncio
async def test_login_wrong_token_does_not_set_cookie(mem_fs: dict[str, str]) -> None:
    """A wrong token redirects to /login?error=1 and sets no session cookie."""
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login", data={"token": "wrong-token"}, follow_redirects=False
        )
    assert r.status_code == 303
    assert "error=1" in r.headers.get("location", "")
    # No maestro_token cookie should be set on failure
    from maestro.dashboard.auth import COOKIE_NAME
    assert COOKIE_NAME not in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# POST-redirect-GET: verify 303 See Other (not 302) semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_success_uses_303_not_302(mem_fs: dict[str, str]) -> None:
    """Successful POST /login must redirect with 303 See Other, not 302 Found.

    HTTP 302 allows clients to change the method on redirect (POST → GET is
    undefined); 303 explicitly mandates GET on the redirected request.  Using
    303 is the correct 'redirect after POST' pattern per RFC 9110 §15.4.4.
    """
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login", data={"token": SECRET}, follow_redirects=False
        )
    assert r.status_code == 303, (
        f"Expected 303 See Other for POST /login success, got {r.status_code}"
    )
    assert r.headers.get("location") == "/account"


@pytest.mark.asyncio
async def test_login_failure_uses_303_not_302(mem_fs: dict[str, str]) -> None:
    """Failed POST /login must redirect with 303 See Other, not 302 Found."""
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login", data={"token": "wrong"}, follow_redirects=False
        )
    assert r.status_code == 303, (
        f"Expected 303 See Other for POST /login failure, got {r.status_code}"
    )
    assert "error=1" in r.headers.get("location", "")


@pytest.mark.asyncio
async def test_logout_uses_303_not_302(mem_fs: dict[str, str]) -> None:
    """POST /logout must redirect with 303 See Other, not 302 Found."""
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/logout", follow_redirects=False)
    assert r.status_code == 303, (
        f"Expected 303 See Other for POST /logout, got {r.status_code}"
    )
    assert r.headers.get("location") == "/login"


# ---------------------------------------------------------------------------
# CSRF cookie issuance on login (proposal 022)
# ---------------------------------------------------------------------------


def _csrf_set_cookie_header(set_cookie_headers: list[str]) -> str:
    """Return the Set-Cookie header for maestro_csrf, or '' if absent."""
    for header in set_cookie_headers:
        if header.startswith("maestro_csrf="):
            return header
    return ""


def _csrf_cookie_value(set_cookie_headers: list[str]) -> str:
    header = _csrf_set_cookie_header(set_cookie_headers)
    return header.split(";", 1)[0].removeprefix("maestro_csrf=")


@pytest.mark.asyncio
async def test_login_sets_readable_csrf_cookie(mem_fs: dict[str, str]) -> None:
    """Successful login sets maestro_csrf: SameSite=Strict, NOT HttpOnly.

    The cookie must stay JS-readable — the dashboard echoes it in the
    X-CSRF-Token header (double-submit); it is a challenge, not a secret.
    """
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login", data={"token": SECRET}, follow_redirects=False
        )
    assert r.status_code == 303
    header = _csrf_set_cookie_header(r.headers.get_list("set-cookie"))
    assert header, "Expected a maestro_csrf Set-Cookie header on login"
    assert "samesite=strict" in header.lower()
    assert "httponly" not in header.lower()
    assert len(_csrf_cookie_value(r.headers.get_list("set-cookie"))) > 20


@pytest.mark.asyncio
async def test_login_rotates_csrf_cookie(mem_fs: dict[str, str]) -> None:
    """Each successful login mints a fresh CSRF token."""
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.post(
            "/login", data={"token": SECRET}, follow_redirects=False
        )
        r2 = await client.post(
            "/login", data={"token": SECRET}, follow_redirects=False
        )
    v1 = _csrf_cookie_value(r1.headers.get_list("set-cookie"))
    v2 = _csrf_cookie_value(r2.headers.get_list("set-cookie"))
    assert v1 != ""
    assert v2 != ""
    assert v1 != v2


@pytest.mark.asyncio
async def test_login_failure_sets_no_csrf_cookie(mem_fs: dict[str, str]) -> None:
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login", data={"token": "wrong"}, follow_redirects=False
        )
    assert r.status_code == 303
    assert _csrf_set_cookie_header(r.headers.get_list("set-cookie")) == ""


@pytest.mark.asyncio
async def test_csrf_cookie_secure_flag_mirrors_session_cookie(
    mem_fs: dict[str, str],
) -> None:
    """Over HTTPS both cookies carry Secure; over HTTP neither does."""
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        r = await client.post(
            "/login", data={"token": SECRET}, follow_redirects=False
        )
    header = _csrf_set_cookie_header(r.headers.get_list("set-cookie"))
    assert "secure" in header.lower()


@pytest.mark.asyncio
async def test_csrf_cookie_not_secure_over_http(mem_fs: dict[str, str]) -> None:
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/login", data={"token": SECRET}, follow_redirects=False
        )
    header = _csrf_set_cookie_header(r.headers.get_list("set-cookie"))
    assert header != ""
    assert "secure" not in header.lower()


@pytest.mark.asyncio
async def test_logout_deletes_csrf_cookie(mem_fs: dict[str, str]) -> None:
    from maestro.dashboard.auth import COOKIE_NAME

    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/logout", cookies={COOKIE_NAME: SECRET}, follow_redirects=False
        )
    assert r.status_code == 303
    header = _csrf_set_cookie_header(r.headers.get_list("set-cookie"))
    assert header != "", "Expected a maestro_csrf deletion Set-Cookie header"
    assert (
        'max-age=0' in header.lower() or '01 jan 1970' in header.lower()
    )
