# 097: Standing Verdict Settlement — One Reconciler Binds Every Recorded Remediation Verdict to the Merge-Truth Chokepoint So the Phantom Sensors Truly Self-Extinguish

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/audit_checks.py` (`check_phantom_remediation`, `check_phantom_resolution`, `check_phantom_escalation`, `check_unconfirmed_own_landing`, `_git_pr_merged_default`, new `reconcile_settled_verdicts` / `load_settled_prs`), `maestro/agent.py` (`_audit_self_model`, `is_pr_settled`), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` (`[audit]`), new `tests/test_settlement_reconciler.py`, and updates to `tests/test_audit_checks.py`, `tests/test_agent.py`, `tests/test_config.py` |
| **What landed** | All three phases (steps 1–8) — and the master flag is defaulted **on**. Phase 1 — the master flag `[audit] settlement_reconciler_enabled` (`maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, now default `true`, pinned by `tests/test_config.py`) and the pure zero-SDK reconciler `reconcile_settled_verdicts(data_dir, n, iteration, *, is_pr_settled)` + reader `load_settled_prs(data_dir)` (`maestro/audit_checks.py`, reusing `_read_recent_task_tail` / `_REMEDIATION_GOAL_PATTERN`), upserting every settled (merged *or* closed) PR to the durable `settled_verdicts.jsonl` ledger under `data_dir`, covered by `tests/test_settlement_reconciler.py`. Phase 2, gated behind the flag — the `settled_prs: frozenset[int]` pre-filter on all four phantom rules (`check_phantom_remediation`, `check_phantom_resolution`, `check_phantom_escalation`, `check_unconfirmed_own_landing`), each skipping a settled PR before its merge predicate (rules 13/14 also prune it from their first-seen sidecar in the same pass), plus `reconcile_settled_verdicts` wired into `_audit_self_model` (`maestro/agent.py`) threaded through the flag (`frozenset()` when off ⇒ byte-identical to the pre-097 audit output; on — now the default — ⇒ the reconciler runs each cycle and threads the settled set into all four rules); covered by `tests/test_audit_checks.py` and `tests/test_agent.py`. Phase 3 step 7, the golden-registry AST guard — `tests/test_settlement_reconciler.py` now asserts every declared phantom rule carries and reads the `settled_prs` pre-filter (`test_settled_pr_registry_matches_declared_prefilter_rules` / `test_each_registered_rule_consumes_settled_prs`) and that `reconcile_settled_verdicts` reaches merge-truth only through the injected `is_pr_settled` chokepoint (`test_reconciler_reaches_merge_truth_only_through_chokepoint`). Phase 3 step 8, the per-rule merge-read retirement — the `settled_prs` pre-filter now runs strictly *ahead* of the merge predicate in all four rules (`check_phantom_resolution` was the last to filter after its predicate pass; it now narrows to a `candidates` list first), so a settled PR's self-extinguish decision is read from the ledger and costs **zero** merge-oracle calls. `_git_pr_merged_default` is not deleted: it survives as the DI fallback for a still-**unsettled** PR (the live `_audit_self_model` dispatch always injects `_host_or_git_pr_merged`, so it is never the live per-rule predicate), which is the "fallback oracle" role step 8 sanctions. The ordering is pinned behaviorally by `tests/test_settlement_reconciler.py` (`test_settled_pr_costs_zero_merge_predicate_calls` over the `_PREDICATE_BEARING_RULES` sub-registry, plus `test_predicate_bearing_registry_matches_declared_merge_predicates`) and per-rule in `tests/test_audit_checks.py`. |
| **What is missing** | Nothing — all three phases are built. |
| **Depends on** | 087 _(shipped — reuses the `is_pr_settled` chokepoint)_; composes with 090, 091 |
| **Derived from** | live production data via /propose, 2026-07-17 |

## Description

> Status updated to shipped on 2026-07-21 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The audit layer advertises a family of "self-extinguishing" phantom sensors —
`phantom_remediation` (rule 10), `phantom_resolution` (rule 13), and their
siblings — each meant to fire once, prod the loop to land or retire a PR, and
then go dark the moment git-merge-truth agrees. In production they do not go
dark. A remediation verdict is written once, frozen in append-only
`task_history.jsonl`, and the sensors re-derive "the recorded stamp disagrees
with git" every audit cycle. When a PR settles *after* its last verdict — the
normal path for an external-blocker PR, which `tend_evolution_prs` or a human
eventually merges, or which is closed as disposed — nothing reconciles the
frozen stamp. So the same finding re-fires for as many cycles as the record
survives in the trailing window: a merged PR flagged "stop re-routing it" for
weeks, a closed PR flagged "land the cure or honestly retire it" long after it
was terminally disposed.

This matters now because the sensors are structurally unable to extinguish for
the two most common terminal states. `phantom_remediation` has no memo at all —
it re-runs each cycle and fires until the stale record scrolls out of the
trailing-20 window. `phantom_resolution` self-extinguishes only on **merge**,
via a merged-only predicate (`_git_pr_merged_default`), so a **closed** PR is
"settled" to the loop but "forever unmerged" to the audit rule. 087 built one
live merge-truth chokepoint (`is_pr_settled`, merged *or* closed) and #2809
bound the verdict to it at *write* time — but the audit rules were deliberately
exempted from the chokepoint (they receive merge-truth by dependency injection),
and there is no *standing* reconciler that keeps already-written verdicts bound
to it as git advances.

The end state: one zero-SDK reconciler runs once per audit cycle, reads the
trailing remediation verdicts, consults the `is_pr_settled` chokepoint (merged
*or* closed) exactly as the loop does, and records each settled PR to a durable
settlement ledger. Every phantom sensor consults that ledger first and skips a
settled PR — so each finding fires at most once and then genuinely
self-extinguishes, whether the PR merged by tend, merged by a human, or was
closed. The audit layer stops re-litigating facts git has already settled, and
the "self-extinguishing" contract in CLAUDE.md becomes true instead of
aspirational.

## Relevant Findings

- `maestro/audit_checks.py` (`check_phantom_remediation`) — reads the trailing
  *n* `Remediate PR #N` records and fires for each whose not-landed stamp git
  contradicts. It holds no first-seen sidecar and no cross-cycle memo, so it
  re-emits every cycle the stale record is in-window; "self-extinguish" here
  means only "scrolls off the trailing-20 window," which for a low-task-volume
  loop is dozens of cycles.
- `maestro/audit_checks.py` (`check_phantom_resolution`) — self-extinguishes via
  a first-seen sidecar keyed by PR that drops **merged** PRs (`if not
  merged[pr_num]`). A PR that is **closed** but unmerged never leaves the
  unmerged set, so the finding re-fires indefinitely — the merged-only-vs-settled
  asymmetry.
- `maestro/audit_checks.py` (`_git_pr_merged_default`) — the merge-truth
  predicate every phantom rule defaults to. It returns `True` only for a PR that
  landed as a merge/squash commit on `origin/main`; it says nothing about
  `closed`. This is not the loop's chokepoint and knows nothing of the
  merged-*or*-closed `settled` state.
- `maestro/agent.py` (`is_pr_settled`) — the 087 chokepoint: fetch-first,
  provenance-stamped, per-iteration-memoized, returns a `MergeTruth` with both
  `branch_landed` (merged) and `settled` (merged *or* closed). The four
  live PR-servicing faces read through it; the audit phantom rules do not.
- `maestro/remediation_tracker.py` (`coerce_verdict_to_settled_on_merge`) —
  binds a verdict to `is_pr_settled(n).branch_landed` at **write** time only,
  and only on the merged branch. A PR merged by tend/human with no subsequent
  lap keeps its last stale `partial` stamp; a closed-unmerged PR is never
  coerced. This is the write-time half of a reconciliation whose standing half
  does not exist.
- `tests/test_merge_truth_chokepoint.py` — AST-enforces `is_pr_settled` as the
  only door over `agent.py` and `loop/pr_remediation.py`, and explicitly scopes
  out the audit rules: "Sensors / audit rules receive the oracle as an injected
  parameter … out of scope." The injection seam this proposal fills is the one
  the guard already anticipates.
- `maestro/agent.py` (`_audit_self_model`) — dispatches each phantom rule,
  prepends findings, and re-clips to `MAX_FINDINGS_PER_CYCLE`, routing them to
  `open_questions` and the audit→directive bridge. This is the single site where
  a reconciler runs once and injects the settled set into every rule.

## Production Evidence

Live data resolved from the loop container's bind-mount at
`/opt/maestro/data` (strictly read-only). Windows stated per figure.

**Phantom firings dominate the audit layer.** Over 175 audit cycles (iterations
10–1680, 2026-06-24 → 2026-07-17, `audit_history.jsonl`), `phantom_remediation`
fired in **119** cycles and `phantom_resolution` in **77** — the two
highest-firing rules after the comfort-completion prediction, ahead of
`orphaned_cure` (61) and `narration_verdict_mismatch` (57).

**A merged PR flagged for weeks.** PR #2835 merged into main on 2026-07-01, yet
`phantom_remediation` surfaced it across **79 distinct audit cycles** (iterations
380 → 1160), each cycle re-emitting (`audit_history.jsonl`):

```
Remediation for PR #2835 was stamped not-landed but git-merge-truth shows it
merged into main — the not-landed verdict is read from stamps, not git; stop
re-routing it.
```

The loop had already stopped re-routing it — **0 of 48** remediation laps in the
window ran after their PR merged (the 087/080 dequeue gates work). Only the
sensor kept firing. PR #2830 (merged 2026-07-01) surfaced across **78 cycles**
the same way.

**A closed PR that can never darken.** PR #2844 was **closed** unmerged on
2026-07-03, yet `phantom_resolution` surfaced it across **50 distinct audit
cycles** (iterations 830 → 1320), each cycle re-emitting:

```
A task was stamped success claiming PR #2844 resolved, but git-merge-truth shows
it still unmerged 10 audit cycles later — a success git can't see is a story;
land the cure or honestly retire it, then re-inspect that it landed.
```

Of the `phantom_resolution` target PRs I could resolve against GitHub, the
majority are **CLOSED**, not open: #2844, #2991, #2884, #2812 — **≥59 of the 77**
firings target confirmed-closed PRs the merged-only self-extinguish can never
clear. Their "land the cure or honestly retire it" demand is already satisfied:
the PR is retired.

**The generator.** Of the last 154 tasks, **48 are `Remediate PR #N` laps across
25 distinct PR lineages, and 47 are stamped `partial`** (`task_history.jsonl`) —
several PRs lapped 3–4 times (#2811 ×4, #2808/#2809/#3001 ×3) before tend or a
human merged them. Each unlanded lap leaves the frozen `partial` stamp that
becomes the phantom sensor's fuel; none of these laps is ever reconciled to the
eventual merge.

**The agent already named the class.** `self_model.json` `open_questions`
carries: `[limitation] stamp-not-git-readmission: #3001/#3078 re-serviced despite
git-merged — read verdict from is_pr_settled, not stamps.`

## Prior Point Fixes (whack-a-mole ledger)

| Identifier | What it patched | Why the class survived |
|---|---|---|
| 087 (`is_pr_settled`) | One live merge-truth chokepoint for the four PR-servicing faces | Chokepoint governs **live** reads; audit rules were exempted (DI) and keep their own merged-only `_git_pr_merged_default` |
| #2809 (`coerce_verdict_to_settled_on_merge`) | Binds a verdict to settled-truth at **write** time | Fires only when a **new** lap writes a verdict; a tend/human-merged PR with no new lap keeps its stale stamp; reads `branch_landed`, not `settled` |
| Rule 10 (`check_phantom_remediation`) | Detects merged-PR not-landed stamps | No memo — re-detects every cycle, fires until the record scrolls off (#2835 ×79) |
| Rule 13 (`check_phantom_resolution`) | Detects unmerged success-claims; sidecar self-extinguish | Self-extinguish keys on **merge only**, so CLOSED PRs never darken (#2844 ×50) |
| 090 rule-12/13/15 forced arms; #2846 age-0 forced disposition | Force a disposition on an **open** unmerged phantom | Act on open PRs; do nothing for a PR git has already settled |
| Rule 20 (`check_undercredited_landing`) | Affirmative complement: non-success task whose SHA landed | Another detector of the stamp/git gap; still does not reconcile the stamp |

The reconciler + settled pre-filter **retires the re-fire tails** of rules
10/12/13/14 on settled PRs and **retires the merged-only-vs-settled asymmetry**
in the audit layer's self-extinguish decision. It does not delete the rules —
they still catch the first, live signal on a genuinely-open, genuinely-unreconciled
verdict.

## Implementation Plan

### Phase 1 — the reconciler and ledger (no behavior change; flag off)

1. [x] **Add the reconciler flag.** `maestro/config.py`, `maestro/toml_config.py`,
   `maestro.toml` — add `[audit] settlement_reconciler_enabled` defaulting to
   `false`, alongside the existing `phantom_*_enabled` master switches.
   - *Depends on:* none.
   - *Acceptance:* `Config.settlement_reconciler_enabled` loads from `[audit]`
     and defaults `False`; a `test_config` assertion pins the default.
2. [x] **Author `reconcile_settled_verdicts`.** `maestro/audit_checks.py` — a
   pure function `reconcile_settled_verdicts(data_dir, n, iteration, *,
   is_pr_settled)` that reads the trailing *n* `Remediate PR #N` verdicts (reusing
   `_read_recent_task_tail` / `_REMEDIATION_GOAL_PATTERN`), consults the injected
   `is_pr_settled` once per distinct PR, upserts every **settled** (merged *or*
   closed) PR to a durable `settled_verdicts.jsonl` ledger under `data_dir`
   (`{pr_number, settled_at_iteration, kind, verdict_task_id}`), and returns the
   settled `frozenset[int]`. Add `load_settled_prs(data_dir)`. Zero SDK cost.
   - *Depends on:* none.
   - *Acceptance:* given a task tail citing a merged PR, a closed PR, and an open
     PR, the returned set contains the merged and closed PR numbers and not the
     open one; the ledger records both with the correct `kind`.

### Phase 2 — the settled pre-filter (behavior change, gated by the flag)

3. [x] **Pre-filter `phantom_remediation`.** `maestro/audit_checks.py` — add
   `settled_prs: frozenset[int] = frozenset()` to `check_phantom_remediation`; a
   PR in the set is skipped before the merge predicate. Empty default = today's
   behavior byte-for-byte.
   - *Depends on:* 2.
   - *Acceptance:* with a merged PR present in `settled_prs`, the rule emits no
     finding for it; with `settled_prs` empty, the finding fires exactly as today.
4. [x] **Pre-filter `phantom_resolution` and close the closed-PR asymmetry.**
   `maestro/audit_checks.py` — add `settled_prs` to `check_phantom_resolution`; a
   settled PR (including **closed**) is skipped and pruned from its first-seen
   sidecar in the same pass.
   - *Depends on:* 2.
   - *Acceptance:* a closed-unmerged PR present in `settled_prs` emits no finding
     and is dropped from the sidecar; an open unmerged aged claim still fires.
5. [x] **Extend the pre-filter to the sibling rules.**
   `maestro/audit_checks.py` — add the `settled_prs` DI param to
   `check_phantom_escalation` and `check_unconfirmed_own_landing`, each skipping a
   PR in the set. (Rule 15, `check_narration_verdict_mismatch`, is deliberately
   excluded — see *Out of scope* — it cites the `task_id`, not a PR, so there is
   no PR key to pre-filter on.)
   - *Depends on:* 2.
   - *Acceptance:* each rule, given a settled PR it would otherwise flag, emits no
     finding for that PR; unaffected PRs still flag.
6. [x] **Wire the reconciler into the audit dispatch.** `maestro/agent.py`
   (`_audit_self_model`) — when `settlement_reconciler_enabled`, call
   `reconcile_settled_verdicts(..., is_pr_settled=self.is_pr_settled)` once at the
   top of the phantom-rule dispatch and pass the returned set into every phantom
   rule; when the flag is off, pass `frozenset()` (byte-identical to today).
   - *Depends on:* 3, 4, 5.
   - *Acceptance:* with the flag on and a settled PR in `task_history.jsonl`, a
     full `_audit_self_model` cycle produces zero phantom findings citing that PR;
     with the flag off, the finding count is unchanged from today.

### Phase 3 — enforce the invariant, then retire the moles

7. [x] **Deterministic enforcement test.** new `tests/test_settlement_reconciler.py`
   — a golden registry of the phantom rules asserts each accepts `settled_prs`
   and skips a PR in it, and that `reconcile_settled_verdicts` reaches merge-truth
   *only* through the injected `is_pr_settled` (no bare `_git_pr_merged_default`
   or `subprocess` call in the reconciler body). Modeled on
   `tests/test_merge_truth_chokepoint.py`.
   - *Depends on:* 6.
   - *Acceptance:* the test fails if a phantom rule is added or edited without the
     `settled_prs` pre-filter, or if the reconciler reads merge-truth off-chokepoint.
8. [x] **Retire the redundant per-rule merge read.** `maestro/audit_checks.py` —
   once `settlement_reconciler_enabled` is defaulted on and proven, route each
   phantom rule's self-extinguish decision through the settled ledger and remove
   the now-dead per-rule `_git_pr_merged_default` invocation for the re-fire
   decision, leaving the reconciler as the audit layer's single settled-truth
   reader (the git-subject helper stays only as the reconciler's fallback oracle).
   - *Depends on:* 7.
   - *Acceptance:* no phantom rule calls `_git_pr_merged_default` to decide whether
     to re-fire on a settled PR; the reconciler is the sole audit-layer settled
     reader; the Phase-3 test still passes.

## Testing Strategy

All tests use `mem_fs` for the ledger and `task_history.jsonl`, inject
`is_pr_settled` / merge predicates as callables (no real git, no subprocess, no
network, no sleep), and assert simple equalities.

- `tests/test_settlement_reconciler.py` — the reconciler over a synthetic task
  tail: merged PR and closed PR land in the returned set and the ledger with the
  right `kind`; open PR does not; the ledger is idempotent across repeated cycles;
  a PR absent from the trailing window is pruned. Plus the golden-registry AST
  guard (every phantom rule carries the `settled_prs` param and skips a settled
  PR; the reconciler reads merge-truth only through the injected chokepoint).
- `tests/test_audit_checks.py` — per rule (10, 12, 13, 14): with the settled
  PR in `settled_prs`, no finding; with it absent, the existing finding still
  fires (guarding the empty-default = current-behavior path). The rule-13 case
  explicitly covers a **closed-unmerged** PR being darkened and pruned from the
  sidecar.
- `tests/test_agent.py` — `_audit_self_model` with `settlement_reconciler_enabled`
  on: a settled PR in history yields zero phantom findings for that PR across a
  cycle; with the flag off, the injected set is empty and the finding count
  matches the pre-change baseline.
- `tests/test_config.py` — the new flag's default and `[audit]` binding.

## Risk Assessment

- **Suppressing a genuine finding.** The pre-filter only skips a PR the
  `is_pr_settled` chokepoint reports **settled** (merged or closed) — the same
  oracle the loop trusts to stop servicing a PR. If that oracle is wrong, the
  live loop is already wrong; the audit layer inherits, not amplifies, the risk.
  A still-open unmerged PR is never in the settled set, so live phantom signal is
  untouched. Mitigation: fail-safe empty set on any reconciler error, and the
  master flag shipped default-**off** through Phases 1–2 (inert), flipped on only
  once Phase 3 step 7's AST guard was in place.
- **Rollback posture.** Every behavior change is behind
  `[audit] settlement_reconciler_enabled` (off restores the pre-097 behavior); the
  `settled_prs` param defaults to `frozenset()` so an un-wired rule is a no-op.
  Steps are independently mergeable and ordered so an interrupted run leaves the
  audit layer working. Full revert is `git revert` of the phase; the ledger file
  is additive and ignored when the flag is off.
- **Data / migration.** `settled_verdicts.jsonl` is created on first reconcile;
  its absence reads as an empty ledger — no `ensure_schema` fingerprint change
  and no migration. It is operator-inspectable and never read by a hot path.
- **Trust chain.** None touched — no approval, sandbox, or public-API surface;
  the reconciler neither merges, closes, nor pushes. It observes and records.

## Falsifiability

The design is right if, after the flag defaults on, a settled PR's phantom
re-fire count collapses from today's tens of cycles (#2835 ×79, #2844 ×50) to at
most one cycle after settlement, measured directly from the per-PR finding counts
in `audit_history.jsonl` and the `rules_fired` tallies for `phantom_remediation`
/ `phantom_resolution`. It failed if a PR the `is_pr_settled` chokepoint reports
merged-or-closed still surfaces in a phantom finding more than one cycle after the
reconciler first records it — that would mean the ledger is not the sensors' sole
gate.

## Out of scope

- **Rule 15 (`check_narration_verdict_mismatch`).** It is deliberately *not* in
  the settled-PR pre-filter. Rule 15 cites the `task_id`, not a PR; consults no
  git oracle (it is a pure regex scan over `result_summary`, extracting `#NNNN`
  only under the opt-in `ready_only` path); and already self-extinguishes the
  moment the record scrolls out of the recent-task window — so it has no
  settled-PR re-fire tail to retire, and no PR key to pre-filter on. Folding it
  in would require adding PR extraction it does not otherwise do, changing its
  documented git-free character for no gain. Its own `partial`-complement forced
  arm (`narration_resolution_unlanded_claims`, rule-15 forced disposition) already
  consults `is_pr_settled` at its own site and needs nothing from this reconciler.
- **Reducing the number of remediation laps** (47/48 `partial`, PRs lapped 3–4×
  before landing). That is the lap-budget / standdown treadmill owned by 080 and
  the progress-aware lap accounting of 089; this proposal reconciles the *verdict
  a lap leaves behind*, not how many laps run. The two compose: fewer laps means
  fewer stamps to reconcile.
- **Autonomous merging or closing of PRs** — a hard human-in-the-loop invariant;
  the reconciler records settlement, it does not cause it.
- **Generalizing the settlement ledger to non-PR external truths** (CI status,
  issue state) — 087 already flagged that generalization as a successor; this
  proposal stays scoped to remediation-verdict settlement.
