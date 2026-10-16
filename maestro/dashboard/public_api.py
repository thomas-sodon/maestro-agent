"""Middleware substrate for ``/api/public/*`` endpoints.

Applies one invariant to every public endpoint by construction so that
future handlers cannot silently skip it:

* **Method allowlist** — only ``GET``. Anything else is rejected with
  ``405`` *before* the rate-limit budget is touched, so a caller that
  mistypes the method can't lock itself out.
* **Per-``(ip, path)`` rate limit** — sliding window, in-process. Origin
  defence; Cloudflare still absorbs real-world load.
* **Weak ``ETag`` + ``Cache-Control``** — only for fully-materialized JSON
  responses; streaming content-types are passed through unmodified. A
  matching ``If-None-Match`` short-circuits to ``304`` with the cache
  headers preserved so intermediaries don't treat the validator refresh
  as uncacheable.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from maestro.dashboard.auth import routed_path
from maestro.dashboard.ratelimit import SlidingWindowLimiter, resolve_client_ip

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request

_PUBLIC_PREFIX = "/api/public/"
_ALLOWED_METHODS = frozenset({"GET"})

# Headers that survive a 304: cache-coherence validators a downstream
# cache needs to keep its entry fresh.
_CACHE_RELEVANT_304_HEADERS = frozenset(
    {b"vary", b"content-language", b"content-encoding"},
)


class PublicApiMiddleware(BaseHTTPMiddleware):
    """Scoped middleware for ``/api/public/*`` routes.

    Parameters
    ----------
    app:
        The inner ASGI app (injected by Starlette).
    rate_limit_per_minute:
        Per-``(ip, path)`` request cap within a sliding 60 s window.
    cache_max_age_seconds:
        Value for the ``max-age`` and ``s-maxage`` directives; ``0``
        effectively disables caching. ``stale-while-revalidate`` is
        always set to ``2 * cache_max_age_seconds``.
    trust_forwarded_for:
        When ``True``, resolve the client IP from ``X-Real-IP`` set by
        nginx (``proxy_set_header X-Real-IP $remote_addr``).
        Production overlay sets this because nginx is the sole trusted
        hop; dev default is ``False``.
    """

    def __init__(
        self,
        app: Any,
        *,
        rate_limit_per_minute: int,
        cache_max_age_seconds: int,
        trust_forwarded_for: bool,
    ) -> None:
        super().__init__(app)
        self._limiter = SlidingWindowLimiter(
            max_requests=rate_limit_per_minute, window_seconds=60,
        )
        self._max_age = cache_max_age_seconds
        self._trust_fwd = trust_forwarded_for
        self._rate_limit = rate_limit_per_minute

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = routed_path(request)
        if not path.startswith(_PUBLIC_PREFIX):
            return await call_next(request)

        # (1) Method check first — do NOT burn rate-limit budget on bad
        # methods. A legit client spamming the wrong verb shouldn't lock
        # itself out of the right one.
        if request.method not in _ALLOWED_METHODS:
            return JSONResponse(
                {"error": "Method Not Allowed"},
                status_code=405,
                headers={"Allow": "GET"},
            )

        # (2) Rate limit — keyed on (resolved IP, path) so one hot
        # endpoint cannot starve another.
        ip = resolve_client_ip(request, trust_forwarded_for=self._trust_fwd)
        verdict = self._limiter.check(f"{ip}:{path}")
        if not verdict.allowed:
            retry = max(1, int(verdict.reset_seconds + 0.5))
            return JSONResponse(
                {"error": "Too Many Requests"},
                status_code=429,
                headers={
                    "Retry-After": str(retry),
                    "X-RateLimit-Limit": str(self._rate_limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(retry),
                },
            )

        # (3) Dispatch.
        response = await call_next(request)

        # (4) Cache discipline — only for 2xx JSON. Non-JSON / non-2xx
        # pass through untouched so this middleware never interferes
        # with error bodies or a future streaming endpoint.
        if not (200 <= response.status_code < 300):
            return response
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
            return response

        # BaseHTTPMiddleware always delivers a ``_StreamingResponse`` whose
        # ``body_iterator`` yields the materialized bytes. Typed as ``Any``
        # to reach through Starlette's ``Response`` parent typing.
        parts: list[bytes] = []
        iterator: Any = response.body_iterator  # type: ignore[attr-defined]
        async for chunk in iterator:
            parts.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        body = b"".join(parts)

        etag = self._make_etag(body)
        cache_control = (
            f"public, max-age={self._max_age}, "
            f"s-maxage={self._max_age}, "
            f"stale-while-revalidate={self._max_age * 2}"
        )
        rl_headers = {
            "X-RateLimit-Limit": str(self._rate_limit),
            "X-RateLimit-Remaining": str(verdict.remaining),
            "X-RateLimit-Reset": str(max(1, int(verdict.reset_seconds + 0.5))),
        }

        if request.headers.get("if-none-match", "") == etag:
            # Some intermediaries drop validator-less 304s as
            # uncacheable, so the 304 must echo the validators the 200
            # would have carried. The raw bytes-tuple list (not a dict)
            # is what preserves repeated keys like Vary.
            passthrough_raw: list[tuple[bytes, bytes]] = [
                (k, v)
                for k, v in response.headers.raw
                if k.lower() in _CACHE_RELEVANT_304_HEADERS
            ]
            not_modified = Response(status_code=304)
            not_modified.raw_headers = [
                *passthrough_raw,
                (b"etag", etag.encode("latin-1")),
                (b"cache-control", cache_control.encode("latin-1")),
                *(
                    (k.encode("latin-1"), v.encode("latin-1"))
                    for k, v in rl_headers.items()
                ),
            ]
            return not_modified

        # Raw bytes-tuple list preserves repeated keys (Set-Cookie,
        # Vary, Link) that a plain dict would collapse. The bytewise
        # lowercase filter also subsumes the case-insensitive
        # Content-Length drop from #1468 — any mixed-case variant
        # matches ``b"content-length"`` after ``k.lower()``. The filter
        # drops:
        # - content-length (recomputed from the canonical body bytes
        #   below; keeping the old value would lie if a hook mutated the
        #   body upstream, and a mixed-case stale entry must not leak
        #   past us either),
        # - cache-control / etag / x-ratelimit-* keys the handler may
        #   have emitted, so the middleware-applied values below are not
        #   shadowed by an earlier duplicate from the inner handler.
        _stripped = {
            b"content-length",
            b"cache-control",
            b"etag",
            b"x-ratelimit-limit",
            b"x-ratelimit-remaining",
            b"x-ratelimit-reset",
        }
        preserved: list[tuple[bytes, bytes]] = [
            (k, v)
            for k, v in response.headers.raw
            if k.lower() not in _stripped
        ]
        rewritten = Response(content=body, status_code=response.status_code)
        rewritten.raw_headers = [
            *preserved,
            (b"cache-control", cache_control.encode("latin-1")),
            (b"etag", etag.encode("latin-1")),
            *(
                (k.encode("latin-1"), v.encode("latin-1"))
                for k, v in rl_headers.items()
            ),
            (b"content-length", str(len(body)).encode("latin-1")),
        ]
        return rewritten

    @staticmethod
    def _make_etag(body: bytes) -> str:
        digest = hashlib.sha256(body).hexdigest()[:8]
        return f'W/"{digest}"'
