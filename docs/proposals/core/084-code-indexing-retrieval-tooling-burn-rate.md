# 084: Code-Indexing and Retrieval Tooling to Lower Burn Rate

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | burn-in:observing |
| **Affects** | `maestro.toml`, `maestro/cli.py`, `maestro/providers/capability_gate.py`, `maestro/tool_gating.py`, docs, tests |
| **What landed** | Phase 1 (Step 1) — the burn-rate baseline. The `maestro burn-baseline` CLI (`maestro/cli.py` `cmd_burn_baseline`) renders `$/task` by task-shape plus the prompt-cache hit rate by reading meters that already exist: `maestro/cost_tracking.py` `cost_per_task_shape` (joins `cost_per_task` output to each task's goal, classified via `audit_checks.classify_task_shape`, bucketed into the frozen `ShapeCost` dataclass) and `aggregate_cache_hit_rate` (matching `DiagnosticSnapshot.cache_hit_rate`'s `cache_read / (cache_read + input)` definition), assembled by `format_burn_baseline` / `generate_burn_baseline` over `Config.usage_log_path` and the `Config.task_history_path` property; the date-stamped operations note `docs/operations/burn-rate-baseline.md`, carrying the first live production capture (2026-06-23: $—/task aggregate, 99.9% prompt-cache hit rate). Phase 2 (Step 2, canary slice) — Context7 is enabled in the `maestro.production.toml` overlay only (`context7_enabled = true`), default `false` upstream in `maestro.toml`; `burn-baseline` gained mutually-exclusive `--since DATE` and `--compare DATE` window modes (`cmd_burn_baseline` → `generate_burn_baseline(..., since=...)` / `generate_burn_comparison(..., cutoff=...)`) to isolate the post-flag rows or split the corpus about the cutoff into one before/after/Δ table in a single run, with the A/B method documented in `docs/operations/context7-canary-delta.md`; that delta is now captured — a populated Results table (with-Context7 window 2026-06-24 → 2026-07-11, $—/task aggregate, about a third below the baseline) and a recorded **hold** decision (2026-07-11, the canary stays on in production). Phase 3 (Steps 3–4) — the Serena symbol-retrieval MCP server is registered behind `[mcp] serena_enabled` (`Config.serena_mcp_enabled`, default off), wired into `Config.mcp_servers_config()` and kept read-only by `Config.external_mcp_disallowed_tools()` denying the enumerated `SERENA_MUTATING_TOOLS` (e.g. `execute_shell_command`); `required_capabilities_for_phase` requires `remote_mcp` when it is on, and the cross-provider parity test in `tests/test_fault_seeding.py` proves the exec tool is denied for both the Anthropic and `cli_paced` paths while the Bash gate still blocks the carrier command. The Step-5 A/B method is documented in `docs/operations/serena-canary-delta.md` (mirroring the Context7 delta doc). Tests in `tests/test_cost_tracking.py`, `tests/test_cli.py`, `tests/test_config.py`, `tests/test_capability_gating.py`, `tests/test_executor.py`, `tests/test_fault_seeding.py`. |
| **What is missing** | Phase 3 (Step 5) — the A/B delta capture of the symbol-level path on implementation-heavy phases and the recorded promote/hold/revert decision: `docs/operations/serena-canary-delta.md` carries the method but its Results table and decision are still `TBD`, and `serena_enabled` stays `false` in every overlay, so no on-vs-off `$/task` delta has been gathered yet. This is a production observation-gated graduation decision (measurement only — the flag stays default-off until a measured `$/task` improvement justifies promotion), not outstanding behaviour code. |
| **Derived from** | _Operator advisory question (2026-06-20): would GitHub-available code-indexing tools raise capability while lowering burn rate?_ |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Maestro's "burn rate" is its LLM token cost, already a first-class throttled
quantity. The question this proposal answers: can a code-indexing or retrieval
tool lower it while raising implementation quality? The honest answer is
**narrowly yes** — but the lever is not "indexing" in the abstract. Running
search is already free (ripgrep costs no tokens); the dominant cost is **whole
files read into context and edited during the implementation and self-review
phases**. The only retrieval that lowers burn is retrieval that lets a phase
read and edit *one symbol* instead of *one file*.

This proposal commits to a **measure-first** path rather than a tool purchase.
It establishes a burn baseline, takes the one low-effort win already wired into
the tree (Context7 library docs, currently a single disabled flag), and only
then adds a symbol-level retrieval MCP server behind a default-off flag — routed
through the existing action-gate and capability-coverage machinery so the
human-in-the-loop invariant is never weakened. Every step keeps the default off
and promotes only on a measured `$/task` improvement.

Two traps are ruled out explicitly. Whole-repo context packers (Repomix,
GitIngest, repo-to-text) *raise* burn by design and are rejected. Heavyweight
self-hosted code-search stacks (Sourcegraph, Zoekt, OpenGrok) carry ops overhead
unjustified for a single repository this size. The end state is: Context7
enabled where it pays, an optional symbol-level retrieval seam available behind a
flag, and a committed baseline + decision record proving whichever way the
numbers fell — including the possibility that prompt caching, not retrieval, was
the real lever all along.

## Relevant Findings

- `maestro/search.py` (`bm25_rank`, `tokenize`) — the only index in the project
  ranks the **journal** and **evolution-plans** corpora, rebuilt per call over
  bounded corpora. It never indexes the source tree; there is no codebase index
  today, by design.
- `maestro/cli.py` (`_build_agent_tools`) — registers four in-process
  introspection MCP servers (`journal`, `self_awareness`, `memory`, `issues`).
  None retrieves source code; code retrieval is delegated to the provider's
  native tool universe.
- `maestro.toml` (`[mcp]` `context7_enabled`, `github_enabled`,
  `fetch_enabled`) — three external MCP servers are already wired but default
  `false`. Context7 (current library documentation) is a one-flag enable that
  reduces hallucinate-then-iterate cycles on library APIs.
- `maestro/sdk.py` (`allowed_tools` / `disallowed_tools`) — implementation-phase
  code retrieval runs through the SDK's native tools (`Read`, ripgrep-backed
  `Grep`, `Glob`, `Bash`). Running `Grep` consumes no model tokens; the cost is
  the *output* and the whole-file `Read` content pulled into context.
- `maestro/providers/capability_gate.py` (`validate_provider_capabilities`,
  `validate_action_gate_coverage`) — startup checks fail loud on missing
  capabilities or guard-less mutating-phase routing. Any new MCP server that can
  mutate during a code-writing phase must be covered here, or startup aborts.
- `maestro/tool_gating.py` and `maestro/providers/cli_paced_gate.py` — the shared
  action gate (in-process **and** across the `cli_paced` subprocess boundary)
  that every mutating tool must respect; this backs the human-in-the-loop
  evolution invariant. A new mutating retrieval/edit tool is not a TOML
  drop-in — it inherits this gate or it does not ship.
- `maestro/cost_tracking.py` (`cost_per_task`) and `usage.jsonl`
  (`IterationUsage`) — per-task and per-iteration cost meters already exist; any
  retrieval tool's burn effect is A/B-measurable with **no new instrumentation**.
- `maestro/agent.py` (`_compute_budget_throttle_multiplier`) +
  `[loop] max_loop_budget_usd_per_hour` — burn rate is already a measured,
  throttled, first-class quantity, so a regression would be visible immediately.
- `maestro/diagnostics.py` (`DiagnosticSnapshot.cache_hit_rate`) — prompt-cache
  hit rate is already surfaced. The cheapest burn lever (caching the stable
  identity slab / `CLAUDE.md` / repo-orientation context that is re-sent every
  call) is observable today, and may dominate any retrieval saving — which is why
  Phase 1 reads it before any tool lands.

## Implementation Plan

### Phase 1 — Baseline before behaviour change

1. [x] **Capture a retrieval-cost baseline.** docs (operations note) using the
   existing `maestro cost-report` / `cost_per_task` meters — record `$/task` by
   `classify_task_shape` bucket and the current `DiagnosticSnapshot.cache_hit_rate`
   so Phases 2–3 have an A/B reference, and so a caching headroom large enough to
   moot retrieval tooling is caught first.
   - *Depends on:* none.
   - *Acceptance:* a date-stamped baseline table (committed under `docs/`) of
     `$/task` by task-shape and the measured prompt-cache hit rate.

### Phase 2 — Lowest-effort win: Context7

2. [x] **Enable Context7 behind a canary and measure.** `maestro.{env}.toml`
   (`[mcp] context7_enabled`) — flip on in one environment only; compare cost and
   quality on library-doc-bearing tasks against the Phase-1 baseline. Reversion is
   a single flag flip with no state change.
   - *Depends on:* 1.
   - *Acceptance:* one window of usage rows with Context7 active plus a documented
     cost/quality delta vs the Phase-1 baseline; the flag defaults `false` upstream.

### Phase 3 — Symbol-level retrieval seam (the real lever)

3. [x] **Register a symbol-level retrieval MCP server behind a config flag.** new
   `[mcp]` entry + `maestro/cli.py` (`_build_agent` `mcp_servers` wiring) — add an
   opt-in external MCP server exposing symbol read / reference / scoped-edit tools
   (e.g. an LSP-backed server such as Serena), default off, threaded into the
   executor for both the Anthropic and `cli_paced` providers.
   - *Depends on:* 1.
   - *Acceptance:* with the flag on, the server appears in the executor's
     materialized MCP set for both providers; with it off, the tool universe is
     byte-identical to today.
4. [x] **Cover the new server under the action gate and capability checks.**
   `maestro/providers/capability_gate.py`, `maestro/tool_gating.py` — any mutating
   symbol-edit tool routes through `validate_action_gate_coverage` and the shared
   action gate exactly as task-execution Bash does; read-only retrieval tools are
   exempt but explicitly enumerated, never implicitly trusted.
   - *Depends on:* 3.
   - *Acceptance:* `validate_action_gate_coverage` passes at startup with the
     server enabled, and a fault-seeded mutating call through the new tool is
     blocked in-process and across the `cli_paced` boundary, matching the bash-gate
     precedent.
5. [ ] **A/B the symbol-level path on implementation-heavy phases.** measurement
   only — compare `$/task` and edit-correctness proxies (replan rate, quality-gate
   `auto_fix` rounds) with the server on vs off, then record a promote / hold /
   revert decision.
   - *Depends on:* 3, 4.
   - *Acceptance:* a committed decision backed by `usage.jsonl` deltas; the flag
     stays `false` by default until the delta justifies promotion.

## Testing Strategy

- **Config flag** (`tests/test_config.py`) — the new `[mcp]` flag parses,
  defaults off, and an env overlay flips it; mirror the existing
  `context7_enabled` coverage. No real file I/O (`mem_fs`).
- **Capability + gate coverage** (`tests/test_capability_gating.py`) —
  `validate_action_gate_coverage` still passes with the server enabled, and a
  mutating tool lacking a guard fails loud at startup.
- **Action-gate parity** (`tests/test_fault_seeding.py`) — a seeded mutating call
  via the new MCP tool is blocked in-process **and** across the `cli_paced`
  subprocess boundary, following the existing bash-gate fault-seeding pattern
  (in-memory mutation, no subprocess).
- **Executor wiring** (`tests/test_executor.py`) — with the flag on, the server is
  present in the materialized MCP set; with it off, the set is unchanged. MCP
  server construction is mocked; no real subprocess, network, or `sleep`, per the
  CLAUDE.md testing rules.

## Risk Assessment

- **Trust boundary — a third-party MCP server with tool-call power.** A
  symbol-edit tool that bypassed the action gate would breach the human-in-the-loop
  evolution invariant. Mitigated by Phase-3 step 4: mutating tools inherit the
  shared `tool_gating` action gate and are covered by the fail-loud
  `validate_action_gate_coverage` startup check; read-only retrieval tools are
  enumerated, not implicitly trusted. The attack surface is the edit tool, and the
  parity test in `tests/test_fault_seeding.py` proves the control fires.
- **Burn could rise, not fall.** A retrieval tool's own overhead (tool schemas,
  server chatter, extra turns) can exceed the whole-file-read saving. Mitigated by
  measure-first gating — every phase defaults off and promotes only on a measured
  `$/task` improvement against the Phase-1 baseline.
- **Provider divergence.** If `cli_paced` and Anthropic do not both honour the new
  server, behaviour forks. Step 3's acceptance requires presence in both providers'
  MCP sets; otherwise the flag stays off.
- **Rollback posture.** Every step is a config flag defaulting `false`; reverting
  is a flag flip with no state migration and no schema change.

## Out of scope

- **Whole-repo context packers** (Repomix, GitIngest, repo-to-text) — they raise
  burn by design; explicitly rejected, not deferred.
- **Heavyweight self-hosted code search** (Sourcegraph, Zoekt, OpenGrok, Cody) —
  ops overhead unjustified for a single repo this size.
- **Replacing the ripgrep-backed `Grep`** — search is already free at the query
  step; only the post-search context cost is in scope.
- **A bespoke in-house codebase index** — extending `maestro/search.py` BM25 to the
  source tree is a heavier alternative considered and not chosen here; an existing
  LSP-backed MCP server reaches symbol-level edits without that build cost.
