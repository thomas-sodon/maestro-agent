"""Tests for Dashboard bearer-token and cookie authentication middleware."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.dashboard.auth import COOKIE_NAME, CSRF_COOKIE_NAME
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner

FAKE_DIR = Path("/fake/auth")
SECRET = "test-secret-token-42"


def _make_app(mem_fs: dict[str, str], *, token: str | None = None):  # type: ignore[no-untyped-def]
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
    return create_app(agent)


@pytest.mark.asyncio
async def test_health_bypasses_auth(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_api_returns_401_without_token(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/status")
    assert r.status_code == 401
    assert r.json() == {"error": "Unauthorized"}


@pytest.mark.asyncio
async def test_api_returns_401_with_wrong_token(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(
            "/api/status", headers={"Authorization": "Bearer wrong-token"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_api_succeeds_with_correct_token(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(
            "/api/status", headers={"Authorization": f"Bearer {SECRET}"},
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_api_succeeds_with_lowercase_bearer_scheme(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(
            "/api/status", headers={"Authorization": f"bearer {SECRET}"},
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_no_auth_when_no_token(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/status")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_401_responses_include_www_authenticate_header() -> None:
    """RFC 7235 §4.1: every 401 response MUST include a WWW-Authenticate header."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from maestro.dashboard.auth import BearerAuthMiddleware

    def homepage(request):  # type: ignore[no-untyped-def]
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/api/status", homepage)])
    app.add_middleware(BearerAuthMiddleware, token=SECRET)

    # Test 1: no Authorization header (wrong scheme path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/status")
    assert r.status_code == 401
    assert "www-authenticate" in r.headers, "Missing WWW-Authenticate on missing-auth 401"

    # Test 2: correct scheme but wrong credentials
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/status", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401
    assert "www-authenticate" in r.headers, "Missing WWW-Authenticate on bad-credentials 401"


@pytest.mark.asyncio
async def test_non_ascii_auth_header_returns_401_not_500(mem_fs: dict[str, str]) -> None:
    """A non-ASCII Authorization header must yield 401, not a 500/TypeError."""
    app = _make_app(mem_fs, token=SECRET)

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
        "method": "GET",
        "path": "/api/status",
        "raw_path": b"/api/status",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
        # Non-ASCII bytes in Authorization header (latin-1 encoded 'café123')
        "headers": [
            (b"authorization", b"Bearer caf\xe9123"),
            (b"host", b"testserver"),
        ],
        "app": app,
    }

    # Before the fix: raises TypeError and propagates as unhandled exception
    # After the fix: returns 401 cleanly
    await app(scope, receive, send)
    assert response_data.get("status") == 401, (
        f"Expected 401 for non-ASCII Authorization header, got {response_data.get('status')}"
    )


# ---------------------------------------------------------------------------
# Whitespace-only token rejection
# ---------------------------------------------------------------------------


def test_empty_token_rejected() -> None:
    """Empty string token raises ValueError."""
    from maestro.dashboard.auth import BearerAuthMiddleware

    with pytest.raises(ValueError, match="must not be empty"):
        BearerAuthMiddleware(MagicMock(), token="")


def test_whitespace_only_token_rejected() -> None:
    """A token of only spaces should raise ValueError."""
    from maestro.dashboard.auth import BearerAuthMiddleware

    with pytest.raises(ValueError, match="must not be empty"):
        BearerAuthMiddleware(MagicMock(), token="   ")


def test_tab_only_token_rejected() -> None:
    """A token of only tabs should raise ValueError."""
    from maestro.dashboard.auth import BearerAuthMiddleware

    with pytest.raises(ValueError, match="must not be empty"):
        BearerAuthMiddleware(MagicMock(), token="\t\t")


def test_newline_only_token_rejected() -> None:
    """A token of only newlines should raise ValueError."""
    from maestro.dashboard.auth import BearerAuthMiddleware

    with pytest.raises(ValueError, match="must not be empty"):
        BearerAuthMiddleware(MagicMock(), token="\n")


def test_valid_token_with_surrounding_whitespace_accepted() -> None:
    """A token with actual content plus whitespace should be accepted and stripped."""
    from maestro.dashboard.auth import BearerAuthMiddleware

    middleware = BearerAuthMiddleware(MagicMock(), token=" secret ")
    assert middleware._token == "secret"


def test_whitespace_padded_token_still_authenticates() -> None:
    """Token with whitespace padding should authenticate correctly."""
    from fastapi import FastAPI

    from maestro.dashboard.auth import BearerAuthMiddleware

    app = FastAPI()
    app.add_middleware(BearerAuthMiddleware, token="  secret  ")

    @app.get("/test")
    async def test_endpoint() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/test", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Cookie authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cookie_auth_succeeds_with_valid_token(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/status", cookies={COOKIE_NAME: SECRET})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_cookie_auth_fails_with_wrong_token(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/status", cookies={COOKIE_NAME: "wrong"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_cookie_auth_non_ascii_returns_401(mem_fs: dict[str, str]) -> None:
    """Non-ASCII cookie value must yield 401, not 500."""
    app = _make_app(mem_fs, token=SECRET)

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
        "method": "GET",
        "path": "/api/status",
        "raw_path": b"/api/status",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
        "headers": [
            (b"cookie", b"maestro_token=caf\xe9123"),
            (b"host", b"testserver"),
        ],
        "app": app,
    }

    await app(scope, receive, send)
    assert response_data.get("status") == 401


@pytest.mark.asyncio
async def test_cookie_auth_survives_unrelated_malformed_cookie(
    mem_fs: dict[str, str],
) -> None:
    """A valid session cookie survives an unrelated malformed cookie segment.

    ``@bad=1`` is an illegal cookie name that the old ``SimpleCookie.load()``
    rejected with ``CookieError`` — dropping the *entire* header and locking
    out the otherwise-valid ``maestro_token`` morsel (401). Starlette's lenient
    parser skips only the bad segment, so the valid session authenticates (200).
    """
    app = _make_app(mem_fs, token=SECRET)
    raw_cookie = f"@bad=1; {COOKIE_NAME}={SECRET}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/status", headers={"Cookie": raw_cookie})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Public paths (login, logout, health) bypass auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_page_bypasses_auth(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/login")
    assert r.status_code == 200
    assert "Maestro" in r.text
    # Same stale-HTML risk as / and /account — pin no-cache so a renamed
    # /static/login.{js,css} doesn't 404 against a cached shell.
    assert r.headers.get("cache-control") == "no-cache"


@pytest.mark.asyncio
async def test_unauthenticated_logout_redirects_to_login(mem_fs: dict[str, str]) -> None:
    """Unauthenticated POST to /logout is redirected to /login gracefully.

    /logout is no longer a public path — it requires a valid session.  But an
    unauthenticated caller has no session to invalidate, so the middleware
    redirects them to /login rather than returning a bare 401.
    """
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.post("/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_unauthenticated_logout_clears_stale_cookie(
    mem_fs: dict[str, str],
) -> None:
    """Unauthenticated POST to /logout clears a stale/rotated cookie.

    A browser holding a cookie that no longer matches the active token never
    reaches the authenticated logout handler (the middleware short-circuits),
    so the middleware's redirect must itself delete the session and CSRF
    cookies — otherwise the dead cookie is resubmitted on every request until
    it expires.
    """
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.post(
            "/logout", cookies={COOKIE_NAME: "stale-or-rotated-token"},
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # httpx collapses multiple Set-Cookie values, so read them individually.
    set_cookies = r.headers.get_list("set-cookie")
    session_deletions = [c for c in set_cookies if COOKIE_NAME in c]
    csrf_deletions = [c for c in set_cookies if CSRF_COOKIE_NAME in c]
    assert len(session_deletions) == 1
    assert len(csrf_deletions) == 1
    # Each cookie is deleted via max-age=0 or an expiry in the past.
    session_cookie = session_deletions[0].lower()
    csrf_cookie = csrf_deletions[0].lower()
    assert 'max-age=0' in session_cookie or '01 jan 1970' in session_cookie
    assert 'max-age=0' in csrf_cookie or '01 jan 1970' in csrf_cookie


# ---------------------------------------------------------------------------
# Smart 401 — browser redirect vs JSON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_401_redirects_to_login(mem_fs: dict[str, str]) -> None:
    """Browser navigation (Accept: text/html) to non-API path gets a redirect."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.get(
            "/account", headers={"Accept": "text/html,application/xhtml+xml"},
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_public_root_bypasses_auth(mem_fs: dict[str, str]) -> None:
    """GET / is public — auth middleware must not redirect or 401."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "Maestro" in r.text


@pytest.mark.asyncio
async def test_public_identity_bypasses_auth(mem_fs: dict[str, str]) -> None:
    """GET /api/public/identity is public — must return 200 with no token."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.status_code == 200
    assert "framing" in r.json()


@pytest.mark.asyncio
async def test_api_401_returns_json_not_redirect(mem_fs: dict[str, str]) -> None:
    """API requests still get JSON 401 even with Accept: text/html."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(
            "/api/status", headers={"Accept": "text/html,application/json"},
        )
    assert r.status_code == 401
    assert r.json() == {"error": "Unauthorized"}


@pytest.mark.asyncio
async def test_json_client_401_not_redirected(mem_fs: dict[str, str]) -> None:
    """Requests without text/html Accept get JSON 401."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.get("/account", headers={"Accept": "application/json"})
    assert r.status_code == 401
    assert r.json() == {"error": "Unauthorized"}


@pytest.mark.asyncio
async def test_unauthenticated_get_logout_falls_through_to_smart_401(
    mem_fs: dict[str, str],
) -> None:
    """Unauthenticated non-POST /logout is NOT caught by the POST-only 303.

    The graceful ``303 -> /login`` special case fires only for POST (there is
    no session to invalidate). A GET must fall through to the standard smart
    401: a browser (Accept: text/html) gets a 302 redirect to /login, while a
    JSON client gets a bare 401. A 303 here — or /logout re-added to
    ``_PUBLIC_PATHS`` — would mean the guard regressed.
    """
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        browser = await client.get(
            "/logout", headers={"Accept": "text/html,application/xhtml+xml"},
        )
        json_client = await client.get(
            "/logout", headers={"Accept": "application/json"},
        )
    # Browser GET falls through to the smart-401 browser branch (302), NOT the
    # POST-only 303 special case.
    assert browser.status_code == 302
    assert browser.headers["location"] == "/login"
    # JSON GET falls through to the smart-401 API branch (bare 401).
    assert json_client.status_code == 401
    assert json_client.json() == {"error": "Unauthorized"}


# ---------------------------------------------------------------------------
# Login / logout route tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_post_correct_token_sets_cookie(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.post("/login", data={"token": SECRET})
    assert r.status_code == 303
    assert r.headers["location"] == "/account"
    cookie_header = r.headers.get("set-cookie", "")
    assert COOKIE_NAME in cookie_header
    assert "httponly" in cookie_header.lower()
    assert "samesite=strict" in cookie_header.lower()


@pytest.mark.asyncio
async def test_login_post_wrong_token_redirects_with_error(
    mem_fs: dict[str, str],
) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.post("/login", data={"token": "wrong"})
    assert r.status_code == 303
    assert "error=1" in r.headers["location"]


@pytest.mark.asyncio
async def test_login_post_empty_token_redirects_with_error(
    mem_fs: dict[str, str],
) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.post("/login", data={"token": ""})
    assert r.status_code == 303
    assert "error=1" in r.headers["location"]


@pytest.mark.asyncio
async def test_authenticated_logout_clears_cookie(mem_fs: dict[str, str]) -> None:
    """Authenticated POST to /logout clears the session cookie."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.post("/logout", cookies={COOKIE_NAME: SECRET})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    cookie_header = r.headers.get("set-cookie", "")
    assert COOKIE_NAME in cookie_header
    # Cookie is deleted via max-age=0 or expires in the past
    assert 'max-age=0' in cookie_header.lower() or '01 jan 1970' in cookie_header.lower()


@pytest.mark.asyncio
async def test_login_cookie_grants_dashboard_access(mem_fs: dict[str, str]) -> None:
    """Full flow: login, then use cookie to access dashboard."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        # Login
        login_r = await client.post("/login", data={"token": SECRET})
        assert login_r.status_code == 303
        # Extract cookie from response and use it
        r = await client.get(
            "/api/status", cookies={COOKIE_NAME: SECRET},
        )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# No login routes when auth is disabled
# ---------------------------------------------------------------------------


def test_public_paths_exact_set() -> None:
    """_PUBLIC_PATHS must match exactly the whitelist.

    Exact enumeration prevents a footgun where a future `/api/public/foo`
    route is silently unauthenticated.  Any new public path must be added
    here *and* to `_PUBLIC_PATHS` together.
    """
    from maestro.dashboard.auth import _PUBLIC_PATHS, _PUBLIC_PREFIXES

    assert {"/health", "/login", "/", "/api/public/identity"} == _PUBLIC_PATHS
    # Prefix-scoped exemptions are tracked in a separate tuple so the
    # exact-match invariant above still holds. ``/static/`` is the only
    # one allowed — anything else requires a deliberate change + test
    # update, matching the spirit of _PUBLIC_PATHS discipline.
    assert _PUBLIC_PREFIXES == ("/static/",)


@pytest.mark.asyncio
async def test_static_prefix_bypasses_auth(mem_fs: dict[str, str]) -> None:
    """``/static/*`` is exempt via ``_PUBLIC_PREFIXES`` even with a token set."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/static/public.js")
    assert r.status_code == 200
    assert "javascript" in r.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_no_login_route_when_auth_disabled(mem_fs: dict[str, str]) -> None:
    """When no dashboard_token is set, /login should return 404."""
    app = _make_app(mem_fs, token=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/login")
    assert r.status_code in (404, 405)


# ---------------------------------------------------------------------------
# Proposal 032 — endpoints must require auth (defence in depth on top of
# the generic ``test_api_returns_401_without_token`` and the
# ``_PUBLIC_PATHS`` lock-down test).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/publications"),
        ("GET", "/api/publications/p-x"),
        ("POST", "/api/publications/p-x/approve"),
        ("POST", "/api/publications/p-x/reject"),
        ("GET", "/api/artifact-reviews"),
        ("GET", "/api/briefs"),
        ("POST", "/api/briefs/brief-x/pursue"),
        ("POST", "/api/briefs/brief-x/kill"),
        ("GET", "/api/quotes"),
        ("GET", "/api/quotes/summary"),
        ("POST", "/api/quotes/q-x/approve"),
        ("POST", "/api/quotes/q-x/reject"),
    ],
)
async def test_publication_endpoints_require_auth(
    mem_fs: dict[str, str], method: str, path: str,
) -> None:
    """Publication and artifact-review endpoints must return 401 unauthenticated."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.request(method, path)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# #4305 — a crafted Host header must not steer middleware path decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["x/login?", "x/static/?"])
async def test_crafted_host_header_cannot_reach_protected_route(
    mem_fs: dict[str, str], host: str,
) -> None:
    """``Host`` injects a path into ``request.url``; auth keys on the routed path."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/status", headers={"Host": host})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_crafted_host_header_on_logout_post_is_401_not_redirect(
    mem_fs: dict[str, str],
) -> None:
    """The unauthenticated-``/logout`` redirect branch keys on the routed path."""
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r = await client.post("/api/messages", headers={"Host": "x/logout?"})
    assert r.status_code == 401


def test_middlewares_never_read_url_path() -> None:
    """Structural guard: no dashboard module reads the Host-derived ``url.path``."""
    dashboard_dir = Path(__file__).resolve().parent.parent / "maestro" / "dashboard"
    sources = sorted(dashboard_dir.rglob("*.py"))
    assert sources
    offenders: list[str] = []
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "path"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "url"
            ):
                offenders.append(f"{source}:{node.lineno}")
    assert offenders == []



@pytest.mark.asyncio
async def test_income_post_requires_token(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post(
            "/api/income",
            json={"amount_usd": 1.0, "kind": "donation", "source_label": "x"},
        )
    assert denied.status_code == 401



@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/intake", {"offering": "audit", "contact": "a@b.c"}),
        ("/api/intake/abc123/status", {"status": "quoted"}),
    ],
)
async def test_intake_posts_require_token(
    mem_fs: dict[str, str], path: str, body: dict[str, str],
) -> None:
    app = _make_app(mem_fs, token=SECRET)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post(path, json=body)
    assert denied.status_code == 401
