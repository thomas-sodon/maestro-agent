"""Tests for dashboard lifespan and app creation."""

from __future__ import annotations

from typing import Any

import pytest

from maestro.dashboard.app import create_app


@pytest.mark.asyncio
async def test_lifespan_trivial(make_agent, mem_fs: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Lifespan starts and stops cleanly with no side effects."""
    agent = make_agent()
    app = create_app(agent)
    lifespan: Any = app.router.lifespan_context
    async with lifespan(app):
        pass  # Should complete without error


def test_create_app_with_dashboard_token_adds_auth_middleware(
    make_agent, mem_fs: dict[str, str],
) -> None:
    """When dashboard_token is set, BearerAuthMiddleware should be added."""
    agent = make_agent()
    agent.config.dashboard_token = "secret-token"

    # create_app should succeed and exercise the BearerAuthMiddleware import branch
    app = create_app(agent)

    # Verify the middleware was added by checking the middleware stack
    from maestro.dashboard.auth import BearerAuthMiddleware
    middleware_classes = [
        layer.cls for layer in app.user_middleware
        if hasattr(layer, "cls")
    ]
    assert BearerAuthMiddleware in middleware_classes
