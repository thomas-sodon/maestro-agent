# Context7 canary delta (proposal 084, Phase 2)

> **Figures redacted.** Dollar figures tied to the operator's billing were removed and are shown as `$—` / `€—`.

How to measure whether the Context7 library-docs MCP server actually lowers
burn rate, by comparing the **with-Context7** window against the Phase 1
[burn-rate baseline](burn-rate-baseline.md). This is the A/B step that either
justifies a symbol-level retrieval seam (Phase 3) or closes the proposal.

## Canary setup

Context7 is enabled in **production only**:

- `maestro.production.toml` sets `context7_enabled = true` (turned on 2026-06-21).
- Base `maestro.toml` keeps `context7_enabled = false`, so every other
  environment — dev, tests, any non-production overlay — is unaffected.

**Reversion** is a single flag flip with no state change: set
`context7_enabled = false` in the production overlay.

## Baseline reference

The pre-tool reference is the table committed in
[burn-rate-baseline.md](burn-rate-baseline.md) — captured 2026-06-23 on the
production instance: **$—/task** aggregate and a **99.9%** prompt-cache hit
rate.

Read it with one caveat: the canary went on 2026-06-21, **two days before** that
capture, so the baseline already mixes a handful of pre-Context7 and
post-Context7 rows. Its `remediation`-dominated majority predates the flag, so
the committed table is the practical pre-Context7 reference — but the
`--since` cutoff below is what cleanly isolates the post-flag window.

## How to capture the delta

The live usage log was reset after the canary went on: its earliest surviving
row is stamped **2026-06-24T16:21Z**, so this corpus holds **no pre-flag
rows**. The single-command split (`burn-baseline --compare 2026-06-21`) renders
an empty *Before* window against it (verified 2026-07-11) and is not usable
here. The primary, reproducible capture is therefore the **windowed report
compared against the committed baseline table** in
[burn-rate-baseline.md](burn-rate-baseline.md):

```bash
MAESTRO_ENV=production maestro burn-baseline --since 2026-06-21   # with-Context7 window
```

`--since` filters `usage.jsonl` to rows whose `started_at` is at or after the
cutoff (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`); rows that cannot be dated are
dropped. Compare the resulting `$/task` by shape and the prompt-cache hit rate
against the baseline table. Date-stamp every capture: the corpus grows by
hundreds of rows a day, so figures reproduce only for the capture date.

**Deployed-vintage caveat.** The `Task quality (window)` and `Implementation
churn (window)` lines below come from the current code; a deployed checkout
that predates them prints the cost table only. The full-fidelity equivalent
runs the current code against the live data paths — `generate_burn_baseline`
takes explicit corpus paths and the read is side-effect-free:

```bash
python3.14 -c "
from pathlib import Path
from datetime import datetime, UTC
from maestro.cost_tracking import generate_burn_baseline
print(generate_burn_baseline(
    Path('/home/maestro/maestro/data/usage.jsonl'),
    Path('/home/maestro/maestro/data/task_history.jsonl'),
    since=datetime(2026, 6, 21, tzinfo=UTC)))"
```

The report surfaces two quality cross-checks the `$/task` cost table does not
itself carry:

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

### `--compare` for future flag flips

Where **both** windows survive in-corpus — a deliberate flag cycle, or a flip
made without resetting `usage.jsonl` (the Serena canary should be run this way
so its A/B gets a real before-window) — the single-command split is the
primary capture:

```bash
MAESTRO_ENV=production maestro burn-baseline --compare <flip-date>
```

`--compare` partitions `usage.jsonl` at the cutoff — **before** = the pre-flag
rows, **after** = rows at or after the cutoff (a row stamped exactly at the
cutoff joins the *after* window) — and renders a `| Signal | Before | After | Δ |`
table over the same signals the baseline reports: `$/task` on the
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

## Decision criteria

- **Promote** (`context7_enabled = true` in the `maestro.toml` default) when
  post-Context7 `$/task` on implementation-heavy shapes (`construction`,
  `module_extension`, `remediation`) drops measurably **and** no quality
  regression appears (outcome mix steady, `replans` per task not up).
- **Hold** (canary stays on, default unchanged) when the windows show no
  measurable difference.
- **Revert** (flag back to `false` in the production overlay) when `$/task`
  rises or `replans` per task increases.

The promote/hold/revert call is human-operated; nothing here automates it.

## Results

| Window | Aggregate $/task | Cache hit rate | Notes |
|--------|-----------------:|---------------:|-------|
| Baseline (pre-flag majority, captured 2026-06-23) | $— | 99.9% | from [burn-rate-baseline.md](burn-rate-baseline.md) |
| With Context7 (`--since 2026-06-21`) | $— | 99.9% | 76 metered tasks, 2026-06-24 → 2026-07-11 |

The with-Context7 capture below ran the current code's
`generate_burn_baseline` over the live `usage.jsonl` + `task_history.jsonl`
with `since=2026-06-21` (the full-fidelity route under the deployed-vintage
caveat above; the window effectively begins at the log's first surviving row,
2026-06-24T16:21Z).

## With-Context7 window (captured 2026-07-11)

| Task shape | Tasks | Total $ | $/task |
|------------|------:|--------:|-------:|
| remediation | 28 | $— | $— |
| other | 11 | $— | $— |
| external_request | 7 | $— | $— |
| measurement | 15 | $— | $— |
| github_issue | 5 | $— | $— |
| construction | 5 | $— | $— |
| investigation | 5 | $— | $— |
| **all** | 76 | $— | $— |

Prompt-cache hit rate: 99.9%

Implementation churn (window): auto_fix calls = 6, self_review calls = 28

Task quality (window): 76 tasks — success 32, partial 43, failure 1; replans
total 0 (avg 0.00/task)

> **Read this capture with two caveats.** (1) **The committed baseline carries
> no task-quality or churn figures**, and its source corpus is gone — so those
> cross-checks read directionally against absolute levels (replans 0.00/task,
> 1 failure in 76 tasks), not as a before/after delta. (2) **A shape bucket
> absent from either window is no-data, not zero** — `module_extension` has no
> metered task in this window, and `external_request` / `measurement` /
> `github_issue` / `investigation` have none in the baseline.

Decision: **hold** (recorded 2026-07-11) — the canary stays on in production;
the base `maestro.toml` default stays `false`.

On its face the promote test passes: aggregate `$/task` fell $— → $—
(−33%), remediation $— (n=28) against a $— baseline (n=20),
construction $— (n=5) against $— (n=1), cache hit rate steady at 99.9%,
replans 0.00/task, and 1 failure in 76 tasks. But attribution of that drop to
Context7 is structurally weak, however large it reads:

- the two windows are **disjoint corpora** captured weeks apart — the usage
  log was reset after the flip, so there is no shared before-window;
- the **task mix shifted materially** (remediation was 20 of 23 baseline
  tasks but 28 of 76 here, with four shapes appearing that the baseline never
  metered);
- the baseline carries **no quality figures** to regress against; and
- the intervening weeks merged **many loop changes aimed precisely at
  remediation cost**, all confounded with the flag.

A promote would need attribution the current evidence cannot give: a
deliberate in-corpus flag cycle so `--compare` gets a real before-window, or a
longer stable window with a comparable task mix. Revert has no case — nothing
suggests the flag raises cost. The verdict is confirmed or overridden by the
operator at PR review; a promote or revert override is enacted as its own
follow-up change.
