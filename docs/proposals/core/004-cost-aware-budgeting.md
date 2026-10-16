# 004: Cost-Aware Query Budgeting

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped — burn-rate only; per-call enforcement removed 2026-04-30 |
| **Affects** | `maestro/sdk.py`, `maestro/agent.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro/messaging.py`, `maestro/dashboard/routes.py` |
| **What landed** | Per-call cost in `QueryUsage.total_cost_usd`, `caller` labels, structured `"SDK call:"` log lines, SDK telemetry rollups in `maestro/diagnostics.py`, dashboard surface at `/api/diagnostics`. **Self-regulating burn-rate throttle** — rolling-window rate via `build_cost_rates()` over `[loop] budget_rate_window_hours`; when the rate approaches `[loop] max_loop_budget_usd_per_hour` and at least `[loop] budget_rate_min_cost_usd` has been spent in the window, the end-of-iteration sleep is stretched by `min([loop] budget_throttle_max_multiplier, max(1.0, rate/limit))`. All 12 phases continue running at every multiplier — only the inter-iteration gap grows. Entry (first multiplier > 1.0) and exit (next multiplier == 1.0) notify via Telegram and write journal milestones; sustained throttling does not re-notify. Throttle state persisted in `lifecycle.json`. Smoke mode bypasses the throttle. |

## Description

A single evolution cycle involves 5+ LLM calls (introspect, reflect, propose, implement, plus retries), and the autonomous loop runs indefinitely. Maestro now *observes* per-call cost and surfaces an aggregated rollup on the dashboard. The shipped spend gate is **rate-based, not total-based**: a session-total cap would be tripped for the wrong reasons (individual iterations have wildly different costs — a quick messaging reply is cents, an `implement_approved_plans` tier is dollars), and the only operationally useful question is "is this burning faster than I'm comfortable with right now?". When the rate approaches the configured limit, the loop **throttles itself**: every phase keeps running, but the inter-iteration sleep is stretched proportionally to `rate/limit` (capped at `budget_throttle_max_multiplier`). As samples age out of the rolling window, the rate falls, the multiplier falls, and the loop auto-returns to base pace — no inbound message required. This replaces the earlier binary message-gated "budget standby" design: the message gate was too coarse (phases Thomas relied on, like `respond_to_messages`, were skipped during standby) and required a human to unblock even when the spike had already passed.

## Relevant Findings

### Per-call cost already captured (`maestro/sdk.py`)
```python
@dataclass
class QueryUsage:
    total_tokens: int = 0
    tool_uses: int = 0
    duration_ms: int = 0
    total_cost_usd: float | None = None
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    caller: str | None = None
    attempts: int = 1
```
The SDK returns a per-call cost (reported directly by `claude-agent-sdk`, not estimated from tokens — so the "pricing constants" concern from the original proposal no longer applies). Each call site sets `caller="<name>"` for attribution.

### Structured log line for every successful call (`sdk.py`)
Every successful `query()` emits one `"SDK call: ..."` log line carrying `tokens_in`, `tokens_out`, `cache_read`, `cache_create`, `cost`, `duration_ms`, `turns`, `caller`, `attempts`. Rate-limit rejections emit `"SDK rate-limited: ..."`. These are machine-parsed downstream.

### SDK telemetry aggregation (`maestro/diagnostics.py`)
```python
@dataclass
class SDKUsageStats:
    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    cache_hit_rate: float = 0.0
    by_caller: dict[str, int] = field(default_factory=dict)
    cost_by_caller: dict[str, float] = field(default_factory=dict)
    rate_limit_hits: int = 0
```
`build_sdk_usage_stats()` parses the structured log lines into this dataclass. The dashboard exposes the rollup at `GET /api/diagnostics`. **The class docstring explicitly states: "All values are observation only — they do not influence retry or backoff behavior."**

### No agent-level accumulation in `MaestroAgent`
`maestro/agent.py` has no `_session_cost`, `_iteration_cost`, or equivalent field. Each SDK call is fire-and-forget from the loop's perspective — the dashboard rollup is the only place cumulative cost exists, and it is read-only.

### Run loop has no spend gate
`run_loop()` walks the phases in `_LOOP_PHASES` (`agent.py`) and invokes `contemplate`, `evolve`, `generate_task`, `dream`, etc., without any check on cumulative spend. A runaway loop simply keeps burning credit.

### Lifecycle file has no cost (`agent.py`)
`_persist_progress()` writes `{"iteration": N, "next_phase": "..."}` into `lifecycle.json`; shutdown writes `last_shutdown`. Neither carries a cost field, so restart loses cumulative spend even when the logs still have it.

### Query call sites (for context on attribution)
All call sites now set an `effort` and a `caller`:
- `consciousness.introspect()` / `assess_stage_readiness()` / `update_self_model()` — `effort="medium"`
- `evolution.reflect()` / `propose_plan()` / `_self_review()` — `effort="high"`
- `evolution.implement()` / `auto_fix()` — tool-using, `evolve_max_turns` budget
- `purpose.generate_hypothesis()` / `evaluate_hypothesis()` — `effort="medium"`
- `dreamer.dream()` — `effort="high"`
- `planner.decompose()` / `replan()` — `effort="medium"`
- `executor.execute()` — tool-using, multi-turn
- `_distill_findings()` / task generation / message replies — `effort="low"`

## Shipped design (self-regulating burn-rate throttle)

### Configuration (`[loop]`)

| Key | Default | Meaning |
|---|---|---|
| `max_loop_budget_usd_per_hour` | `0.0` | Max burn rate. `0` disables throttling. |
| `budget_rate_window_hours` | `1.0` | Rolling window for rate calculation. |
| `budget_rate_min_cost_usd` | `0.10` | Suppress throttling until at least this much has been spent in the window (avoids first-sample extrapolation false positives). |
| `budget_throttle_max_multiplier` | `60.0` | Hard ceiling on the sleep multiplier. Prevents pathological stalls if rate is wildly above limit. |

### Mechanism

`_compute_budget_throttle_multiplier(smoke) -> (multiplier, snapshot)` runs at end-of-iteration (just before the inter-iteration sleep). Returns `1.0` when throttling is disabled (`max_loop_budget_usd_per_hour == 0`), in smoke mode, when spend-floor is not yet met, or when `rate < limit`. Otherwise returns `min(budget_throttle_max_multiplier, rate / limit)` clamped to at least `1.0`. The sleep block uses `interval * multiplier` as its deadline and sleeps in `_STANDBY_MAX_SLEEP_SECONDS`-bounded chunks on the shutdown event so `maestro shutdown` remains responsive even under long throttled sleeps.

All 12 phases continue running at every multiplier — only the gap between iterations stretches. As samples age out of the rolling window, the rate falls, the multiplier falls, and the loop returns to base pace on its own. No inbound message is required.

### State machine

`MaestroAgent` owns a single piece of persisted state: `_budget_throttle_entered_at` (ISO timestamp, set on first `multiplier > 1.0`, cleared on first `multiplier == 1.0`). Persisted alongside `iteration` / `next_phase` in `lifecycle.json`. Legacy `budget_standby_entered_at` / `budget_standby_anchor_message_id` keys left over from the prior implementation are silently ignored on read so in-flight deploys do not crash startup.

`_maybe_notify_throttle_transition(multiplier, snapshot)` runs every iteration (even the last, when no sleep follows) so a released throttle always fires its exit notification. On the entry edge (`> 1.0` with previous state `None`) it captures the timestamp, notifies via Telegram, and writes a journal milestone (`triggered_by="budget_throttle_enter"`). On the exit edge (`== 1.0` with previous state set) it clears the timestamp, notifies, and journals (`triggered_by="budget_throttle_exit"`). Sustained throttling does not re-notify. Notify/journal failures are swallowed with a warning so a transient Telegram / journal outage cannot crash the loop.

### Dashboard surface

During the stretched sleep, `current_phase = "budget_throttle"` is persisted so the dashboard's public visualizer renders a throttling state instead of the idle halo. `budget_throttle_active` / `budget_throttle_entered_at` are exposed on `GET /api/diagnostics`.

### Smoke mode

`maestro loop --smoke` bypasses `_compute_budget_throttle_multiplier()` entirely (it always returns `1.0` under `smoke=True`) so deploy-validation runs deterministically regardless of configured thresholds or historical cost samples.

## Testing Strategy

1. **Config validation** — each of `max_loop_budget_usd_per_hour`, `budget_rate_window_hours`, `budget_rate_min_cost_usd`, `budget_throttle_max_multiplier` rejects bool, NaN, inf, negative; each honors disabled/default.
2. **`current_burn_rate_usd_per_hour`** — returns `build_cost_rates(...).cost_per_hour` from persisted samples; returns `0.0` when tracking is unavailable.
3. **Multiplier computation** — disabled (`0.0`) returns `1.0`. Smoke mode returns `1.0`. Below min-floor returns `1.0`. At floor but below limit returns `1.0`. Above limit scales linearly. Far above limit caps at `budget_throttle_max_multiplier`. `build_cost_rates` error returns `(1.0, None)` with a logged warning (never crashes the loop).
4. **Transition notifications** — entry edge fires once (state set, notify called, journal milestone written). Sustained `> 1.0` does not re-notify. Exit edge fires once. Stable `== 1.0` does not notify. Notify / journal failures are swallowed.
5. **All phases run under throttle** — even when the multiplier is high, every phase still runs to completion. The only observable effect is the longer inter-iteration sleep.
6. **Lifecycle round-trip** — `_budget_throttle_entered_at` persists across restart; legacy `budget_standby_*` keys are ignored on read.
7. **Disabled (`0.0`)** — rate never triggers throttling regardless of spend.
8. **Smoke mode** — bypasses throttling entirely.

Run: `python3.12 -m pytest tests/ -v -k "throttle or budget"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Rate window too narrow trips on a single outlier call | Low | `budget_rate_min_cost_usd` floor suppresses first-sample extrapolation; default 1h window is wide enough that normal iteration cost variation doesn't spike the rate |
| Pathological sleep stalls the loop indefinitely | Low | `budget_throttle_max_multiplier` (default `60.0`) caps the sleep, so worst-case inter-iteration gap is `interval × 60`; shutdown is still responsive in `_STANDBY_MAX_SLEEP_SECONDS`-bounded chunks |
| Restart during throttling loses the entry timestamp | Low | `_budget_throttle_entered_at` persists in `lifecycle.json`; legacy `budget_standby_*` keys are tolerated on read |
| Auto-exit resumes spend before operator is aware | Accepted | Self-regulation is the design goal — the rolling window has already priced in the cooldown; entry/exit still notify via Telegram so the operator has observability |
| Smoke mode gated out deploy-validation | Addressed | Smoke mode returns multiplier `1.0` unconditionally so deploy smoke iterations never throttle on historical cost samples |
