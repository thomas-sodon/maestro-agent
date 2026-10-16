# 122: Overhaul the Account Dashboard — One Design-System Token Source, Adaptive Navigation, and Accessible Interaction

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | burn-in:graduated |
| **Affects** | `maestro/dashboard/static/index.css`, `maestro/dashboard/static/index.js`, `maestro/dashboard/templates/index.html`, new `maestro/dashboard/static/tokens.css`, `maestro/dashboard/static/public.css`, `maestro/dashboard/static/login.css`, `maestro/dashboard/routes.py`, `tests/browser/`, tests |
| **Derived from** | UI/UX Pro Max design-intelligence skill (`ui-ux-pro-max`, v2.13.0) — dark developer-tool palette, "Dashboard Data" type pairing, Phosphor icon set, and the WCAG-2.2-anchored `ux` guideline dataset |
| **What landed** | Step 1 (Phase 1 design-system foundation) landed as PR #3617: the triplicated `:root` palette blocks were removed from `index.css` / `public.css` / `login.css` and consolidated into a single `maestro/dashboard/static/tokens.css` linked first by `index.html` / `public.html` / `login.html`, with the hex values byte-identical to the former per-page blocks (the union covers the fields that had already drifted in coverage — `--surface-2` / `--text-mute` / `--accent-soft`). The additive dense spacing (`--space-1…--space-8`), radius (`--radius-sm/md/lg`), and elevation (`--elevation-1/2/3`) scale tokens were seeded for the later steps to consume. Parity is guarded by `tests/browser/test_token_parity.py` and `tests/test_dashboard_tokens.py`. Step 2 (the typographic hierarchy) then landed on this branch: a two-face split — `--font-ui` (a `system-ui` sans stack) for headings, labels, navigation, prose, and controls, and `--font-data` (the existing monospace stack) reserved for the data-bearing selectors (`.stat`, `.log-entry`, `.budget-table`, `.version-table`, `.sdk-session-content`, `.sdk-turn-content`, `.readme-preview`) — was applied in `tokens.css` / `index.css`, plus an unconsumed `--text-xs…--text-2xl` size scale seeded for the later type-hierarchy increment. Both faces are system stacks, so the surface still loads no webfont (no first-paint block, no font-swap shift). Guarded by `tests/test_dashboard_typography.py` (token pair + text scale + a no-render-blocking-font detector) and `tests/browser/test_typography.py` (the two faces resolve onto the right nodes). Step 3 (the accessible confirmation dialog + `aria-live` toast) then landed as PR #3626: both native `confirm(...)` sites were replaced by one reusable native `<dialog id="confirm-dialog">` populated through `openConfirmDialog()` (`index.js`) — focus-trapped, `aria-labelledby`, Escape/Cancel/backdrop all resolve `confirmed:false`, focus restores to the trigger — reused by plan and publication approve/reject, carrying the unchanged `content_hash` / `csrfHeaders()` payload, and a persistent `#toast-region` (`role="status"` / `aria-live="polite"`) into which `showToast()` appends an auto-dismissing toast. Guarded by `tests/browser/test_confirm_dialog.py` and `tests/browser/test_toasts.py`. Step 4 (loading skeletons) then landed on this branch: each of the seventeen `Loading...` placeholder nodes in `index.html` was replaced by a `<div class="skeleton" aria-busy="true">` block of shimmer bars that reserves layout height (so the 10 s refresh does not shift content) and is auto-cleared on first render via the existing `innerHTML` swap — no `index.js` change was needed. The shimmer (`@keyframes skeleton-shimmer`, gradient from the `--surface-2`/`--border`/`--space-2`/`--radius-sm` tokens seeded in step 1) is stilled under a scoped `prefers-reduced-motion: reduce` guard. The existing labelled `.empty` blocks (e.g. "No pending plans.") already satisfy step 4's empty-state acceptance and were retained unchanged; rewording that italic copy is a deferred, non-blocking refinement. Guarded by `tests/browser/test_skeletons.py`. Step 5 (the spend-composition chart) then landed as PR #3647: a ranked horizontal bar chart of the Budget panel's `by_group` series was added above the group table as hand-built inline SVG (`renderGroupChart()` in `index.js`, a `#budget-group-chart` container in `index.html`, `.budget-chart*` token-styled rules in `index.css`) with no charting dependency — every bar carries its own direct cost/share text value (meaning is never encoded in bar length or colour alone), bar width is a fraction of the largest cost in the window (the divisor guarded for all-unmetered windows), the `<svg role="img">` takes its single accessible name from the *visible* composition caption via `aria-labelledby`, and each label is clipped to a reserved left column so a long group name cannot overrun the right-anchored value. The group table beneath it was wrapped in a focusable `role="region"` (`tabindex="0"` + `aria-label`), giving keyboard users a reachable data equivalent of the graphic and a wide table a horizontal-scroll affordance; an empty window clears the chart to the same labelled `.empty` block the tables get. Guarded by `tests/browser/test_budget_chart.py` (one labelled bar per group, caption-named SVG, focusable data table, the all-unmetered em-dash, and the empty-window clear), with the cold-state skeleton count bumped seventeen→eighteen in `tests/browser/test_skeletons.py`. Step 6 (sortable Budget/Versions tables) then landed on this branch: a shared `sortableHeaderCells()` emits each `<th>` with a borderless `.sort-btn` carrying `data-sort-table` / `data-sort-key` and `aria-sort` (ascending/descending on the active column, `none` elsewhere), driven by a `SORT_TABLES` registry of per-column descriptors + row accessors; a delegated click handler toggles direction (numbers default descending, text/date ascending) and — because the `innerHTML` swap destroys the clicked button — restores focus to the freshly-rendered replacement so keyboard operation is not dropped. The chosen order is held in module `sortState` (mirroring `filterState`) so the 10 s refresh never resets it, and `sortRows()` sorts a non-mutating copy, so the composition chart — which reads the untouched `by_group` — stays in server order while the group table sorts. The wide by-caller and Versions tables were wrapped in the same focusable `.table-scroll` `role="region"` the by-group table already uses. Guarded by `tests/browser/test_table_sort.py`. Step 7 (the flagged navigation shell) then landed as PR #3662: behind the default-off `[dashboard] account_ui_v2` flag (registered in `EXPERIMENTAL_FLAGS` with `review_by=2027-09-30`, `gate=None`, in `maestro/flag_lifecycle.py`; the `Config.account_ui_v2` field in `maestro/config.py` / `config_settings.py` / `maestro.toml`), the `/account` handler in `maestro/dashboard/routes.py` stamps a single `data-account-ui-v2="true"` marker onto `<body>` (a one-shot `html.replace`, so the template is served byte-identical when the flag is off) and the progressive-enhancement `enhanceNavV2()` in `index.js` — a no-op absent the marker — turns the flat pill row into a grouped ARIA tablist: a skip-to-content link, a `role="main"` focus target, `NAV_GROUPS` clusters (Monitor / Govern / Introspect / System) with hand-built inline-SVG Phosphor-style icons, `role="tablist"/"tab"/"tabpanel"` with `aria-controls`/`aria-labelledby`/`aria-selected`, `aria-current="page"` on the active tab, `aria-orientation` synced to the rail breakpoint, roving-tabindex arrow/Home/End traversal, and focus moved to the panel on explicit activation only. The `body[data-account-ui-v2]`-scoped CSS in `index.css` renders a two-column `.account-shell` grid with a fixed grouped sidebar rail ≥1024 px and collapses to the existing horizontal-scroll strip below it. Guarded by `tests/browser/test_nav_shell.py` and `tests/test_dashboard.py` (marker stamped iff flag on) / `tests/test_config_flag_lifecycle.py` (manifested default-off experiment). Step 8 (the accessibility and responsive polish) then landed on this branch as unflagged CSS/JS/template edits closing four gaps at once. One consolidated `@media (prefers-reduced-motion: reduce)` block, placed last in `index.css` so its equal-specificity overrides win the cascade, stills every motion the stylesheet declares — the skeleton shimmer, the `.status-dot.active` `pulse`, the `.toast` `fadeIn`, the `.nav-link` / `.sign-out-btn` colour transitions, and the disclosure-marker and `.confidence-fill` transitions that are inert as written but would otherwise escape the guard the moment an edit made them live — subsuming the narrower skeleton-only guard step 4 had added. That exhaustiveness is a prose claim no reviewer can check by eye, so `tests/test_dashboard_motion.py` parses the stylesheet and holds it: every selector declaring motion must be named in the block, and the block must follow those rules in source order. The shared `2px solid var(--accent)` `:focus-visible` ring `.sort-btn` / `.table-scroll` already used was extended to `.nav-link`, `.log-filter-btn`, `.load-more-btn`, the `.compose-form` controls, and the `.confirm-dialog-actions` buttons, placed after the `.compose-form textarea:focus { outline: none }` rule so it wins at equal specificity; and because step 8's acceptance asks that the ring is *not obscured by the sticky header*, `.tab-panel` / `h2[id^="section-"]` take a `scroll-margin-top` bound to a `--header-offset` custom property that `syncHeaderOffset()` measures from whichever element actually pins — the `.sticky-header`, or `.nav-bar` below 640 px where the header is `display: contents` and so reports no box — and keeps current through a `ResizeObserver`, so a focused panel or anchor-jumped heading clears the content-dependent header height instead of a fixed approximation that under-shoots it. A `--gutter` custom property now drives the `body` horizontal padding and the `.sticky-header` edge-bleed (`margin-inline` / `padding-inline`) in lock-step, stepped up at a new `768px` breakpoint and capped plus centred at a new `1440px` one; the existing `640px` block narrows the same property to `12px` and threads the `.nav-bar` bleed through it — below that width `.sticky-header` is `display: contents` and has no box, so `.nav-bar` is the element that pins and bleeds — keeping the lock-step true at every breakpoint (held by `tests/browser/test_polish.py::test_gutter_stays_in_lock_step_at_every_breakpoint`), and the v2 `1023/1024px` breakpoints are untouched; the `position: fixed` `.toast-region` is re-anchored to that capped column above `1440px` so a toast is not announced from the far edge of a wide monitor. The sign-out control is a plain `<form method="POST" action="/logout">` inside `.sticky-header` but outside `.nav-bar`, `hidden` until `revealSignOut()` confirms the JS-readable `maestro_csrf` cookie (reusing `csrfHeaders()`' auth-on inference) so it never appears — and never 404s to an unregistered `/logout` — in the no-auth local-dev configuration; the POST carries no CSRF header because `/logout` is in `_CSRF_EXEMPT_PATHS`. Guarded by `tests/browser/test_polish.py` (the pulse, toast, and transition stilling under reduce; the nav-link and secondary-control focus rings; the measured header clearance and its mobile `.nav-bar` fallback; the 768/1440 breakpoints and the toast region's anchoring to the capped column; and the sign-out control staying hidden with auth off) and `tests/browser/test_login.py::test_sign_out_control_visible_and_clears_cookies`. |
| **What is missing** | Step 10, the flag collapse. Step 9's graduation half landed on this branch: `account_ui_v2` was flipped on as the base default (`Config.account_ui_v2`, `maestro.toml`) and moved off `EXPERIMENTAL_FLAGS` onto `CUTOVER_FLAGS` at `stage="cutover"` (`review_by=2027-03-31`, its own `off_path_symbol` because the off-branch is HTML/CSS/JS the Python symbol scan cannot see), so an operator gets the grouped console with no config edit and setting the flag `false` is the one-line rollback to the flat pill nav for the observation window. What remains is collapsing that flag once the window closes clean: deleting the `Config.account_ui_v2` field, the `_DashboardSettings.account_ui_v2` setting in `maestro/config_settings.py` (a required field carrying no default, so it must go in the same diff as the `maestro.toml` key), the `/account` handler's conditional marker stamp, the `enhanceNavV2()` no-marker early return, the now-dead flat-nav off-branch markup/CSS, and the two test pins that set the key — `tests/browser/conftest.py`'s auth mock-app stub config and `tests/component/test_config_integration.py`'s fixture TOML — with the `CUTOVER_FLAGS` entry dropped (not left behind as a `collapsed` tombstone, which `find_stage_violations` reds) and `_MAX_CUTOVER_FLAGS` lowered in the same diff. |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

The "Account" surface — the authenticated operator dashboard served at `/account`
(`index.html`, whose browser title is literally *Maestro — Account*, reached
through the **Account** link on the public page) — is where Thomas governs a
self-evolving agent: approving plans, tending PRs, reading the journal, watching
the budget. It has grown to eleven flat tab panels of dense, high-stakes data,
but its presentation layer never grew with it. It is monospace-only, has no icon
system, renders every panel as the literal text `Loading...`, gates irreversible
approve/reject on the browser's blocking `confirm()` dialog, ships no charts, and
carries almost no accessibility semantics. Its design tokens are copy-pasted
three times across `index.css`, `public.css`, and `login.css`, so the palette
cannot evolve without editing three files in lock-step.

This proposal overhauls that surface into a coherent, accessible operator
console without changing a single API or datum it displays. It consolidates the
triplicated tokens into one imported source of truth, introduces a typographic
hierarchy (mono for data, sans for prose/labels), replaces the flat pill nav with
an adaptive, icon-labelled, ARIA-correct navigation that groups the eleven panels
by intent, swaps the native `confirm()` blockers for a focus-trapped confirmation
dialog plus an `aria-live` success/undo toast, replaces `Loading...` with
skeletons and real empty states, and adds a spend-by-group composition bar
chart (with a keyboard-navigable data-table fallback), built from the Budget
payload the tab already renders, to the Budget tab.

The end state: the same data, rendered as a professional dark developer-tool
console that reads as one system, degrades gracefully to 375 px, honours
`prefers-reduced-motion`, and meets the CRITICAL accessibility bar the current
surface silently fails. Every step is independently shippable behind the existing
browser-test guard; the one structural swap (the navigation shell) is gated by a
default-off flag so it can dark-launch.

## Relevant Findings

Grounded in the current tree and in the `ui-ux-pro-max` dataset queried while
authoring this proposal.

- `maestro/dashboard/static/index.css` (`:root`) — the entire palette is a flat
  set of custom properties (`--bg:#0a0a0f`, `--surface`, `--border`, `--text`,
  `--text-dim`, `--accent:#7c6ff0`, plus semantic `--success/--warning/--danger/--info`).
  There is **no spacing scale, no elevation scale, and no typographic scale** —
  spacing and radii are hand-typed literals throughout the file.
- `maestro/dashboard/static/public.css` (`:root`) and
  `maestro/dashboard/static/login.css` (`:root`) — **redefine the same core
  token block independently**. Any palette change today must be applied three
  times to stay consistent; there is no shared token file.
- `maestro/dashboard/static/index.css` (`body` font stack) — the whole surface
  is `'SF Mono', 'Fira Code', 'Cascadia Code', monospace`. Labels, headings,
  prose, and numeric data all share one monospace face, so there is no
  size/weight/face hierarchy distinguishing a section heading from a log line.
  The skill's `typography` domain returns the **"Dashboard Data"** pairing (Fira
  Code for data, Fira Sans for labels) as its top match for *admin panels*.
- `maestro/dashboard/templates/index.html` (`.nav-bar`) — eleven sibling
  `.nav-link` anchors (Diagnostics, Identity, Purpose, Plans, Publications,
  Journal, Tasks, Messages, Budget, Versions, Logs) in a single flat pill row
  with **no grouping and no icons**. The skill's guideline dataset flags
  `adaptive-navigation` (sidebar ≥1024 px, top/bottom on small screens) and
  `nav-label-icon` (icon + text, never icon-only) as the target patterns.
- `maestro/dashboard/static/index.css` (`.tab-panel` / `.tab-panel.active`) —
  panel switching toggles `display:none/block` via an `.active` class with **no
  ARIA tab semantics** (`role="tablist"/"tab"/"tabpanel"`, `aria-selected`,
  `aria-current`) and no focus move on activation.
- `maestro/dashboard/static/index.js` (the two `confirm(...)` call sites —
  `'Approve this evolution plan?'` and `'Approve this publication proposal?'`) —
  irreversible governance actions are gated on the browser's **native blocking
  `confirm()`**, which cannot be styled, focus-trapped, `aria-live`-announced, or
  offered an undo. The skill's `ux` dataset rates `Confirmation Dialogs` **High**
  and pairs them with `Confirmation Messages` (a brief success toast) and
  `undo-support`.
- `maestro/dashboard/templates/index.html` (every panel container, e.g.
  `#diagnostics`, `#identity`, `#budget-summary`) — each ships the literal string
  `Loading...`. There are **no skeletons and no `aria-busy`**; the skill rates
  `Loading Indicators` **High** ("stable skeleton or progress with aria-busy",
  not flickering text).
- `maestro/dashboard/static/index.js` (`setInterval(refresh, 10000)` and
  `setInterval(refreshLogs, 3000)`) — the surface full-refreshes on fixed timers;
  the empty/loading state therefore reappears on the first paint of every cold
  panel, making the missing skeletons user-visible on every load.
- `maestro/dashboard/templates/index.html` (Budget panel: `#budget-summary`,
  `#budget-by-group`, `#budget-by-caller`; Versions panel `#versions`) — Budget
  and Versions are **tables only, no visualization**. The `/api/budget` payload
  already exposes a per-group/per-caller spend *composition* for the selected
  window (`by_group`/`by_caller`), not a time-bucketed series — so the buildable,
  zero-API-change visualization the skill's `chart` domain supports here is a
  ranked **bar chart** of that composition (with a mandated data-table + keyboard
  fallback), not a trend line; `number-tabular` figures are advised for numeric
  columns to prevent layout shift.
- `maestro/dashboard/static/index.css` (`@media (max-width: 640px)`) — a **single
  breakpoint**. The skill's `Layout & Responsive` guidance calls for a systematic
  375/768/1024/1440 set with adaptive gutters and a container max-width.
- `maestro/dashboard/static/index.css` — contains **no `prefers-reduced-motion`
  block**, yet defines the always-on `@keyframes pulse` status dot. Only
  `public.css` honours reduced motion today.
- `maestro/dashboard/routes.py` (`create_router`, the `POST /logout` route) — a
  logout endpoint exists, but **no element in `index.html`/`index.js` calls it**;
  there is no visible sign-out control. The skill's `Navigation` guidance
  (`destructive-nav-separation`) wants sign-out spatially separated from primary
  nav.
- `maestro/dashboard/templates/login.html` (`<input type="password"
  autocomplete="current-password">`) — the login form already satisfies the
  skill's **Critical** `accessible-authentication` rule (password-manager /
  paste friendly). This proposal must not regress it.
- `tests/browser/test_tab_navigation.py`, `tests/browser/test_page_load.py`,
  `tests/browser/test_budget_tab.py`, `tests/browser/test_login.py`,
  `tests/browser/test_public_page.py` — the dashboard already has a Playwright
  browser-test suite built on the `create_router()` server fixture
  (`tests/browser/conftest.py`), the guard every step below extends.
- `docs/proposals/ux/119-public-surface-bound-projection.md` — the sibling UX
  proposal binds the **public** page's framing to its owners; it explicitly calls
  `/account` "the operator dashboard … an authenticated internal surface for
  Thomas." This proposal is its `/account` counterpart and must not touch the
  public projection except the shared token file.

## Implementation Plan

Steps are ordered so an interrupted run leaves the surface working. Steps 1–6
are pure additive/replacement enhancements covered by the browser suite; step 7
(the navigation shell) is gated by the `[dashboard] account_ui_v2` flag
following the four-stage flag lifecycle — dark-launch, flip, observe, collapse.
It dark-launched default-off and was flipped on as the base default on
2026-09-03 (step 9's graduation half), so the flag now reads `true` and turning
it `false` is the rollback to the flat pill nav for the observation window.

### Phase 1 — Design-system foundation

1. [x] **Extract one shared token source.** new `maestro/dashboard/static/tokens.css`,
   `templates/index.html` / `public.html` / `login.html`, `static/index.css` /
   `public.css` / `login.css` — move the triplicated `:root` palette into a
   single `tokens.css` imported first by all three pages; keep the current hex
   values byte-identical in this step so nothing renders differently. Add a
   spacing scale (`--space-1…--space-8`, dense 4–32 px per the skill's density-8
   dashboard scale), a radius scale, and an elevation scale as new tokens the
   later steps consume.
   - *Depends on:* none.
   - *Acceptance:* a browser test asserts `getComputedStyle(:root)` returns
     identical `--bg/--surface/--accent/...` values on `/account`, `/`, and
     `/login`, all sourced from `tokens.css`.

2. [x] **Introduce a typographic hierarchy.** `static/tokens.css`,
   `static/index.css` — add a `--font-data` (mono) / `--font-ui` (sans) split
   from the skill's "Dashboard Data" pairing and a `--text-xs…--text-2xl` scale;
   apply mono to numeric/log/code content and sans to headings, labels, prose,
   and nav. Both faces are **system stacks** (`--font-ui` is a `system-ui` sans
   stack, `--font-data` the existing monospace stack), so the surface loads no
   webfont — nothing blocks first paint, nothing is fetched from a third party
   (which on this authenticated single-operator surface would also leak the
   operator's presence), and there is no font-swap layout shift. A bespoke
   self-hosted face with `font-display: swap` stays a later, isolated token-file
   change if one is ever wanted.
   - *Depends on:* 1.
   - *Acceptance:* headings/labels compute to the sans stack and log/budget rows
     to the mono stack; a test asserts no page loads a render-blocking external
     font URL without `font-display: swap`.

### Phase 2 — Interaction & feedback

3. [x] **Replace `confirm()` with an accessible dialog + toast.**
   `static/index.js`, `static/index.css` — swap both native `confirm(...)` sites
   for one in-page dialog component (focus-trapped, `role="dialog"`,
   `aria-labelledby`, Escape-to-cancel, restores focus to the trigger) reused by
   plan and publication approve/reject, then confirm the outcome with an
   `aria-live="polite"` toast that auto-dismisses in 3–5 s and offers Undo where
   the action is reversible. The dialog carries the existing `content_hash` /
   `csrfHeaders()` payload unchanged.
   - *Depends on:* none.
   - *Acceptance:* a browser test drives approve → dialog appears, Escape
     cancels with no POST, Confirm fires exactly the same request body as today
     and shows a `role="status"` toast.

4. [x] **Skeletons over the cold `Loading...` state.**
   `static/index.css`, `templates/index.html` — replace the literal `Loading...`
   nodes with skeleton placeholders carrying `aria-busy="true"`, reserving the
   final layout height so the 10 s refresh does not shift content (the render
   functions' existing `innerHTML` swap clears them on first paint, so no
   `index.js` change is needed). The already-labelled `.empty` blocks the render
   functions emit (e.g. "No pending plans.") satisfy the empty-state acceptance
   and are retained; rewording that italic copy into fuller guidance ("No pending
   plans — nothing awaiting your approval.") is a deferred, non-blocking
   refinement rather than part of this increment.
   - *Depends on:* none.
   - *Acceptance:* a test asserts a cold panel shows `[aria-busy="true"]`
     skeleton nodes (not the string `Loading...`) and that an empty list renders
     a labelled empty-state block.

### Phase 3 — Data visualization

5. [x] **Spend-composition chart with a data-table fallback.** `static/index.js`,
   `static/index.css`, `templates/index.html` — add a ranked bar chart of spend
   by phase-group for the selected 1 h/24 h/7 d window to the Budget panel (SVG,
   no heavy chart dependency; solid bars + direct value label, never hue-only per
   the skill's `chart` a11y note), built from the `by_group` array the panel
   already renders — no `/api/*` payload change — backed by a visible,
   keyboard-reachable data table and a one-line composition summary for screen
   readers. Apply tabular figures (`font-variant-numeric: tabular-nums`) to
   numeric columns.
   - *Depends on:* 2 (tabular figures token).
   - *Acceptance:* the Budget panel renders an `<svg>` bar chart and a
     `role="table"` fallback with the same series; a test asserts both are
     present and the table is focus-reachable.

6. [x] **Sortable Budget/Versions tables.** `static/index.js`,
   `static/index.css` — make the Budget-by-caller/group and Versions tables
   sortable with `aria-sort` reflecting the active column, and wrap wide tables
   in an `overflow-x-auto` container so they never break the 375 px layout.
   - *Depends on:* 5.
   - *Acceptance:* clicking a column header re-sorts and toggles
     `aria-sort="ascending|descending"`; a test asserts the reorder and the ARIA
     state.

### Phase 4 — Navigation shell, accessibility & responsive polish

7. [x] **Adaptive, grouped, ARIA-correct navigation shell (flagged).**
   `templates/index.html`, `static/index.css`, `static/index.js`,
   `maestro/dashboard/routes.py` — behind a default-off `[dashboard]
   account_ui_v2` flag registered in `EXPERIMENTAL_FLAGS` with a dated
   `review_by`, render the eleven panels as a grouped **sidebar rail ≥1024 px**
   (e.g. *Monitor*: Diagnostics/Budget/Logs · *Govern*: Plans/Publications/Messages
   · *Introspect*: Identity/Purpose/Journal/Tasks · *System*: Versions), each item
   an icon (Phosphor) + text label with `aria-current="page"` on the active tab;
   collapse to the existing horizontal-scroll tabs below 1024 px. Add proper
   `role="tablist"/"tab"/"tabpanel"`, move focus to the panel on activation, and
   add a skip-link to main content. Old flat nav stays as the off-branch.
   - *Depends on:* 1.
   - *Acceptance:* with the flag on, the active tab exposes `aria-current`, tab
     order matches visual order, and a test drives keyboard-only navigation
     across all panels; with the flag off, the current nav is byte-identical.

8. [x] **Focus, motion, sign-out, and breakpoints.** `static/index.css`,
   `static/index.js`, `templates/index.html` — add a visible `focus-visible`
   ring (≥3:1 against its surface, not obscured by the sticky header), a
   `prefers-reduced-motion` block that stills the `pulse` dot and disables
   non-essential transitions, a small sign-out control wired to the existing
   `POST /logout` — a route that is intentionally CSRF-exempt
   (`_CSRF_EXEMPT_PATHS` in `maestro/dashboard/auth.py`), so it is a plain form
   POST that needs no CSRF header — placed away from the primary nav, and the
   768/1024/1440 breakpoints with a container max-width and adaptive gutters.
   - *Depends on:* 7.
   - *Acceptance:* a test asserts a reachable logout control that POSTs to
     `/logout` and lands on `/login` with the session and CSRF cookies cleared,
     and that with `prefers-reduced-motion` emulated the pulse animation is
     absent.

9. [x] **Graduate the flag to the base default.** `maestro/config.py`,
   `maestro.toml`, `maestro/flag_lifecycle.py` — flip `account_ui_v2` on as the
   shipped default and move its manifest entry off the default-off
   `EXPERIMENTAL_FLAGS` axis onto `CUTOVER_FLAGS` at `stage="cutover"`
   (`review_by=2027-03-31`, its own `off_path_symbol` because the off-branch
   lives in the HTML/CSS/JS the Python symbol scan cannot see), raising
   `_MAX_CUTOVER_FLAGS` in the same diff. The flag stays readable, so setting it
   `false` is the one-line rollback that carries the observation window — the
   flip *starts* that window rather than ending it. Landed 2026-09-03.
   - *Depends on:* 7, 8.
   - *Acceptance:* `Config().account_ui_v2` is `True` and `maestro.toml` agrees;
     `account_ui_v2` is registered in `CUTOVER_FLAGS` and absent from
     `EXPERIMENTAL_FLAGS`, so the default-on classification guard in
     `tests/test_cutover_flag_collapse.py` covers it; the flag-off `/account`
     response is still byte-identical to the template.

10. [ ] **Collapse the flag.** `maestro/config.py`, `maestro/config_settings.py`,
    `maestro/toml_config.py`, `maestro.toml`, `maestro/dashboard/routes.py`,
    `maestro/flag_lifecycle.py`, `static/index.js` / `index.css`,
    `tests/browser/conftest.py`, `tests/component/test_config_integration.py` —
    after the bounded observation window closes with no regression, delete the
    flag and the flat-nav off-branch it keeps alive, so the v2 shell becomes
    unconditional. The full set: the `Config.account_ui_v2` field, the
    `_DashboardSettings.account_ui_v2` setting (a required field with no default,
    so it must go in the same diff as the `maestro.toml` key), the `/account`
    handler's conditional marker stamp, `enhanceNavV2()`'s no-marker early
    return, the unflagged nav markup/CSS, the `CUTOVER_FLAGS` entry (dropped, not
    advanced to `collapsed`) with `_MAX_CUTOVER_FLAGS` lowered alongside it, and
    the two test pins that set the key — the auth mock-app stub config in
    `tests/browser/conftest.py` and the fixture TOML in
    `tests/component/test_config_integration.py`.
    - *Depends on:* 9.
    - *Acceptance:* today is past the entry's `review_by` floor (2027-03-31), so
      `cutover_burn_in_report` reports `past_floor` for it — its
      `recommend_collapse_eligible` stays `false` by construction, because a
      presentation shell wires no divergence gate, so the collapse is a human
      call on the browser-suite evidence rather than an automated
      recommendation; afterwards no `account_ui_v2` symbol survives under
      `maestro/`, which makes `find_stage_violations` demand the manifest entry
      be dropped rather than left behind as a `collapsed` tombstone, and the
      off-branch nav markup is gone.

## Testing Strategy

All UI behaviour is proven through the existing Playwright suite
(`tests/browser/`, `create_router()` fixture, `@pytest.mark.browser`, 60 s
budget) plus DOM/token assertions; no real network or file I/O, per CLAUDE.md.

- **Token parity** (component/browser): assert the three pages compute identical
  palette tokens from `tokens.css` (step 1), and that the sans/mono split applies
  to the right node classes (step 2).
- **Interaction** (browser): the confirmation-dialog flow — open, Escape-cancel
  (no POST), Confirm-fires-identical-body, `role="status"` toast, Undo where
  applicable (step 3); skeleton `aria-busy` vs empty-state rendering (step 4).
- **Visualization** (browser): Budget renders both the `<svg>` bar chart and the
  focus-reachable `role="table"` fallback (step 5); header click re-sorts and
  updates `aria-sort` (step 6).
- **Navigation & a11y** (browser): flag-on exposes `role="tab"`/`aria-current`
  and keyboard-only traversal reaches every panel; flag-off nav is unchanged
  (step 7); reduced-motion stills `pulse`, the logout control is present and its
  POST to `/logout` clears the session on redirect to `/login`, focus ring is
  visible (step 8).
- **Regression guards**: extend `test_page_load.py`/`test_tab_navigation.py`;
  keep `test_login.py`'s `autocomplete="current-password"` assertion green; the
  public page's `test_public_page.py` and `test_public_surface_fidelity.py` must
  stay green since only the shared token file is touched there.
- **Flag lifecycle** (unit): `test_config.py` pins the shipped `account_ui_v2`
  default; `test_config_flag_lifecycle.py` holds the manifest axis after the
  step-9 graduation — registered in `CUTOVER_FLAGS`, absent from
  `EXPERIMENTAL_FLAGS`, so the default-off overdue guard no longer applies to it —
  and `test_cutover_flag_collapse.py` holds the collapse side (the default-on
  classification, the `off_path_symbol` binding, and the growth budget).

## Risk Assessment

- **Blast radius is low and internal.** `/account` is an authenticated,
  single-operator surface; it renders no third-party content and mutates nothing
  the API does not already gate. Steps 1–6 are additive or drop-in replacements
  covered by browser tests; step 7 is gated so the old shell is one config flip
  away throughout observation. Rollback is a flag flip (step 7) or a revert of a
  self-contained CSS/JS step (1–6, 8).
- **Cross-page token change (step 1).** Consolidating three `:root` blocks into
  `tokens.css` is the one edit that touches the public and login pages. Mitigated
  by keeping hex values byte-identical in step 1 (parity test), so any palette
  *evolution* is a later, isolated change to one file.
- **Approval trust chain (step 3).** The confirmation dialog replaces `confirm()`
  but must carry the unchanged `content_hash` + `csrfHeaders()` payload; the
  attack surface (a mutation firing without the double-submit CSRF token or with
  a stale content hash) is proven closed by the step-3 browser test asserting the
  request body matches today's, and the server-side 409-on-hash-mismatch and
  CSRF middleware are untouched.
- **New logout control (step 8).** It POSTs to the pre-existing `/logout`, which
  is intentionally CSRF-exempt (`_CSRF_EXEMPT_PATHS` in
  `maestro/dashboard/auth.py`: a body-less logout carries no header a plain form
  could attach, and a forged logout costs the victim only a re-login, not state).
  The control therefore adds no CSRF header — the test grounds on reachability, a
  POST to `/logout`, and the session/CSRF cookies being cleared on the redirect
  to `/login`, not on a header assertion, so it cannot enshrine a false model of
  the boundary. Adding a CSRF token to logout would require first removing
  `/logout` from `_CSRF_EXEMPT_PATHS` — a server change outside this proposal's
  no-API-change scope.
- **Font loading (step 2).** A webfont must not block first paint or leak the
  operator's presence to a third party; this increment ships **system stacks
  only**, so no font is fetched at all, and a test forbids any page introducing a
  render-blocking external font without `font-display: swap`. A bespoke
  self-hosted face (with `font-display: swap` and reserved space) is deferred to
  a later, isolated token-file change if one is ever wanted.

## Out of scope

- Redesigning the **public** page (`public.html`) beyond the shared `tokens.css`
  import and its single `.account-link` touchpoint — that surface is governed by
  proposal 119.
- Net-new account management (profile, credential rotation, multi-user) — the
  only account control added here is the sign-out button wired to the existing
  `/logout`.
- Changing any `/api/*` payload, polling cadence, or the data any panel displays.
