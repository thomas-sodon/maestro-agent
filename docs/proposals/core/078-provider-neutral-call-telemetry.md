# 078: Provider-Neutral Per-Call Telemetry and Resilience Signals

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | proposed |
| **Affects** | `maestro/sdk.py`, `maestro/providers/cli_paced.py`, `maestro_sdk/src/maestro_sdk/{client,types}.py`, `maestro/cost_tracking.py` (consumed), `docs/sdk-internals.md`, tests |
| **Depends on** | 061 (shipped); 075 (marker packs — required for the warning-channel slice); complements 067 Step 2 |

## Description

Maestro's per-call telemetry and resilience spine is wired *inside* the
Anthropic implementation, not at the provider-neutral seam. `_log_sdk_call` —
which emits the structured "SDK call:" log line, appends the cost sample, and
feeds the `_usage_sink` that builds `IterationUsage` → `usage.jsonl` → the
burn-rate throttle — fires only within `_query_impl` / `_query_on_session_impl`.
The public `query()` / `query_on_session()` delegators added by proposal 058
do none of it. So under `cli_paced`, even a perfectly metered sidecar row
(real token counts, `unmetered=False`) never reaches `usage.jsonl`, never
becomes a cost sample, and never moves the burn-rate gate: the loop's primary
cost-control invariant is blind to an entire provider. The provider docstring's
claim that "cost is derived downstream from the counts" is not realized by any
current code path — `_estimate_cost_usd` also runs only inside the Anthropic
message pipeline.

The same impl-locality hides the resilience signals. The warning throttle
(`_update_warning_throttle_state`) and the warning hold gate consume
`allowed_warning` rate-limit notices observed on the Anthropic stream;
`cli_paced` re-packs results with an always-empty `rate_limit_events` and
never updates throttle state, so the hold gate is structurally inert. The
ghost-completion detector (`_is_rate_limit_ghost_completion`) and
empty-response diagnostics likewise never see a `cli_paced` result. And the
`include_partial_messages` heartbeat path — throttled INFO lines proving a
long turn is alive — has no CLI-side counterpart even though the driver's
parser already produces incremental `StreamDelta`s.

This proposal lifts the telemetry spine to the neutral delegators so every
provider's calls are logged, priced, and budget-counted through one seam, and
gives `cli_paced` the three missing resilience inputs: a non-fatal rate-limit
warning channel (marker-based, so it rides proposal 075's packs), ghost/empty
response detection over the re-packed result, and throttled stream-delta
heartbeats. End state: routing any phase to `cli_paced` leaves the burn-rate
throttle, warning hold gate, cost dashboard, and per-task cost queries exactly
as informed as they are under `anthropic` — which is the observability floor
067's canary runbook implicitly assumes when it asks the operator to "watch
attributed iterations confirm it is healthy."

## Relevant Findings

- `maestro/sdk.py` (`_log_sdk_call`) — emits the parseable "SDK call:" line,
  appends a `CostSample`, and calls `_usage_sink`; its only call sites are
  inside the Anthropic `_consume_messages` / impl bodies. The public
  `query()` / `create_session()` / `query_on_session()` delegators call the
  routed provider and return — no logging, no sink, no estimation.
- `maestro/sdk.py` (`_estimate_cost_usd` and its Anthropic-path call site) —
  token-count pricing fires only when the Anthropic pipeline sees a missing
  cost; nothing prices a `cli_paced` row.
- `maestro/providers/cli_paced.py` (`_to_query_result`) — produces metered
  `QueryUsage(unmetered=False)` rows with `total_cost_usd=None`, documented as
  "cost is derived downstream from the counts"; today no downstream derivation
  exists for this provider, and `_log_sdk_call` would coerce the `None` to
  `0.0` even if the row reached it.
- `maestro/sdk.py` (`_update_warning_throttle_state`,
  `warning_hold_active` machinery) — throttle state advances only from
  `allowed_warning` membership in the Anthropic stream's rate-limit events;
  CLAUDE.md's "warning hold gate pauses the loop on sustained per-call warning
  saturation" invariant has no input under `cli_paced`.
- `maestro/sdk.py` (`QueryResult.rate_limit_events`) — populated on the
  Anthropic path; `cli_paced.py` `_to_query_result` constructs results without
  it (always empty).
- `maestro_sdk/src/maestro_sdk/client.py` (`ClientMarkers.rate_limit`) — the
  only CLI-side rate-limit channel is *fatal* (`RateLimitError`); there is no
  non-fatal "warning observed, turn still succeeded" channel to mirror
  `allowed_warning`.
- `maestro/sdk.py` (`_is_rate_limit_ghost_completion`,
  `_emit_empty_response_diagnostic`) — quota-exhaustion ghost replies and
  empty responses are detected only on the Anthropic path; a CLI-synthesized
  quota notice would come back as a successful `cli_paced` answer.
- `maestro/sdk.py` (`_build_sdk_options`, `include_partial_messages`) — the
  Anthropic heartbeat rationale: "a long think-only span … produces no log
  output for tens of seconds" without partial messages. The CLI driver's
  parser already diffs snapshots into `StreamDelta`s
  (`maestro_sdk/src/maestro_sdk/types.py` `StreamDelta`), but nothing surfaces
  them as heartbeats.
- `maestro/cost_tracking.py` (`CostSample`, `cost_per_task`) — per-task cost
  queries and the burn-rate window read what `_log_sdk_call` writes; rows that
  never reach it are invisible to both.
- `docs/proposals/operations/067-cli-paced-rollout-canary-observability.md`
  (Step 2) — attributes the provider on usage rows; that step presumes the
  rows exist, which this proposal makes true for per-call telemetry.

## Implementation Plan

Ordered so the single-seam telemetry refactor (the risky move) lands first and
behaviour-identically for `anthropic`, then the `cli_paced`-only signal
sources land behind it. All steps are inert under the default provider.

1. [ ] **One telemetry finalizer at the neutral seam.** `maestro/sdk.py` —
   extract a `_finalize_call_telemetry(result)` helper that (a) runs
   `_estimate_cost_usd` when the usage is metered but `total_cost_usd` is
   `None`/`0.0`, stamping the derived figure with the existing
   `estimated`-style honesty flag, and (b) calls `_log_sdk_call`. Call it from
   the public `query()` / `query_on_session()` delegators for every provider,
   and remove the now-duplicating call sites inside the Anthropic impls so a
   call is finalized exactly once (the impls keep their mid-retry logging;
   only the per-call terminal accounting moves). The Anthropic path's emitted
   line, cost sample, and sink payload must be byte-equivalent before/after.
   - *Depends on:* none.
   - *Acceptance:* under a fake non-Anthropic provider, a metered result
     produces one "SDK call:" line, one cost sample with a token-derived
     `cost_usd`, and one `_usage_sink` invocation; under `anthropic`, existing
     telemetry tests pass unchanged and no call double-logs.
2. [ ] **Sidecar rows price into the burn-rate window.**
   `tests/test_provider_cli_paced.py` + `tests/test_cost_tracking.py` — with
   Step 1 in place, pin the end-to-end contract this proposal exists for: a
   `cli_paced` query whose fixture sidecar carries counts yields a cost sample
   with a non-zero derived `cost_usd` and an `IterationUsage` row via the
   sink; an unmetered fallback row passes through with cost honestly unknown
   (the existing `unmetered` handling), never a fabricated figure.
   - *Depends on:* 1.
   - *Acceptance:* the metered fixture path shows non-zero derived cost in the
     sample and sink payload; the unmetered path shows the unknown-cost shape;
     the burn-rate window query (`cost_tracking` helpers) sees the metered
     spend.
3. [ ] **Non-fatal rate-limit warning channel.**
   `maestro_sdk/src/maestro_sdk/client.py` + `types.py` — add a
   `rate_limit_warning` marker family to `ClientMarkers` (prefixes that signal
   throttling-adjacent notices on an otherwise successful turn) and carry the
   matched notices on `QueryResult` (e.g. `rate_limit_events: list[str]`),
   distinct from the existing fatal `rate_limit` markers. Default empty —
   matches nothing until a proposal 075 pack configures it.
   - *Depends on:* none (markers ship empty; 075 populates them).
   - *Acceptance:* a fake transcript carrying a configured warning line yields
     a successful turn whose result lists the event; the fatal marker still
     raises; unconfigured markers yield empty events.
4. [ ] **Thread warnings into the throttle.**
   `maestro/providers/cli_paced.py` + `maestro/sdk.py` — `_to_query_result`
   re-packs the driver's events into `QueryResult.rate_limit_events`, and the
   Step 1 finalizer (not the provider) feeds
   `_update_warning_throttle_state("allowed_warning" in events)`-equivalent
   state so the warning throttle and hold gate observe every provider through
   one seam. The Anthropic impls stop updating throttle state directly to keep
   single-update semantics.
   - *Depends on:* 1, 3.
   - *Acceptance:* a warning-bearing `cli_paced` result advances the throttle
     multiplier exactly as an Anthropic `allowed_warning` does; a clean call
     decays it; Anthropic throttle tests pass unchanged.
5. [ ] **Ghost-completion and empty-response parity.**
   `maestro/providers/cli_paced.py` — after re-pack, run
   `_is_rate_limit_ghost_completion` over the response text and usage; a ghost
   match raises the rate-limit-categorised `SDKError` (resume-in-place's
   existing handling) instead of returning the notice as an answer. An empty
   response with no tool calls routes through the existing empty-response
   diagnostic before returning, mirroring the agent path's layered detection.
   - *Depends on:* 1 (so the raised path skips finalize-as-success).
   - *Acceptance:* a fixture turn whose text is a quota notice with zero-count
     usage raises a `rate_limit`-categorised error; a genuine paid reply that
     merely discusses rate limits (non-zero counts) passes through; an empty
     response emits the diagnostic.
6. [ ] **Stream-delta heartbeats.** `maestro_sdk/src/maestro_sdk/client.py` +
   `maestro/providers/cli_paced.py` — an optional `on_delta` callback on the
   driver (invoked per parsed `StreamDelta`), which the provider wires to a
   throttled INFO heartbeat matching the agent path's cadence discipline
   (bounded log volume, thinking/answer kind included). Default `None` keeps
   today's silent stream.
   - *Depends on:* none.
   - *Acceptance:* a multi-chunk fake stream produces throttled heartbeat
     lines (fewer lines than chunks under the throttle), and no callback means
     no new logging.
7. [ ] **Documentation.** `docs/sdk-internals.md` — document the neutral
   telemetry seam (one finalize per call, provider-agnostic), the derived-cost
   honesty flag on sidecar rows, the warning-channel marker dependency on 075,
   and the heartbeat knob. Update the `cli_paced` module docstring's "cost is
   derived downstream" sentence to point at the real mechanism.
   - *Depends on:* 1–6.
   - *Acceptance:* docs name the finalizer as the single accounting seam and
     the provider docstring's cost claim matches shipped behaviour.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-I/O + no-real-network + no-real-`sleep`;
provider results are driven through fake providers / fake runners, and the
sink/cost-sample surfaces are captured in-memory.

- `tests/test_sdk.py` (extend) — Step 1 seam: single finalize per call across
  both delegator paths; Anthropic byte-equivalence (existing log/sample/sink
  assertions unchanged); derived-cost estimation branch (metered + `None`
  cost) and its honesty flag; no estimation for unmetered rows.
- `tests/test_provider_cli_paced.py` (extend) — Steps 2, 4, 5: metered sidecar
  → non-zero derived cost in sample + sink; warning events re-packed and
  throttle advanced/decayed; ghost completion raises with `rate_limit`
  category; empty response diagnostic; genuine rate-limit-discussing reply
  unaffected (the zero-usage guard branch).
- `maestro_sdk/tests/test_client.py` (extend) — Step 3 warning-vs-fatal marker
  split; Step 6 `on_delta` invocation per delta and the no-callback default.
- Warning-hold-gate tests (extend where they live today) — the gate engages
  from sustained `cli_paced` warning saturation exactly as from Anthropic.

## Risk Assessment

- **Double or dropped accounting during the seam move.** Moving
  `_log_sdk_call` out of the impls risks a call logged twice (delegator +
  impl) or not at all (a path that bypasses the delegators — e.g. internal
  impl-to-impl calls or tests patching impls directly). Mitigation: Step 1's
  acceptance pins exactly-once on both providers; the existing
  fully-qualified-name test patches are audited in the same step; the
  Anthropic byte-equivalence requirement makes any drift a test failure, not
  a silent skew of `usage.jsonl`.
- **Derived cost wrong for unknown models.** Sidecar rows carry the CLI's
  model name; a pricing-table miss must not fabricate. Mitigation:
  `_estimate_cost_usd` already returns `None` on an unknown model — the row
  then stays honestly costless, the same posture as today, and the honesty
  flag distinguishes derived from metered figures.
- **Warning-marker false positives inflating the throttle.** An over-broad
  warning prefix would slow the loop needlessly. Mitigation: markers ship
  empty, are pack-authored against fixtures (075's discipline), and the
  throttle's existing hysteresis decays spurious singles.
- **Heartbeat log volume.** A fast stream could flood INFO. Mitigation: the
  throttling is part of the step's acceptance, mirroring the agent path's
  bounded-emission design.

**Rollback posture.** Default `[sdk] provider = "anthropic"` keeps Steps 2–6
inert. Step 1 is the one change live under the default; it is
behaviour-pinned byte-equivalent and a single revert restores impl-local
logging. No flags are needed beyond the existing provider selection.

**Data migration.** None. `usage.jsonl` / cost samples gain rows from a new
provider but no shape change; the derived-cost flag reuses the existing
`estimated` vocabulary.

## Out of scope

- **Provider attribution on usage rows** — 067 Step 2 owns adding the
  provider name to rows; this proposal makes the rows exist and priced.
- **Plan-headroom gating** — `/usage`-based throttle awareness stays
  report-only per proposal 062's deferral.
- **A generic provider-side retry pipeline** — resume-in-place and the
  category taxonomy already cover retries; this proposal only feeds them the
  signals they expect.
