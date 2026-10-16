# 034: Visitor Event Capture Infrastructure

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/visitor_tracking.py` (new), `maestro/dashboard/visitor_middleware.py` (new), `maestro/dashboard/app.py`, `maestro/dashboard/auth.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `.gitignore`, `CLAUDE.md` |

## Description

The dashboard container serves a public intro page at `/` (HTML) and a polled JSON endpoint at `/api/public/identity`. Today the service is blind to its audience — neither the operator nor Maestro itself can see who or what is looking. For a self-aware agent, being unaware of observers is a conspicuous blind spot.

This proposal adds the minimum infrastructure to capture privacy-respecting visit events: a JSONL store, a GET-only middleware on `/`, SHA-256-with-salt IP hashing, UA classification (browser / bot / unknown), and operator self-exclusion via a shared authentication helper. No endpoints, no UI, and no agent behaviour change in this phase — data silently accumulates.

The deliberate property of this phase: after 034 lands and before proposals 035 / 036 / 037 follow, visit data exists on disk but has *no viewer* — not the operator, not the agent, not the public. This write-only period lets operators validate shape and correctness via direct JSONL inspection before any automated read-path is wired up. Not an oversight — an intentional privacy posture.

## Relevant Findings

- `maestro/messaging.py` (`file_lock` + append JSONL and `atomic_write` for rewrite) are the exact idioms to copy — visitor events cross container boundaries (dashboard writes, loop reads), so locking is required.
- `maestro/cost_tracking.py` shape is the model for `build_visitor_summary` and the `cost_samples.jsonl` analogue.
- `maestro/dashboard/app.py` currently adds only `BearerAuthMiddleware`. Starlette's last-added-is-outermost rule means visitor middleware must be registered **first** so auth wraps it. The visitor dispatch in step 3 already short-circuits on `path != "/"` and on `is_request_authenticated`, and `/` is in `_PUBLIC_PATHS`, so bearer never rejects a request visitor would have recorded — the wrapping is defense-in-depth: if the visitor path filter is ever loosened, bearer still prevents private-path requests from reaching the recorder.
- `maestro/dashboard/auth.py` already contains `_check_bearer` and `_check_cookie`; unify them behind a new `is_request_authenticated(request, token)` helper so both middlewares use the same self-exclusion logic.
- `_PUBLIC_PATHS` in `auth.py` already contains `/` and `/api/public/identity`; this phase does not modify it (proposal 036 does).

## Implementation Plan

1. Create `maestro/visitor_tracking.py` with `VisitorEvent` (`schema_version: int = 1`, `timestamp: str`, `path: str`, `visitor_hash: str`, `user_agent: str`, `ua_class: str`, `referer: str`), a `PairCount` dataclass (`name: str`, `count: int`), and `VisitorSummary` (`window_start`, `window_end`, `window_hours`, `deduped_visits`, `raw_events`, `unique_visitors`, `browser_visits`, `bot_visits`, `unknown_visits`, `top_user_agents: list[PairCount]`, `top_referers: list[PairCount]`). `PairCount` serializes as `{"name": ..., "count": ...}` via `asdict()` — mypy-clean and matches the shape the Visitors-tab bar-chart JS (proposal 035) expects. Functions: `append_visitor_event`, `build_visitor_summary(path, window_hours, dedupe_seconds=60, ua_class_filter=None)`, `hash_visitor_ip`, `classify_user_agent`, `prune_visitor_events(path, retention_days=14)`, `load_or_create_visitor_salt(memory_dir)`. Dedupe runs at read time keyed on `(visitor_hash, floor(ts/dedupe_seconds))` — no in-memory LRU. When `ua_class_filter` is set (e.g. `"browser"`), rows whose `ua_class` does not match are dropped before the dedupe key is computed, so all derived counts (`deduped_visits`, `raw_events`, `unique_visitors`, `browser_visits`/`bot_visits`/`unknown_visits`) reflect the filtered subset. `build_visitor_summary` tolerates a malformed last line (drops silently) so a loop-container read that races a dashboard write at EOF never crashes. `unique_visitors` explicitly excludes empty hashes (the catastrophic salt-fail sentinel).
2. Salt resolution in `load_or_create_visitor_salt`: (a) `MAESTRO_VISITOR_IP_SALT` env var wins; (b) read persistent `.visitor_salt` file (mode 0600) if present; (c) auto-generate via `secrets.token_hex(32)`, write via `atomic_write`, then `os.chmod(0o600)`; (d) catastrophic fallback emits `visitor_hash=""` for every event in this process + WARNING (the empty-hash sentinel that step 1's `unique_visitors` exclusion and Testing Strategy test 8 rely on). Wrap the whole load-or-create body in `file_lock(memory_dir / ".visitor_salt.lock")` and re-check file existence inside the lock to close the multi-worker race. Never log the salt value itself.
3. Middleware (`maestro/dashboard/visitor_middleware.py`) is a `BaseHTTPMiddleware` subclass. Dispatch skips when: method ≠ GET, path ≠ `/`, `is_request_authenticated(request, token)` returns True, or the whole body fails. Client IP comes from `X-Forwarded-For` (first entry, strip IPv6 brackets) when `trust_forwarded_proto=True`, else `request.client.host`. Entire body wrapped in `try/except Exception` with a `time.monotonic()`-gated 60s-throttled WARNING — tracking must never break a request.
4. Before persistence: strip query string from referer (`urllib.parse.urlsplit(...)._replace(query="", fragment="").geturl()`), truncate to 200 chars; UA also truncated to 200.
5. Pruning: (a) opportunistic every 1000 writes via module-level counter — note that the counter lives in the dashboard process only; the loop container reads `visitor_events.jsonl` but never prunes on write, so the 1/1000 threshold is owned by the dashboard worker that observes the writes; (b) startup prune in `maestro serve` so low-traffic deployments stay bounded. Prune failures are logged and swallowed — retention drift is preferable to service disruption. If the dashboard is ever scaled to multiple workers (Gunicorn / uvicorn `--workers N`), each worker maintains its own counter and the effective prune frequency multiplies by N; under that deployment shape prune responsibility should move to a single authority — the loop container, which already owns the write-side of every other long-lived JSONL via `Memory.archive()`.
6. In `app.py`, call `app.add_middleware(VisitorMiddleware, …)` *before* `app.add_middleware(BearerAuthMiddleware, token=token)` so the request-time order is `request → BearerAuth → Visitor → route` (Starlette: last-added is outermost — see the bullet in *Relevant Findings*). Token is propagated as `""` when unset so `is_request_authenticated(request, "")` returns False for everyone and self-exclusion degrades gracefully.
7. Extract `is_request_authenticated(request, token)` in `auth.py` — used by both `BearerAuthMiddleware` and the new visitor middleware.
8. Config: three new `[dashboard]` keys owned by this phase — `visitor_tracking_enabled` (default `true`), `visitor_dedupe_window_seconds` (60), `visitor_retention_days` (14). Add `Config.visitor_events_path` property returning `memory_dir / "visitor_events.jsonl"`. New `_TOML_TO_CONFIG` entries in `toml_config.py`. `maestro.toml` documents the defaults. `visitor_public_line_enabled` lands in proposal 036; `visitor_awareness_enabled` lands in proposal 037 — each config introduction lives with its consumer.
9. `check_secrets()` warns when `MAESTRO_VISITOR_IP_SALT` is unset (not fatal — the persistent file is the fallback).
10. `.gitignore`: add `.visitor_salt`. `memory/` is already ignored; this is belt-and-suspenders for operators who relocate `memory_dir`.
11. `CLAUDE.md`: add `MAESTRO_VISITOR_IP_SALT` row to the env-var table; add one Key-Design-Decisions bullet summarising the capture design. Subsequent phases (035 / 036 / 037) each add their own CLAUDE.md bullet so the doc tracks the feature as it rolls out.

Timestamps use `datetime.now(UTC).isoformat()` (matches `cost_tracking`).

## Testing Strategy

Unit:
1. `hash_visitor_ip` is deterministic for a given salt; different salts produce different hashes.
2. `classify_user_agent` returns `"bot"` for `Googlebot/2.1`, `curl/7.x`, `python-requests/2.x`; `"browser"` for Firefox/Chrome UAs containing `"Mozilla"` without bot keywords; `"unknown"` for empty string and other non-Mozilla strings.
3. `build_visitor_summary` collapses two events 30 s apart into one `deduped_visits` bucket while leaving `raw_events == 2`; two events 90 s apart produce `deduped_visits == 2`.
4. `build_visitor_summary(..., ua_class_filter="browser")` against a fixture of two browser events with `visitor_hash="A"` and one bot event with `visitor_hash="B"`, all in the same dedupe window, reports `raw_events == 2`, `deduped_visits == 1`, `unique_visitors == 1`; calling with `ua_class_filter=None` against the same fixture reports `raw_events == 3`, `deduped_visits == 2`, `unique_visitors == 2`. The two same-hash browser rows collapsing into one dedupe bucket only when the filter is applied first distinguishes filter-before-dedupe from filter-after-dedupe.
5. `prune_visitor_events` drops events older than `retention_days` at the exact boundary (inclusive of the cutoff), preserves recent rows, and cleans up the temp file on `atomic_write` failure.
6. `load_or_create_visitor_salt` — env var wins; existing file is reused; auto-generate creates file with mode 0600; WARNING emitted on auto-generate; race-safety test runs two threads against a real `tmp_path` (mem_fs cannot exercise `fcntl.flock`) and asserts identical salt returned.
7. Malformed last line in the JSONL is dropped silently by `build_visitor_summary` (no exception).
8. Rows with `visitor_hash == ""` (catastrophic salt failure) are excluded from `unique_visitors` but still included in `raw_events` and per-class counts.
9. Timestamp output matches `"+00:00"` tail.

Component (AsyncClient + ASGITransport, per `tests/test_dashboard_auth.py`):
1. GET `/` writes one row with expected fields.
2. Two GETs of `/` within 60 s produce two raw rows; `build_visitor_summary` reports `deduped_visits == 1`.
3. GET `/health`, `/api/public/identity`, `/account` write zero rows.
4. Authenticated `Bearer <token>` GET `/` writes zero rows (operator self-exclusion).
5. HEAD `/` writes zero rows (method filter).
6. Patched `open()` raising `OSError` does not propagate — request returns 200, one throttled WARNING logged.

```bash
python3.12 -m pytest tests/test_visitor_tracking.py tests/component/test_visitor_middleware.py -v
```

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Salt file race between workers / startup | Medium | `file_lock` around load-or-create + re-check existence inside the lock |
| Referer PII leakage (sensitive internal URL in referer header) | Medium | Strip query string before persistence; operator-only visibility via proposal 035; 14d retention bounds exposure window |
| JSONL growth unbounded on low-traffic deployments | Low | Startup prune in `maestro serve` + opportunistic 1/1000-write prune |
| fcntl advisory-lock semantics on NFS | Low | Current arch is single-host bind mount; documented limitation if that changes |
| Tracking middleware breaks a request | High (if unhandled) | Entire dispatch wrapped in `try/except Exception` with throttled WARNING — request continues regardless |
