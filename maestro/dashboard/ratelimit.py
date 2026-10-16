"""In-process sliding-window rate limiter for the dashboard.

:class:`SlidingWindowLimiter` is the shared primitive. Consumers:
:mod:`maestro.dashboard.public_api` caps per-``(client_ip, path)`` traffic
on the anonymous surface, and :class:`MutationRateLimitMiddleware` (proposal
022) caps per-IP login attempts and approval mutations on the authenticated
surface. Cloudflare still absorbs real load at the edge.

Single-process by design — the dashboard runs as one process behind nginx
(``127.0.0.1:8420``), so a local ``deque`` map is sufficient and avoids a
Redis dependency.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from maestro.dashboard.auth import routed_path

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request


@dataclass(frozen=True)
class RateLimitVerdict:
    """Result of a :meth:`SlidingWindowLimiter.check` call."""

    allowed: bool
    remaining: int
    reset_seconds: float


class SlidingWindowLimiter:
    """Per-key sliding-window limiter.

    Each key owns a ``deque`` of monotonic timestamps for its in-window
    requests.  ``check(key)`` evicts stale entries, appends ``now`` if the
    window still has room, and returns a :class:`RateLimitVerdict`.

    Keys idle for longer than ``max(60, 2*window_seconds)`` are evicted on
    access so the map stays bounded. Thread-safe via a single lock — fine
    for the dashboard's request rate.
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        if max_requests < 1:
            raise ValueError(f"max_requests must be >= 1, got {max_requests!r}")
        if window_seconds < 1:
            raise ValueError(
                f"window_seconds must be >= 1, got {window_seconds!r}"
            )
        self._max = max_requests
        self._window = float(window_seconds)
        self._ttl = float(max(60, 2 * window_seconds))
        self._state: dict[str, deque[float]] = {}
        self._last_touch: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> RateLimitVerdict:
        """Record a request attempt for *key* and return whether it's allowed."""
        now = time.monotonic()
        with self._lock:
            self._evict_idle(now)
            bucket = self._state.get(key)
            if bucket is None:
                bucket = deque()
                self._state[key] = bucket
            cutoff = now - self._window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            self._last_touch[key] = now
            if len(bucket) >= self._max:
                oldest = bucket[0]
                reset = max(0.0, oldest + self._window - now)
                return RateLimitVerdict(
                    allowed=False, remaining=0, reset_seconds=reset,
                )
            bucket.append(now)
            remaining = self._max - len(bucket)
            reset = max(0.0, bucket[0] + self._window - now)
            return RateLimitVerdict(
                allowed=True, remaining=remaining, reset_seconds=reset,
            )

    def _evict_idle(self, now: float) -> None:
        """Drop keys untouched for longer than ``self._ttl``."""
        if not self._last_touch:
            return
        stale = [
            k for k, ts in self._last_touch.items() if now - ts > self._ttl
        ]
        for k in stale:
            self._state.pop(k, None)
            self._last_touch.pop(k, None)


# The human-approval mutation surface: flipping a plan or publication
# proposal. Path params never contain ``/`` so one segment is enough.
_APPROVAL_PATH_RE = re.compile(
    r"^/api/(?:plans|publications)/[^/]+/(?:approve|reject)$"
)


class MutationRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP sliding-window limits for login and approval POSTs (022).

    Two independent budgets, both keyed on the resolved client IP:

    - ``POST /login`` — brute-force guard on the token form.
    - ``POST /api/{plans,publications}/{id}/{approve,reject}`` — caps how
      fast the human-approval surface can be flipped.

    Mounted inside ``BearerAuthMiddleware`` so unauthenticated spray at the
    approval routes is rejected with 401 *before* it can burn this budget
    (mirroring ``PublicApiMiddleware``'s method-check-before-budget rule);
    ``/login`` is public, so its budget is reachable by design.
    """

    def __init__(
        self,
        app: Any,
        *,
        login_rate_limit_per_minute: int,
        approval_rate_limit_per_minute: int,
        trust_forwarded_for: bool,
    ) -> None:
        super().__init__(app)
        self._login_limiter = SlidingWindowLimiter(
            max_requests=login_rate_limit_per_minute, window_seconds=60,
        )
        self._approval_limiter = SlidingWindowLimiter(
            max_requests=approval_rate_limit_per_minute, window_seconds=60,
        )
        self._trust_fwd = trust_forwarded_for

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method != "POST":
            return await call_next(request)
        path = routed_path(request)
        if path == "/login":
            limiter = self._login_limiter
        elif _APPROVAL_PATH_RE.match(path):
            limiter = self._approval_limiter
        else:
            return await call_next(request)
        ip = resolve_client_ip(request, trust_forwarded_for=self._trust_fwd)
        verdict = limiter.check(ip)
        if not verdict.allowed:
            retry = max(1, int(verdict.reset_seconds + 0.5))
            return JSONResponse(
                {"error": "Too Many Requests"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        return await call_next(request)


def resolve_client_ip(request: Request, *, trust_forwarded_for: bool) -> str:
    """Return the client IP for rate-limiting keys.

    With ``trust_forwarded_for=False`` (dev default), always use
    ``request.client.host`` — an attacker can't spoof the peer address of
    the direct TCP connection, whereas ``X-Forwarded-For`` is freely
    forgeable by any client on a direct-binding dashboard.

    With ``trust_forwarded_for=True`` (production, behind nginx as the
    single trusted hop), read ``X-Real-IP`` which nginx sets to
    ``$remote_addr`` via an overwriting ``proxy_set_header`` directive —
    unspoofable by clients. ``X-Forwarded-For`` is **not** safe here:
    nginx's ``$proxy_add_x_forwarded_for`` *appends* ``$remote_addr`` to
    any client-supplied value, so the leftmost hop is attacker-controlled
    and can be rotated to bypass the per-IP rate limiter (#1470).
    Falls back to the peer address when ``X-Real-IP`` is absent or empty.
    """
    if trust_forwarded_for:
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
    client = request.client
    if client is None:
        return "unknown"
    return client.host or "unknown"
