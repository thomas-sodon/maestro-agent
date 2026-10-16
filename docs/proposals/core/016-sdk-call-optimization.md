# 016: SDK Call Optimization (Observe-Heavily, Intervene-Lightly)

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped (A1, A2, A3 all shipped; cost-trend and cache-ratio diagnostics now gated through `is_noteworthy()`) |
| **Affects** | `maestro/sdk.py`, `maestro/agent.py`, `maestro/executor.py`, `maestro/diagnostics.py`, `maestro/config.py`, `maestro/toml_config.py` |
| **Source study** | [docs/proposals/research/clauditor-turn-optimization.md](../research/clauditor-turn-optimization.md) |
| **Related proposals** | [004-cost-aware-budgeting](004-cost-aware-budgeting.md) |
| **What landed** | A1 per-iteration `IterationUsage` persisted to `usage.jsonl` via module-level usage sink + `read_iteration_usage()` (see CLAUDE.md "Per-iteration SDK usage persistence"). A2 post-hoc fingerprint-based loop detector in `executor.py` (see CLAUDE.md "Executor loop detection"; `executor_loop_detect_repeats`, default 3). A3 `_compress_bash_output()` strips ANSI escapes, progress glyphs, collapses identical lines, hard-caps at 2000 chars (see CLAUDE.md "Bash output compression"). |
| **What is missing** | Nothing material. `recent_iteration_costs` and `cost_trend` are computed in `DiagnosticSnapshot` and gated through `is_noteworthy()` (rising/spiking trends inject diagnostics into contemplation), and prompt-cache hit-rate is now also gated through the same `is_noteworthy()` path (`cache_hit_rate < _CACHE_HIT_RATE_FLOOR`). The proposal's Step 2.4 ("append the trend line to `DiagnosticSnapshot.summary`") shipped as a separate field rather than a string append — the contemplation-gating effect is equivalent. |

## Description

> Status updated to shipped on 2026-06-01 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The clauditor study (research note above) catalogues 18 mechanisms a third-party tool uses to keep Claude Code sessions cheap. Three of those ideas map directly onto Maestro's shape and are small enough to land as one focused evolution; the rest are either deferred to follow-up proposals or rejected as a poor fit.

This proposal implements the three "adopt now" items from the study:

- **A1.** Persist `QueryUsage` per loop iteration so Maestro stops flying blind on cumulative cost.
- **A2.** Add a fingerprint-based loop detector to the only unbounded query site (`executor.py`).
- **A3.** Compress Bash tool output before it re-enters the executor's conversation context.

The guiding principle, taken from the study's closing observation, is **observe heavily, intervene lightly**: instrument the loop, surface anomalies through the existing diagnostics snapshot, and reserve hard blocks for the cases where the model has already lost the plot.

This proposal is **complementary** to #004 (Cost-Aware Budgeting). #004 proposes enforcement (budget gate). This proposal proposes the persistence and observability layer that #004 silently assumes already exists. A1 is the prerequisite for #004 and for the deferred items listed at the bottom.

## Relevant Findings

### `QueryUsage` is captured per call and discarded (`maestro/sdk.py`)
`QueryUsage` already records `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `total_cost_usd`, `num_turns`, `duration_ms`, `caller`, `attempts`. Each call is logged exactly once via `_log_sdk_call()` (`sdk.py`) as a structured `"SDK call: ..."` line. Nothing else consumes the value. The aggregated `SDKUsageStats` in `diagnostics.py` is rebuilt from log lines on demand and never persisted.

### `lifecycle.json` is the natural durability surface (`CLAUDE.md` "Phase-level loop progress persistence")
The loop already writes `{"iteration": N, "next_phase": "..."}` to `{memory_dir}/lifecycle.json` after every phase via `_persist_progress()`. Adding per-iteration cost/token totals to a sibling `usage.jsonl` (append-only, one line per iteration) reuses the same `atomic_write` / `file_lock` plumbing and matches Maestro's existing JSONL conventions.

### `executor.py` is the only unbounded query site
Both `EvolutionEngine.implement()` and `auto_fix()` use `max_turns=config.loop_evolve_max_turns` (default 50). `Executor.execute` passes no explicit `max_turns`, relying on the SDK default. The executor is also where Bash tool calls are densest, so it is the natural place for both A2 (loop detection) and A3 (Bash output compression) to live.

### Diagnostics is already gated by `is_noteworthy()` (`maestro/diagnostics.py`)
The diagnostic snapshot is only injected into contemplation context when there is something worth reporting. New signals (cost spikes, loop detector trips, cache ratio collapse) slot into this gate naturally without polluting clean runs.

### `QueryOptions.max_budget_usd` (resolved by removal 2026-04-30)
The unchecked field was removed entirely; the burn-rate throttle from #004 is the sole dollar-budget mechanism. This proposal only persists the data the throttle needs.

## Implementation Plan

### Step 1 — Persist per-iteration usage (A1)

**Goal:** Every loop iteration ends with one durable record of how much SDK time and money it spent.

1. Add a new module-level constant in `agent.py`: `_USAGE_LOG = "usage.jsonl"`.
2. In `MaestroAgent.__init__()`, add an in-memory accumulator:
   ```python
   self._iteration_usage: dict[int, IterationUsage] = {}
   ```
   where `IterationUsage` is a new dataclass in `sdk.py`:
   ```python
   @dataclass
   class IterationUsage:
       iteration: int
       started_at: str  # ISO timestamp
       calls: int = 0
       input_tokens: int = 0
       output_tokens: int = 0
       cache_read_tokens: int = 0
       cache_creation_tokens: int = 0
       total_cost_usd: float = 0.0
       per_caller: dict[str, int] = field(default_factory=dict)  # caller -> calls
   ```
3. Provide a sink hook on `query()`. The cleanest option: add an optional module-level callback `_usage_sink: Callable[[QueryUsage], None] | None = None` in `sdk.py` and a `set_usage_sink()` setter. `agent.py` registers a sink in `_build_agent()` that updates `self._iteration_usage[current_iteration]`. (Alternative: pass a sink through `QueryOptions`. The module-level callback is simpler because `Executor`/`EvolutionEngine` do not need to thread it through.)
4. In `run_loop()`, immediately after the `"SDK iteration: iter=N"` marker is written (top of an iteration), create a fresh `IterationUsage(iteration=N, started_at=...)` keyed by `N`.
5. At the end of each iteration (after `dream` phase, before the loop repeats), append a JSONL line to `{memory_dir}/usage.jsonl` via `atomic_write` semantics on the directory file (use `file_lock` over an `open(..., "a")` since it is append-only).
6. Add `read_iteration_usage(memory_dir, n=20)` helper in `sdk.py` (or a new `maestro/usage.py` if it grows): `deque(maxlen=n)` streaming read so it stays bounded like `parse_app_log_tail`.

**Size:** ~120 LOC including the dataclass, sink wiring, and one test fixture.

### Step 2 — Surface usage in diagnostics

1. In `diagnostics.py`, extend `DiagnosticSnapshot` with optional fields:
   ```python
   recent_iteration_costs: list[float] = field(default_factory=list)
   cost_trend: Literal["flat", "rising", "spiking"] | None = None
   ```
2. In `build_diagnostic_snapshot()`, call `read_iteration_usage(memory_dir, n=10)` and compute:
   - `recent_iteration_costs` = last 10 totals
   - `cost_trend = "spiking"` if `last > avg(prev_5) × 3 and last > 0.10`
   - `cost_trend = "rising"` if `avg(last_3) > avg(prev_5) × 1.5`
3. Add to `is_noteworthy()`: spike → noteworthy, rising → noteworthy only if absolute last cost > $0.05.
4. Append the trend line to `DiagnosticSnapshot.summary` so `contemplate()` sees it as plain text.

**Size:** ~60 LOC.

### Step 3 — Loop detector in the executor (A2)

**Goal:** When the executor's underlying agent loops on the same tool call ≥3 turns in a row, abort the step cleanly instead of burning the SDK budget.

The tool-call stream is observable through `result.tool_calls` (already populated on `QueryResult`; see `sdk.py`). Maestro currently only inspects this after the call completes — too late to interrupt mid-stream. There are two viable scopes:

**Option A (recommended, smaller):** post-hoc loop detection.
1. After `result = await query(...)` in `Executor._execute_step`, walk `result.tool_calls` and fingerprint each as `f"{tool.name}:{_hash(tool.input)}:{_hash(tool.result)}"`. The hash is `hashlib.sha1(json.dumps(input, sort_keys=True).encode()).hexdigest()[:12]`.
2. If any fingerprint appears `≥3` consecutive times in the sequence, mark the step result as `success=False` with `error="loop detected: <fingerprint> repeated N times"`.
3. Add a `loop_detected: bool = False` field on `StepResult` so the planner's `replan()` path can include "the previous attempt looped on X" in the new prompt.
4. The agent already writes a journal realization on step failure; piggyback on that by including the looped tool name in the failure message so the realization is meaningful.

**Option B (deferred, larger):** mid-stream interruption via an SDK callback. This requires SDK plumbing we do not currently have. Out of scope.

**Size:** ~80 LOC including a new `_detect_tool_loop()` helper in `executor.py` plus tests.

### Step 4 — Bash output compression (A3)

**Goal:** When the executor's underlying agent runs verbose Bash commands, the output is replayed in every subsequent turn at full length. Compress it before it re-enters context.

The compression itself is portable from clauditor's `bash-filter.ts`:
1. Strip ANSI escape sequences (`\x1b\[[0-9;]*[a-zA-Z]`) and progress-bar glyphs (`█░▓▒` and runs of `=` or `+`).
2. Collapse runs of identical lines into one surviving copy followed by `[line repeated N more times]`.
3. If output is recognisably a package-manager log (`pip install`, `npm install`, `apt-get`), keep the first line, the last 3 lines, and any line containing `error|warn|fail|exception|✗` (case-insensitive).
4. Hard cap at 2000 characters: 800 head + 800 tail + `\n... [N chars elided] ...\n` middle.

Where to apply it is the harder question. The executor does not own the Bash tool — the SDK does. Two options:

**Option A (recommended):** post-process tool results in `QueryResult.tool_calls` before they are summarised back to the model. This requires intercepting the SDK loop, which we cannot do without hooks. **Skip this option.**

**Option B (recommended fallback):** apply compression to `result.tool_calls[*].result` for *the value Maestro itself logs and journals*, not for what re-enters the model context. This does not save tokens directly but it (a) reduces journal/log bloat and (b) is the only intervention we can make from outside the SDK. Document this limitation explicitly.

**Option C (proper fix, deferred):** add a Bash wrapper MCP server. The `Executor` registers a `bash_compressed` MCP tool that runs the command via `subprocess` and applies compression before returning. Then prompt the agent to prefer `bash_compressed` over the built-in `Bash` tool. This is real but it is an *additional* MCP server with its own tests and is bigger than this proposal should be.

**Decision:** ship Option B in this proposal as `_compress_bash_output()` in `executor.py`, applied to journal / log writes only. File a follow-up issue for Option C ("Bash compression MCP wrapper") so it is tracked but not entangled with A1/A2.

**Size:** ~70 LOC for the compressor + its unit tests.

### Step 5 — Wire it all together

1. `_build_agent()` registers the usage sink before any component is constructed.
2. `run_loop()`'s top-of-iteration block opens a fresh `IterationUsage` and clears `self._iteration_usage[N-1]` if needed.
3. `run_loop()`'s end-of-iteration block flushes the `IterationUsage` to `usage.jsonl`.
4. `diagnostics.build_diagnostic_snapshot()` reads it back via `read_iteration_usage`.
5. `Executor._execute_step()` calls `_detect_tool_loop(result.tool_calls)` and sets `loop_detected` on `StepResult`.
6. `Executor._execute_step()` runs `_compress_bash_output()` on journal-bound copies of tool results.

### Step 6 — Update CLAUDE.md

Add to the **Key Design Decisions** section:

> - **Per-iteration SDK usage persistence**: Every `query()` call updates an in-memory `IterationUsage` accumulator via a module-level usage sink registered in `_build_agent()`. At the end of each loop iteration, the accumulator is appended to `{memory_dir}/usage.jsonl`. `diagnostics.py` reads the last 10 iterations to compute `cost_trend` (flat/rising/spiking) and surfaces it through `is_noteworthy()` so contemplation sees cost anomalies without polluting clean runs. This is the persistence layer assumed by #004 (cost-aware budgeting).
> - **Executor loop detection**: After each `_execute_step` query, the tool-use stream is fingerprinted as `tool:hash(input):hash(result)`. Three consecutive identical fingerprints mark the step as failed with `loop_detected=True`, which `Planner.replan()` includes in the revised prompt. Post-hoc detection only — there is no mid-stream interrupt.
> - **Bash output compression for journal/log writes**: Verbose Bash output is compressed before being persisted to journal entries and log lines (strip ANSI, collapse repeats, prefer error-bearing lines, hard cap 2000 chars). The compressor cannot intercept the SDK's own context replay; that requires the deferred Bash MCP wrapper.

## Testing Strategy

All tests must obey the project's testing principles (`mem_fs`, mocked SDK, no real I/O, no real sleep).

1. **`IterationUsage` accumulation** — register the sink, dispatch three fake `QueryUsage` records via the sink callable, assert per-caller counts and totals match.
2. **`usage.jsonl` round-trip** — write three iterations, read back via `read_iteration_usage(n=2)`, assert only the last two are returned in order.
3. **Bounded reader** — write 100 iterations, assert `read_iteration_usage(n=10)` returns exactly 10 and never loads the full file (verify by patching `open` to count `readline` calls or by reading via the same `deque(maxlen=n)` pattern as `parse_app_log_tail`).
4. **Cost trend classification** — feed synthetic cost sequences into `build_diagnostic_snapshot` and assert `flat`, `rising`, `spiking` outputs match the threshold rules in Step 2.
5. **`is_noteworthy()` gating** — assert spike triggers, rising-with-low-absolute-cost does not.
6. **Loop detector — happy path** — three distinct tool calls → `loop_detected=False`.
7. **Loop detector — repeat trip** — same `(tool, input, result)` triple three times consecutively → `loop_detected=True`, error string contains the tool name.
8. **Loop detector — interleaved** — `A B A B A B` → not flagged (not consecutive); `A A A B` → flagged at the third `A`.
9. **Loop detector — input differs** — `Read /a`, `Read /b`, `Read /c` → not flagged (different input hashes).
10. **Bash compression — ANSI strip** — input with `\x1b[31mERROR\x1b[0m` → output without escape codes, `ERROR` preserved.
11. **Bash compression — line collapse** — 50 identical lines → one surviving copy + `[line repeated 49 more times]`.
12. **Bash compression — hard cap** — 10 000-char input → ≤2000-char output with head/tail/ellipsis structure.
13. **Bash compression — error preference** — long log with one `error: foo` line buried in the middle → `error: foo` preserved in output even when surrounding lines are dropped.
14. **End-to-end iteration** — drive `run_loop()` for two iterations against a `SDKMock`, assert `usage.jsonl` has two lines, each with the right call count, and that the in-memory accumulator is reset between iterations.

Run: `python3.12 -m pytest tests/ -v -k "usage or loop_detect or bash_compress"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Module-level usage sink leaks state across tests | Medium | Provide `clear_usage_sink()`; add an autouse fixture that resets it after each test |
| `usage.jsonl` grows without bound | Low | Append-only by design; bounded reads via `deque(maxlen=n)`; can be rotated by future tiered-memory work (#002) |
| Loop detector false positives on idempotent tools (e.g. repeated `Read` of same file) | Medium | The agent should not normally re-read the same file 3× in a row; if it does, that *is* the symptom worth flagging. Bound the impact by emitting a single failure (which the planner then re-plans around) instead of escalating. Tunable threshold lives in config: `[loop] executor_loop_detect_repeats = 3`. |
| Bash compression hides errors | Medium | The "prefer error-bearing lines" rule keeps error lines even when surrounding context is dropped. Compression applies only to journal/log writes, not to the SDK context, so the model itself never sees a compressed view. |
| Cost-trend thresholds drift as model pricing changes | Low | Thresholds are absolute USD, not token counts. If pricing changes by 2× we re-tune; documented as a known limitation. |
| Sink callback throws and breaks `query()` | High | Wrap the sink invocation in `try/except Exception` inside `query()` and log a warning. The sink is observation-only and must never fail a query. |

## Out of Scope (Deferred to Future Proposals)

These were considered and intentionally left out so this proposal stays focused. Each is worth its own future proposal:

- **D1 — Auto-calibrated waste threshold from past sessions.** Building on 016's A1 (`usage.jsonl` persistence), a follow-up can read `usage.jsonl`, compute the break-even rotation point per past iteration, and trigger an early `dream()` when the current iteration is on track to exceed it. This is the autonomous-agent analogue of clauditor's session rotation and the most genuinely novel idea in the source study.
- **D2 — Cache-degradation, token-spike, and context-fill detectors.** Same shape as the cost-trend detector in Step 2 but using cache ratios and per-call token spikes. They depend on the sink (Step 1) being in place.
- **D3 — Error index with proximity-based fix capture.** A `(baseCommand, first_line_of_error) → fix-within-60s` index, persisted under memory, automatically injected as advisory context into future executor prompts. Its own subsystem with its own storage.
- **D4 — Bash compression MCP wrapper.** Option C from Step 4. Adds a `bash_compressed` MCP tool so compression actually saves SDK tokens, not just journal bytes.
- **Skipped entirely:** clauditor's seven-hook plumbing, session-blocking UI, CLAUDE.md footprint audit, buggy-version detector, skill/workflow pattern detection, cross-project session handoffs. See the source study for rationale.

## Top-Level Effort Estimate

Roughly five focused commits:

1. `IterationUsage` + sink + `usage.jsonl` round-trip + tests (Steps 1)
2. Diagnostics integration + tests (Step 2)
3. Executor loop detector + tests (Step 3)
4. Bash compressor + tests (Step 4)
5. CLAUDE.md update + integration test driving `run_loop()` for two iterations (Steps 5-6)

Each commit is independently mergeable; commits 3 and 4 depend on nothing in commits 1-2 and could be parallelised by a future agent if useful.
