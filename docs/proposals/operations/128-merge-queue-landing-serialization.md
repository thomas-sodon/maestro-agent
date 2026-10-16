# 128: Serialize Landings Through a Merge Queue — Test Each PR Against the Merge Tip Once, Retiring the Eager Main-Resync Rebuild Storm

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | `.github/workflows/quality-gates.yml`, `.github/workflows/auto-update.yml`, `docs/ci.md`, `maestro/evolution.py`, `maestro/config.py`, `maestro/config_settings.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, `tests/test_config.py`, `tests/test_evolution.py`, `tests/test_capability_gating.py`, `tests/test_fault_seeding.py`, `tests/test_auto_update_workflow.py`, new `tests/test_merge_queue_workflow.py` |
| **What landed** | Steps 1–5. Steps 1–2 wired the inert workflow-and-doc foundation: the `merge_group` trigger is in `.github/workflows/quality-gates.yml`'s `on:` block (alongside `push`, `pull_request`, `workflow_dispatch`), so the required jobs — `quality`, `browser-tests`, and `package-quality` (the `review-quality`/`sdk-quality` jobs were collapsed into `package-quality` by proposal 125) — run on the merge queue's temporary branch, the existing per-`github.sha` concurrency group already routes a `merge_group` event to a run-to-completion group, `tests/test_merge_queue_workflow.py` pins the trigger contract (positive and seeded-negative controls), and `docs/ci.md` gained a §Merge Queue section (the §Auto-Update section untouched). Steps 3–4 landed the reversible kill-switch: `[loop] tend_main_merge_enabled` (default `true`) is registered in `Config`, `config_settings`, `maestro.toml`, and — following the `account_ui_v2` idiom for a default-on flag whose off-branch is live logic — directly in `CUTOVER_FLAGS` (stage `cutover`, `gate=None`), not `EXPERIMENTAL_FLAGS`. `_tend_single_pr` now gates the eager `origin/main` merge-and-push behind the flag: flag-off skips only that resync while preserving every other tend role — quiet-PR reminders, conflict routing via the `merge_tree_conflicts` probe, quality-gate routing, and delivery (fast-forward, a diverged own-remote head reconciled first — the reconcile merge quality-gate-validated and rewound-on-red before the push, mirroring the flag-on divergence guard) of a local remediation commit already on the branch. Flag-on is byte-identical to the pre-128 tend phase. Step 5 cut production over: `maestro.production.toml` pins `tend_main_merge_enabled = false` (a `True`→`False` overlay override that `enabled_only_in_overlay` leaves out of the prod-only-enabled set, so it needs no `PROD_ONLY_CANARIES` entry) while base `maestro.toml` keeps `true` as the instant, code-free rollback, `docs/ci.md` §Merge Queue gained a "Cutover & observation" subsection carrying the observation window and tripwires, and `tests/test_config_flag_lifecycle.py` pins both the production override and the base default. The operator has enabled GitHub's merge queue on `main` and relaxed strict branch currency, so the bounded burn-in observation window is now running. Step 6 then retired the eager PR-head resync sweep now that the window has closed cleanly: `.github/workflows/auto-update.yml` and `tests/test_auto_update_workflow.py` were deleted, the `docs/ci.md` §Auto-Update section was folded into a slimmer §Action Pins (preserving the runtime-floor and Dependabot content), and `tests/test_merge_queue_workflow.py` gained a golden guard (`_eager_resync_report`) that reds if any workflow reintroduces the `autoupdate-action` sweep. |
| **What is missing** | Step 7. The flag and the tend `main`-merge code path have not been collapsed (step 7). |
| **Depends on** | 110, 125 |
| **Derived from** | live production data via /propose, 2026-09-04 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Every open PR re-runs the full required CI suite each time `main` advances,
because branch protection requires a PR head to be current with `main` *and* to
carry a passing check on that current head before it can land. When a merge
lands, every other open PR goes `BEHIND`, and each one must merge `main` forward
and re-run the required checks to regain up-to-date status — so N open PRs across
M `main` advances cost up to N×M required-check runs, nearly all of them
superseded before their PR is ready to merge. This is the "strict-protection CI
convoy" that proposal 110 named in its `## Out of scope` and deferred as an
operational companion change, and the storm proposal 125 measures as a sustained
CI rate 2.6× its own 29-day mean — a rate that, on Blacksmith's per-minute
billing, is now a direct dollar cost rather than a queue that merely waited.

Three independent mechanisms each re-derive and re-act on the same fact — "this
PR must be current with `main`": the `auto-update.yml` GitHub Action sweeps a
`main`-merge into every open PR head, `EvolutionEngine.tend_evolution_prs()`
merges `origin/main` into behind branches and pushes, and every remediation lap
pushes a fix that re-runs CI. There is no single owner of "test this PR against
the actual post-merge state exactly once, then merge." A GitHub **merge queue**
is that missing chokepoint: a PR enters the queue only after it is approved and
green, the queue builds a temporary branch stacking `main` plus the queued PRs,
runs the required suite once against that merged tip, and merges on green.
Branch currency stops being a per-PR eager precondition, so a `BEHIND` PR is
never re-tested on a `main` advance — it is tested once, at the front of the
queue, against the state it will actually land into.

The end state: `main` is protected by "Require merge queue" instead of "require
branches up to date," `quality-gates.yml` fires its required jobs on the
`merge_group` event, the `auto-update.yml` sweep and the loop's tend
`main`-merge-and-push are retired, and one deterministic golden test pins the
merge-queue trigger contract so the storm-shaped CI deformations cannot silently
re-accrete. Human-in-the-loop is unchanged: enqueuing a PR requires the same
merge permission the Bash action-gate already denies the loop, so only Thomas
places an approved PR into the queue — the queue serializes *his* merges and
runs CI once each, it does not grant the loop a new way to land code.

## Relevant Findings

- `.github/workflows/quality-gates.yml` (`on:` / `concurrency`) — the required
  suite fires on `push:[main]` and `pull_request:[main]`; the concurrency key is
  `github.ref` for PRs with `cancel-in-progress: true` and per-`github.sha` for
  `main`. Both the per-SHA split (issue #1249, governing `main`-push runs) and
  the PR `cancel-in-progress` scoping (`075011c6`, governing PR-push runs) are
  machinery to manage *runs piling up on shared capacity* — and the PR-side
  pile-up is exactly the per-`main`-advance required-check re-queue this proposal
  removes. There is no `merge_group` trigger today.
- `.github/workflows/auto-update.yml` (`chinthakagodawita/autoupdate-action`) —
  merges `main` into every open non-draft PR head on `push:[main]`. Its docstring
  states its purpose plainly: keep PRs current "so a 'require branches to be up
  to date before merging' protection rule does not force a manual **Update
  branch** click." It pushes with `GITHUB_TOKEN`, which by design does not
  re-trigger CI — so the sweep keeps the *branch* current but leaves the required
  check **stale on the new head**, and the PR still needs a fresh CI-triggering
  push before it can land. The storm is deferred to that next push, not removed.
- `maestro/evolution.py` (`tend_evolution_prs`, `_tend_single_pr`) — the tend
  phase, top-priority and per-iteration (`tend_prs_every = 1`), merges
  `origin/main` into `BEHIND` PR branches and pushes with the loop's real token.
  Its own CI-wait gate comment states "pushing is the only thing that re-runs CI
  on the stale-red head" — i.e. the tend `main`-merge push is, by construction, a
  CI-retriggering event, and its `deferred_for_ci` counter exists precisely
  because a push invalidates any in-flight run on the prior head.
- `docs/proposals/core/110-gate-convergence-to-green.md` (`## Out of scope`) —
  "The strict-protection CI convoy … whose fix is operational — enabling a GitHub
  **merge queue** on `main` (or relaxing the strict up-to-date requirement) — not
  a change to Maestro's code … the recommended companion change: convergence-to-
  green stops producing half-done heads; the merge queue stops the green ones
  from re-queuing." This proposal is that companion.
- `docs/proposals/operations/125-ci-off-host-blacksmith-and-deploy-architecture.md`
  (`## Out of scope`, whack-a-mole ledger entries 6–8) — 125 moves the runners
  off-host and retires the *single-runner scarcity* deformations, but explicitly
  keeps "the per-SHA concurrency design (ledger 6)" out of scope and never
  proposes a merge queue. It changes *where* CI runs, not *how often*; on its own
  Blacksmith economics the redundant re-runs become billable.
- `docs/ci.md` (§Self-Hosted Runner, §Auto-Update) — documents the single-runner
  serialization and the `GITHUB_TOKEN` non-recursion safety argument as
  load-bearing fact, but is already drifting: `quality-gates.yml` now targets
  `blacksmith-4vcpu-ubuntu-2404` while the doc still asserts a single self-hosted
  runner. No section documents a merge-queue trigger contract.
- `maestro/tool_gating.py` (`evaluate_bash_command`) and CLAUDE.md
  (Human-in-the-loop evolution invariant) — `gh pr merge` / ready / open are
  action-gated against the loop; `gh pr close` is the sole permitted disposal.
  Enqueuing a PR uses the merge permission, so the loop cannot self-enqueue; the
  merge queue composes with the existing gate rather than piercing it.

## Production Evidence

Live data resolved from the loop container bind-mount at
`/opt/maestro/data` (host lookup succeeded; no `docker exec` fallback
needed). All reads strictly read-only.

**The O(N×M) re-sync pattern, in the raw** — `journal.jsonl`, window
2026-08-16 → 2026-09-04 (5,431 entries):

```
distinct PRs flagged BEHIND main: 25
  top: #3544 ×42, #3525 ×33, #3528 ×32, #3550 ×26, #3524 ×22, #3549 ×21
'delivered/pushed to PR' entries (each a CI-triggering push): 953
'tend merged origin/main into PR' entries: 13
```

A handful of PRs each go `BEHIND` main *dozens* of times as `main` advances —
that repetition is the convoy: each `BEHIND` observation, under strict
protection, is a required-check re-run owed before the PR can land. The pool of
concurrently-open PRs is small (six open at investigation time: #3520, #3652,
#3699, #3701, #3702, #3703), so the storm is dominated by the *M* (main
advances), not the *N*.

**The storm now has a price** — from proposal 125's Production Evidence
(`docs/proposals/operations/125-…md`), workflow mix over 200 runs in the
10.90 h window 2026-09-02 00:02 → 2026-09-03 02:39 UTC: Quality Gates **71**,
AI Code Review 53, Reply Coverage 53, Deploy 18, Auto-Update 5. 125 models
≈ 13,808 CI min/month at the 29-day mean but ≈ **25,218 min/month (2.6×)** at the
sustained 2026-09-02 rate. On Blacksmith's $0.004/min that spread is the
difference between ≈ $— and ≈ $—+/month — the redundant re-runs, formerly a
free queue wait on the shared runner, are now billed.

**Remediation monoculture feeds the push volume** — `task_history.jsonl` hot
tail, window 2026-08-16 → 2026-09-01 (33 rows): **19 of 33** recent tasks are
`Remediate PR #N` laps; PR #3525 took 3 laps, #3524/#3528/#3544/#3586/#3610 two
each. Each lap pushes a fix that re-runs CI. (These lap pushes are *legitimate*
CI triggers and are out of scope here — see `## Out of scope` — but they
establish that the open-PR pool is churned constantly, so every `main` advance
lands on a set of PRs all owing a re-run.)

## Prior Point Fixes (whack-a-mole ledger)

Each of these patched a *symptom* of "too many required-check runs pile up on the
same capacity," never the eager-resync trigger that produces them.

| # | Identifier | What it patched | Why the class survived |
|---|---|---|---|
| 1 | `99504e26` fix(ci): per-SHA concurrency group on main (issue #1249) | Per-SHA concurrency so a *pending* main run isn't cancelled when a third commit queues behind an in-progress one. | Reachable only because runs queue at all; does not reduce the number of PR re-runs. |
| 2 | `075011c6` fix(ci): scope quality-gates cancel-in-progress to PRs only | Cancel only superseded PR runs, let main runs complete. | Manages the pile-up; a `BEHIND` PR still re-runs on every main advance. |
| 3 | `aae50770` fix: gate browser-tests job on quality (#216) | Fan the heavy jobs in behind the fast `quality` gate to avoid burning slots on an already-broken build. | Rations one run; N×M runs still enter the queue. |
| 4 | `7b802c08` Serialize review-quality and sdk-quality behind the fast gate | Same fan-in for the review/sdk jobs. | Same — shapes each run, not the count. |
| 5 | `4575a73d` / `f2d52731` xdist on then off for CI | Removed test parallelism after OOM-kills under co-tenancy. | Trades speed for capacity; irrelevant once run *count* is the cost. |
| 6 | `32c9d097` feat: add `auto-update.yml` | Keep PRs current so the strict up-to-date rule doesn't force manual **Update branch** clicks. | Adds a *third* eager-resync owner; CI-silent, so it defers the re-run to the next real-token push instead of removing it. |
| 7 | `9427f0fc` Migrate workflows to Blacksmith (proposal 125) | Move runners off the contended host. | Changes *where* CI runs, not *how often*; makes each redundant run billable. |

Entry 6 (`auto-update.yml`) is retired by this design (step 6). Entries 1 and 2
are *relieved but retained*: `merge_group` merges still push to `main` and PRs
still run their required checks on push, so both concurrency keys stay load-
bearing — the merge queue only removes the per-`main`-advance re-runs that made
the pile-up they guard so large. Entries 3, 4, 5, 7 are 125's substrate concern
and stay owned there; the merge queue lowers the run count feeding them but does
not delete them.

## Implementation Plan

Ordered so an interrupted run leaves CI working. The workflow-trigger and doc
steps are inert until the operator enables the queue; the tend behaviour change
follows the four-stage flag lifecycle; the retirement steps come last, after the
replacement is proven.

1. [x] **Add the `merge_group` trigger to the required suite.**
   `.github/workflows/quality-gates.yml`, new `tests/test_merge_queue_workflow.py`
   — add `merge_group:` to `on:` so the four required jobs (`quality`,
   `browser-tests`, `review-quality`, `sdk-quality`) run on the queue's temporary
   branches, and give `merge_group` its own per-`github.ref` concurrency entry
   (each queue entry runs to completion, like `push:[main]`). Inert until the
   queue is enabled — `merge_group` simply never fires before then.
   - *Depends on:* none.
   - *Acceptance:* `tests/test_merge_queue_workflow.py` parses the YAML and
     asserts `merge_group` is an `on:` trigger and every job that runs on
     `pull_request` also runs on `merge_group`.

2. [x] **Document the merge-queue trigger contract and operator runbook.**
   `docs/ci.md` — add a §Merge Queue section naming `merge_group` as the sole
   required-check trigger for a landing, with the one-time operator steps (enable
   "Require merge queue" on `main`, set the required checks, relax "require
   branches up to date"), and correct the stale single-self-hosted-runner and
   `blacksmith` drift while here. No behaviour change.
   - *Depends on:* 1.
   - *Acceptance:* `docs/ci.md` contains a §Merge Queue section that names the
     `merge_group` trigger and states branch-currency is no longer a per-PR
     landing precondition; no §Auto-Update text is deleted yet.

3. [x] **Dark-launch `[loop] tend_main_merge_enabled` (default `true`).**
   `maestro/config.py`, `maestro/config_settings.py`, `maestro.toml`,
   `maestro/flag_lifecycle.py` — register a default-`true` switch (current
   behaviour) guarding the tend phase's `origin/main` merge-and-push. Because it
   ships default-on with a live off-branch (the `account_ui_v2` idiom), it is a
   `CUTOVER_FLAGS` entry at stage `cutover` (`gate=None`, dated `review_by`), not
   an `EXPERIMENTAL_FLAGS` experiment. No behaviour change at default.
   - *Depends on:* none.
   - *Acceptance:* `Config.tend_main_merge_enabled` defaults `true`, is listed in
     `CUTOVER_FLAGS` (stage `cutover`), stays off `EXPERIMENTAL_FLAGS`, and a
     config test pins the default and manifest entry.

4. [x] **Gate the tend `main`-merge-and-push behind the flag.**
   `maestro/evolution.py` (`_tend_single_pr`) — when
   `tend_main_merge_enabled` is `false`, skip the `merge_from("origin/main")` +
   push entirely; keep the non-CI-triggering tend roles intact (quiet-PR
   reminders, `conflicts_routed` / `quality_gates_routed` routing, recorded
   review-thread replies). Flag-on path is unchanged.
   - *Depends on:* 3.
   - *Acceptance:* a `tests/test_evolution` case with the flag `false` asserts
     `tend_evolution_prs()` issues no `merge_from("origin/main")` and no push for
     a `BEHIND` PR, while still emitting its reminder/conflict routing.

5. [x] **Flip on everywhere and prove.** `maestro.toml` (+ overlay parity),
   operator action — enable the merge queue on `main`, relax "require branches up
   to date," and flip `tend_main_merge_enabled` to `false` in one reversible
   step (the flag remains the kill switch). Observe over a bounded window against
   the tripwires in Risk Assessment.
   - *Depends on:* 1, 2, 4.
   - *Acceptance:* with the queue live and the flag `false`, a landed PR shows one
     `merge_group` required-check run and no per-`main`-advance re-run in its
     check history; tend logs no `tend merged origin/main into PR` journal entry
     across a full day. That flag-gated merge is the signal, not
     `deferred_for_ci`: the counter is incremented at the CI-wait gate upstream of
     the `merge_from("origin/main")` and stays entangled with the
     `quality_gates_routed` / `conflicts_routed` routing step 4 keeps, so it can
     read non-zero under the flag-off path and cannot isolate the flag's effect.

6. [x] **Retire `auto-update.yml` and its pin test.**
   delete `.github/workflows/auto-update.yml` and
   `tests/test_auto_update_workflow.py`; extend `tests/test_merge_queue_workflow.py`
   to assert no workflow carries the eager-resync sweep (no
   `autoupdate-action`, no `push:[main]`-triggered PR-head mutation). Only after
   step 5 proves the queue keeps branches landable without the sweep.
   - *Depends on:* 5.
   - *Acceptance:* `auto-update.yml` is absent and the golden test fails if any
     workflow reintroduces an eager PR-head resync sweep.

7. [ ] **Collapse the flag and delete the tend `main`-merge off-branch.**
   `maestro/flag_lifecycle.py` (drop the `CUTOVER_FLAGS` entry + the matching
   growth-budget decrement), `maestro/evolution.py`, `maestro/config.py`,
   `maestro.toml` — after the observation window, delete `tend_main_merge_enabled`
   and the now-dead `main`-merge-and-push path in `_tend_single_pr`, leaving the
   golden guard as the single enforcement point.
   - *Depends on:* 6.
   - *Acceptance:* the flag and the tend `main`-merge code path are gone; the full
     gate suite is green; `tests/test_merge_queue_workflow.py` remains the sole
     owner of the trigger contract.

## Testing Strategy

- **`tests/test_merge_queue_workflow.py` (new, deterministic golden guard).**
  Parses `.github/workflows/quality-gates.yml` in-memory (no subprocess, no
  network): asserts `merge_group` is an `on:` trigger; asserts every job on
  `pull_request` is also on `merge_group`; asserts the `merge_group` concurrency
  entry runs each queue entry to completion (no cross-entry `cancel-in-progress`
  on the queue ref). After step 6, asserts no workflow file contains an
  eager-resync sweep. A seeded negative control (a fixture YAML missing the
  `merge_group` trigger) proves the assertion is not vacuous.
- **`tests/test_evolution` (tend flag-off branch).** With `mem_fs`, a mocked
  `CodeHost`/VCS, and `tend_main_merge_enabled=false`, assert `tend_evolution_prs()`
  performs no `merge_from("origin/main")` and no push for a `BEHIND` PR, and still
  emits the reminder and `conflicts_routed` entries — covering the guard branch,
  not just the happy path. A companion flag-on case pins the unchanged current
  behaviour.
- **`tests/test_config.py` / `tests/test_cutover_flag_collapse.py`.** Pin
  `tend_main_merge_enabled` default `true`, its `CUTOVER_FLAGS` manifest entry
  (stage `cutover`, `gate=None`) and `review_by`, and (at step 7) the collapse
  that drops the entry.
- **Human-in-the-loop regression.** `tests/test_capability_gating.py` /
  `tests/test_fault_seeding.py` already prove `gh pr merge` is action-gated; add a
  case asserting the merge-queue enqueue verb (the merge button / `gh pr merge
  --auto`) is denied to the loop, so enabling the queue grants no new landing
  path.

All externals mocked; no real network, sleep, or subprocess, per CLAUDE.md
testing principles.

## Risk Assessment

- **Landing latency.** A merge queue serializes merges; a stacked queue lands
  slower than an immediate click. Mitigation: GitHub's queue tests entries
  speculatively in parallel, and Maestro's measured landing rate is a few PRs per
  week — serialization cost is negligible against the re-run cost it removes.
  Tripwire: median time-from-approve-to-land rising materially week-over-week
  flips `tend_main_merge_enabled` back to `true` and the operator disables the
  queue (both reversible; the flag is the kill switch).
- **Human-in-the-loop trust chain.** The queue must not become a self-merge path.
  Enqueuing uses the merge permission the Bash action-gate denies the loop
  (`maestro/tool_gating.py`), and branch protection still requires human review
  approval before a PR is queue-eligible — so only Thomas enqueues, and the queue
  merges only what he approved. Proven by the enqueue-denied case added to
  `tests/test_capability_gating.py`.
- **A required check missing on `merge_group`.** If a required check is not wired
  to `merge_group`, the queue stalls (fails safe — nothing merges). Mitigation:
  step 1's golden test pins that every `pull_request` required job also runs on
  `merge_group`; step 5's acceptance confirms a real landing shows the
  `merge_group` run before retiring anything.
- **Rollback posture.** Steps 1–4 are inert or default-preserving and land
  independently. Step 5 is one reversible flip plus an operator toggle. The
  retirements (6, 7) come only after step 5 is proven; reverting them restores
  `auto-update.yml` and the tend `main`-merge from git history. No state-file or
  schema migration is involved.
- **Advisory workflows unchanged.** `code-review.yml` and `reply-coverage.yml`
  fire on `pull_request`, are not required checks, and are untouched — they
  continue to run on PR pushes but never on `merge_group`, so the queue does not
  re-run them.

## Out of scope

- **Per-lap remediation CI runs.** Every `Remediate PR #N` lap pushes a fix that
  *should* re-run CI to validate it (953 delivery pushes in the window). That is a
  legitimate trigger, not a redundant one; the lap *count* is owned by the
  PR-remediation lap budget and stand-down ledger (080, 089, 094, 099) and by
  convergence-to-green stopping half-done heads (110). This proposal closes only
  the `main`-advance-driven re-runs.
- **The CI substrate and the single-runner scarcity deformations** (125). Where
  CI runs, the runner fleet, the `needs:[quality]` fan-in, xdist, and per-SHA
  concurrency-on-`main` (ledger 3, 4, 5, 7 above) stay owned by 125. The merge
  queue lowers the run count feeding them but does not delete them.
- **The `auto-update.yml` `:v1` → digest pin.** A supply-chain hardening 125
  tracks; moot here once the workflow is retired (step 6), so not separately
  addressed.
- **Semantic conflicts that pass the gates but are wrong.** The required suite is
  the trusted-but-incomplete oracle; the queue tests the merged tip against it but
  cannot catch textually-clean semantic breakage — that is review's job, as 110
  also notes.

## Falsifiability

If the design is right, the required-check run count per open-PR-week collapses
toward "≈ one `merge_group` run per landing plus one per development push," the
sustained-rate spike proposal 125 measured (2.6× its mean) flattens toward the
mint rate, and the tend `main`-merge stops firing — the `tend merged origin/main
into PR` journal count (13 in the measured window) goes to 0 (tend no longer
merges `main`; `deferred_for_ci` is not the signal here, since the retained
CI-wait gate can still increment it upstream of the flag-gated merge). The design
is refuted if Blacksmith billed minutes do not fall
after step 5, or if median approve-to-land latency rises materially with no
reduction in redundant runs — evidence that the strict-protection convoy was not
the dominant CI-cost driver, or that queue serialization costs more than the
re-runs it removes.
