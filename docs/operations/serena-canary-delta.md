# Serena symbol-retrieval canary delta (proposal 084, Phase 3)

How to measure whether the Serena symbol-retrieval MCP server actually lowers
burn rate on implementation-heavy work, by comparing the **with-Serena** window
against the Phase 1 [burn-rate baseline](burn-rate-baseline.md). This is the
A/B step ([proposal 084](../proposals/core/084-code-indexing-retrieval-tooling-burn-rate.md)
Phase 3, Step 5) that either justifies promoting the symbol-level retrieval seam
or closes it out. It mirrors the
[Context7 canary delta](context7-canary-delta.md) for the symbol-level server.

Serena is an LSP-backed server that lets the agent read and edit *one symbol*
instead of *one file*, the lever proposal 084 expects to pay off on the
implementation-heavy shapes (`construction`, `module_extension`, `remediation`).

## Canary setup

Serena is enabled in **production only**:

- `maestro.production.toml` sets `serena_enabled = true` (record the date it is
  turned on — that date is the `--since` cutoff below).
- Base `maestro.toml` keeps `serena_enabled = false`, so every other
  environment — dev, tests, any non-production overlay — is unaffected.
- The runtime must reach Serena's launcher: `uvx` on `PATH` and the pinned
  `serena-agent==1.5.3` resolvable by it. This is now validated at startup by
  `maestro preflight` (the `mcp_launchers` check) and by `maestro run` itself —
  a missing `uvx` fails loud and early with an actionable message instead of
  crashing the loop mid-iteration on the first query. Run `maestro preflight`
  on the production host **after** flipping the flag and confirm `mcp_launchers:
  ok` before starting the loop.

**Reversion** is a single flag flip with no state change: set
`serena_enabled = false` in the production overlay. No migration, no cleanup.

## On-enable verification

Serena's entire tool surface auto-exposes under `bypassPermissions`, so the
read-only guard is denial-by-name. Confirm both halves on enable:

- **Read-only tools are available** — the symbol read / reference / overview
  tools (e.g. `mcp__serena__find_symbol`, `mcp__serena__find_referencing_symbols`,
  `mcp__serena__get_symbols_overview`) appear in the session's tool list.
- **Mutating tools are denied** — every `mcp__serena__*` tool in
  `SERENA_MUTATING_TOOLS` (`execute_shell_command`, `create_text_file`,
  `replace_regex`, `replace_content`, `delete_lines`, `replace_lines`,
  `insert_at_line`, `replace_symbol_body`, `rename_symbol`, `safe_delete_symbol`,
  `insert_after_symbol`, `insert_before_symbol`, `write_memory`, `edit_memory`,
  `rename_memory`, `delete_memory`) is absent / denied, via
  `Config.external_mcp_disallowed_tools()`. This keeps the server
  read-only so it cannot bypass the `Bash`-scoped action gate (e.g. a `git push`
  through `execute_shell_command`); the built-in `Agent` tool stays denied as
  today. `SERENA_MUTATING_TOOLS` is the authoritative list — read the tuple, not
  this parenthetical, if the two ever disagree.

## How to capture the delta

Run on the live instance (the dev checkout has no runtime data). The primary,
reproducible capture is a single command: it splits the usage corpus about the
canary date and renders one before/after/Δ table.

```bash
MAESTRO_ENV=production maestro burn-baseline --compare <serena-on-date>
```

`--compare` partitions `usage.jsonl` at the cutoff — **before** = the pre-flag
rows, **after** = rows at or after `<serena-on-date>` (a row stamped exactly at
the cutoff joins the *after* window) — and renders a `| Signal | Before | After |
Δ |` table over the same signals the baseline reports: `$/task` on the
implementation-heavy shapes (`construction`, `module_extension`, `remediation`)
plus the `all` rollup, the prompt-cache hit rate, the implementation-churn
counts (`auto_fix`, `self_review`), and the task-quality outcome mix and average
replans. Deltas are signed `after - before` with an ↑/↓/→ direction glyph, so
the whole A/B pastes straight into the decision record below. Each `$/task` cell
also carries its window task count as `(n=…)`, so a large delta on a one-task
shape reads as the small sample it is. Rows that cannot be dated are dropped from
both windows.

**Caveat — tasks straddling the cutoff.** Only the *usage* rows are split at the
cutoff; a task's outcome and replan count are task-level. A multi-iteration task
with usage rows on both sides of the flip is therefore counted once in *each*
window's task-quality and replan rows (its cost is apportioned per row and is
unaffected). Windows span days and flags flip at quiescent points, so this is
rare — but read a narrow window near the flip with the double-count in mind.

**Manual fallback** — the same comparison by hand, running the report once per
window and doing the arithmetic by eye:

```bash
MAESTRO_ENV=production maestro burn-baseline --since <serena-on-date>   # after window
MAESTRO_ENV=production maestro burn-baseline                            # whole-corpus reference
```

`--since` filters `usage.jsonl` to rows whose `started_at` is at or after the
cutoff (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`); rows that cannot be dated are
dropped. Compare the resulting `$/task` by shape and the prompt-cache hit rate
against the baseline table.

Both modes surface the same two quality cross-checks the `$/task` cost table
does not itself carry — as before/after rows under `--compare`, and as the
window-level lines below under `--since` / no flag:

- **Task quality** — `Task quality (window): N tasks — success A, partial B,
  failure C; replans total R (avg X.XX/task)` (a `, unknown U` segment appears
  only when a task carries an unrecognised outcome), computed over exactly the
  same windowed task population as the `$/task` table. A shift toward `partial`
  / `failure`, or a rising `avg X.XX/task`, is the edit-quality regression a
  falling `$/task` can hide. For per-task `replans` granularity the window-level
  figure does not carry, query `task_history.jsonl` directly for the `replans`
  field on each individual task record.
- **Implementation churn** — `Implementation churn (window): auto_fix calls = N,
  self_review calls = M`, read alongside `$/task` and the cache hit rate as the
  window-level churn proxy. A rise against the baseline means the tool is
  dragging the quality gates back to green more often, not less (auto-fix runs
  in the non-task-attributed implementation flow, so this is a window aggregate,
  not a per-task figure).

## Decision criteria

- **Promote** (`serena_enabled = true` in the `maestro.toml` default) when
  post-Serena `$/task` on implementation-heavy shapes (`construction`,
  `module_extension`, `remediation`) drops measurably **and** no quality
  regression appears (outcome mix steady, `replans` per task not up).
- **Hold** (canary stays on, default unchanged) when the windows show no
  measurable difference.
- **Revert** (flag back to `false` in the production overlay) when `$/task`
  rises or `replans` per task increases.

The promote/hold/revert call is human-operated; nothing here automates it.

## Results

_To be completed by the operator after capturing the live delta._

| Window | Aggregate $/task | Cache hit rate | Notes |
|--------|-----------------:|---------------:|-------|
| Baseline (pre-flag majority, captured 2026-06-23) | $— | 99.9% | from [burn-rate-baseline.md](burn-rate-baseline.md) |
| With Serena (`--since <serena-on-date>`) | _TBD_ | _TBD_ | |

Decision: _TBD (promote / hold / revert)._
