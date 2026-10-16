"""Tests for ``PublicApiMiddleware`` — rate limiting, caching, method check."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.dashboard.auth import _PUBLIC_PATHS, _PUBLIC_PREFIXES
from maestro.dashboard.public_schemas import PublicIdentityResponse
from maestro.evolution import EvolutionEngine
from maestro.executor import Executor
from maestro.journal import Journal
from maestro.memory import Memory
from maestro.notifications.base import NullNotifier
from maestro.planner import Planner

FAKE_DIR = Path("/fake/pubmw")


def _make_app(
    mem_fs: dict[str, str],
    *,
    token: str | None = None,
    rate_limit: int = 60,
    cache_max_age: int = 30,
) -> Any:
    """Build a dashboard app with tunable public-API middleware knobs."""
    from maestro.agent import MaestroAgent
    from maestro.dashboard.app import create_app

    config = Config(
        memory_dir=FAKE_DIR,
        workspace=FAKE_DIR,
        dashboard_token=token,
        public_api_rate_limit_per_minute=rate_limit,
        public_api_cache_max_age_seconds=cache_max_age,
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


def _all_route_paths(routes: Any) -> set[str]:
    """Recursively collect every string ``path`` reachable from ``routes``.

    Starlette's ``include_router`` wraps mounted routers in an
    ``_IncludedRouter`` proxy (exposing the real routes via
    ``original_router.routes``) rather than flattening them into
    ``app.routes``, and ``Mount`` nests its children under ``.routes``.
    Descend into both so route-introspection sees the actual endpoints.
    """
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
        nested = getattr(route, "routes", None)
        if nested is None:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None)
        if nested:
            paths |= _all_route_paths(nested)
    return paths


# ---------------------------------------------------------------------------
# Method check — must run BEFORE rate-limit bookkeeping.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_to_public_endpoint_returns_405(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.post("/api/public/identity")
    assert r.status_code == 405
    assert r.headers.get("allow") == "GET"


@pytest.mark.asyncio
async def test_head_to_public_endpoint_returns_405_without_burning_rate_limit(
    mem_fs: dict[str, str],
) -> None:
    """HEAD must 405 with ``Allow: GET`` and not consume the budget.

    Locks the fix for #1473: the middleware previously advertised
    ``Allow: GET, HEAD`` and let HEAD pass the method check, charging the
    caller before the router replied 405. Mirror the bad-method shape so
    the rate-limit budget claim is exercised end-to-end for HEAD.
    """
    app = _make_app(mem_fs, rate_limit=2)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        for _ in range(10):
            r = await client.head("/api/public/identity")
            assert r.status_code == 405
            assert r.headers.get("allow") == "GET"
        ok1 = await client.get("/api/public/identity")
        ok2 = await client.get("/api/public/identity")
    assert ok1.status_code == 200
    assert ok2.status_code == 200


@pytest.mark.asyncio
async def test_bad_methods_do_not_consume_rate_limit_budget(
    mem_fs: dict[str, str],
) -> None:
    """A flood of 405s must not exhaust the budget for legitimate GETs."""
    # Tiny limit so we'd trip it fast if POSTs counted.
    app = _make_app(mem_fs, rate_limit=2)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        # 10 bad-method attempts.
        for _ in range(10):
            r = await client.post("/api/public/identity")
            assert r.status_code == 405
        # Legit GET still has its full budget — both should succeed.
        ok1 = await client.get("/api/public/identity")
        ok2 = await client.get("/api/public/identity")
    assert ok1.status_code == 200
    assert ok2.status_code == 200


# ---------------------------------------------------------------------------
# Cache-Control + weak ETag round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_identity_sets_cache_headers_and_etag(
    mem_fs: dict[str, str],
) -> None:
    app = _make_app(mem_fs, cache_max_age=45)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.status_code == 200
    cc = r.headers["cache-control"]
    assert "public" in cc
    assert "max-age=45" in cc
    assert "s-maxage=45" in cc
    assert "stale-while-revalidate=90" in cc  # 2x max-age
    etag = r.headers["etag"]
    assert etag.startswith('W/"') and etag.endswith('"')


@pytest.mark.asyncio
async def test_public_identity_if_none_match_returns_304_with_headers(
    mem_fs: dict[str, str],
) -> None:
    """Gap #5: the 304 must carry both ETag and Cache-Control."""
    app = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        first = await client.get("/api/public/identity")
        etag = first.headers["etag"]
        r = await client.get(
            "/api/public/identity", headers={"If-None-Match": etag},
        )
    assert r.status_code == 304
    assert r.headers.get("etag") == etag
    # Cache headers MUST survive on a 304 — some intermediaries drop
    # validator-less 304s as uncacheable.
    assert "max-age" in r.headers.get("cache-control", "")
    # Body should be empty on 304.
    assert r.content == b""


@pytest.mark.asyncio
async def test_etag_stable_across_identical_requests(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r1 = await client.get("/api/public/identity")
        r2 = await client.get("/api/public/identity")
    assert r1.headers["etag"] == r2.headers["etag"]


@pytest.mark.asyncio
async def test_mismatched_if_none_match_returns_200(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get(
            "/api/public/identity", headers={"If-None-Match": 'W/"deadbeef"'},
        )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Rate-limit — 429 with Retry-After + X-RateLimit-*.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exceeding_rate_limit_returns_429(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, rate_limit=3)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        for _ in range(3):
            r = await client.get("/api/public/identity")
            assert r.status_code == 200
        blocked = await client.get("/api/public/identity")
    assert blocked.status_code == 429
    assert blocked.headers.get("retry-after")
    assert blocked.headers.get("x-ratelimit-limit") == "3"
    assert blocked.headers.get("x-ratelimit-remaining") == "0"
    assert blocked.headers.get("x-ratelimit-reset")


@pytest.mark.asyncio
async def test_ok_response_carries_rate_limit_counters(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, rate_limit=5)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert r.headers.get("x-ratelimit-limit") == "5"
    # After one request, 4 remain.
    assert r.headers.get("x-ratelimit-remaining") == "4"


# ---------------------------------------------------------------------------
# Reverse-whitelist invariants (gap: orphan-entry detection).
# ---------------------------------------------------------------------------


def test_every_registered_public_route_is_in_public_paths(
    mem_fs: dict[str, str],
) -> None:
    """Catches a new ``/api/public/foo`` route that forgets to update auth."""
    app = _make_app(mem_fs)
    for path in _all_route_paths(app.routes):
        if path.startswith("/api/public/"):
            assert path in _PUBLIC_PATHS, (
                f"Route {path} is under /api/public/* but missing from "
                "auth._PUBLIC_PATHS — add it or the endpoint will 401."
            )


def test_every_public_path_entry_maps_to_registered_route(
    mem_fs: dict[str, str],
) -> None:
    """Catches an orphan exemption left over after a route rename/delete."""
    app = _make_app(mem_fs)
    registered = _all_route_paths(app.routes)
    # /login is only registered when a dashboard token is set — mount auth.
    app_with_auth = _make_app(mem_fs, token="test-tok")
    registered |= _all_route_paths(app_with_auth.routes)
    for entry in _PUBLIC_PATHS:
        assert entry in registered, (
            f"_PUBLIC_PATHS contains {entry!r} but no route exposes it — "
            "remove the stale exemption."
        )


def test_public_prefixes_exact_set() -> None:
    """Locks ``_PUBLIC_PREFIXES`` contents so nothing silently sneaks in."""
    assert _PUBLIC_PREFIXES == ("/static/",)


# ---------------------------------------------------------------------------
# Schema conformance — public_identity returns exactly the TypedDict keys.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_identity_response_matches_typed_dict(
    mem_fs: dict[str, str],
) -> None:
    """Adding a new key without widening the TypedDict must fail this test."""
    app = _make_app(mem_fs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/identity")
    assert set(r.json().keys()) == set(PublicIdentityResponse.__annotations__.keys())


# ---------------------------------------------------------------------------
# Pass-through branches: the middleware must not mangle non-JSON or non-2xx
# responses (future streaming / error paths).
# ---------------------------------------------------------------------------


def _build_direct_app(
    handler_response: Any, rate_limit: int = 60, cache_max_age: int = 30,
) -> Any:
    """Mount PublicApiMiddleware directly on a handcrafted Starlette app.

    Used to exercise content-type / status-code branches that the real
    ``/api/public/identity`` handler never produces.
    """
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.responses import Response as _Resp
    from starlette.routing import Route

    from maestro.dashboard.public_api import PublicApiMiddleware

    async def _handler(_request: Any) -> Any:
        return handler_response

    app = Starlette(routes=[Route("/api/public/foo", _handler)])
    app.add_middleware(
        PublicApiMiddleware,
        rate_limit_per_minute=rate_limit,
        cache_max_age_seconds=cache_max_age,
        trust_forwarded_for=False,
    )
    _ = HTMLResponse, _Resp  # appease ruff's F401 — used indirectly by caller
    return app


@pytest.mark.asyncio
async def test_non_2xx_response_passes_through_untouched() -> None:
    """A 500 from a future public handler must not gain ETag/Cache-Control."""
    from starlette.responses import JSONResponse

    app = _build_direct_app(JSONResponse({"error": "boom"}, status_code=500))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/foo")
    assert r.status_code == 500
    assert "etag" not in {k.lower() for k in r.headers}
    assert "cache-control" not in {k.lower() for k in r.headers}


@pytest.mark.asyncio
async def test_non_json_response_passes_through_untouched() -> None:
    """A future HTML / text / image public endpoint is not rewritten."""
    from starlette.responses import HTMLResponse

    app = _build_direct_app(HTMLResponse("<p>ok</p>"))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/foo")
    assert r.status_code == 200
    assert "etag" not in {k.lower() for k in r.headers}


@pytest.mark.asyncio
async def test_large_json_body_materializes_intact() -> None:
    """Many-chunk JSON body must round-trip byte-identical with stable ETag.

    Regression-lock for #1471: the cache-rewrite branch previously assembled
    the upstream body via ``body += chunk`` on an immutable ``bytes`` —
    O(n*k) for k chunks. The list-and-join replacement is O(n); this test
    pins the contract that the join's output and the SHA-256 input agree
    byte-for-byte with the pre-fix behaviour, so the ETag a downstream
    cache holds today survives the perf rewrite.
    """
    import hashlib

    from starlette.responses import StreamingResponse

    chunks = [b"x" * 1024 for _ in range(64)]  # ~64 KB across 64 chunks
    expected_body = b"".join(chunks)

    async def _gen() -> Any:
        for c in chunks:
            yield c

    app = _build_direct_app(
        StreamingResponse(_gen(), media_type="application/json"),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/foo")
    assert r.status_code == 200
    assert r.content == expected_body
    expected_etag = f'W/"{hashlib.sha256(expected_body).hexdigest()[:8]}"'
    assert r.headers.get("etag") == expected_etag


# ---------------------------------------------------------------------------
# Multi-valued header preservation — fix for #1467.
# The rewrite must use a tuple-list container so repeated keys
# (Set-Cookie, Vary, Link, WWW-Authenticate) survive instead of being
# collapsed by ``dict(response.headers)``.
# ---------------------------------------------------------------------------


def _json_with_headers(
    body: bytes, extra_headers: list[tuple[str, str]],
) -> Any:
    """Build a Starlette JSON-shaped Response with extra raw headers.

    Used to drive multi-valued / duplicate-key header scenarios past the
    inner handler so the middleware sees them on ``response.headers.raw``.
    Starlette's ``Response(headers=...)`` accepts only a Mapping, which
    collapses repeated keys; assigning ``raw_headers`` directly is the
    documented way to keep duplicates.
    """
    from starlette.responses import Response as _Resp

    resp = _Resp(content=body, media_type="application/json")
    extras: list[tuple[bytes, bytes]] = [
        (k.encode("latin-1").lower(), v.encode("latin-1"))
        for k, v in extra_headers
    ]
    resp.raw_headers = [*resp.raw_headers, *extras]
    return resp


@pytest.mark.asyncio
async def test_200_path_multi_valued_set_cookie_survives() -> None:
    """Both Set-Cookie entries from the inner handler must survive."""
    app = _build_direct_app(
        _json_with_headers(
            b'{"ok": 1}',
            [
                ("set-cookie", "a=1; Path=/"),
                ("set-cookie", "b=2; Path=/"),
            ],
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/foo")
    assert r.status_code == 200
    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert "a=1; Path=/" in cookies
    assert "b=2; Path=/" in cookies


@pytest.mark.asyncio
async def test_200_path_multi_valued_vary_survives() -> None:
    """Both Vary entries from the inner handler must survive."""
    app = _build_direct_app(
        _json_with_headers(
            b'{"ok": 1}',
            [
                ("vary", "Accept-Encoding"),
                ("vary", "Accept"),
            ],
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/foo")
    assert r.status_code == 200
    varies = r.headers.get_list("vary")
    assert len(varies) == 2
    assert "Accept-Encoding" in varies
    assert "Accept" in varies


@pytest.mark.asyncio
async def test_200_path_handler_cache_control_is_overridden() -> None:
    """Middleware-applied Cache-Control must be the only one on the response."""
    app = _build_direct_app(
        _json_with_headers(
            b'{"ok": 1}',
            [("cache-control", "no-store")],
        ),
        cache_max_age=30,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/foo")
    assert r.status_code == 200
    ccs = r.headers.get_list("cache-control")
    assert len(ccs) == 1
    # Handler's ``no-store`` must not survive — the middleware value wins.
    assert "no-store" not in ccs[0]
    assert "max-age=30" in ccs[0]


@pytest.mark.asyncio
async def test_200_path_strips_handler_content_length_and_recomputes() -> None:
    """Handler-side ``Content-Length: 99`` (mixed-case) must not survive.

    The middleware filters raw header keys with ``.lower()``, so a
    mixed-case ``Content-Length`` injected directly into raw_headers
    still matches the strip set. The replacement value must equal the
    real body length — a regression that drops the recomputed length
    would silently leak a stale value to a cache.
    """
    from starlette.responses import Response as _Resp

    body = b'{"ok": 1}'
    handler_resp = _Resp(content=body, media_type="application/json")
    # Push a mixed-case ``Content-Length`` directly so Starlette's
    # ``_json_with_headers`` lowercasing doesn't pre-normalize the key
    # before the middleware sees it.
    handler_resp.raw_headers = [
        *handler_resp.raw_headers,
        (b"Content-Length", b"99"),
    ]
    app = _build_direct_app(handler_resp)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/foo")
    assert r.status_code == 200
    assert r.headers.get("content-length") == str(len(body))


@pytest.mark.asyncio
async def test_304_path_vary_passes_through() -> None:
    """Vary on the original response must be carried into the 304 reply."""
    app = _build_direct_app(
        _json_with_headers(
            b'{"ok": 1}',
            [("vary", "Accept-Encoding")],
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        first = await client.get("/api/public/foo")
        etag = first.headers["etag"]
        r = await client.get(
            "/api/public/foo", headers={"If-None-Match": etag},
        )
    assert r.status_code == 304
    assert r.headers.get("etag") == etag
    assert r.headers.get("vary") == "Accept-Encoding"
    assert "max-age" in r.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_304_path_set_cookie_does_not_pass_through() -> None:
    """A 304 must NOT echo Set-Cookie — cookie delivery is the 200 path's job."""
    app = _build_direct_app(
        _json_with_headers(
            b'{"ok": 1}',
            [("set-cookie", "session=abc; Path=/")],
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        first = await client.get("/api/public/foo")
        etag = first.headers["etag"]
        r = await client.get(
            "/api/public/foo", headers={"If-None-Match": etag},
        )
    assert r.status_code == 304
    assert r.headers.get_list("set-cookie") == []


@pytest.mark.asyncio
async def test_304_path_content_type_does_not_pass_through() -> None:
    """A 304 carries no body, so the upstream Content-Type must not leak."""
    app = _build_direct_app(
        _json_with_headers(b'{"ok": 1}', []),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        first = await client.get("/api/public/foo")
        etag = first.headers["etag"]
        r = await client.get(
            "/api/public/foo", headers={"If-None-Match": etag},
        )
    assert r.status_code == 304
    assert "content-type" not in {k.lower() for k in r.headers}


# ---------------------------------------------------------------------------
# Content-Length sanitization on the cache-rewrite branch (issue #1468).
# The rewrite emits the body through a fresh ``Response`` whose ``init_headers``
# only recomputes ``Content-Length`` if it isn't already present in the
# headers dict. So any stale entry surviving the snapshot would mask the
# true body length. The drop must be case-insensitive — Starlette stores
# raw header keys lowercase today, but the public ``MutableHeaders`` contract
# is case-insensitive and we rely on that contract rather than the storage
# convention.
# ---------------------------------------------------------------------------


def _build_direct_app_with_handler(handler_factory: Any) -> Any:
    """Like ``_build_direct_app`` but accepts a *factory* that builds a fresh
    response per request, so the test can mutate ``raw_headers`` per call.
    """
    from starlette.applications import Starlette
    from starlette.routing import Route

    from maestro.dashboard.public_api import PublicApiMiddleware

    async def _handler(_request: Any) -> Any:
        return handler_factory()

    app = Starlette(routes=[Route("/api/public/foo", _handler)])
    app.add_middleware(
        PublicApiMiddleware,
        rate_limit_per_minute=60,
        cache_max_age_seconds=30,
        trust_forwarded_for=False,
    )
    return app


@pytest.mark.asyncio
async def test_content_length_matches_body_on_cache_rewrite() -> None:
    """Regression: lowercase ``content-length`` is dropped so the emitted
    header reflects the body bytes the middleware actually re-serialized.
    """
    from starlette.responses import JSONResponse

    def _factory() -> Any:
        return JSONResponse({"x": "y"})

    app = _build_direct_app_with_handler(_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/foo")
    assert r.status_code == 200
    cl_values = [v for k, v in r.headers.items() if k.lower() == "content-length"]
    assert len(cl_values) == 1
    assert int(cl_values[0]) == len(r.content)


@pytest.mark.asyncio
async def test_mixed_case_content_length_is_dropped_on_cache_rewrite() -> None:
    """A mixed-case ``Content-Length`` injected upstream must not survive
    into the rewritten response. Without the case-insensitive drop, a stale
    ``Content-Length: 99`` would mask the real 9-byte body and the client
    would read past the body or truncate it.

    The inner response carries *only* a mixed-case Content-Length (no
    lowercase duplicate). That matters because ``Headers.__getitem__`` is
    case-insensitive and returns the first match, so a duplicate lowercase
    entry would silently rewrite the mixed-case value to the canonical
    length when ``dict(response.headers)`` is built — masking the bug we
    are trying to regress against.
    """
    from starlette.responses import JSONResponse

    def _factory() -> Any:
        resp = JSONResponse({"x": "y"})  # body is 9 bytes
        # Replace raw_headers so only a mixed-case Content-Length remains.
        # Simulates an upstream hook (or a future Starlette release) that
        # preserves original case without also storing a lowercase copy.
        resp.raw_headers = [
            (b"Content-Length", b"99"),
            (b"content-type", b"application/json"),
        ]
        return resp

    app = _build_direct_app_with_handler(_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        r = await client.get("/api/public/foo")
    assert r.status_code == 200
    cl_values = [v for k, v in r.headers.items() if k.lower() == "content-length"]
    assert len(cl_values) == 1, f"expected exactly one Content-Length, got {cl_values}"
    assert int(cl_values[0]) == len(r.content)
    # The stale 99 from raw_headers must not survive.
    assert cl_values[0] != "99"


@pytest.mark.asyncio
async def test_crafted_host_header_cannot_skip_public_rate_limit(
    mem_fs: dict[str, str],
) -> None:
    """#4305: ``Host: x/?`` must not move a public request outside the substrate."""
    app = _make_app(mem_fs, rate_limit=1)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        first = await client.get("/api/public/identity", headers={"Host": "x/?"})
        blocked = await client.get("/api/public/identity", headers={"Host": "x/?"})
    assert first.status_code == 200
    assert blocked.status_code == 429
