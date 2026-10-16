# 095: Origin-Grounded PR Tending — Reconcile Local `main`, Ground Catch-Up in `commits_behind`, Retire the Whole-Phase Refusal

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/evolution.py` (`tend_evolution_prs`, `_tend_single_pr`, `ensure_main_branch`, `_pr_needs_conflict_resolution`), new `maestro/pr_staleness.py`, `maestro/pr_readiness.py` (`observe_pr_readiness` — read-only reuse), `maestro/vcs.py` (`commits_ahead_behind`, `merge_tree_conflicts` — read-only reuse), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `tests/test_tend_origin_grounded.py` (new; since retired by proposal 109 — its checks migrated to the `_ORIGIN_GROUNDED_GUARD` registry entry in `tests/structural_guards.py`), tests |
| **What landed** | Step 1 — the reconcile-and-surface path in `maestro/evolution.py` (`tend_evolution_prs`), gated by `[loop] reconcile_local_main_enabled` (`maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, default-on). When local `main` is ahead of `origin/main`, a reachability check (`local_main_ahead_commits` / `remote_refs_containing`) hard-resets `main` to `origin/main` iff every ahead commit is already on an `origin/*` ref — logging a loud WARNING and an `evolution_note` journal entry naming the stranded SHAs — then tends; an unpushed local-`main` commit is preserved and the phase refuses. Flag off ⇒ the pre-095 whole-phase refusal byte-for-byte. Covered by `tests/test_evolution_tend.py` and the config parse/default tests in `tests/test_config.py` / `tests/component/test_config_integration.py`. Step 2 — the origin-grounded staleness fact `maestro/pr_staleness.py` (`observe_pr_staleness`, frozen `PrStaleness`): a pure observer taking injected `commits_behind_fn` / `merge_clean_fn` and deriving `needs_catchup = commits_behind > 0 and cleanly_mergeable`, zero SDK / zero host calls; covered by `tests/test_pr_staleness.py`. Step 3 — the staleness fact is wired into the catch-up actuator behind `[loop] behind_main_catchup_grounded_enabled` (`maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, default-on): the adapter `EvolutionEngine._observe_pr_staleness` (`maestro/evolution.py`) wraps the pure observer with this engine's real git seams (`commits_ahead_behind("origin/<branch>", "origin/main")` for behind-ness, `not merge_tree_conflicts("origin/main", "origin/<branch>")` for clean), and `tend_evolution_prs` drives its catch-up no-op on `staleness.commits_behind == 0` and its conflict-route on `staleness.commits_behind > 0 and not staleness.cleanly_mergeable` instead of the host `mergeStateStatus` / `looks_clean` proxy, so a `BLOCKED`-and-behind clean PR now reaches the catch-up merge; flag off ⇒ `staleness is None` and both decisions keep their host-proxy computation byte-for-byte. Covered by the grounded cases in `tests/test_evolution_tend.py`. Step 4 — the golden enforcement landed as `tests/test_tend_origin_grounded.py` (since retired by proposal 109 — its checks migrated to the `_ORIGIN_GROUNDED_GUARD` registry entry in `tests/structural_guards.py`, run by `tests/test_structural_guards.py`), asserting `tend_evolution_prs` references `_observe_pr_staleness`, drops the host-proxy `_pr_needs_conflict_resolution` from the catch-up loop, heals a drifted local `main` via `reset_hard("origin/main")`, and that neither retired 095 flag survives in `maestro/`; the whole-phase refusal block and the `looks_clean` catch-up short-circuit are deleted, and the `[loop] reconcile_local_main_enabled` / `behind_main_catchup_grounded_enabled` flags are retired so origin-grounded tend is now unconditional (PRs #3056, #3099). |
| **What is missing** | Nothing — all four steps landed (see What landed); origin-grounded tend is unconditional. |
| **Depends on** | 080, 089 _(both shipped — reuses 080's `merge_tree_conflicts` probe and 089's grounded behind-ness)_ |
| **Derived from** | live production data via /propose, 2026-07-14 |

## Description

> Status updated to shipped on 2026-07-17 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

`tend_evolution_prs` exists to keep every open evolution-PR branch current with
`origin/main` through a purely mechanical, zero-SDK merge. In production it is
doing almost none of that. Over the four active days 2026-07-11 → 07-14 the
phase aborted at its very first gate **139 times** with `local main is ahead of
origin/main — refusing to tend PRs`, and every downstream skip counter
(CI-pending, conflict-cooldown, head-CI-failing) read **zero** because control
never reached them. The trigger was a single stranded commit: `38f54678` — the
head of evolution branch `maestro/evolve/ea395f21664b` — sitting on the loop
container's local `main`, one commit ahead of `origin/main` since Jul 11. One
silent ref-drift became a total, self-perpetuating PR-maintenance outage, and PR
#3001 (the reported symptom) only reached `main`-parity because a human merged it
by hand.

The failure is structural, not a stuck branch. Tend leans on two signals that
are *not* the fact it needs. First, its whole-phase guard keys on the local
`main` ref — but the actual catch-up merge targets `origin/main` and the
pre-merge reset targets `origin/<branch>`, so tend never legitimately reads local
`main` at all; the guard aborts on a ref its own operations don't consume, and
`ensure_main_branch()` guarantees only "on `main`, clean tree," never that
`main` equals `origin/main`, so nothing heals the drift. Second, when the phase
*does* run, whether to merge is decided by GitHub's asynchronous,
branch-protection-dependent `mergeStateStatus` / `mergeable` proxy
(`looks_clean`, `_pr_needs_conflict_resolution`) — never by a direct
`commits_behind(branch, "origin/main")`. `mergeable == MERGEABLE` means "no
conflicts," not "up to date with `main`," which is exactly the conflation behind
"reported as mergeable when it isn't": a PR blocked on review (like #3001, which
reads `BLOCKED/MERGEABLE`) has its behind-ness masked by the higher-priority
host status and is never recognised as needing the catch-up merge. Branches then
drift until they hard-conflict and must be rescued by *expensive LLM*
remediation — 46 of the last 118 tasks (39%) are conflict / merge-up rescues.

The end state grounds every tend decision in origin-anchored, directly-measured
facts. A reconcile step heals a drifted local `main` back to `origin/main`
(safe: the loop owns no local-only `main` commits by invariant) while surfacing
the anomaly loudly, so the phase can no longer refuse itself into an outage. One
pure `commits_behind`-grounded staleness fact — reusing 089's grounded
behind-ness and 080's in-memory clean-merge probe — becomes the single owner of
"how far behind `origin/main` is this branch, and does it merge cleanly," and the
catch-up actuator keys on it instead of the host status proxy. A golden test
pins the grounding so the refusal and the proxy short-circuit cannot creep back.

## Relevant Findings

- `maestro/evolution.py` (`tend_evolution_prs`) — after `fetch`, when `rev_parse("main") != rev_parse("origin/main")` and `commits_ahead_behind("main", "origin/main")` reports `ahead > 0`, it appends `local main is ahead of origin/main … refusing to tend PRs` and returns before enumerating any PR. A single drifted local-`main` commit halts maintenance for **all** open PRs.
- `maestro/evolution.py` (`_tend_single_pr`) — the actual catch-up is `merge_from("origin/main", …)`, and the pre-merge reset is `reset_hard(f"origin/{branch}")`. Neither reads the local `main` ref, so the whole-phase guard defends against a pollution the merge code cannot cause.
- `maestro/evolution.py` (`ensure_main_branch`) — ensures the tree is *on* a branch named `main` with a clean worktree; it never compares `main`'s SHA to `origin/main` and never reconciles it. No component owns the invariant "loop-container `local main ≡ origin/main`."
- `maestro/evolution.py` (`tend_evolution_prs` `looks_clean` short-circuit) — `status == "CLEAN" or (status in ("", "UNKNOWN") and mergeable == "MERGEABLE")` `continue`s a PR as a no-op. `mergeable == MERGEABLE` reflects only conflict-freedom; a behind-but-clean or async-`UNKNOWN` PR is skipped without ever measuring `commits_behind`.
- `maestro/evolution.py` (`_pr_needs_conflict_resolution`) — decides catch-up routing purely from `merge_state_status ∈ {DIRTY, BLOCKED, UNSTABLE}` or `mergeable == CONFLICTING`. GitHub reports the single highest-priority blocker, so a `BLOCKED` (awaiting-review) PR that is *also* behind never surfaces its behind-ness here.
- `maestro/evolution.py` — the only `commits_ahead_behind` calls in the tend path compare `main` vs `origin/main` (the refusal) and `branch` vs `origin/<branch>` (the reset). **No call compares `branch` vs `origin/main`** — the staleness the phase exists to fix is never measured.
- `maestro/pr_readiness.py` (`observe_pr_readiness`, `PRReadiness.branch_behind`) — 089 already derives a grounded `branch_behind` in `observe_pr_readiness` via an injected `merge_block_oracle` (the deterministic `classify_merge_block(commits_behind, check_conclusion, mergeable_state)` reason code in `maestro/remediation_tracker.py`) — `branch_behind = reason == MERGE_BLOCK_BEHIND_MAIN` — but it feeds the *remediation readiness / stand-down* machinery; the tend *catch-up* actuator does not consume it. The sensor exists; tend ignores it.
- `maestro/vcs.py` (`merge_tree_conflicts`) — 080 Phase 2 already provides a zero-SDK in-memory `git merge-tree` clean-merge probe (`CodeHost.merge_tree_conflicts(ref_a, ref_b)`), the exact building block a grounded "cleanly mergeable against `origin/main`" fact needs.
- `tests/test_evolution_tend.py` — exercises the CI-wait, cooldown, and conflict-routing branches, but has no test asserting that a behind-but-clean or a `BLOCKED`-and-behind PR is caught up, and none pinning that tend must not refuse on local-`main` drift.

## Production Evidence

All measurements read strictly read-only from the live bind-mount
`/opt/maestro/data` (host mount of the `maestro-loop` container).

- **Whole-phase refusal dominates.** `logs/maestro.log`, window 2026-07-08 → 07-14 (~6 days):

  ```
  refusing to tend (local main ahead): 139
  deferred CI pending:                 0
  conflict cooldown skip:              0
  head CI failing defer:               0
  ```

  Refusals by day: 07-11 → 10, 07-12 → 60, 07-13 → 43, 07-14 → 25. Every
  downstream tend skip counter is 0 because the phase never advanced past the
  first gate.

- **The stranded commit.** In the loop container, `git log origin/main..main` shows one commit on local `main` not on `origin/main`:

  ```
  38f54678  maestro: Owned-disposal lever — name `gh pr close` … (Maestro, 2026-07-11)
  ## main...origin/main [ahead 1, behind 495]
  ```

  `38f54678` is the head of `origin/maestro/evolve/ea395f21664b`. The `main`
  reflog shows it repeatedly `reset` back to that SHA after being reset to real
  `origin/main` heads (`87cd0c21`, `4f71a764`, …) — a tug-of-war no loop invariant
  resolves.

- **Behind-main is the norm, not the exception.** Live `gh pr list` for the three currently-open evolution PRs against a fresh `git rev-list --count origin/<head>..origin/main`:

  ```
  #3043  BEHIND/MERGEABLE    behind=2   looks_clean=False
  #3042  BEHIND/MERGEABLE    behind=2   looks_clean=False
  #3001  BLOCKED/MERGEABLE   behind=0   looks_clean=False   (human-updated)
  ```

  `#3001` reads `MERGEABLE` while its blocked status masks the behind-ness that
  actually kept it off `main`. Across all open `maestro/evolve/*` remote branches
  the behind-`origin/main` counts run into the hundreds and thousands (495, 722,
  986, 1801, 4471, 5368…), evidence that "keep branches current" has not been
  happening at scale.

- **Cost of the drift falls on the LLM.** `task_history.jsonl` (118 rows): 46 (39%) are conflict / merge-up remediation tasks; outcomes `partial: 37, success: 9`; 28 are `Remediate PR #N`. The user had to prod the agent directly — recorded goal *"Do you ever bring any work to completion? You are so quick to give up when PRs can be easily merged up with main"* — to run LLM tasks that reconciled #3001/#3042/#3043, work a zero-SDK catch-up should have done cheaply and continuously.

## Prior Point Fixes (whack-a-mole ledger)

The tend / conflict path has absorbed a long trail of point fixes and five
adjacent proposals, none of which grounds the catch-up decision or heals a
drifted local `main`.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| `090b6e6f` | Introduced `tend_evolution_prs` **with** the "local main ahead → refuse to tend" guard | The guard trades a rare tamper for a total outage; keys on local `main`, which the merge never reads. |
| #2109 (`acd2cf68`) | Proceed on PRs with no CI configured instead of deferring forever | Removed one deferral leak; catch-up still gated on host status, not `commits_behind`. |
| #2508 (`2e3ab78f`) | Reconcile a diverged `origin/<branch>` head before push | Fixed the branch-vs-its-own-remote race; branch-vs-`origin/main` staleness still unmeasured. |
| #2521 (`b1d98412`) | Route behind-**and**-conflicting PRs to LLM remediation | Handles the conflicting case; a behind-but-*clean* PR still short-circuits as `looks_clean`. |
| #2836 (`e52703d6`) | Log the CI-failure merge-deferral on every lap | Better observability; the refusal above it still returns first, so the log never fires. |
| Proposal 080 | Merge-truth oracle + lap budget + stand-down ledger | Bounds the remediation *treadmill* downstream; does not prevent the drift that feeds it. |
| Proposal 087 | One provenance-stamped merge-truth chokepoint | Answers "is this PR merged?", not "how far behind `origin/main` is it?" |
| Proposal 089 | `observe_pr_readiness` grounded `branch_behind` + progress-aware laps | Built the grounded behind-ness sensor — but wired it to readiness/stand-down, never to the tend catch-up actuator. |
| Proposal 091 | Delivery-truth markers + stop orphaning open PRs | Fixes *who* tends; not *whether the branch gets caught up*. |
| Proposal 092 | WIP-primacy over task selection | Prioritises finishing owned PRs; still relies on the same un-grounded tend merge to finish them. |

**Retired by this design:** the whole-phase refusal (step 1 replaces it with
reconcile-and-surface) and the `looks_clean` / `_pr_needs_conflict_resolution`
role as the *catch-up trigger* (step 3 replaces it with the grounded staleness
fact); step 4 deletes both once the flags default on.

## Implementation Plan

1. [x] **Reconcile local `main` to `origin/main`; retire the whole-phase refusal.** `maestro/evolution.py` (`tend_evolution_prs`, `ensure_main_branch`) — after `fetch`, when `local main != origin/main`, verify every local-`main`-only commit is reachable from some `origin/*` ref (already pushed — true for `38f54678`); if so, `reset_hard("origin/main")` and emit a loud, greppable WARNING + a journal/health note naming the stranded SHA and the matching branch head, then proceed to tend. If any local-`main`-only commit is *not* on any `origin/*` ref (never expected — the loop commits only to task/evolve branches), preserve it and escalate rather than tend. Gate behind `[loop] reconcile_local_main_enabled`; flag off ⇒ today's refusal is byte-for-byte unchanged.
   - *Depends on:* none.
   - *Acceptance:* with local `main` one already-pushed commit ahead of `origin/main`, `tend_evolution_prs` resets `main` to `origin/main`, records the anomaly, and enumerates + tends open PRs (no early return); with an unpushed local-`main` commit it does **not** reset and does not tend.

2. [x] **One origin-grounded staleness fact.** new `maestro/pr_staleness.py` (`observe_pr_staleness`, frozen `PrStaleness`) — a pure function taking injected `commits_behind_fn` / `merge_clean_fn`, returning `commits_behind` (via `commits_ahead_behind(branch, "origin/main")`) and `cleanly_mergeable` (via `not merge_tree_conflicts("origin/main", branch)`), zero SDK / zero host calls. Reuses 080's `merge_tree_conflicts` probe and 089's grounded behind-ness rather than minting a rival metric.
   - *Depends on:* none.
   - *Acceptance:* `observe_pr_staleness` returns `commits_behind > 0, cleanly_mergeable=True` for a behind-but-clean branch and `cleanly_mergeable=False` for a behind-and-conflicting branch, with no host/SDK call in the path.

3. [x] **Wire the staleness fact into the catch-up actuator.** `maestro/evolution.py` (`tend_evolution_prs`, `_tend_single_pr`) — drive the catch-up decision from `observe_pr_staleness`: `commits_behind > 0 and cleanly_mergeable` → the existing `merge_from("origin/main")` + gates + push path; `commits_behind > 0 and not cleanly_mergeable` → the existing LLM conflict-routing path (unchanged); `commits_behind == 0` → no-op. GitHub `mergeStateStatus` / CI still govern the CI-wait and approval logic but no longer act as the behind-ness gate, so a `BLOCKED`-and-behind PR is caught up. Gate behind `[loop] behind_main_catchup_grounded_enabled`; flag off ⇒ today's `looks_clean` proxy path.
   - *Depends on:* 2.
   - *Acceptance:* a `BLOCKED`-and-behind PR with a clean merge-tree probe reaches the `merge_from("origin/main")` path instead of being skipped, and a `commits_behind == 0` PR short-circuits as a no-op.

4. [x] **Golden enforcement + delete the moles.** new `tests/test_tend_origin_grounded.py` (since retired by proposal 109 — its checks migrated to the `_ORIGIN_GROUNDED_GUARD` registry entry in `tests/structural_guards.py`, run by `tests/test_structural_guards.py`) — assert `tend_evolution_prs` contains no early `return`/abort keyed on a `main`-vs-`origin/main` ahead comparison, and that the catch-up decision references `observe_pr_staleness` / `commits_ahead_behind(…, "origin/main")` (grounded, not `mergeStateStatus`-only). Once steps 1 & 3 default on after a soak, remove the whole-phase refusal block and the `looks_clean` catch-up short-circuit.
   - *Depends on:* 1, 3.
   - *Acceptance:* the enforcement test fails if a future edit reintroduces a local-`main` refusal or a `mergeStateStatus`-only catch-up gate.

## Testing Strategy

All tests use `mem_fs` and mock every external (VCS subprocess, host API, SDK,
clock); no real network, sleep, or filesystem.

- `tests/test_evolution_tend.py` — new cases: (a) local `main` ahead by an
  already-pushed commit ⇒ reconcile-and-tend, with the reset target asserted as
  `origin/main` and the anomaly recorded; (b) local `main` ahead by an
  unpushed commit ⇒ no reset, escalate, no tend; (c) `BLOCKED`-and-behind PR with
  a clean probe ⇒ reaches `merge_from("origin/main")`; (d) behind-and-conflicting
  PR ⇒ LLM conflict route (regression guard); (e) `commits_behind == 0` ⇒ no-op.
  Both flag states covered so the flag-off path is byte-for-byte the old
  behaviour.
- `tests/test_pr_staleness.py` (new) — `observe_pr_staleness` over injected
  fakes: behind-clean, behind-conflicting, current; assert zero host/SDK calls
  (spy fakes raise if invoked).
- `tests/test_tend_origin_grounded.py` (new; since retired by proposal 109 — its
  checks migrated to the `_ORIGIN_GROUNDED_GUARD` registry entry in
  `tests/structural_guards.py`, run by `tests/test_structural_guards.py`) — the
  AST/golden guard from step 4, in the house style of
  `tests/test_merge_truth_chokepoint.py`.
- `tests/test_config.py` — the two new `[loop]` flags parse; reconcile defaults on, grounded catch-up defaults off.

## Risk Assessment

- **Discarding real local `main` work.** Mitigated by the reachability check in
  step 1: `reset_hard("origin/main")` fires only when every local-`main`-only
  commit is already on an `origin/*` ref; an unpushed commit is preserved and
  escalated, never dropped. The loop's Human-in-the-loop invariant means such
  commits should not exist, so this path is a fail-safe, not a routine.
- **Masking the reset-site bug.** The reconcile *heals and surfaces* — a durable,
  greppable diagnostic records the stranded SHA and matching branch every time —
  so the separate defect of *what reset `main` off `origin/main`* stays visible
  for its own RCA rather than being silenced (it is Out of scope here).
- **Wrong catch-up on a mis-probed branch.** `merge_tree_conflicts` is the same
  in-memory probe 080 already trusts for merge-block classification; a false
  "clean" still lands in the existing post-merge quality-gate + rewind path, and
  a false "conflict" routes to LLM as today — no new unsafe branch.
- **Rollback.** Every behaviour change is behind `[loop] reconcile_local_main_enabled`
  and `[loop] behind_main_catchup_grounded_enabled`, reconcile and grounded catch-up now both default-on; steps are
  independently mergeable and each leaves the phase working. Reverting either
  flag restores current behaviour with no state migration.
- **Trust chain.** No approval, sandbox, or public-API surface changes; force-push
  stays structurally banned and the merge target stays `origin/main`.

**Falsifiability.** If the design is right: `refusing to tend PRs` in
`logs/maestro.log` falls to ≈0; open-PR `behind origin/main` counts stay near
zero instead of accumulating; and the conflict / merge-up share of tasks
(currently 39%) drops, visible via `format_pr_churn_summary` / `measure_no_progress_laps`
(089). If, after both flags default on, branches keep accumulating behind-counts
or the conflict-task share holds near 39%, the grounding did not close the class
and the root is one level deeper.

## Out of scope

- **Why something reset local `main` onto an evolution-branch head.** The reset
  *site* that produced `38f54678` on `main` is a distinct defect; step 1 heals
  and surfaces the drift, but pinning the offending write belongs to `/rca` on
  that site, not this proposal.
- **Abandoned evolve branches hundreds/thousands behind with no open PR.** Cold
  branch pruning is `scripts/prune-branches.sh` / operator territory; this
  proposal governs branches with an *open* PR that tend is responsible for.
- **GitHub branch-protection configuration.** Whether the repo requires
  up-to-date branches is an org setting; the design grounds on `origin/main`
  regardless of how the host surfaces behind-ness.
