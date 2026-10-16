"""Bearer-token, cookie, and CSRF middleware for the dashboard."""

from __future__ import annotations

import hmac
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

COOKIE_NAME = "maestro_token"
CSRF_COOKIE_NAME = "maestro_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
# Explicit enumeration rather than a prefix namespace: future `/api/public/*`
# routes must be listed here individually so no route is silently exempted.
_PUBLIC_PATHS = {"/health", "/login", "/", "/api/public/identity"}
# Prefix-scoped exceptions. Kept separate from ``_PUBLIC_PATHS`` (which stays
# exact-match) so "no silent exemption" still applies: every entry here is
# locked down by ``test_public_prefixes_exact_set`` and every route under a
# prefix has to be deliberate.
_PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)
# Methods that must never mutate state and therefore skip the CSRF check.
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# ``/login`` is covered by ``_PUBLIC_PATHS`` (no session cookie exists yet —
# the login POST is what issues the CSRF cookie). ``/logout`` is exempted
# explicitly: it is body-less, plain HTML forms cannot attach headers, and a
# forged logout costs the victim a re-login, not state.
_CSRF_EXEMPT_PATHS = frozenset({"/logout"})


def routed_path(request: Request) -> str:
    """Return the ASGI routed path — the path the router dispatches on.

    Never use ``request.url.path`` for a security decision: Starlette builds
    ``request.url`` from the client-supplied ``Host`` header, so a crafted
    ``Host: x/login?`` makes ``url.path == "/login"`` while the router still
    dispatches ``scope["path"]`` — bypassing auth, CSRF, and rate limits.
    """
    return str(request.scope["path"])


def _bearer_token_matches(request: Request, token: str) -> bool:
    """Return True if the ``Authorization`` header carries exactly *token*."""
    auth = request.headers.get("authorization", "")
    parts = auth.split(None, 1)
    scheme = parts[0] if parts else ""
    credentials = parts[1].strip() if len(parts) > 1 else ""
    if scheme.lower() != "bearer":
        return False
    try:
        return hmac.compare_digest(credentials, token)
    except (TypeError, ValueError):
        return False


def _read_cookie_value(request: Request, name: str) -> str:
    """Return the named cookie's value, or ``""`` when absent.

    Uses Starlette's already-parsed ``request.cookies`` dict rather than a
    hand-rolled ``SimpleCookie`` round-trip. Starlette's parser is best-effort:
    it skips only an unparseable segment, so an unrelated malformed browser
    cookie no longer locks out an otherwise-valid session or CSRF cookie (the
    old ``SimpleCookie.load()`` raised ``CookieError`` and dropped the whole
    header). Callers still guard the returned value's ``compare_digest`` with a
    ``(TypeError, ValueError)`` except, so a non-ASCII value denies rather than
    500s.
    """
    return request.cookies.get(name, "")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <token>`` or a session cookie.

    Public paths (health, login) are exempt from authentication.
    The ``/logout`` endpoint is *not* public — it requires a valid session.
    Unauthenticated POST requests to ``/logout`` are redirected gracefully to
    ``/login`` and have their session and CSRF cookies cleared (there is no
    session to invalidate server-side, but a stale/rotated cookie must still be
    removed from the browser).
    Browser requests (Accept: text/html, non-API paths) receive a redirect
    to ``/login`` on failure; all others get a JSON 401.
    """

    def __init__(self, app: object, token: str) -> None:
        if not token or not token.strip():
            raise ValueError("Dashboard bearer token must not be empty")
        super().__init__(app)  # type: ignore[arg-type]
        self._token = token.strip()

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Response],
    ) -> Response:
        path = routed_path(request)
        if path in _PUBLIC_PATHS or any(
            path.startswith(p) for p in _PUBLIC_PREFIXES
        ):
            return await call_next(request)  # type: ignore[misc,no-any-return]

        # Try Bearer header first
        if self._check_bearer(request):
            return await call_next(request)  # type: ignore[misc,no-any-return]

        # Fall back to cookie
        if self._check_cookie(request):
            return await call_next(request)  # type: ignore[misc,no-any-return]

        return self._unauthorized_response(request)

    def _check_bearer(self, request: Request) -> bool:
        """Return True if a valid Bearer token is present in the header."""
        return _bearer_token_matches(request, self._token)

    def _check_cookie(self, request: Request) -> bool:
        """Return True if a valid session cookie is present."""
        value = _read_cookie_value(request, COOKIE_NAME)
        if not value:
            return False
        try:
            return hmac.compare_digest(value, self._token)
        except (TypeError, ValueError):
            return False

    def _unauthorized_response(self, request: Request) -> Response:
        """Return a redirect for browsers or a JSON 401 for API clients.

        Unauthenticated POST to ``/logout`` always redirects to ``/login`` and
        clears the session and CSRF cookies — there is no valid session to
        invalidate server-side, but a browser holding a stale or server-rotated
        cookie would otherwise keep resubmitting it, so the redirect deletes both
        cookies to match the authenticated logout handler in ``routes.py``.
        """
        if request.method == "POST" and routed_path(request) == "/logout":
            response: Response = RedirectResponse("/login", status_code=303)
            response.delete_cookie(COOKIE_NAME, path="/")
            response.delete_cookie(CSRF_COOKIE_NAME, path="/")
            return response
        accept = request.headers.get("accept", "")
        is_browser = "text/html" in accept and not routed_path(request).startswith("/api/")
        if is_browser:
            return RedirectResponse("/login", status_code=302)
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="maestro"'},
        )


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit CSRF guard for cookie-authenticated mutations.

    Mounted INSIDE ``BearerAuthMiddleware`` (added first, so innermost), so
    every request reaching it has already authenticated. Mutating methods
    must echo the JS-readable ``maestro_csrf`` cookie in the
    ``X-CSRF-Token`` header; ``POST /login`` issues and rotates the cookie.

    Exemptions, each load-bearing:

    - Safe methods (GET/HEAD/OPTIONS) — they must never mutate state.
    - Public paths/prefixes and the ``_CSRF_EXEMPT_PATHS`` set — see the
      comments on those constants.
    - Requests carrying a *valid* ``Authorization: Bearer`` header. Header
      credentials are attached explicitly per-request by the caller, never
      ambiently by a browser, so they cannot be forged cross-site. An
      INVALID bearer header does not exempt: such a request only passed the
      outer auth via its session cookie, so the CSRF check still applies.
    """

    def __init__(self, app: object, token: str) -> None:
        if not token or not token.strip():
            raise ValueError("Dashboard bearer token must not be empty")
        super().__init__(app)  # type: ignore[arg-type]
        self._token = token.strip()

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Response],
    ) -> Response:
        if request.method in _CSRF_SAFE_METHODS:
            return await call_next(request)  # type: ignore[misc,no-any-return]
        path = routed_path(request)
        if (
            path in _PUBLIC_PATHS
            or path in _CSRF_EXEMPT_PATHS
            or any(path.startswith(p) for p in _PUBLIC_PREFIXES)
        ):
            return await call_next(request)  # type: ignore[misc,no-any-return]
        if _bearer_token_matches(request, self._token):
            return await call_next(request)  # type: ignore[misc,no-any-return]
        if self._csrf_ok(request):
            return await call_next(request)  # type: ignore[misc,no-any-return]
        return JSONResponse(
            {"error": "CSRF token missing or invalid"}, status_code=403,
        )

    @staticmethod
    def _csrf_ok(request: Request) -> bool:
        """Return True when the CSRF header echoes the CSRF cookie."""
        cookie_value = _read_cookie_value(request, CSRF_COOKIE_NAME)
        header_value = request.headers.get("x-csrf-token", "")
        if not cookie_value or not header_value:
            return False
        try:
            return hmac.compare_digest(header_value, cookie_value)
        except (TypeError, ValueError):
            return False
