# 022: Dashboard CSRF, Rate Limiting, and Strict Request Schemas

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/dashboard/auth.py`, `maestro/dashboard/routes.py`, `maestro/dashboard/app.py`, `maestro/dashboard/templates/login.html`, `maestro/config.py`, `maestro.toml` |

## Description

> Status updated to shipped on 2026-08-07 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The dashboard already sets `SameSite=Strict`, `HttpOnly`, and a conditionally-`Secure` session cookie, but none of the state-changing endpoints (`/login`, `/logout`, `/api/plans/*/approve|reject`, `/api/messages`) carry a CSRF token, have a rate limit, or validate their request bodies against a strict schema. A single leaked bearer token or a subdomain-scoped XSS is enough to flip an arbitrary plan to `approved`. This proposal adds three in-process controls with no new runtime dependency.

## Relevant Findings

- `BearerAuthMiddleware` at `maestro/dashboard/auth.py` only checks the bearer header / cookie; it does not enforce CSRF.
- Cookie is set at `maestro/dashboard/routes.py` with `HttpOnly=True`, `SameSite="strict"`, conditionally `Secure`.
- Middleware order in `maestro/dashboard/app.py` registers only `BearerAuthMiddleware`; no CORS, `TrustedHost`, or security-header middleware. <!-- 038 has since added `PublicApiMiddleware` and `SecurityHeadersMiddleware`; they do not enforce CSRF on the authenticated mutation surface, so this proposal's scope still stands. -->
<!-- (see #1380) The original IP-resolution bullet below conflated `trust_forwarded_proto` with the IP-trust decision; proposal 038 shipped a separate `trust_forwarded_for` flag plus the `resolve_client_ip` helper. The rewritten bullet and Implementation Plan step 6 reflect that shipped reality, so future implementers of the unimplemented CSRF / per-route rate-limit pieces below cannot re-introduce the conflation. -->
- Approve/reject routes at `maestro/dashboard/routes.py` use raw `await request.json()` with no Pydantic model.
- `POST /api/messages` at `maestro/dashboard/routes.py` mirrors the same raw-body pattern.
- `trust_forwarded_for` (`maestro/config.py`, consumed by `resolve_client_ip` at `maestro/dashboard/ratelimit.py`) is the IP-trust gate that proposal 038 shipped for rate-limit key resolution. `trust_forwarded_proto` (`maestro/config.py`, read at `maestro/dashboard/routes.py`) remains scoped to the cookie `Secure` flag and is **not** the IP-trust decision.

## Implementation Plan

1. New `CSRFMiddleware` in `maestro/dashboard/auth.py` — mounted after `BearerAuthMiddleware` in `app.py`. Rejects any `POST/PUT/DELETE/PATCH` without an `X-CSRF-Token` header that matches the `maestro_csrf` cookie. Public paths and `/login` are exempt (login has no prior cookie); the `/login` response sets the CSRF cookie for subsequent requests.
2. On successful login (`maestro/dashboard/routes.py`), generate `secrets.token_urlsafe(32)` and set it as a second cookie `maestro_csrf` with `SameSite=Strict`, conditionally `Secure`, `HttpOnly=False` (JS needs to read it). Rotate on every login.
3. Update `login.html` and any approve/reject UI to include a tiny helper that reads the cookie and attaches `X-CSRF-Token` to every `fetch()`.
4. Add Pydantic models to `routes.py`: `ApproveRequest {content_hash: str}`, `RejectRequest {content_hash: str, reason: str | None}`, `MessageRequest` (matching today's body), each with `model_config = ConfigDict(extra="forbid")`. Replace raw `request.json()` calls.
5. New in-process token-bucket rate limiter (dict keyed by resolved client IP; `collections.deque` of timestamps per bucket; no external dep). Apply to `/login` (per IP, config `login_rate_limit_per_minute`, default 10) and `/api/plans/*/approve|reject` (config `approval_rate_limit_per_minute`, default 30). Return `429` with `Retry-After` when exceeded.
6. Client IP resolution: call into `resolve_client_ip(request, trust_forwarded_for=...)` at `maestro/dashboard/ratelimit.py` — proposal 038 already shipped this helper and the `[dashboard] trust_forwarded_for` config key. The CSRF / login-rate-limit work in this proposal must reuse that helper rather than re-derive trust from `trust_forwarded_proto` (which is scoped to the cookie `Secure` flag, a different decision). Note that `trust_forwarded_for` honours `X-Real-IP` (set unspoofably by nginx via overwriting `proxy_set_header X-Real-IP $remote_addr`), **not** `X-Forwarded-For` — the leftmost hop of `X-Forwarded-For` is attacker-controlled under nginx's `$proxy_add_x_forwarded_for` and must never gate rate-limit keys (#1470).
7. New config keys in `[dashboard]`: `login_rate_limit_per_minute`, `approval_rate_limit_per_minute`. Document in `maestro.toml`.

## Testing Strategy

1. POST `/api/plans/.../approve` with a valid bearer cookie but missing `X-CSRF-Token` → 403.
2. POST with matching header + cookie → 200 (regression guard for the new middleware order).
3. Login rotates the CSRF cookie value on each successful login.
4. Bucket: 11th login attempt within the window → 429 with `Retry-After`; bucket drains on time.
5. `X-Real-IP` honoured for IP resolution only when `trust_forwarded_for=True`; `X-Forwarded-For` ignored regardless (matches the shipped `resolve_client_ip` contract at `maestro/dashboard/ratelimit.py`).
6. Pydantic models reject unknown fields and wrong types; message body length still clamped to `message_max_length`.
7. Browser tests in `tests/browser/test_login.py` and `tests/browser/test_approve_reject.py` updated to include the CSRF header; add a negative case that omits it.

Run: `python3.12 -m pytest tests/test_dashboard_routes.py tests/browser/test_login.py tests/browser/test_approve_reject.py -v`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| In-memory bucket loses state on restart | Low | Bucket is per-process; restart re-opens fresh windows. Acceptable — the attack we care about is sustained flooding |
| JS helper is XSS-sensitive (reads a readable cookie) | Accepted | The attacker model already assumes the bearer cookie is HttpOnly; CSRF cookie is not a second secret, only a challenge |
| Behind a reverse proxy, bucket keys collide under a single hop | Low | `trust_forwarded_for` gates `X-Real-IP` (set unspoofably by nginx) for IP resolution via `resolve_client_ip` — keeps proxies and direct connections consistent |
| Adds latency to every request | Very low | Middleware is a constant-time dict lookup |
