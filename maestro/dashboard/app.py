"""FastAPI application for the Maestro dashboard."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from maestro.dashboard.public_api import PublicApiMiddleware
from maestro.dashboard.routes import create_router
from maestro.dashboard.security_headers import SecurityHeadersMiddleware

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent

logger = logging.getLogger("maestro")

_STATIC_DIR = Path(__file__).parent / "static"


class _NoCacheStaticFiles(StaticFiles):
    # Default StaticFiles emits Last-Modified + ETag but no Cache-Control,
    # so Cloudflare applies its default edge-cache rules and pins users to
    # the old CSS/JS across deploys. The HTML shell already revalidates
    # (Cache-Control: no-cache on /, /account, /login); force the same on
    # the assets it points at so a deploy is actually visible.
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(agent: MaestroAgent) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(title="Maestro", version="0.1.0", lifespan=lifespan)

    # Mount extracted static assets (CSS/JS blocks that used to live inline
    # in the HTML templates). CSP ``script-src 'self'`` requires these to be
    # served from the same origin; the auth middleware whitelists /static/
    # via ``_PUBLIC_PREFIXES`` so they don't need a token.
    app.mount(
        "/static",
        _NoCacheStaticFiles(directory=_STATIC_DIR),
        name="static",
    )

    # Middleware stack — registration order matters. Starlette prepends on
    # ``add_middleware``, so last-added ends up OUTERMOST. That's the order
    # we want for responses on the way out:
    #   request  → SecurityHeaders → PublicApi → BearerAuth
    #            → MutationRateLimit → CSRF → route
    #   response flows back through the same chain in reverse.
    # With SecurityHeaders outermost, even 401s emitted by BearerAuth flow
    # back through it and acquire the CSP / Permissions-Policy headers.
    # CSRF and MutationRateLimit sit INSIDE BearerAuth so they only ever see
    # authenticated requests (unauthenticated spray gets a 401 before it can
    # burn rate-limit budget); both exist only when auth is enabled — without
    # a token there is no login flow to issue the CSRF cookie and no
    # credential for a cross-site request to ride.
    # Do not flip this order without re-reading the middleware comments.
    token = agent.config.dashboard_token
    if token:
        from maestro.dashboard.auth import BearerAuthMiddleware, CSRFMiddleware
        from maestro.dashboard.ratelimit import MutationRateLimitMiddleware

        app.add_middleware(CSRFMiddleware, token=token)
        app.add_middleware(
            MutationRateLimitMiddleware,
            login_rate_limit_per_minute=(
                agent.config.login_rate_limit_per_minute
            ),
            approval_rate_limit_per_minute=(
                agent.config.approval_rate_limit_per_minute
            ),
            trust_forwarded_for=agent.config.trust_forwarded_for,
        )
        app.add_middleware(BearerAuthMiddleware, token=token)
    elif agent.config.dashboard_host != "127.0.0.1":
        logger.warning(
            "Dashboard exposed on %s without MAESTRO_DASHBOARD_TOKEN — "
            "all endpoints are unauthenticated",
            agent.config.dashboard_host,
        )

    app.add_middleware(
        PublicApiMiddleware,
        rate_limit_per_minute=agent.config.public_api_rate_limit_per_minute,
        cache_max_age_seconds=agent.config.public_api_cache_max_age_seconds,
        trust_forwarded_for=agent.config.trust_forwarded_for,
    )

    if agent.config.security_headers_enabled:
        app.add_middleware(SecurityHeadersMiddleware)

    router = create_router(agent, dashboard_token=token)
    app.include_router(router)
    return app
