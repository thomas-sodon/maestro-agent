# 028: Dashboard Session TTL and Bearer Token Rotation

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/dashboard/auth.py`, `maestro/dashboard/routes.py`, `maestro/config.py`, `maestro.toml` |

## Description

The dashboard bearer is a single long-lived secret. A leaked value remains usable indefinitely, and a compromised browser session carries no expiry. Add a configurable session TTL to the login cookie and an in-repo rotation tool that writes a new bearer and invalidates prior sessions by replacing the stored token value (byte-for-byte equality, not a version prefix).

## Considered alternatives

- **Versioned token (`v1:<opaque-random>`).** Rejected. With exactly one stored token compared byte-for-byte, a freshly-generated opaque token already invalidates every prior session, so a monotonically increasing prefix would add a parser in `check_secrets()` and prefix matching in `BearerAuthMiddleware` without any discrimination benefit. If race-free rotation across multiple hosts is later required, a bounded two-version ring (accept the previous generation during a grace window) would justify a version prefix; absent that requirement, byte-for-byte equality is sufficient.

## Relevant Findings

- Cookie is currently browser-session only (`routes.py` sets no `Max-Age`).
- `BearerAuthMiddleware` compares the cookie value byte-for-byte with `config.dashboard_token`.
- `MAESTRO_DASHBOARD_TOKEN` is the only auth secret; no versioning.

## Implementation Plan

1. Rotation generates a new opaque random token. Because only one token is stored in `config.dashboard_token` and `BearerAuthMiddleware` compares it byte-for-byte, replacing the stored value already invalidates every prior session — no version prefix is needed. No prefix parsing in `check_secrets()` and no prefix matching in `BearerAuthMiddleware`.
2. Add `[dashboard] session_ttl_seconds` config (default 86400). Login sets `Max-Age` / `Expires` on both `maestro_token` and `maestro_csrf`. Expired cookies fall through to the login redirect.
3. New CLI `maestro rotate-dashboard-token` — generates a new token, prints it, updates `.env` or prints instructions. Existing sessions become invalid because the stored token value changed; the new opaque token no longer matches the cookies prior sessions hold.

## Testing Strategy

1. Cookie set with `Max-Age=session_ttl_seconds`.
2. Expired cookie treated as anonymous.
3. Rotation CLI produces a new opaque token; old tokens are rejected.
4. Middleware rejects a token that does not match the current stored value byte-for-byte.

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Operators forget to deploy the new token | Low | Rotation CLI prints the update steps; `check_secrets()` validates format |
| Rotation invalidates active sessions mid-review | Accepted | By design — rotation is a security event |
