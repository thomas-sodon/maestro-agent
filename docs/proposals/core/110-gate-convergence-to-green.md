# 110: Gate Convergence to Green — One Owner That Grinds the Quality Gates to Green or Fails Loud, Retiring the Round-Capped Proceed-Without-LGTM Escape Hatches

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/evolution.py`, `maestro/agent.py`, new `maestro/gate_convergence.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, tests |
| **What landed** | All six steps. Step 6's default-on flip, flag retirement, positive gate-acceptance chokepoint guard, and the `self_review_max_rounds` ceiling fold-in (Steps 1–3 in PR #3349; Step 4 in PR #3358; Step 5's long-context caps in PR #3362; Step 5's warning-throttle bypass in PR #3369; Step 6's default-on flip in PR #3374; Step 6's flag retirement in PR #3380 — the `[loop] gate_convergence_enabled` rollout flag and both flag-off escape hatches deleted, `converge_to_green` and the loud self-review terminal made unconditional, `auto_fix_max_rounds` folded into the convergence ceiling via `ceiling=max(gate_convergence_ceiling, auto_fix_max_rounds)`, and the negative retired-flag structural guard added to `tests/structural_guards.py`; Step 6's positive sole-owner chokepoint guard in PR #3387 — the `gate_acceptance` `ChokepointGuard` in `tests/structural_guards.py` asserting no code-writing phase reaches gate acceptance except through `converge_to_green`; Step 6's final `self_review_max_rounds` fold-in in PR #3393 — the new pure, zero-I/O `plan_next_review_round` sibling stuck-detector in `maestro/gate_convergence.py`, `self_review` (`maestro/evolution.py`) rerouted through it as a progress-gated review-fix loop, and `self_review_max_rounds` folded into the convergence-ceiling floor via `ceiling=max(gate_convergence_ceiling, self_review_max_rounds)` so it is no longer a hard terminal, exactly as `auto_fix_max_rounds` is for the gate loop). Step 1 — new `maestro/gate_convergence.py`: the pure, zero-I/O `plan_next_round(prev_failures, curr_failures, *, rounds_used, ceiling)` returning `"green"` / `"continue"` / `"stuck"`, plus the frozen `ConvergenceResult`, unit-covered in `tests/test_gate_convergence.py`. Step 2 — `QualityGateResult.failure_signature()` (`maestro/evolution.py`) returning the frozenset of failing-gate identifiers (`ruff` / `mypy` / `tests`). Step 3 — the `EvolutionEngine.converge_to_green` orchestrator (`maestro/evolution.py`) driving `run_quality_gates` + `auto_fix` through `plan_next_round`, and the `implement_plan` routing (`maestro/agent.py`) that replaces the `round_num < auto_fix_max_rounds` demote tail with `converge_to_green` under `gate_convergence_enabled` (flag-off ⇒ byte-identical legacy capped loop), failing loud (`ERROR`) and demoting on `"stuck"`; the `[loop] gate_convergence_enabled` (now default-on) and `gate_convergence_ceiling` (default 20) config in `maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`. Step 4 — `self_review` (`maestro/evolution.py`) now returns `bool` (`True` on `LGTM` or a benign VCS skip — unobtainable HEAD, empty `base_hash`, or empty diff; `False` on rounds-exhausted-without-`LGTM`); under `gate_convergence_enabled` the `implement_plan` caller (`maestro/agent.py`) routes a `False` return to the loud fail-loud terminal (`ERROR` + `cleanup_branch` + demote to `failed`) so no unapproved head reaches the gate loop or `create_pr`, while flag-off preserves the legacy `"Self-review exhausted … proceeding"` WARNING (module constant `_SELF_REVIEW_EXHAUSTED_MSG`) and a byte-identical proceed path. Step 5 (caps half) — the `converge_to_green` fix step now widens its committed-diff, cumulative-diff, and raw gate-output slices to long-context caps via a shared `_long_context_active` seam owner read by both `self_review` and `converge_to_green`, threaded `converge_to_green` → `auto_fix` → `_run_auto_fix_query` behind a `long_context` flag that defaults off, so every flag-off and non-convergence call stays byte-for-byte unchanged. Step 5 (throttle half) — the `converge_to_green` fix step now also skips the soft per-call SDK warning-throttle sleep on every round: a `bypass_warning_throttle` opt-out (the existing one-shot `QueryOptions.bypass_warning_throttle`) is plumbed through the session-query path (`_query_on_session_impl` / `query_on_session` and the `Provider.query_on_session` chain — Anthropic honours it, `cli_paced`/`openai` accept-and-ignore) and threaded `converge_to_green` → `auto_fix` → `_run_auto_fix_query`, defaulting off so non-convergence work stays byte-identical; the hard rate-limit protection and the throttle's own decay counter (`_update_warning_throttle_state`, still run post-call) are untouched. |
| **What is missing** | Nothing — the proposal is fully shipped. The final Step-6 slice (folding `self_review_max_rounds` into the convergence ceiling so it no longer hard-terminates `self_review`) landed in PR #3393, completing every step. The proposal's original "burn-rate throttle" framing was a no-op at the fix-step granularity — the burn-rate throttle is loop-level with no per-call reach — so the soft per-call warning throttle was the lever that actually paced the grind, and it is now bypassed for the convergence op. |
| **Depends on** | 089 _(shipped)_, 102 _(shipped)_ |
| **Derived from** | code + git history via `/propose`, 2026-07-31 (no live production data available) |

## Description

> Status updated to shipped on 2026-08-08 by /plan-proposal — see [README.md](../README.md#shipped) for the landing record.

A human running the `/merge-main` skill does not babysit it: the harness grinds
`ruff` + `mypy` + `pytest` until they are actually green, then stops, because a
Stop hook forbids finishing red. The result lands unattended and reliably.
Maestro, driving the same model against the same gate suite, does not get the
same guarantee — not because it cannot resolve the failures, but because every
code-writing phase caps how long it will grind and then hands the still-red
result onward. `self_review` runs `self_review_max_rounds` and, on exhaustion,
logs "exhausted N rounds without LGTM, proceeding" and proceeds. The
post-implementation loop runs `auto_fix_max_rounds` and, on exhaustion, demotes
the plan to `failed`. Both loops run inside the budget-throttled implementation
session under the standard context-bloat caps. The oracle Maestro needs already
exists and is already trusted — `run_quality_gates()` at 100% coverage — and the
tend path already binds to it correctly ("never pushing a red head", it rewinds
on a red reconcile). The gap is purely **persistence**: nothing owns "grind this
branch to green," so each phase gives up at a fixed round count and the half-done
residue becomes downstream tend work.

This proposal introduces one owner of gate convergence. A single
`converge_to_green` chokepoint drives the fix step in a loop bounded not by a
fixed round count but by a **progress predicate** — keep going while each round
strictly shrinks the failing-gate set — and terminates on exactly two honest
outcomes: **green** (proceed) or **no-progress stuck** (fail loud, escalate,
never proceed). It runs the fix step exempt from the soft per-call warning
throttle and the context-bloat caps that currently starve it, because a merge
that is not green is not done. Every code-writing phase routes its "make the gates pass" step through
this owner; the scattered round caps become inputs to the stuck detector rather
than hard terminals, and the "proceeding without LGTM" smoothing layer is
deleted.

The end state: a plan either reaches a genuinely green head or fails loudly with
its diagnosis — no phase ever passes an unproven, still-red result to the next
stage, and the fail-loud invariant is strengthened, not eroded.

## Relevant Findings

- `maestro/evolution.py` (`self_review`) — after the bounded review loop it logs
  `"Self-review exhausted %d rounds without LGTM, proceeding"` at WARNING and
  returns, letting an un-approved, possibly-red head continue toward PR creation.
  This is a smoothing layer: it papers a not-green state into "proceed" rather
  than grinding to green or failing loud, in tension with the fail-loud invariant
  (`tests/test_no_silenced_exceptions.py` enforces the exception form of that
  invariant; this narrative escape hatch is uncovered).
- `maestro/agent.py` (`implement_plan`) — the post-implementation gate
  loop is `while not gate_review.passed and round_num < max_rounds` with
  `max_rounds = auto_fix_max_rounds`; on exhaustion it calls `cleanup_branch` and
  demotes the plan to `failed`. Its only progress signal is `auto_fix` returning
  "no changes" (a *change-made* test), not whether the failing-gate set actually
  shrank, so a fix that changes files without reducing failures still consumes
  rounds until the cap.
- `maestro/config.py` (`self_review_max_rounds` = 2, `auto_fix_max_rounds` = 3,
  `max_implementation_attempts` = 3, `pr_remediation_max_attempts_per_head` = 3)
  — four independent fixed caps govern "how long to grind," each owned by a
  different phase, with no single owner of the convergence decision.
- `maestro/evolution.py` (`_try_mechanical_cap_reconcile`) — the tend
  path already treats the gate suite as the trusted landing oracle: it runs
  `run_quality_gates()` on the reconciled merge and, on failure, rewinds to
  `pre_merge_head` and routes remediation *without pushing a red head*. The
  binding-to-green pattern this proposal generalizes is already present here.
- `maestro/remediation_tracker.py` (`measure_no_progress_laps`,
  `measure_unlanded_remediations`) — existing sensors that count downstream
  residue; the progress-predicate pattern from proposal 089
  (`pr_readiness_improved`) is the same shape this owner applies one layer
  earlier, at the gate loop instead of the PR lap.
- `maestro/loop/pr_remediation.py` / proposal 102 (`ci_diagnosis`) — the
  CI-failure diagnosis oracle answers *why* a check is red; it feeds the fix step
  this owner drives, so the two compose (diagnosis in, convergence out).

## Production Evidence

No live production data was available at authoring time — the `maestro-loop`
container was not running and no `data/` bind-mount exposed `lifecycle.json`, so
this proposal is grounded in code and git history only. The frequency of the
class (how many plans per week exit via `proceed-without-LGTM` or
`demote-to-failed-on-gate` versus reaching green) is therefore **unquantified**;
the first implementation step below adds the counter that would quantify it, and
the falsifiability metric in Risk Assessment is stated against that counter.

## Prior Point Fixes (whack-a-mole ledger)

Each entry patched one symptom of "the gate/merge convergence step stops half-done
or misbehaves at its edges," without giving convergence a single owner:

| Fix | What it patched | Why the class survived |
|-----|-----------------|------------------------|
| `62fa5f0b` | Mechanical reconcile-and-land actuator for brevity-cap conflicts | Handles one conflict shape only; every other still-red merge falls back to the capped loop |
| `af5242b9` | Fix reconcile: union resolution + fire on real merges | Second correction to the same narrow actuator |
| `1c988c3a` | Gate the mechanical cap-reconcile before pushing | Third correction — re-adds the "run gates before push" binding the general path lacks |
| `479e5522` | "Dissolve the tend-path conflict magnet" | Names the area as a recurring magnet; still per-shape, not a convergence owner |
| `bfbe18fe` | `self-review`: fail loud on empty `base_hash` | Fixes one edge of the self-review terminal; the "proceed without LGTM" terminal remains |
| `2ceaf5d7` | `self-review`: split no-HEAD warning from empty-diff no-op | Another self-review terminal edge |
| `05e4b51e` | Pin `cwd` to workspace in `auto_fix`, `implement`, `self_review` | Environment bug shared by all three code-writing queries — evidence they are ad-hoc siblings, not one owner |

Adjacent structural proposals attack the *PR-remediation* layer of the same
instinct (give up too early) but none touch the *implement-phase* gate loop:
**089** (progress-aware lap accounting), **094** (salvage before disengage),
**099** (base-advance re-entry), **102** (CI-failure diagnosis). This proposal
scopes itself to the uncovered gap — the initial gate-convergence loop — and
reuses 089's progress predicate and 102's diagnosis rather than duplicating them.

## Implementation Plan

1. [x] **Pure convergence predicate.** new `maestro/gate_convergence.py` — a
   pure, zero-I/O, zero-SDK `plan_next_round(prev_failures, curr_failures, *,
   rounds_used, ceiling)` returning `Green` (empty failure set) / `Continue`
   (set strictly shrank) / `Stuck` (set did not shrink, or `ceiling` reached).
   The ceiling is a generous fail-loud backstop, never a proceed trigger.
   - *Depends on:* none.
   - *Acceptance:* unit tests prove all four transitions — empty→`Green`,
     strict-shrink→`Continue`, no-shrink→`Stuck`, ceiling→`Stuck`.
2. [x] **Structured failing-gate signature.** `maestro/evolution.py`
   (`run_quality_gates` / its `QualityGateResult`) — add a stable
   `failure_signature()` returning the set of failing-gate identifiers so
   successive rounds are comparable.
   - *Depends on:* none.
   - *Acceptance:* a review with the same failing gates twice yields an equal
     signature; resolving one gate yields a strict subset.
3. [x] **Route the implement-phase loop through the owner, behind a flag.**
   `maestro/agent.py` (`implement_plan`) — replace the
   `round_num < max_rounds` + demote tail with a `converge_to_green` call driving
   `auto_fix`, gated by `[loop] gate_convergence_enabled` (flag-off ⇒
   byte-identical legacy behavior). On `Stuck`: loud `ERROR` + the existing
   terminal-issue escalation; never proceed with a red head. Adds the
   convergence-exit counter named in Production Evidence.
   - *Depends on:* 1, 2.
   - *Acceptance:* with the flag on, a plan whose failure set shrinks each round
     reaches a green head; a no-progress plan fails loud and opens the terminal
     issue instead of silently demoting.
4. [x] **Delete "proceed without LGTM."** `maestro/evolution.py` (`self_review`)
   — under the flag, an exhausted-but-not-green review terminates through the
   owner's loud-stuck path; the `"exhausted … without LGTM, proceeding"` return
   is removed.
   - *Depends on:* 3.
   - *Acceptance:* a golden test asserts the "proceeding" log string is
     unreachable when the flag is on.
5. [x] **Focused-operation exemptions.** `maestro/evolution.py` /
   `maestro/sdk.py` / `maestro/providers/` — issue the convergence fix step
   exempt from the soft per-call warning throttle (a `bypass_warning_throttle`
   opt-out plumbed through the session-query path) and with the long-context
   diff/file caps raised (reusing the existing `long_context` seam), because the
   convergence op needs full context and must not be paced by soft backpressure.
   The proposal's original "burn-rate throttle" framing is a no-op here — that
   throttle is loop-level with no per-call reach; the warning throttle is the
   lever that actually paced the grind.
   - *Depends on:* 3.
   - *Acceptance:* the convergence fix query is issued with the warning-throttle
     bypass and raised caps, pinned by a test; non-convergence phases are
     unchanged.
6. [x] **Retire the scattered terminals + chokepoint guard.** After 3–5 prove
   parity, flip `[loop] gate_convergence_enabled` default-on, fold
   `self_review_max_rounds` / `auto_fix_max_rounds` into the stuck-detector
   inputs (ceiling), and add `tests/test_gate_convergence_chokepoint.py`
   asserting no code-writing phase reaches gate acceptance except through the
   owner and that no `proceed`-without-green terminal remains.
   - *Depends on:* 3, 4, 5.
   - *Acceptance:* the AST guard passes; the two former caps no longer appear as
     hard loop terminals.

## Testing Strategy

- **Unit** (`tests/test_gate_convergence.py`) — the pure predicate across every
  branch (green, shrink→continue, no-progress→stuck, ceiling→stuck) with no I/O.
- **Component** (`tests/test_run_task_phase.py` / `tests/test_evolution.py`) —
  the routed implement loop with a mocked `VCS` and a scripted
  `run_quality_gates` sequence: happy path converges to green; a no-progress
  sequence fails loud and escalates (no demote-proceed); flag-off yields
  byte-identical current behavior (parity guard).
- **Golden** (`tests/test_prompts.py` or a targeted log-string test) — the
  "proceeding without LGTM" string is unreachable under the flag.
- **AST chokepoint** (`tests/test_gate_convergence_chokepoint.py`) — the sole-door
  guard, in the house style of `tests/test_merge_truth_chokepoint.py`.
- All tests honour CLAUDE.md rules: `mem_fs`, mocked `VCS`/gates/SDK, no real
  subprocess, network, or `sleep`; the scripted gate sequences are in-memory
  fixtures, not real `pytest`/`ruff` runs.

## Risk Assessment

- **Cost runaway** (the primary risk, and the reason the caps exist). Mitigated
  four ways: the loop is **progress-gated** — a round that fails to shrink the
  failing-gate set terminates immediately, so it cannot spin on a plateau; a
  generous **ceiling** backstops pathological oscillation and its terminal is a
  loud crash, not a proceed; the burn-rate exemption applies **only** to the
  focused convergence op, never the loop at large; and rollout is **default-off**
  behind `[loop] gate_convergence_enabled` with a flag-off parity guard.
- **Fail-loud invariant.** This change *strengthens* it. It deletes the
  "proceed without LGTM" smoothing layer and replaces the silent
  demote-to-failed with a loud stuck terminal plus the existing issue
  escalation. No new broad `except`; the stuck path raises/escalates visibly.
- **Oracle incompleteness.** The gate suite at 100% coverage is trusted but not
  omniscient — a semantically-wrong merge can still pass. Closing that gap is out
  of scope (below); strict-branch-protection CI re-runs and human review remain
  the backstop, unchanged.
- **Rollback.** Single config flag, default matching current behavior; steps are
  independently mergeable, so an interrupted rollout leaves the loop working on
  the legacy capped path. No state-file or schema migration.
- **Falsifiability.** With the flag on, the step-3 convergence-exit counter for
  `proceed-without-LGTM` / `demote-to-failed-on-gate` should fall to ~0
  (replaced by green-or-loud-stuck), and the downstream residue measured by
  `measure_unlanded_remediations` / `measure_no_progress_laps` (089) attributable
  to half-done implementations should drop. The design is refuted if burn cost
  per landed plan rises materially with no reduction in that residue — evidence
  the progress predicate is not converging and the fixed caps were load-bearing.

## Out of scope

- **The strict-protection CI convoy.** When a stack of open PRs each go `BEHIND`
  on every merge and must re-run all required checks to regain up-to-date status,
  landings serialize behind CI. That is a distinct class whose fix is
  operational — enabling a GitHub **merge queue** on `main` (or relaxing the
  strict up-to-date requirement) — not a change to Maestro's code, and it is not
  addressed here. It is the recommended companion change: convergence-to-green
  stops producing half-done heads; the merge queue stops the green ones from
  re-queuing.
- **The PR-remediation / tend lap budget** (080, 089, 094, 099). Those own the
  *PR-level* give-up decision; this proposal deliberately does not re-litigate
  them and touches only the implement-phase gate loop. The convergence owner
  could later back the tend-side fix step too, but that composition is future
  work.
- **Semantic conflicts that pass the gates but are wrong.** The gate suite is the
  trusted-but-incomplete oracle; hardening it against textually-clean semantic
  breakage is a separate concern left to strict-protection re-runs and review.
