# 035: Operator Visitors Dashboard

| Field | Value |
|-------|-------|
| **Impact** | LOWER |
| **Status** | proposed |
| **Affects** | `maestro/dashboard/routes.py`, `maestro/dashboard/templates/index.html`, `maestro/dashboard/static/index.js`, `maestro/dashboard/static/index.css`, `CLAUDE.md` |

## Description

Proposal 034 lands visit events onto disk but deliberately provides no viewer. This proposal surfaces them to the authenticated operator: one auth-gated JSON endpoint and one new tab on `/account`, mirroring the existing Budget tab pattern so the addition is familiar to anyone who has reviewed that surface.

Landing this before the public line (036) and the agent-awareness injection (037) is the intentional order — the operator gets a chance to eyeball the data and confirm shape / correctness before it is exposed publicly or threaded into the agent's prompt. Sanity-check before amplification.

## Relevant Findings

- `/api/budget` in `maestro/dashboard/routes.py` is the contract shape to copy — `Query(default=..., ge=..., le=...)`, `asdict(...)` return, private-by-default.
- `maestro/dashboard/templates/index.html` (nav and Budget panel) plus `maestro/dashboard/static/index.js` (Budget JS) show the tab-addition pattern including `activeBudgetWindow` state, window buttons, and `refreshBudget()` / `renderBudget()` split. The dashboard JS now lives in `static/index.js`, not inline in `index.html`.
- `escapeHtml` helper already exists in `maestro/dashboard/static/index.js` — use it for every rendered UA and referer string.
- `.bar-track` / `.bar-fill` styling is already defined in `maestro/dashboard/static/index.css` and reusable for the bar-chart tables.

## Implementation Plan

1. `GET /api/visitors?window_hours=<float>` in `routes.py`: behind `BearerAuthMiddleware`. `Query(default=24.0, ge=0.1, le=168.0)`. Returns `asdict(build_visitor_summary(config.visitor_events_path, window_hours=window_hours))`. No TTL cache — authenticated traffic is bounded and the full JSONL read at 14d retention is sub-millisecond.
2. Add a "Visitors" tab to `templates/index.html`:
   - Nav: `<a data-nav="visitors">Visitors</a>` placed alongside the Budget tab.
   - Panel: `<section data-tab="visitors">` with `#visitors-summary`, `#visitors-by-agent`, `#visitors-by-referer` placeholders and an empty-state placeholder (`"no visitors yet"`) shown when `raw_events == 0`.
   - JS: `activeVisitorsWindow = 24` state, three buttons `data-visitors-window="1|24|168"`, `refreshVisitors()` → `fetch("/api/visitors?window_hours=...")` → `renderVisitors(s)`. Header renders `deduped_visits` / `raw_events` / `unique_visitors` / `browser_visits` / `bot_visits` / `unknown_visits`. Two `.bar-track` / `.bar-fill` tables render `top_user_agents` and `top_referers` — names passed through `escapeHtml`.
3. Update `CLAUDE.md` with one Key-Design-Decisions bullet describing the Visitors tab.

## Testing Strategy

Component:
1. `GET /api/visitors` returns 401 without auth.
2. `GET /api/visitors` returns 200 with bearer + response shape matches `asdict(VisitorSummary)`.
3. `window_hours=0.05` and `window_hours=200.0` are rejected with 422.

Browser:
1. The `[data-nav="visitors"]` nav entry is present and clickable on `/account`.
2. Clicking it reveals `[data-tab="visitors"]` and hides the other panels.
3. With a fixture event written to `visitor_events.jsonl` before the test, `#visitors-summary` renders non-empty text and at least one `.bar-fill` element appears in `#visitors-by-agent`.

```bash
python3.12 -m pytest tests/component/test_visitors_route.py -v
python3.12 -m pytest tests/browser/test_visitors_tab.py -v -m browser --no-cov --browser chromium
```

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| UA / referer HTML injection into dashboard | Low | Reuse existing `escapeHtml` helper for every rendered cell |
| Full JSONL read on every request | Low | 14d retention + authenticated-only access bounds cost; observed sub-ms |
| Empty-state rendering looks broken | Low | Explicit "no visitors yet" placeholder when `raw_events == 0` |
