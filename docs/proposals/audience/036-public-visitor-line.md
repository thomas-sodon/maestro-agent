# 036: Public Visitor Line

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/dashboard/routes.py`, `maestro/dashboard/auth.py` (`_PUBLIC_PATHS`), `maestro/dashboard/public_schemas.py` (`PublicIdentityResponse` — the curated `/api/public/identity` subset gains `visitor_retention_days`), `maestro/dashboard/templates/public.html`, `maestro/dashboard/static/public.js` (`loadIdentity()`), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `tests/test_dashboard_auth.py`, `tests/test_dashboard_public_api.py` (`test_public_identity_payload_keys_are_exact` — exact-keys assertion updated in lockstep), `CLAUDE.md` |

## Description

Add one humanising line on the public intro page: *"N people have seen me in the last 24 hours — anonymous, hashed IPs, 14d retention"*. Driven by a rate-limited public endpoint that consumes the summary built by proposal 034.

Two decisions shape this phase. First, the count is **humans-only** (browser-class visitors). "People have seen me" is dishonest if most of the "people" are Googlebot — treating the public line as a humanising gesture rather than telemetry means only browser-class visits feed it. Second, the line is **hidden when count < 2** to avoid the awkward "1 = just the operator" case.

A disclosure span inside the line itself ("anonymous, hashed IPs, 14d retention") sets the privacy expectation where it matters — on the public-facing surface that visitors can see. Operators who want internal capture without the public line can disable it via the new `visitor_public_line_enabled` config.

## Relevant Findings

- `templates/public.html` — existing `.uptime` element to mirror (`<div class="uptime" id="uptime"></div>`); the file is short, so the visitor line lands alongside, not inside, this element.
- `loadIdentity()` in `maestro/dashboard/static/public.js` already polls `/api/public/identity` every 30 s (via `setInterval(loadIdentity, 30000)`); adding a parallel `fetch("/api/public/visitor-count")` at the same cadence costs one extra request per poll.
- `_PUBLIC_PATHS` in `maestro/dashboard/auth.py` gates which paths bypass auth; this phase adds `/api/public/visitor-count` to it.
- `tests/test_dashboard_auth.py::test_public_paths_exact_set` asserts the exact set — must be updated in lockstep.

## Implementation Plan

1. `GET /api/public/visitor-count` in `routes.py`: unauthenticated (`_PUBLIC_PATHS` entry added). Returns `{"unique_visitors_24h": int, "deduped_visits_24h": int}`. Both counts are derived from **browser-class visitors only** via `build_visitor_summary(path, window_hours=24, ua_class_filter="browser")` (the `ua_class_filter` parameter introduced in proposal 034) — pre-dedupe filtering ensures `deduped_visits` and `unique_visitors` reflect the browser subset directly, not a post-hoc partition of mixed totals. Wrapped in a 30 s in-process TTL cache: module-level `dict[tuple[float, str | None], tuple[float, dict]]` keyed on `(window_hours, ua_class_filter)`, returns cached value when `time.monotonic() - cached_at < 30` else rebuilds. Keying on the full `(window_hours, ua_class_filter)` tuple prevents a future caller in the same module that passes a different filter or window (e.g. a debug variant) from silently receiving the browser-filtered 24 h totals stored by this endpoint. Registration of both the endpoint and the template element is gated on the new config key (next step) — when false, the endpoint is not registered at all (404 on GET) and the template element is not emitted.
2. Template change in `public.html`: add one `<p class="uptime visitor-line" id="visitor-line" hidden>` element near the existing uptime line, reusing `.uptime` styling. Extend `loadIdentity()` (in `maestro/dashboard/static/public.js`) with a parallel `fetch("/api/public/visitor-count")`. Render when `unique_visitors_24h >= 2`; hide (`el.hidden = true`) on fetch failure or count < 2. Markup:
   ```html
   <strong>${count}</strong> people have seen me in the last 24 hours
     — <span class="muted-small">anonymous, hashed IPs, ${retention}d retention</span>
   ```
   The `retention` value comes from the same `/api/public/identity` payload (add `visitor_retention_days` there as a small extension) so the disclosure never drifts from the actual config. Adding `visitor_retention_days` to `PublicIdentityResponse` (`maestro/dashboard/public_schemas.py`) widens the curated subset, so the exact-keys test `test_public_identity_payload_keys_are_exact` and its `EXPECTED_KEYS` constant (`tests/test_dashboard_public_api.py`) must be updated in the same step.
3. Introduce `[dashboard] visitor_public_line_enabled` (default `true`) in `config.py`, `toml_config.py` (`_TOML_TO_CONFIG` entry), and `maestro.toml`. Gates endpoint registration and template element emission.
4. Update `tests/test_dashboard_auth.py::test_public_paths_exact_set` with the new public path.
5. Update `CLAUDE.md` with one Key-Design-Decisions bullet describing the public visitor line.

## Testing Strategy

Component:
1. `GET /api/public/visitor-count` returns 200 without auth; response contains both integer keys.
2. Second call within 30 s returns the cached value — assert `build_visitor_summary` is called exactly once across two requests.
3. With `visitor_public_line_enabled=false`, `GET /api/public/visitor-count` returns 404.
4. Counts reflect only `ua_class == "browser"` — a fixture with one browser event + five bot events produces `unique_visitors_24h == 1` and `deduped_visits_24h == 1`.
5. `test_public_paths_exact_set` still passes after the addition.
6. `GET /api/public/identity` now includes `visitor_retention_days`; update `test_public_identity_payload_keys_are_exact` (and `EXPECTED_KEYS`) to the new exact key set, and assert the value reflects the configured retention.

Browser:
1. Public page element `#visitor-line` is present in the DOM but `hidden` when fixture browser count < 2.
2. With fixture browser count ≥ 2, the element is visible and contains the rendered count and retention string.

```bash
python3.12 -m pytest tests/component/test_public_visitor_count.py tests/test_dashboard_auth.py::test_public_paths_exact_set tests/test_dashboard_public_api.py::test_public_identity_payload_keys_are_exact -v
python3.12 -m pytest tests/browser/test_public_page.py -v -m browser --no-cov --browser chromium
```

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Unauthenticated endpoint scraped in a loop | Low | 30 s TTL cache bounds backend cost regardless of request rate |
| Humans-only count under-reports when bots spoof browser UAs | Low | Documented in proposal; public line is a humanising gesture, not telemetry — precision not required |
| Disclosure span drifts from actual retention config | Low | Render retention value from `/api/public/identity` payload, not hardcoded |
| Count reveals that Maestro has few human visitors | Accepted | Honesty is the point — hiding when count < 2 avoids the worst case |
