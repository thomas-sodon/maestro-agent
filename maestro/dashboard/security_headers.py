"""Security-headers middleware applied to every dashboard response.

Defence-in-depth: nginx already sets HSTS, ``X-Content-Type-Options``,
``X-Frame-Options``, and ``Referrer-Policy`` at the edge (see
``nginx/maestro.conf``), but a direct request to
``127.0.0.1:8420`` bypasses that.  This middleware ensures the same
posture regardless of how the dashboard is reached — and adds CSP +
``Permissions-Policy``, which nginx does not set.

Registered **outermost** so even auth-failure 401s, 404s, and redirects
carry the headers.  Gated by ``[dashboard] security_headers_enabled`` so
an operator who wants to opt out (e.g. debugging a broken-CSP page) can
do so without ripping the middleware out of the stack.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response


_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "interest-cohort=(), browsing-topics=()",
    "Content-Security-Policy": _CSP,
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject a fixed set of security headers on every outgoing response.

    ``style-src 'self' 'unsafe-inline'`` is a deliberate compromise —
    existing inline ``style=""`` attributes and the one ``.style.cssText``
    assignment in ``index.js`` would otherwise break.  ``script-src 'self'``
    stays strict because that's the real XSS attack surface.
    """

    def __init__(self, app: Any) -> None:
        super().__init__(app)

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        for key, value in _HEADERS.items():
            response.headers[key] = value
        return response
