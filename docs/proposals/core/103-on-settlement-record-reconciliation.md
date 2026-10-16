# 103: On-Settlement Record Reconciliation — One Owner Clears Every PR-Keyed Record When a PR Settles

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/settlement_record_reconciler.py` _(new)_, `maestro/audit_checks.py`, `maestro/remediation_tracker.py`, `maestro/consciousness.py`, `maestro/evolution.py`, `maestro/pr_ledger.py` (reused: `PRLedger.prune`), `maestro/escalation_bridge.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `tests/test_settlement_record_reconciler.py` _(new)_, `tests/structural_guards.py`, `tests/test_config.py` |
| **What landed** | Steps 1–7. Step 1 (PR #3263) — new `maestro/settlement_record_reconciler.py`: the `@runtime_checkable SettlementReconciler` protocol, the frozen `ReconcileResult`, the `reconcile_pr_settlement` driver appending one provenance row per store to `settlement_reconciliations.jsonl`, and the `[audit] settlement_record_reconciler_enabled` flag (`maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`, default-off) with `tests/test_settlement_record_reconciler.py`. Step 2 (PRs #3273, #3315, #3321, #3363) — the recurring per-cycle drive of the registry over the settled set in `MaestroAgent._audit_self_model` (`maestro/agent.py`), confined to warm merge-truth-memo PRs so it adds no merge-oracle calls, plus the `EscalationStanddownReconciler` (store 1) wrapping `escalation_bridge.clear_standdown` as the first registry entry (PR #3273); the shared `_reconcile_settled_pr_records` helper and the merge-transition drive `_reconcile_settled_merge_check`, which fires `reconcile_pr_settlement` for each PR the instant `check_merged_plans` reports its plan merged or closed (driven from `run_loop`, not `evolution.py`) (PR #3321); and the `PRLedgerReconciler` (store 5) forgetting a settled PR's `pr_ledger.json` row through the new `PRLedger.evict_settled` (`maestro/pr_ledger.py`), registered as the fourth module-level `RECONCILER_REGISTRY` entry (PR #3315); and the tend pass as the third settlement drive face (PR #3363) — `PRLedger.prune` now returns the sorted PR numbers it dropped this lap, `tend_evolution_prs` captures them onto `TendPRsResult.settled_pr_numbers`, and `_reconcile_settled_tend_prs` (`maestro/agent.py`) drives each distinct number through the shared `_reconcile_settled_pr_records` helper, completing the merge-transition / audit-cycle / tend-pass routing so every store clear flows through the one provenance-stamped owner. Step 3 (PR #3288) — `SelfModel.reconcile_settled_pr` (`maestro/consciousness.py`) evicting `[audit]`/`[limitation]` `open_questions` / `limitations_known` rows whose sole cited PR is settled and clearing a sole-settled-PR `next_task_directive`, surfaced by the registered `SelfModelReconciler` (store 2). Step 4 (PR #3300) — `RemediationTracker.evict_settled` (`maestro/remediation_tracker.py`) purging all four axes, surfaced by the agent-composed `RemediationTrackerReconciler` (store 3) that holds the single long-lived tracker instance. Step 5 (PR #3305) — the module-level `has_live_stand_down` (`maestro/remediation_tracker.py`), the head-agnostic liveness complement to `find_stand_down`, plus `StandDownVerdictReconciler` (store 4) releasing a settled PR's stand-down verdict head-agnostically through `release_stand_downs`, registered as the third module-level `RECONCILER_REGISTRY` entry — idempotent, appending a release row only when `has_live_stand_down` reports a live verdict. Step 6 (PR #3328) — `audit_checks.upsert_settled_pr`, the window-independent complement to `reconcile_settled_verdicts` that records *any* observed settlement into `settled_verdicts.jsonl` (not only a PR named by a surviving trailing `Remediate PR #N` record), bound into the shared `_reconcile_settled_pr_records` drive (`maestro/agent.py`) so both the per-cycle audit sweep and the merge-transition path upsert on settlement, closing 097's window-bound re-fire gap. Step 7 (this branch) — the net-new `_SETTLEMENT_RECORD_GUARD` entry in the shared structural-guard registry (`tests/structural_guards.py`'s `CHOKEPOINT_GUARDS`, proposal 109's harness, in place of a bespoke chokepoint test file), making `reconcile_pr_settlement`'s `RECONCILER_REGISTRY` the AST-enforced sole on-settlement PR-keyed store-clear door — the two sanctioned non-owner clears in `loop/pr_remediation.py` (`route_feedback`, `route_user_pr_message`) are allowlisted — plus the `settlement_record_reconciler_enabled` flip to default-on (`maestro/config.py` / `maestro.toml` / `tests/test_config.py`), so the five per-store reconcilers now run at the merge transition and each audit cycle in production. |
| **What is missing** | Nothing — step 2's remainder landed. The tend pass is the third settlement drive face (`_reconcile_settled_tend_prs` off `PRLedger.prune`'s dropped-PR return, captured on `TendPRsResult.settled_pr_numbers`), completing the merge-transition / audit-cycle / tend-pass routing; all six PR-keyed stores are reconciled per-PR through `RECONCILER_REGISTRY`. The design's bulk `prune_resolved_standdowns` and `reconcile_settled_verdicts` are retained as sanctioned out-of-scope sites — allowlisted in the step-7 `_SETTLEMENT_RECORD_GUARD` chokepoint — rather than registered, since their stores are already owned per-PR by `EscalationStanddownReconciler` (`clear_standdown`) and `SettledVerdictReconciler` (`upsert_settled_pr`). |
| **Depends on** | 087 _(shipped)_, 097 _(shipped)_ |
| **Derived from** | live production data via /propose, 2026-07-27 |

## Description

> Status updated to shipped on 2026-08-04 by /plan-proposal — see [README.md](../README.md#shipped) for the landing record.

When a Maestro-owned PR settles — merges or closes — its trace lives on in a
dozen internal stores keyed on the PR number, branch, or plan id: the self-model
directive slot, `open_questions`, `limitations_known`, the remediation tracker,
the stand-down ledger, the escalation handoff store, the PR ledger, and the
settled-verdicts pre-filter. Today those traces are cleared — *if at all* — by
five independent sites, each keyed on a **different** signal and each touching
**only its own store**. Two stores and the entire self-model have no on-merge
clear at all. The result is what this proposal's originating concern names:
Maestro keeps treating already-merged PRs as live work — re-surfacing them in
contemplation, aiming the next-task directive at them, and re-firing audit
findings about them — long after they landed.

This is not the mechanical tend loop mis-behaving: `tend_evolution_prs`
enumerates only open PRs and skips merged ones by construction. The residue is
in the *records*, and every fix so far has drained one puddle. The house already
built the two primitives this needs — proposal 087's `is_pr_settled` chokepoint
(the settlement *signal*) and proposal 097's `reconcile_settled_verdicts` (a
reconciler *shape*) — but 097 wired its reconciliation to exactly one consumer
(the phantom-rule pre-filter) and explicitly deferred generalizing it. No single
owner reconciles the rest.

This proposal adds that owner: one provenance-stamped
`reconcile_pr_settlement` driver over a **registry** of small per-store
reconcilers, invoked from the one place that already detects a merge. The
existing scattered clearers become registry entries; the three uncovered stores
(self-model, remediation tracker, stand-down verdicts) get the reconcilers they
lack; and an AST-enforced chokepoint test makes the registry the *only* door
through which a PR-keyed store may be cleared on settlement — so the next
settle-side shape is a registry entry, not the fifty-first `*_enabled` flag.

Once it lands, a settled PR is fully forgotten within one reconcile pass: its
directive is cleared, its self-model rows evicted, its tracker and stand-down
entries purged, its phantom findings pre-filtered — all recorded to a durable
`settlement_reconciliations.jsonl` ledger the operator can inspect.

## Production Evidence

Live data read strictly read-only from the loop container's bind-mount at
`/opt/maestro/data` (host mount of `maestro-loop`). All windows stated
inline; PR titles and tokens redacted where present.

- **The self-model still names PRs merged days ago.** `self_model.json`
  (written 2026-07-27T16:45, the latest loop write) carries, verbatim:

  ```
  next_task_directive:
    "Action-zero: fire deliver_handoff on green #3192 BEFORE any --is-ancestor
     read on any PR. … Report the new pr_number:3192 stand_down_verdicts row."
  limitations_known / open_questions:
    "re-lapped-same-prs: #3195 (9x) and #3197 (8x) re-serviced as remediation
     instead of landed once …"
  ```

  PR #3192 merged 2026-07-23T10:07:45Z; #3195 merged 2026-07-23T16:06:47Z; #3197
  merged 2026-07-24T16:51:16Z (GitHub `mergedAt`, checked 2026-07-27). All three
  are settled, yet the directive aims the next task at one and the limitation
  re-surfaces two in every contemplation. There is no on-merge clear for either
  field.

- **The loop re-confronts merged PRs in priced contemplation.** `logs/maestro.log`
  across 2026-07-27 16:34–16:45 (six consecutive iterations) repeats, e.g.:
  `"The audit findings are mostly already declared (#3195/#3197 both live in
  re-lapped-same-prs…)"`. Both PRs merged four days earlier; the introspect that
  emits this is the one priced SDK call in `contemplate()`.

- **The settled-work detectors re-fire relentlessly.** Over all 239 audit cycles
  in `audit_history.jsonl` (window 2026-06-24 → 2026-07-27), `rules_fired`
  counts: `phantom_remediation` 142, `orphaned_cure` 115, `phantom_resolution`
  77, `repeated_target_attempt` 75. In the trailing 60 cycles alone,
  `repeated_target_attempt` fired 49 times and `phantom_remediation` 19. These
  rules are detection-only — they surface the residue, they do not clear it.

- **The task path is *already* protected; the record path is not.** Of 222
  `task_history.jsonl` records (same window), 90 are `Remediate PR #N` laps
  across 42 PRs, yet cross-referencing each lap's timestamp against the PR's
  `mergedAt` shows ~0 laps *after* merge — the dequeue gates catch the task path.
  The un-cleared records are what keep leaking settled PRs back into task
  generation, contemplation, and audit. Outcome mix over the window: 146
  `partial`, 74 `success`, 2 `failure` — the loop is dominated by PR servicing.

- **PR servicing crowds out everything else.** `gate_activations.jsonl` (window
  2026-06-24 → 2026-07-27): `wip_primacy` blocked discretionary generation 133
  times; `cost_risk` 517.

- **The whack-a-mole is measurable in the config surface and the commit log.**
  Of `maestro/config.py`'s 84 `*_enabled` flags, ~33 fall in the
  settlement/remediation class by a noun match over the flag names
  (`phantom_remediation_enabled`, `settlement_reconciler_enabled`,
  `orphaned_cure_enabled`, `session_landing_enabled`, `obligation_fork_floor_enabled`,
  … ). And `git log --since=2026-06-20 --format="%s"` (commit *subjects* only,
  avoiding this repo's documented git-log measurement trap) has ~150 subjects
  matching the class's nouns (settle / merge-truth / phantom / remediation / tend
  / handoff / awaiting / push-landing). Neither figure is load-bearing — the
  class's persistence across dozens of point-fixes, not its exact count,
  motivates the design.

## Prior Point Fixes (whack-a-mole ledger)

Each row is a prior change aimed at this subsystem, and why the "records outlive
the merge" class survived it. Status per `docs/proposals/README.md` tables.

| Fix | What it patched | Why the class survived |
|---|---|---|
| **070** (shipped) — `PrRemediationState`, dequeue-gate table | Named the remediation lifecycle and ordered the dequeue gates | Lifecycle + queue eviction only; no on-merge sweep of persistent stores |
| **080** (shipped) — merge oracle + lap budget + `clear_standdown` | One host-first merge oracle; delivers one handoff then stops; clears the escalation standdown on a merged/closed verdict | Clears **only** the escalation-standdown store |
| **087** (shipped) — `is_pr_settled` chokepoint | One provenance-stamped merge-truth **read**, AST-enforced sole door | READ only; audit rules exempted by design; reconciles nothing already written |
| **088 / 090** (shipped / partial) — reality-refutation + `prune_resolved_standdowns` | Auto-releases a PR's escalation standdown when `is_pr_settled().settled` | Escalation-handoff store only |
| **091** (shipped) — retire "awaiting tend push" costume | Releases a PR's stand-down + escalation standdown on a **user message** | User-message / retirement axis, not on-merge |
| **097** (shipped) — `reconcile_settled_verdicts` + `settled_verdicts.jsonl` | Binds trailing remediation verdicts to `is_pr_settled`; phantom rules gain a `settled_prs` pre-filter | Audit read-back **only**; one consumer; the settle upsert is window-bound (needs a live `Remediate PR #N` record) |
| **098** (shipped) — `classify_blocker_ownership` | Single owned/external partition reduces deferral-verdict **generation** | Reduces what gets written, clears nothing on merge |
| **099** (open) — base-advance re-entry | Re-keys stall/cooldown on `(pr_head_sha, base_head_sha)` | Base-advance axis, not settlement |
| **Self-committed torrent** — "Green-ready handoff arm", "Push-discharged handoff routing", "Obligation-fork verdict floor", "Version-archive corroborated settlement", "§5-push delivery-truth", "Squash-merge-aware settlement", "Same-turn push-landing verifier" | Each fixes one *shape* of stale keyed state | Each is a per-shape point-fix; none is a general on-settlement record sweep |

**What the design retires / subsumes:** the driver registers 080's
`clear_standdown` path, 088/090's `prune_resolved_standdowns`, `PRLedger.prune`,
and 097's `reconcile_settled_verdicts` as first-class registry entries running
under one provenance-stamped owner; it makes 097's settled-set *total* (no longer
window-bound), closing the re-fire gap; and it adds the three missing store
reconcilers. The AST chokepoint test then forbids any *new* scattered
on-settlement clear — the retirement of the pattern that produced this ledger.

## Relevant Findings

- `maestro/pr_facts.py` (`observe_pr_merge_truth`, `MergeTruth`) and
  `maestro/agent.py` (`is_pr_settled`) — proposal 087's chokepoint is the one
  provenance-stamped settlement *signal*; the new owner consumes it and adds
  nothing rival.
- `maestro/audit_checks.py` (`reconcile_settled_verdicts`, `load_settled_prs`,
  `_SETTLED_VERDICTS_LEDGER`) — the reconciler *shape* to mirror. It upserts a
  settle row only for a PR named by a trailing `Remediate PR #N` record, and its
  returned `frozenset` feeds exactly one place: the `settled_prs` pre-filter of
  the phantom rules. It clears no other store.
- `maestro/audit_checks.py` (`check_phantom_remediation`, `check_orphaned_cure`,
  `check_phantom_resolution`, `check_repeated_target_attempt`) — all
  detection-only; each returns `list[AuditFinding]` and mutates no PR-keyed
  record. "Self-extinguishing" means the rule stops *firing*, not that the
  underlying record is evicted.
- `maestro/consciousness.py` (`SelfModel.next_task_directive`,
  `SelfModel.open_questions`, `SelfModel.limitations_known`,
  `surface_limitations_as_questions`) — the self-model has **no** merge/settled
  awareness. `next_task_directive` is a one-shot slot consumed by the next
  curation pass regardless of the cited PR's state; `[audit]`/`[limitation]`
  rows are removed only by LLM judgment or cap eviction.
- `maestro/remediation_tracker.py` (`RemediationTracker.reset`,
  `RemediationTracker.record_outcome`, `record_skip`) — `reset(pr_number)`
  clears all four axes but fires only on **branch-state change**, never on
  merge. A merged PR's entry is never evicted; it simply stops being enumerated.
- `maestro/remediation_tracker.py` (`record_stand_down`, `release_stand_downs`,
  `find_stand_down`) — `stand_down_verdicts.jsonl` rows are voided only by an
  exact `(pr, head_sha)` SHA-miss or by a human-demand `release_stand_downs`
  call; append-only, no on-merge release.
- `maestro/escalation_bridge.py` (`prune_resolved_standdowns`,
  `prune_expired_standdowns`, `clear_standdown`) — the escalation-handoff store
  *does* auto-release on `is_pr_settled().settled` each tend pass. This is the
  reconciler pattern the design generalizes to the other stores.
- `maestro/evolution.py` (`check_merged_plans`, `tend_evolution_prs`) —
  `check_merged_plans` is the *only* code that reacts to a merge event, and it
  mutates exactly one record class (plan status via `update_plan_status`). It is
  the on-merge write site the driver will be invoked from.
- `maestro/pr_ledger.py` (`PRLedger.prune`) — `prune(keep)` clears the PR ledger
  for PRs absent from the open set; `evolution.py` only instantiates the ledger
  and calls it. This is the second on-merge clearer the driver will wrap /
  register.
- `tests/test_merge_truth_chokepoint.py` — the AST-enforcement house pattern the
  new `tests/test_settlement_record_reconciler_chokepoint.py` mirrors: a golden test
  asserting a single door.

## Implementation Plan

1. [x] **Settlement-reconciler owner, registry, and provenance ledger.** New
   `maestro/settlement_record_reconciler.py`; `maestro/config.py`,
   `maestro/toml_config.py`, `maestro.toml`. Add a
   `SettlementReconciler` protocol (`name`, `reconcile(data_dir, pr_number,
   truth) -> ReconcileResult`) and a `reconcile_pr_settlement(data_dir,
   pr_number, truth, registry)` driver that runs each registered reconciler once
   and appends one `{pr_number, reconciler, cleared, at_iteration}` row per store
   to `settlement_reconciliations.jsonl`. Registry starts **empty** (pure no-op).
   Gate on `[audit] settlement_record_reconciler_enabled` (default **off**).
   - *Depends on:* none.
   - *Acceptance:* with the flag on and an empty registry, `reconcile_pr_settlement`
     runs, writes zero `cleared` rows, and is idempotent across repeated calls.

2. [x] **Route every merge observation through the driver, wrap existing
   clearers as registrations, and drive the registry recurrently each cycle.**
   `maestro/evolution.py`, `maestro/audit_checks.py`,
   `maestro/settlement_record_reconciler.py`. Invoke `reconcile_pr_settlement`
   from `check_merged_plans` (and the tend pass) for each PR `is_pr_settled`
   reports settled — the merge-*transition* trigger — **and** also drive the
   registry recurrently each audit cycle over the settled set (`load_settled_prs`,
   which step 6 makes total), alongside 097's per-cycle
   `reconcile_settled_verdicts`. Because `check_merged_plans` iterates only
   `implemented`-status plans and the tend pass enumerates only open PRs, neither
   re-observes an *already*-settled PR on a later cycle; the recurring sweep is
   what re-clears a store whose writer re-adds a settled-PR row *after* the merge
   event, rather than leaving it until the next merge. Every reconciler is
   idempotent (Risk Assessment), so the recurring pass is a no-op once a store is
   clean. Register the *existing* on-merge clearers — `PRLedger.prune`,
   `prune_resolved_standdowns`, `reconcile_settled_verdicts` — as the first
   registry entries so they run under the one provenance-stamped owner.
   Behavior-preserving.
   - *Depends on:* 1.
   - *Acceptance:* a merged PR drives all three existing clearers through the
     driver with the same net effect as today, emitting one provenance row per
     store; and a PR still in the settled set on a later cycle re-drives every
     registered reconciler (an idempotent no-op when already clean), not only at
     the merge transition.

3. [x] **Self-model settlement reconciler.** `maestro/consciousness.py`,
   `maestro/settlement_record_reconciler.py`. Add `SelfModel.reconcile_settled_pr(pr,
   truth)` that (a) evicts `[audit]` / `[limitation]` `open_questions` and
   `limitations_known` rows whose **sole** cited PR is settled and (b) clears
   `next_task_directive` when it names only settled PRs; register it. Multi-PR
   rows and rows citing an unsettled PR are preserved.
   - *Depends on:* 1, 2.
   - *Acceptance:* given a self-model with a `[limitation] … #3195` row and a
     directive naming only #3195, one reconcile pass with #3195 settled removes
     exactly that row and clears the directive, leaving unsettled-PR rows intact.

4. [x] **Remediation-tracker settlement reconciler.** `maestro/remediation_tracker.py`,
   `maestro/settlement_record_reconciler.py`. Add `RemediationTracker.evict_settled(pr)`
   purging all four axes (`_data`, `_last_items`, `_skips`, `_merge_block_skips`)
   for a settled PR; register it.
   - *Depends on:* 1, 2.
   - *Acceptance:* a settled PR's tracker entry is absent from every axis after
     one reconcile pass; an unsettled PR's entry is untouched.

5. [x] **Stand-down-verdict settlement reconciler.** `maestro/remediation_tracker.py`,
   `maestro/settlement_record_reconciler.py`. Append a head-agnostic release row to
   `stand_down_verdicts.jsonl` for a settled PR (mirroring the `release_stand_downs`
   idiom) and register it, so a settled PR's stand-down is voided regardless of
   head SHA.
   - *Depends on:* 1, 2.
   - *Acceptance:* after a reconcile pass on a settled PR, `find_stand_down`
     returns `None` for that PR at any head SHA.

6. [x] **Make the settled-set total.** `maestro/audit_checks.py`,
   `maestro/settlement_record_reconciler.py`. Have the driver upsert a settled PR into
   `settled_verdicts.jsonl` on **any** settlement observation, not only when a
   trailing `Remediate PR #N` record survives the window — closing 097's
   window-bound re-fire gap so all four phantom rules self-extinguish the cycle
   after settlement.
   - *Depends on:* 1, 2.
   - *Acceptance:* a PR settled with no trailing `Remediate` record still enters
     `settled_verdicts.jsonl` and pre-filters `check_phantom_remediation` on the
     next audit cycle.

7. [x] **AST chokepoint test; flip flag default-on.** `tests/structural_guards.py`
   (proposal 109's shared guard registry, in place of a bespoke
   `tests/test_settlement_record_reconciler_chokepoint.py`); `maestro/config.py`,
   `maestro.toml`, `tests/test_config.py`. Added the net-new `_SETTLEMENT_RECORD_GUARD`
   entry asserting that no function calling a PR-keyed settlement-clear primitive
   exists **outside** `reconcile_pr_settlement`'s `RECONCILER_REGISTRY` (the two
   sanctioned non-owner clears in `loop/pr_remediation.py` allowlisted), mirroring
   the merge-truth chokepoint guard. With the reconcilers proven, flipped
   `settlement_record_reconciler_enabled` to default-on. This step retires the
   scattered-clear pattern.
   - *Depends on:* 2, 3, 4, 5, 6.
   - *Acceptance:* the test fails if a new on-settlement PR-keyed mutation is
     added outside the registry; the full suite passes with the flag default-on.

## Testing Strategy

All tests use `mem_fs`, mock `is_pr_settled` / the code host, and touch no real
network, filesystem, subprocess, or `sleep` (CLAUDE.md testing principles).

- **Driver + registry (`tests/test_settlement_record_reconciler.py`).** Empty-registry
  no-op and idempotency (step 1); provenance-ledger row shape and one-row-per-store
  invariant; a reconciler that raises surfaces loudly (no broad swallow) rather
  than silently skipping the rest.
- **Existing-clearer wrapping (step 2).** A merged PR runs `PRLedger.prune`,
  `prune_resolved_standdowns`, and `reconcile_settled_verdicts` via the driver
  with the same net state as calling them directly; a not-settled PR triggers no
  clears.
- **Per-store reconcilers (steps 3–5).** Self-model: sole-PR-settled row evicted,
  multi-PR and unsettled rows preserved, directive cleared only when all cited
  PRs are settled. Remediation tracker: all four axes purged for the settled PR
  only. Stand-down: head-agnostic release voids the verdict at any SHA. Each also
  covers the guard branch (unsettled PR → no mutation).
- **Total settled-set (step 6).** A settled PR with no trailing `Remediate`
  record still enters `settled_verdicts.jsonl`; the retraction path (settle →
  reopen → not-settled) still supersedes, so a reopened PR is not permanently
  suppressed.
- **Deterministic enforcement (step 7).** The `_SETTLEMENT_RECORD_GUARD` entry in
  `tests/structural_guards.py` (driven by `tests/test_structural_guards.py`)
  parses the tree and fails on any on-settlement PR-keyed mutation outside a
  registered reconciler — the golden test that keeps the door single.
- **Config plumbing (steps 1, 7).** `tests/test_config.py` asserts
  `[audit] settlement_record_reconciler_enabled` round-trips through `Config` /
  `toml_config` / `maestro.toml` with its documented default (off through
  step 6, on after step 7), per the repo's 3-file flag pattern.

## Risk Assessment

- **Over-eager eviction of a still-live record.** A PR that closes then reopens,
  or a squash-merge whose `mergedAt` is briefly unreadable, must not have its
  records purged permanently. *Mitigation:* the driver keys solely on
  `is_pr_settled().settled`, whose retraction path (proposal 097) already handles
  settle → retract → re-settle; every reconciler is idempotent and re-runnable;
  the self-model reconciler evicts a row only when its **sole** cited PR is
  settled; and every clear is stamped to `settlement_reconciliations.jsonl` for
  operator audit.
- **Behavior drift while wrapping existing clearers (step 2).** *Mitigation:*
  step 2 is behavior-preserving and lands before any new reconciler; its
  acceptance test pins net-state equality against calling the clearers directly.
- **Rollback posture.** All behavior gates behind
  `settlement_record_reconciler_enabled`, default-off through steps 1–6 and
  flipped on only in step 7; steps are independently mergeable, so an interrupted
  rollout leaves the loop working exactly as today. Reverting the flag restores
  current behavior with no data migration — the provenance ledger is additive and
  never read by a hot path.
- **Falsifiability.** If the design is right: because step 2 drives the registry
  recurrently each cycle over the total settled set — not only at the one merge
  transition — the count of self-model `open_questions` + `limitations_known`
  rows citing a settled PR reaches 0 within one recurring reconcile pass, and a
  row re-added post-merge is re-cleared on the next cycle rather than persisting;
  `next_task_directive` never names a settled-only PR for more than one cycle; and
  `phantom_remediation` / `repeated_target_attempt` cycles citing a settled PR
  fall to ~0 (measurable against the 142 / 75 baselines over 239 cycles above).
  The design has **failed** if settled-PR rows persist in the self-model for more
  than one cycle after settlement, or if new settle-side `*_enabled` flags keep
  landing — evidence the class is still being point-fixed.

## Out of scope

- The **pre-merge** remediation-lap treadmill (a PR lapped repeatedly *before* it
  settles) — owned by proposals 080 (lap budget), 089 (progress-aware accounting),
  and 094 (salvage-before-disengage). This proposal reconciles records *after*
  settlement only.
- The mechanical `tend_evolution_prs` enumeration — it already skips merged PRs
  by construction (open-set only); no change is needed there.
- Generalizing the settlement ledger to **non-PR external truths** (CI, issues) —
  explicitly deferred by proposal 097 and still deferred here.
