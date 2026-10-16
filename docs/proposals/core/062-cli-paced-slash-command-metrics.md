# 062: Slash-Command Session-Metrics Probe for the CLI-Paced Provider

| Field | Value |
|-------|-------|
| **Impact** | LOWER |
| **Status** | shipped |
| **Affects** | new `maestro_sdk/src/maestro_sdk/slash_probe.py`, `maestro_sdk/src/maestro_sdk/{session,types}.py`, `maestro/providers/cli_paced.py`, `maestro/diagnostics.py`, `maestro/{cost_tracking,sdk,agent}.py`, `maestro/{config,toml_config,cli}.py`, `maestro.toml`, `docs/sdk-internals.md`, `docs/deployment.md`, `CLAUDE.md`, tests |
| **Depends on** | 061 (shipped) |
| **What landed** | Step 1 — `Session.run_command(text)` in `maestro_sdk/src/maestro_sdk/session.py` issues a slash command on the live pty runner, reads the redraw to the idle marker, and returns the snapshot without mutating turn/answer state (PR #2502); covered by tests in `maestro_sdk/tests/test_session.py`. Step 2 — `maestro_sdk/src/maestro_sdk/slash_probe.py` parses `/cost`, `/usage`, `/context` snapshots via `SlashProbeMarkers` into `CostReport` / `UsageReport` / `ContextReport` dataclasses in `maestro_sdk/src/maestro_sdk/types.py`, each field `\| None` and returning `None` on a non-matching snapshot (PR #2512); covered by `maestro_sdk/tests/test_slash_probe.py`. Step 3 — `_run_probe` in `maestro/providers/cli_paced.py` issues `/cost`/`/usage`/`/context` via `Session.run_command` and parses via `slash_probe`; the cadence gate in `CliPacedProvider.query_on_session` attaches a `ProbeReports` to `session.last_probe` when `_probe_metrics_enabled` and the turn hits `_probe_metrics_every`, with the default-off path byte-for-byte today's behaviour (PR #2544); covered by `tests/test_provider_cli_paced.py`. Step 4 — `ContextWindowSnapshot` on `DiagnosticSnapshot` with the `blind_spots()` gap line, fed by `context_window_from_reports` / `latest_context_window` (PR #2559). Step 5 — `cost_tracking.log_metering_divergence` cross-checks the `/cost` token total against the metered sidecar `QueryUsage` on each probed turn (one `metering_divergence` WARNING past `METERING_DIVERGENCE_THRESHOLD`, sidecar authoritative), and `/usage` plan headroom surfaces read-only as `cost_tracking.PlanHeadroom` via `latest_plan_headroom` into the burn-rate throttle's entry report. Step 6 — `sdk_cli_paced_probe_metrics_enabled` / `sdk_cli_paced_probe_metrics_every` validated in `config.py`, parsed from `[sdk.cli_paced]`, and wired through `cli.main` into `configure_cli_paced`. Step 7 — probe documentation in `docs/sdk-internals.md` / `docs/deployment.md` plus the one-line CLAUDE.md invariant. |
| **What is missing** | Nothing — all seven steps landed. Deferred to the production rollout proposal: the per-CLI-version `SlashProbeMarkers` pack (and the idle-marker continuity the resident-runner probe needs), plus consuming plan headroom in the throttle's gating math. |

## Description

> Status updated to shipped on 2026-06-12 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The `claude` TUI answers three slash commands with live session metrics —
`/cost` (tokens + a locally estimated dollar amount for the session), `/usage`
(token consumption and, on a subscription login, plan rate-limit headroom), and
`/context` (a token-by-token breakdown of what fills the *current context
window*: system prompt, tool defs, MCP registrations, `CLAUDE.md`, skills,
conversation history, free space, auto-compact buffer). This proposal adds an
opt-in probe that issues those commands as follow-up turns on the persistent pty
session 061 introduced, parses the redraw through the existing screen emulator,
and surfaces the result as structured Maestro data.

It matters because two of those three readings are *not* recoverable from the
per-session transcript JSONL sidecar that 061 Step 10 mines for token counts.
The sidecar gives cumulative spend; it does not report **context-window
composition** (`/context`) — the load-bearing "context bloat" axis CLAUDE.md
already gates everywhere — nor **subscription plan rate-limit headroom**
(`/usage`), which neither the API response nor the sidecar exposes and which the
loop's burn-rate throttle is currently blind to. The third reading, `/cost`,
overlaps the sidecar deliberately: it becomes an independent cross-check that
flags drift when the sidecar parser falls behind a CLI layout change.

End state: with `[sdk.cli_paced] probe_metrics_enabled = true`, after every
`probe_metrics_every`-th turn on a `cli_paced` session the provider runs the
probe, emits a `ContextWindowSnapshot` into `diagnostics`, surfaces plan-headroom
as a read-only signal the throttle can consult (consuming it to gate the loop
is deferred to a future proposal), and logs a `metering_divergence` WARNING when the
`/cost` reading and the sidecar's `QueryUsage` disagree beyond a threshold — but
**never** overrides the sidecar's authoritative count and never fabricates a
number when the parse misses. Default stays disabled and `[sdk] provider =
"anthropic"`, so this is inert until an operator both selects `cli_paced` and
opts the probe in. This is observability for a non-default provider, not a
metering substitute: 061 Step 10 remains the source of truth for spend.

## Relevant Findings

- `maestro/providers/cli_paced.py` — `_to_query_result` re-packs every
  turn into `QueryUsage(caller=caller, unmetered=True)`; 061 Step 10 replaces
  this with transcript-sidecar counts. This proposal layers *on top of* that
  authoritative path, not in place of it.
- `maestro/providers/cli_paced.py` — the eight-flag `Capabilities`
  block (all `False` today; 061 Step 11 flips them). Probe readings are
  observability, not a `Capabilities` flag — no new flag is added here.
- `maestro/providers/cli_paced.py` — `query_on_session` is the seam the
  provider-side probe hangs off: after a turn returns, conditionally issue the
  slash-command probe on the same session before re-packing the result.
- `maestro_sdk/src/maestro_sdk/session.py` — 061's `Session` keeps one
  `PtyRunner` alive across `query` turns (reusing the child on the idle redraw,
  respawning on EOF). A slash-command probe is just another turn typed into that
  live runner; **without the persistent session it could not see the
  just-completed turn's state**, which is why this proposal depends on 061.
- `maestro_sdk/src/maestro_sdk/screen.py` — `Screen.display()` renders the
  current snapshot to text; this is the scrape surface the probe parses. The
  metrics live in a TUI region, not the answer region, so they need their own
  extractor (next finding).
- `maestro_sdk/src/maestro_sdk/parser.py` — the answer/thinking
  `Parser` is markers-driven and **version-specific by design**, degrading
  honestly to "everything is answer, nothing extracted" when markers do not
  match. The slash-command extractor reuses this exact discipline: a
  marker/regex pack per CLI version, fail-soft to `None` on a miss, never a
  fabricated value.
- `maestro/diagnostics.py` — `DiagnosticSnapshot` already carries `cost_trend`
  (and, since Step 4 landed, `context_window`) as **direct** optional fields,
  plus a `blind_spots()` reporter conditional on missing measurements.
  `cache_hit_rate` is *not* a direct field — it lives on the separate
  `SDKUsageStats` dataclass and is reached indirectly through
  `sdk_usage`. A `ContextWindowSnapshot` slots in as one more direct optional
  field — mirroring the `cost_trend` pattern — whose absence `blind_spots()`
  can name.
- `maestro/config.py` — the `[sdk.cli_paced]` config surface
  (`sdk_cli_paced_cadence` / `sdk_cli_paced_stream_idle_timeout_seconds`,
  validated in `configure_cli_paced`) is where the two new probe knobs land,
  following the existing validation pattern.
- CLAUDE.md "Context bloat caps" invariant — every list/string entering a prompt
  has a load-bearing cap. `/context` is the only runtime surface that *measures*
  the realized composition for the CLI path; today that path reports nothing.

## Implementation Plan

Steps are ordered so an interrupted run leaves the tree working: the probe
mechanism lands in `maestro_sdk` first (inert — nothing calls it), then the
provider wiring behind a default-off flag, then the consumers, then config and
docs. Until Step 4 flips the flag's consumers on, every intermediate merge is a
dormant mechanism addition that changes nothing for any provider.

### Phase 1 — Probe mechanism (maestro_sdk)

1. [x] **Slash-command turn on the live session.** *(Landed — PR #2502.)*
   `maestro_sdk/src/maestro_sdk/session.py` — add `Session.run_command(text)`
   that types a slash command into the live `PtyRunner`, reads the redraw to the
   idle marker via the existing `_drive_turn`/`Screen.display()` path, and
   returns the rendered snapshot text **without** mutating answer/turn state (a
   probe is not a conversational turn). Reuses 061's persistent runner; the
   one-shot `AsyncClient.query` path is untouched.
   - *Depends on:* none (061 Step 1 already landed).
   - *Acceptance:* with a fake runner + `InstantCadence`, `run_command("/cost")`
     drives the *same* live runner as the preceding `query`, returns the redraw
     snapshot, and leaves `turn_count` / accumulated answer text unchanged.
2. [x] **Metrics extractor + result types.** *(Landed — PR #2512.)* new
   `maestro_sdk/src/maestro_sdk/slash_probe.py`, extending the existing
   `maestro_sdk/src/maestro_sdk/types.py` — a marker/regex pack (mirroring
   `parser.ParserMarkers`) that parses a `/cost`, `/usage`, or `/context`
   snapshot into `CostReport` / `UsageReport` / `ContextReport` dataclasses, each
   field `| None`, returning `None` on a non-matching snapshot (the parser's
   honest-degenerate discipline). No provider import — pure text → dataclass.
   - *Depends on:* 1.
   - *Acceptance:* a captured `/context` fixture parses into a `ContextReport`
     with the per-bucket token counts; a garbled/empty snapshot yields `None`
     (no fabricated zeros), pinned for all three commands.

### Phase 2 — Provider integration (default-off)

3. [x] **Provider-side probe hook.** *(Landed — PR #2544.)*
   `maestro/providers/cli_paced.py` — after
   `query_on_session` produces a result, when probing is enabled and the turn
   index hits the cadence, call `Session.run_command` for the configured
   command set, parse via `slash_probe`, and attach the reports to a new
   optional `QueryResult` side field (or a provider-held last-probe slot).
   Behind `sdk_cli_paced_probe_metrics_enabled`; default-off path is byte-for-byte
   today's behaviour.
   - *Depends on:* 1, 2.
   - *Acceptance:* with the flag on and cadence 1, a `query_on_session` turn
     carries a populated probe result; with the flag off, no `run_command` is
     issued and the result is identical to today's.
4. [x] **Context-window snapshot into diagnostics.** *(Landed — PR #2559.)*
   `maestro/diagnostics.py` +
   the `cli_paced` probe consumer — fold a parsed `ContextReport` into a
   `ContextWindowSnapshot` on `DiagnosticSnapshot`, and have `blind_spots()`
   name its absence (mirroring the `cost_trend is None` branch) so a CLI path
   running without the probe reports the gap honestly.
   - *Depends on:* 3.
   - *Acceptance:* a `DiagnosticSnapshot` built from a probe-carrying iteration
     exposes the context composition; one without it lists the context-window
     gap in `blind_spots()`.
5. [x] **`/cost` cross-check + plan-headroom signal.** *(Landed.)*
   `maestro/providers/cli_paced.py` (+ a thin `cost_tracking` log helper) — when
   both a `CostReport` and 061's sidecar `QueryUsage` exist for a turn, compare
   them; on a relative gap beyond a threshold, log a `metering_divergence`
   WARNING (the sidecar stays authoritative — the probe never overrides it).
   Surface `/usage` plan rate-limit headroom as a read-only signal the throttle
   can consult. No silent fall-through — a divergence is a logged warning, a
   parse miss is an honest `None`.
   - *Depends on:* 3; 061 Step 10 (sidecar metering).
   - *Acceptance:* a sidecar count and a `/cost` reading that disagree past the
     threshold emit exactly one `metering_divergence` WARNING and leave the
     persisted `QueryUsage` (sidecar value) unchanged; agreement logs nothing.

### Phase 3 — Config + docs

6. [x] **Config knobs.** *(Landed.)* `maestro/config.py` + `maestro.toml` — add
   `sdk_cli_paced_probe_metrics_enabled: bool = False` and
   `sdk_cli_paced_probe_metrics_every: int` (≥ 1) under `[sdk.cli_paced]`,
   validated in `configure_cli_paced` alongside the existing cadence/idle checks
   (fail loud on a non-positive interval).
   - *Depends on:* 3.
   - *Acceptance:* an out-of-range `probe_metrics_every` raises at config-load
     with a message naming the key; the default leaves probing disabled.
7. [x] **Documentation.** *(Landed.)* `docs/sdk-internals.md`, `docs/deployment.md`,
   `CLAUDE.md` — document the probe as a CLI-side observability side channel
   (distinct from 061's sidecar metering), the three commands and what each
   uniquely yields, the screen-scrape fragility caveat, and that it never
   overrides the authoritative sidecar count. One-line CLAUDE.md invariant.
   - *Depends on:* 6.
   - *Acceptance:* `docs/sdk-internals.md` describes the probe + its
     relationship to sidecar metering; CLAUDE.md gains one accurate line.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-I/O + no-real-network + no-real-`sleep`; the
`claude` CLI and pty are mocked via the existing fake-runner seam and
`InstantCadence`. Slash-command snapshots are captured fixtures, never a live
CLI; real-CLI parsing stays opt-in behind `RUN_CLAUDE_TESTS=1`.

- `maestro_sdk/tests/test_session.py` (extend) — `run_command` reuses the live
  runner and leaves turn/answer state untouched; a probe before `aclose` does
  not leak a child/fd.
- `maestro_sdk/tests/test_slash_probe.py` (new) — `/cost`, `/usage`, `/context`
  fixtures each parse into their report dataclass; garbled/empty/version-skewed
  snapshots return `None` (no fabricated counts) for all three.
- `tests/test_provider_cli_paced.py` (extend) — flag-on cadence issues the probe
  and attaches reports; flag-off issues no `run_command` and matches today's
  result byte-for-byte; a `/cost`-vs-sidecar divergence logs exactly one warning
  and does not mutate the persisted `QueryUsage`.
- `tests/test_diagnostics.py` (extend) — a probe-carrying snapshot exposes
  context composition; one without it names the gap in `blind_spots()`.
- `tests/test_config*.py` (extend) — `probe_metrics_every` range validation
  raises with the keyed message; defaults keep probing off.

## Risk Assessment

**What could regress.**

- **Screen-scrape fragility.** The extractor reads a repainting TUI region whose
  layout is not a stable contract — exactly the brittleness the `parser` module
  was built to contain. Mitigation: per-version marker packs with honest
  fail-soft to `None`; a layout change degrades to "no reading," never to a wrong
  number, and the sidecar (061 Step 10) remains the spend source of truth.
- **`/cost` is a local estimate.** The TUI figure is computed from token counts ×
  published rates, not authoritative billing. Mitigation: it is used only as a
  cross-check and a `CostReport` is flagged estimated; it never overrides the
  sidecar `QueryUsage` or feeds the burn-rate aggregate directly.
- **Probe latency.** Each probe is extra keystrokes + a redraw read per cadence
  hit. Mitigation: default-off, and `probe_metrics_every` lets an operator amortise
  it across N turns; the one-shot `query` path is never probed.
- **No new trust surface.** The probe issues read-only slash commands and parses
  text; it adds no tool execution and no `Capabilities` flag, so the 061
  action-gate caveat is unaffected. Default `anthropic` keeps the whole change
  inert.

**Rollback posture.** `sdk_cli_paced_probe_metrics_enabled = false` (the default)
disables every consumer; reverting the Step 3–5 PRs leaves the dormant
`maestro_sdk` mechanism harmless. **Data migration:** none — `DiagnosticSnapshot`
and `QueryUsage` gain optional fields that absent/old rows tolerate.

## Out of scope

- **Replacing 061 Step 10 sidecar metering** — the probe complements it
  (context composition + plan headroom + drift cross-check); the sidecar stays
  authoritative for spend.
- **Wiring plan rate-limit headroom into the throttle's actual gating math** —
  this proposal surfaces the signal read-only; consuming it to slow the loop is
  a follow-up against the burn-rate throttle (future throttle work).
- **Production rollout of `cli_paced`** — owned by 061's deferred rollout
  proposal; this change is observability-only and inert under the default.
