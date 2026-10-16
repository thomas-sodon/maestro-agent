# Dashboard Security

The hardening substrate for the dashboard: public-API protections for `/api/public/*` (proposal 038) and authenticated-mutation protections — CSRF, rate limiting, strict request schemas — for the rest (proposal 022). For the high-level overview, see [CLAUDE.md](../CLAUDE.md). Original proposals: [038-public-api-hardening.md](proposals/security/038-public-api-hardening.md), [022-dashboard-csrf-rate-limit-schemas.md](proposals/security/022-dashboard-csrf-rate-limit-schemas.md).

## Middleware Stack

Registered in `maestro/dashboard/app.py`. Registration order matters because `add_middleware` **prepends** to the stack (last-added is outermost):

```
CSRF → MutationRateLimit → BearerAuth → PublicApi → SecurityHeaders   (registration order)
SecurityHeaders → PublicApi → BearerAuth → MutationRateLimit → CSRF   (request flow, outermost first)
```

`SecurityHeadersMiddleware` is outermost so even a 401 emitted by `BearerAuthMiddleware` flows back through it and carries CSP + `Permissions-Policy` + the other headers. `CSRFMiddleware` and `MutationRateLimitMiddleware` sit **inside** `BearerAuth` so they only ever see authenticated requests (unauthenticated spray gets its 401 before it can burn rate-limit budget), and they are mounted only when `MAESTRO_DASHBOARD_TOKEN` is set — without auth there is no login flow to issue the CSRF cookie and no ambient credential for a cross-site request to ride.

Every middleware path decision reads the ASGI routed path (`scope["path"]`, via `auth.routed_path`), never the Host-derived `request.url` — a crafted `Host: x/login?` makes `request.url.path == "/login"` while the router still dispatches the real path. `tests/test_dashboard_auth.py::test_middlewares_never_read_url_path` forbids any `.url.path` read under `maestro/dashboard/`.

## `CSRFMiddleware` (proposal 022)

Lives in `maestro/dashboard/auth.py`. Double-submit pattern:

- `POST /login` (success only) sets a second cookie `maestro_csrf` = `secrets.token_urlsafe(32)`, rotated on every login. `SameSite=Strict`, conditionally `Secure` (mirrors the session cookie), **not** `HttpOnly` — the dashboard JS must read it. It is a challenge, not a second secret; the session cookie stays `HttpOnly`.
- Every mutating request (anything outside GET/HEAD/OPTIONS) must echo that cookie in an `X-CSRF-Token` header or it is rejected with 403. `index.js`'s `csrfHeaders()` helper attaches it to every mutating `fetch()`.
- Exempt: public paths/prefixes, `/login` (no cookie exists before login), `/logout` (body-less; plain HTML forms cannot attach headers; a forged logout costs a re-login, not state), and requests carrying a **valid** `Authorization: Bearer` header (explicit per-request credentials cannot be forged cross-site). An *invalid* bearer header does not exempt — such a request only passed auth via its session cookie.

## `MutationRateLimitMiddleware` (proposal 022)

Lives in `maestro/dashboard/ratelimit.py`, reusing the same `SlidingWindowLimiter` primitive as the public API. Two independent per-IP budgets (sliding 60 s window, `Retry-After` on 429):

- `POST /login` — brute-force guard; failed attempts count.
- `POST /api/{plans,publications}/{id}/{approve,reject}` — caps how fast the human-approval surface can be flipped.

Client IP resolution goes through `resolve_client_ip()` (see below) — `X-Real-IP` only when trusted, never `X-Forwarded-For`.

## Strict Request Schemas (proposal 022)

`maestro/dashboard/routes.py` validates mutation bodies with Pydantic models carrying `model_config = ConfigDict(extra="forbid")`, so unknown fields and wrong types are refused instead of being silently dropped or coerced. Plan approve takes a `PlanApproveBody` (`content_hash` required); FastAPI validates it at parameter binding, so a missing hash or an extra field is a 422. Plan reject takes a `PlanRejectBody` (`content_hash` required, `reason` optional), validated inside the route, so an invalid body is a 400. `RejectRequest` covers publication reject, and `MessageRequest` covers `POST /api/messages`; both are 400s on an invalid body. The `message_max_length` clamp and blank-reason fallback are preserved. Publication approve is the only approve endpoint that takes no body. A stale `content_hash` on plan approve or reject returns 409; the authoritative check is `update_plan_status`'s `expect_content_hash`, evaluated under the plan `file_lock` (proposal 021).

## `PublicApiMiddleware`

Lives in `maestro/dashboard/public_api.py`. Scopes itself to the `/api/public/` path prefix.

### Method check before rate-limit bookkeeping

The method check runs **before** rate-limit bookkeeping so a flood of 405s cannot starve legitimate GETs.

### Sliding-window rate limiter

- Keyed on `(resolved_ip, path)` so one hot endpoint can't block another.
- Uses `time.monotonic()` so NTP/wall-clock jumps can't prematurely free the window.
- Evicts keys idle for `max(60, 2 * window)` seconds on access to stay bounded.

### `resolve_client_ip()`

Reads `X-Real-IP` when `[dashboard] trust_forwarded_for=true` (production: nginx sets this to `$remote_addr` via an overwriting `proxy_set_header` directive — unspoofable by clients). Falls back to the peer address when `X-Real-IP` is absent/empty or when the flag is off (dev default). `X-Forwarded-For` is intentionally **not** used: nginx's `$proxy_add_x_forwarded_for` *appends* `$remote_addr` to any client-supplied value, so the leftmost hop is attacker-controlled and can be rotated to bypass per-IP rate limiting (#1470).

### Cache headers (JSON-only)

Injects `Cache-Control: public, max-age=<cfg>, s-maxage=<cfg>, stale-while-revalidate=<2*cfg>` and a weak `ETag: W/"<sha256-8>"` — but only on fully-materialized JSON responses (`Content-Type` starts with `application/json`). Non-JSON / non-2xx pass through untouched so the middleware never interferes with streaming or error bodies.

A matching `If-None-Match` short-circuits to 304 **with the cache headers preserved**. Some intermediaries drop validator-less 304s as uncacheable, so preserving them is load-bearing.

## `SecurityHeadersMiddleware`

Lives in `maestro/dashboard/security_headers.py`. Gated by `[dashboard] security_headers_enabled` (default `true`). Sets:

- `X-Content-Type-Options`
- `Referrer-Policy`
- `X-Frame-Options`
- `Permissions-Policy`
- A CSP of:
  ```
  default-src 'self';
  script-src 'self';
  style-src 'self' 'unsafe-inline';
  img-src 'self' data:;
  font-src 'self';
  connect-src 'self';
  frame-ancestors 'none';
  base-uri 'self';
  form-action 'self'
  ```

`script-src 'self'` is strict (blocks the real XSS vector). `style-src 'self' 'unsafe-inline'` is a deliberate compromise to keep existing inline `style="..."` attributes and `index.js`'s `.style.cssText = …` toast helper working.

HSTS stays at nginx (the `Strict-Transport-Security` `add_header` in the 443 `server` block of `nginx/maestro.conf`) — not duplicated at the app layer.

## Static Asset Extraction

The CSS/JS blocks that used to live inline in `public.html`, `index.html`, and `login.html` are now extracted into `maestro/dashboard/static/*.css` and `*.js` and served by a `_NoCacheStaticFiles` subclass of `StaticFiles` at `/static/*`.

The subclass stamps `Cache-Control: no-cache` on every file response so Cloudflare and browsers always revalidate after a deploy (matching the policy already applied to the HTML shells `/`, `/account`, `/login`). Without this, the bind-mounted asset on disk updates but the edge serves the stale copy until its default cache expires.

## Auth Exemption Discipline

Auth exempts static assets via `_PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)` in `maestro/dashboard/auth.py`. This is kept as a **separate, explicitly named** tuple rather than mixed into `_PUBLIC_PATHS` (which stays exact-match) so the "no silent exemption" discipline still holds.

### Invariant tests

- `test_public_paths_exact_set` asserts both containers verbatim.
- The public-API test module asserts the **reverse-whitelist invariant**: every registered `/api/public/*` route is in `_PUBLIC_PATHS`, and every `_PUBLIC_PATHS` entry maps to a registered route.

## Diagnostics Fail-Soft Boundary

`/api/diagnostics` is the **only** dashboard endpoint that holds a fail-soft boundary — every other route fails loud, per CLAUDE.md → "Fail loud — no warning-silencing exception handlers". It is best-effort observability polled on every dashboard refresh, so `_diagnostics_response` (`maestro/dashboard/routes.py`) tolerates *any* raise while building the diagnostic snapshot or reading an agent accessor: a permission error on the app-log path, a corrupt log line, a burn-rate or throttle accessor raising.

- **What the caller sees.** The degraded response is `_DIAGNOSTICS_FALLBACK`, which is also the single source of the success path's key set — so both paths carry the full response schema and the browser keeps a stable shape whichever one runs. Its role, not its keys, is the contract; read the dict for the values.
- **The failure stays loud.** The handler logs at `warning` with `exc_info`, so the raise is recoverable from the app log for RCA. Degrading the *response* is not silencing the *failure*.
- **The exemption is named, not implied.** `_diagnostics_response` is a module-level helper rather than an inline `try`/`except` in the endpoint closure precisely so the fail-loud fence can exempt this one guard by name: `("maestro/dashboard/routes.py", "_diagnostics_response")` in `_FUNCTION_ALLOWLIST` (`tests/test_no_silenced_exceptions.py`). Its breadth is pinned by `_ENTRY_HANDLER_BASELINE`, and `_ENTRY_DOC_ANCHOR` binds the entry to this section, so this rationale cannot be deleted or renamed while the exemption stands.

Narrowing the handler is not an improvement to chase: the contract genuinely covers any raise while reading operational state.

## Response Schemas

Response schemas live in `maestro/dashboard/public_schemas.py` as TypedDicts. `public_identity()` constructs a `PublicIdentityResponse`, and a test asserts `set(handler.keys()) == set(TypedDict.__annotations__)` so adding a new key without widening the schema fails the test.

## Tuning

| Key | Default | Purpose |
|---|---|---|
| `[dashboard] public_api_rate_limit_per_minute` | 60/min | Sliding-window limiter ceiling |
| `[dashboard] public_api_cache_max_age_seconds` | 30s | `max-age` / `s-maxage`; SWR is `2× max-age` |
| `[dashboard] security_headers_enabled` | `true` | Master gate for `SecurityHeadersMiddleware` |
| `[dashboard] trust_forwarded_for` | `false` | Read `X-Real-IP` (set by nginx) in `resolve_client_ip()` |
| `[dashboard] login_rate_limit_per_minute` | 10/min | Per-IP `POST /login` budget (brute-force guard) |
| `[dashboard] approval_rate_limit_per_minute` | 30/min | Per-IP plan/publication approve+reject budget |
