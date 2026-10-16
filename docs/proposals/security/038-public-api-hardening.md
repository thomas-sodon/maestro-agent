# 038: Public-API Hardening Substrate

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/dashboard/app.py`, `maestro/dashboard/auth.py`, `maestro/dashboard/routes.py`, `maestro/dashboard/templates/*.html`, new `maestro/dashboard/{public_api,security_headers,ratelimit,public_schemas}.py`, new `maestro/dashboard/static/*.{css,js}`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro.production.toml`, `tests/test_dashboard_{auth,public_middleware,security_headers,static,ratelimit}.py`, `tests/browser/conftest.py`, `CLAUDE.md` |

## Description

> Status updated to shipped on 2026-04-27 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Install a one-shot substrate so every current and future `/api/public/*` endpoint is protected by construction: per-IP rate limiting, HTTP caching, response-schema discipline, and security headers (CSP + Permissions-Policy). Today `/api/public/identity` is hand-rolled and the only public endpoint; proposal 036 (`visitor-count`) and future additions would each need to re-invent the same protections.

Two pragmatic choices scope the work tightly. First, CSP is strict for scripts (`script-src 'self'` — the real XSS vector) but permissive for styles (`style-src 'self' 'unsafe-inline'`) so the existing 30+ inline `style="..."` attributes and `index.js`'s `.style.cssText = …` toast helper keep working. Follow-up PRs can tighten. Second, the rate limiter is in-process (dashboard is a single process bound to `127.0.0.1:8420`); Cloudflare still absorbs real load at the edge.

## Relevant Findings

- `_PUBLIC_PATHS` in `maestro/dashboard/auth.py` is an exact-match set; `tests/test_dashboard_auth.py::test_public_paths_exact_set` locks the contents. Every new public route needs two lockstep edits — good discipline, but no structural rate-limit / cache / CSP protection comes with it.
- `nginx/maestro.conf` already sets HSTS, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and `X-XSS-Protection`. CSP and `Permissions-Policy` are missing at both layers — worth adding at the app so a direct `127.0.0.1:8420` hit (e.g. local smoke) still gets them.
- `routes.py` (`public_identity`) handcrafts the 10-field curated-subset dict. A future handler could easily leak a sensitive `SelfModel` field without noticing.
- `templates/public.html`, `index.html`, `login.html` carry their CSS and JS inline. `script-src 'self'` requires both to move out — block-move only (zero inline `on*=` handlers, confirmed by audit).
- `trust_forwarded_proto` at `routes.py` is the existing pattern for header-trust config. Rate-limiter IP resolution mirrors it as `trust_forwarded_for`.

## Implementation Plan

1. **Static extraction.** Move `<style>` / `<script>` blocks from the three templates to `maestro/dashboard/static/{public,index,login}.{css,js}`, swap in `<link rel="stylesheet">` / `<script src>` references. Browser tests need `StaticFiles` mounted on their mock servers — `tests/browser/conftest.py` gains a `_mount_static()` helper invoked from all three mock-app factories. Inline `style="..."` attributes and `.cssText` assignment stay (covered by `style-src 'unsafe-inline'`).
2. **`SlidingWindowLimiter` + `resolve_client_ip`.** `maestro/dashboard/ratelimit.py` — stdlib-only, thread-safe via `threading.Lock`, `deque[float]` of monotonic timestamps per key, TTL-evict idle keys at `max(60, 2*window)`. `resolve_client_ip(request, trust_forwarded_for)` reads `X-Real-IP` (set by nginx to `$remote_addr`) when the flag is on, peer otherwise.
3. **`PublicApiMiddleware`.** `maestro/dashboard/public_api.py` — scoped by the `/api/public/` path prefix. **Method check first** (405 for non-`GET`/`HEAD`, no rate-limit budget consumed); per-`(ip, path)` rate limit; `Cache-Control: public, max-age=<cfg>, s-maxage=<cfg>, stale-while-revalidate=<2*cfg>` + weak `ETag` only on 2xx `application/json`; `If-None-Match` → 304 **with cache headers preserved**. 429 carries `Retry-After` + `X-RateLimit-*`.
4. **`SecurityHeadersMiddleware`.** `maestro/dashboard/security_headers.py` — applied to every response. Gated by `[dashboard] security_headers_enabled` (default `true`).
5. **`PublicIdentityResponse` TypedDict.** `maestro/dashboard/public_schemas.py`. `public_identity()` constructs it; test asserts key equality.
6. **Middleware wiring.** In `app.py`: `add_middleware` calls in order `BearerAuth → PublicApi → SecurityHeaders` (Starlette prepends, so last-added is outermost). Inline comment documents the reversed-intuition order.
7. **Auth exemption.** `_PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)` in `auth.py`, checked alongside `_PUBLIC_PATHS`. Test locks the tuple contents.
8. **Config + TOML.** Four new `[dashboard]` keys: `trust_forwarded_for`, `public_api_rate_limit_per_minute`, `public_api_cache_max_age_seconds`, `security_headers_enabled`. Production overlay sets `trust_forwarded_for = true`.

## Testing Strategy

Component (`tests/`):
1. **`test_dashboard_ratelimit.py`** — constructor validation, sliding-window correctness, per-key isolation, TTL eviction, monotonic-clock independence, `resolve_client_ip` honors/ignores `X-Forwarded-For` correctly.
2. **`test_dashboard_public_middleware.py`** — 405 before rate-limit consumption; `Cache-Control` + weak `ETag`; 304 round-trip with headers preserved; 429 carries `Retry-After` + `X-RateLimit-*`; reverse-whitelist invariant (route → `_PUBLIC_PATHS`) + its mirror (entry → route); `PublicIdentityResponse` schema conformance; `_PUBLIC_PREFIXES == ("/static/",)`.
3. **`test_dashboard_security_headers.py`** — all five headers on public 200, authed 200, 401, `/login`, `/static/*`; removed entirely when config disabled.
4. **`test_dashboard_static.py`** — all six asset paths return 200 + correct content-type without auth; path traversal rejected; missing asset → 404.
5. **`test_dashboard_auth.py`** — `test_public_paths_exact_set` extended to lock `_PUBLIC_PREFIXES`; sibling test confirms `/static/*` bypasses auth under a real token.

Browser (`tests/browser/`):
- Existing `test_public_page.py`, `test_login.py`, `test_dashboard.py` suites exercise the externalized CSS/JS paths end-to-end — no regression means CSP + extraction work together.
- Existing `test_toasts.py::test_toast_disappears` implicitly exercises the `.style.cssText = …` path — proves `style-src 'unsafe-inline'` is sufficient without refactoring the toast helper.

```bash
python3.12 -m ruff check maestro/ tests/
python3.12 -m mypy maestro/
python3.12 -m pytest tests/ -v
python3.12 -m pytest tests/browser/ -v -m browser --no-cov --browser chromium
```

## Risk

- **CSP blocking a legitimate feature.** Browser tests cover all three pages end-to-end. Known risk: the `.style.cssText` toast helper — explicitly kept working by the `'unsafe-inline'` compromise in `style-src`.
- **ETag body buffering breaking a future streaming endpoint.** Middleware only buffers + rewrites when content-type starts with `application/json`; everything else passes through untouched. A future streaming handler that returns `text/event-stream` is safe by default.
- **Rate-limit starving legitimate traffic under spoofed headers.** In dev (`trust_forwarded_for=false`) forwarded headers are ignored entirely. In production, the rate limiter reads `X-Real-IP` which nginx sets to `$remote_addr` via an overwriting `proxy_set_header` directive — unspoofable by clients. `X-Forwarded-For` is not used because nginx's `$proxy_add_x_forwarded_for` *appends* (not strips) `$remote_addr` to any client-supplied value, making the leftmost hop attacker-controlled (#1470).
- **`BaseHTTPMiddleware` iterating the body of the `/api/public/identity` response uses extra memory per request.** Payload is small (< 4 KB), rate limit is 60/min per IP, so the marginal memory is negligible.
