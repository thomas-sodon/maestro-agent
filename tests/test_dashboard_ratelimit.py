"""Tests for the dashboard sliding-window rate limiter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from maestro.config import Config
from maestro.dashboard.ratelimit import (
    RateLimitVerdict,
    SlidingWindowLimiter,
    resolve_client_ip,
)


class _FakeClock:
    """Controllable monotonic clock used for deterministic eviction tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def test_rejects_invalid_constructor_args() -> None:
    with pytest.raises(ValueError, match="max_requests"):
        SlidingWindowLimiter(max_requests=0, window_seconds=60)
    with pytest.raises(ValueError, match="window_seconds"):
        SlidingWindowLimiter(max_requests=5, window_seconds=0)


def test_first_request_allowed_and_remaining_counts_down() -> None:
    limiter = SlidingWindowLimiter(max_requests=3, window_seconds=60)
    v1 = limiter.check("k")
    v2 = limiter.check("k")
    v3 = limiter.check("k")
    assert (v1.allowed, v2.allowed, v3.allowed) == (True, True, True)
    assert (v1.remaining, v2.remaining, v3.remaining) == (2, 1, 0)


def test_blocks_after_max_requests_in_window() -> None:
    clock = _FakeClock()
    with patch("maestro.dashboard.ratelimit.time.monotonic", clock):
        limiter = SlidingWindowLimiter(max_requests=2, window_seconds=10)
        assert limiter.check("k").allowed is True
        assert limiter.check("k").allowed is True
        v = limiter.check("k")
        assert v.allowed is False
        assert v.remaining == 0
        # reset_seconds reflects time until the oldest slot falls out.
        assert 0.0 < v.reset_seconds <= 10.0


def test_sliding_window_recovers_after_window_passes() -> None:
    clock = _FakeClock()
    with patch("maestro.dashboard.ratelimit.time.monotonic", clock):
        limiter = SlidingWindowLimiter(max_requests=2, window_seconds=10)
        limiter.check("k")
        limiter.check("k")
        assert limiter.check("k").allowed is False
        clock.advance(11.0)
        v = limiter.check("k")
        assert v.allowed is True
        assert v.remaining == 1


def test_keys_are_isolated() -> None:
    limiter = SlidingWindowLimiter(max_requests=1, window_seconds=60)
    assert limiter.check("alice").allowed is True
    assert limiter.check("alice").allowed is False
    # Bob has his own budget.
    assert limiter.check("bob").allowed is True


def test_idle_keys_are_evicted() -> None:
    clock = _FakeClock()
    with patch("maestro.dashboard.ratelimit.time.monotonic", clock):
        limiter = SlidingWindowLimiter(max_requests=5, window_seconds=10)
        limiter.check("stale")
        assert "stale" in limiter._state
        # TTL is max(60, 2*window) = 60s here — advance past that.
        clock.advance(61.0)
        limiter.check("fresh")  # triggers eviction on access
        assert "stale" not in limiter._state
        assert "fresh" in limiter._state


def test_uses_monotonic_clock_not_wall() -> None:
    """Wall-clock jumps (NTP / suspend) must not free up the window early."""
    clock = _FakeClock()
    with patch("maestro.dashboard.ratelimit.time.monotonic", clock):
        limiter = SlidingWindowLimiter(max_requests=1, window_seconds=10)
        limiter.check("k")
        # Wall clock jumps forward (DST, NTP) but monotonic clock does not.
        # Window must still be in effect.
        assert limiter.check("k").allowed is False


def test_verdict_is_frozen_dataclass() -> None:
    v = RateLimitVerdict(allowed=True, remaining=1, reset_seconds=5.0)
    with pytest.raises(AttributeError):
        v.allowed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# resolve_client_ip
# ---------------------------------------------------------------------------


def _make_request(
    *, headers: dict[str, str] | None = None, client_host: str | None = "10.0.0.1",
) -> object:
    """Build a minimal stand-in for starlette Request.

    Only the attributes ``resolve_client_ip`` actually reads are populated.
    """
    class _Headers:
        def __init__(self, data: dict[str, str]) -> None:
            self._data = {k.lower(): v for k, v in data.items()}

        def get(self, key: str, default: str = "") -> str:
            return self._data.get(key.lower(), default)

    client = None if client_host is None else SimpleNamespace(host=client_host)
    return SimpleNamespace(headers=_Headers(headers or {}), client=client)


def test_resolve_client_ip_ignores_forwarded_for_when_disabled() -> None:
    req = _make_request(
        headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
        client_host="10.0.0.1",
    )
    assert resolve_client_ip(req, trust_forwarded_for=False) == "10.0.0.1"  # type: ignore[arg-type]


def test_resolve_client_ip_uses_x_real_ip_when_enabled() -> None:
    """X-Real-IP is set by nginx to $remote_addr — unspoofable."""
    req = _make_request(
        headers={"X-Real-IP": "203.0.113.9"},
        client_host="10.0.0.1",
    )
    assert (
        resolve_client_ip(req, trust_forwarded_for=True)  # type: ignore[arg-type]
        == "203.0.113.9"
    )


def test_resolve_client_ip_ignores_spoofed_forwarded_for_when_x_real_ip_present() -> None:
    """Attacker-controlled X-Forwarded-For must not influence the resolved IP.

    nginx's $proxy_add_x_forwarded_for appends $remote_addr to any
    client-supplied X-Forwarded-For, so the leftmost hop is freely
    forgeable. X-Real-IP is set to $remote_addr by an overwriting
    proxy_set_header directive and is the only safe source (#1470).
    """
    req = _make_request(
        headers={
            "X-Forwarded-For": "198.51.100.99, 10.0.0.1",
            "X-Real-IP": "203.0.113.9",
        },
        client_host="10.0.0.1",
    )
    assert (
        resolve_client_ip(req, trust_forwarded_for=True)  # type: ignore[arg-type]
        == "203.0.113.9"
    )


def test_resolve_client_ip_falls_back_to_peer_when_x_real_ip_missing() -> None:
    """When X-Real-IP is absent, fall back to request.client.host — not X-Forwarded-For."""
    req = _make_request(
        headers={"X-Forwarded-For": "198.51.100.99"},
        client_host="10.0.0.1",
    )
    assert (
        resolve_client_ip(req, trust_forwarded_for=True)  # type: ignore[arg-type]
        == "10.0.0.1"
    )


def test_resolve_client_ip_falls_back_when_x_real_ip_empty() -> None:
    req = _make_request(headers={"X-Real-IP": ""}, client_host="10.0.0.1")
    assert (
        resolve_client_ip(req, trust_forwarded_for=True)  # type: ignore[arg-type]
        == "10.0.0.1"
    )


def test_resolve_client_ip_handles_missing_client() -> None:
    req = _make_request(client_host=None)
    assert (
        resolve_client_ip(req, trust_forwarded_for=False)  # type: ignore[arg-type]
        == "unknown"
    )


# ---------------------------------------------------------------------------
# MutationRateLimitMiddleware (proposal 022)
# ---------------------------------------------------------------------------

FAKE_DIR = Path("/fake/mutlimit")
SECRET = "mutation-limit-secret"


def _make_app(mem_fs: dict[str, str], **config_overrides: object):  # type: ignore[no-untyped-def]
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
        memory_dir=FAKE_DIR, workspace=FAKE_DIR, dashboard_token=SECRET,
        **config_overrides,  # type: ignore[arg-type]
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


_BEARER = {"Authorization": f"Bearer {SECRET}"}


@pytest.mark.asyncio
async def test_login_posts_beyond_limit_get_429_with_retry_after(
    mem_fs: dict[str, str],
) -> None:
    """The (limit+1)-th login attempt within the window is rejected."""
    app = _make_app(mem_fs, login_rate_limit_per_minute=2)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        # Failed attempts burn budget too — brute force is the threat model.
        r1 = await client.post("/login", data={"token": "wrong"})
        r2 = await client.post("/login", data={"token": "wrong"})
        r3 = await client.post("/login", data={"token": "wrong"})
    assert r1.status_code == 303
    assert r2.status_code == 303
    assert r3.status_code == 429
    assert r3.json() == {"error": "Too Many Requests"}
    assert int(r3.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_login_bucket_drains_as_the_window_slides(
    mem_fs: dict[str, str],
) -> None:
    clock = _FakeClock()
    with patch("maestro.dashboard.ratelimit.time.monotonic", clock):
        app = _make_app(mem_fs, login_rate_limit_per_minute=1)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            follow_redirects=False,
        ) as client:
            r1 = await client.post("/login", data={"token": "wrong"})
            r2 = await client.post("/login", data={"token": "wrong"})
            clock.advance(61.0)
            r3 = await client.post("/login", data={"token": "wrong"})
    assert r1.status_code == 303
    assert r2.status_code == 429
    assert r3.status_code == 303


@pytest.mark.asyncio
async def test_approval_posts_beyond_limit_get_429(mem_fs: dict[str, str]) -> None:
    """Plan approve/reject POSTs share the approval bucket."""
    app = _make_app(mem_fs, approval_rate_limit_per_minute=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # No plan exists — a 404 still proves the budget was spent, and the
        # second request must be cut off before reaching the route.
        r1 = await client.post(
            "/api/plans/nope/approve", headers=_BEARER, json={"content_hash": "x"},
        )
        r2 = await client.post(
            "/api/plans/nope/approve", headers=_BEARER, json={"content_hash": "x"},
        )
    assert r1.status_code == 404
    assert r2.status_code == 429
    assert int(r2.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_publication_approvals_share_the_approval_bucket(
    mem_fs: dict[str, str],
) -> None:
    app = _make_app(mem_fs, approval_rate_limit_per_minute=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post("/api/publications/p-x/approve", headers=_BEARER)
        r2 = await client.post(
            "/api/publications/p-y/reject", headers=_BEARER, json={"reason": "no"},
        )
    assert r1.status_code == 404
    assert r2.status_code == 429



@pytest.mark.asyncio
async def test_login_and_approval_buckets_are_independent(
    mem_fs: dict[str, str],
) -> None:
    app = _make_app(
        mem_fs, login_rate_limit_per_minute=1, approval_rate_limit_per_minute=1,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r_login = await client.post("/login", data={"token": "wrong"})
        # The login spend must not consume the approval budget.
        r_approve = await client.post(
            "/api/plans/nope/approve", headers=_BEARER, json={"content_hash": "x"},
        )
    assert r_login.status_code == 303
    assert r_approve.status_code == 404


@pytest.mark.asyncio
async def test_get_requests_are_never_rate_limited(mem_fs: dict[str, str]) -> None:
    app = _make_app(mem_fs, login_rate_limit_per_minute=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.get("/login")
        r2 = await client.get("/login")
    assert r1.status_code == 200
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_other_posts_are_not_rate_limited(mem_fs: dict[str, str]) -> None:
    """Only login and approval paths carry a budget."""
    app = _make_app(
        mem_fs, login_rate_limit_per_minute=1, approval_rate_limit_per_minute=1,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # message_store is None in this fixture → 501, never 429.
        r1 = await client.post("/api/messages", headers=_BEARER, json={"content": "a"})
        r2 = await client.post("/api/messages", headers=_BEARER, json={"content": "b"})
    assert r1.status_code == 501
    assert r2.status_code == 501


@pytest.mark.asyncio
async def test_x_real_ip_separates_buckets_when_trusted(
    mem_fs: dict[str, str],
) -> None:
    """With trust_forwarded_for=True, X-Real-IP keys the budget per client."""
    app = _make_app(
        mem_fs, login_rate_limit_per_minute=1, trust_forwarded_for=True,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r1 = await client.post(
            "/login", data={"token": "wrong"}, headers={"X-Real-IP": "203.0.113.1"},
        )
        r2 = await client.post(
            "/login", data={"token": "wrong"}, headers={"X-Real-IP": "203.0.113.2"},
        )
        r3 = await client.post(
            "/login", data={"token": "wrong"}, headers={"X-Real-IP": "203.0.113.1"},
        )
    assert r1.status_code == 303
    assert r2.status_code == 303
    assert r3.status_code == 429


@pytest.mark.asyncio
async def test_x_real_ip_ignored_when_trust_disabled(mem_fs: dict[str, str]) -> None:
    """Untrusted X-Real-IP must not let a client mint fresh buckets."""
    app = _make_app(
        mem_fs, login_rate_limit_per_minute=1, trust_forwarded_for=False,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r1 = await client.post(
            "/login", data={"token": "wrong"}, headers={"X-Real-IP": "203.0.113.1"},
        )
        r2 = await client.post(
            "/login", data={"token": "wrong"}, headers={"X-Real-IP": "203.0.113.2"},
        )
    assert r1.status_code == 303
    assert r2.status_code == 429


@pytest.mark.asyncio
async def test_x_forwarded_for_never_keys_the_bucket(mem_fs: dict[str, str]) -> None:
    """X-Forwarded-For is attacker-controlled (#1470) — ignored even when
    trust_forwarded_for is enabled."""
    app = _make_app(
        mem_fs, login_rate_limit_per_minute=1, trust_forwarded_for=True,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        follow_redirects=False,
    ) as client:
        r1 = await client.post(
            "/login", data={"token": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        r2 = await client.post(
            "/login", data={"token": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.2"},
        )
    assert r1.status_code == 303
    assert r2.status_code == 429


@pytest.mark.asyncio
async def test_crafted_host_header_still_charges_approval_bucket(
    mem_fs: dict[str, str],
) -> None:
    """#4305: a Host-injected ``/login`` path cannot reroute an approve POST."""
    app = _make_app(
        mem_fs, login_rate_limit_per_minute=100, approval_rate_limit_per_minute=1,
    )
    headers = {**_BEARER, "Host": "x/login?"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r1 = await client.post(
            "/api/plans/nope/approve", headers=headers, json={"content_hash": "x"},
        )
        r2 = await client.post(
            "/api/plans/nope/approve", headers=headers, json={"content_hash": "x"},
        )
    assert r1.status_code == 404
    assert r2.status_code == 429
