# Burn-rate baseline (proposal 084, Phase 1)

**Captured:** 2026-06-23 (production instance) · **Baseline note created:** 2026-06-20

This is the burn-rate reference for [proposal 084](../proposals/core/084-code-indexing-retrieval-tooling-burn-rate.md)
— *code-indexing and retrieval tooling to lower burn rate*. Phase 1 commits to a
**measure-first** path: establish today's `$/task` by task-shape and the current
prompt-cache hit rate **before** any retrieval tool lands, so Phases 2–3 (Context7,
the symbol-level retrieval seam) can be A/B'd against a real reference — and so a
caching headroom large enough to moot retrieval tooling is caught first.

> **Why caching is checked first.** The cheapest burn lever is re-using the stable
> context (identity slab, `CLAUDE.md`, repo orientation) that is re-sent every call.
> If the measured prompt-cache hit rate below is low, caching — not retrieval — may be
> the dominant lever, and that conclusion should be drawn before any tool is added.

## How to capture

The baseline reads only meters that **already exist** — no new loop instrumentation
(proposal 084 *Relevant Findings*). Run on the live instance (the dev checkout has no
runtime data):

```bash
maestro burn-baseline
```

`burn-baseline` reads `usage.jsonl` and `task_history.jsonl` from the configured
`memory_dir` and prints the Markdown block below. Internally it:

- joins per-task spend (`cost_tracking.cost_per_task`, keyed on a usage row's
  `task_id`) to each task's goal from `task_history.jsonl`, classifies the goal with
  `audit_checks.classify_task_shape`, and aggregates `$/task` per shape bucket
  (`cost_tracking.cost_per_task_shape`). A tagged row contributes its task-scoped
  `task_cost_usd` — only the spend made between `begin_task_run` and
  `teardown_task_run`, not the other phases of that iteration; legacy rows written
  before that field existed fall back to the whole-iteration `total_cost_usd` and
  are an upper bound. Unmetered rows contribute `0.0`; spend with
  no `task_id` or no matching history goal is excluded as non-task-attributable;
- computes the aggregate prompt-cache hit rate
  (`cost_tracking.aggregate_cache_hit_rate`) as
  `cache_read / (cache_read + input)` summed over usage rows — the same definition as
  `diagnostics.DiagnosticSnapshot.cache_hit_rate`.

Cross-check the per-shape distribution against `maestro cost-report` (the existing
by-date / by-provider view) for the same window.

> **The cross-check is directional, not exact.** The join above only sees tasks
> still present in the *hot* `task_history.jsonl`, and attributes spend by
> `task_id`. Dream-cycle archival (`Memory.archive`) moves older task records to a
> cold sibling that hot read paths never load — and now rotates `usage.jsonl` into
> a cold sibling under the *same* threshold, so a task whose history row has aged
> into cold storage has had its usage rows aged out too: it drops from **both** the
> baseline join **and** `maestro cost-report` together, not from one side only. The
> baseline `all` total stays a **lower bound** on cost-report's total because of
> spend the join cannot attribute — rows with no `task_id` (or no matching
> hot-history goal), and the non-task-phase remainder of tagged rows, which the
> baseline excludes as non-task-attributable while `cost-report` (a by-date /
> by-provider sum that ignores `task_id`) still counts while their usage row is hot. Reconcile by trend, not to the dollar.

To re-stamp this file, paste the command's output over the **Baseline** block below
and set **Captured** to the run's UTC date.

## Baseline

The figures below are the first live capture from the production instance
(`maestro burn-baseline`, `MAESTRO_ENV=production`, over the hot `usage.jsonl` +
`task_history.jsonl`). Only shape buckets with at least one metered task are shown,
per the capture instructions.

## Burn-rate baseline (captured 2026-06-23)

| Task shape | Tasks | Total $ | $/task |
|------------|------:|--------:|-------:|
| remediation | 20 | $— | $— |
| other | 1 | $— | $— |
| construction | 1 | $— | $— |
| module_extension | 1 | $— | $— |
| **all** | 23 | $— | $— |

Prompt-cache hit rate: 99.9%

> **Read this baseline with two caveats.** (1) **`remediation` dominates** — 20 of
> 23 metered tasks and $— of the $— total, so the aggregate `$/task`
> ($—) is essentially the remediation cost; the implementation-heavy shapes
> `construction` and `module_extension` have a single task each, so their `$/task`
> is a point estimate, not a stable mean. (2) **Cache is already near-saturated at
> 99.9%** — there is effectively no caching headroom left to capture, so the
> caching-headroom check below resolves *negative*: caching is not the dominant
> remaining lever, and any burn win has to come from the retrieval side (Phases
> 2–3), not from closing a cache gap.

## Interpreting the baseline (for Phases 2–3)

- **`$/task` by shape** is the A/B reference. A retrieval tool earns promotion only
  when a later capture shows a measured `$/task` drop on the implementation-heavy
  shapes (`construction`, `module_extension`, `remediation`) that outweighs the
  tool's own overhead — proposal 084's *measure-first* gate.
- **Prompt-cache hit rate** is the caching-headroom check. A low rate means re-sent
  stable context dominates burn; closing that gap may beat any retrieval saving and
  should be addressed before Phase 3 lands a symbol-level server.
- Every phase defaults **off** and promotes only on a measured improvement against
  this table; reversion is a single flag flip with no state change.
