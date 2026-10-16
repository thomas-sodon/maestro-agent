# 118: Multi-PR Evolution Proposals — Runtime Ownership of Multi-Slice Self-Improvement

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:ready |
| **Affects** | `maestro/memory.py` (`EvolutionPlan`, new `EvolutionProposal` + `evolution_proposals.jsonl` store), `maestro/evolution.py` (`propose_plan`, `submit_for_review`, `check_merged_plans`), `maestro/agent.py` (`evolve`, `_implement_approved_plans`, new `next_eligible_slice` oracle), `maestro/migration.py` (`_TRACKED_CLASSES`), `maestro/cli.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, `tests/test_structural_guards.py`, new `tests/test_evolution_proposals.py` |
| **Depends on** | 059, 087 |
| **Derived from** | code + git history + the proposal corpus via /propose, 2026-08-24 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. Steps 1-6 landed in PR #3604. See [README.md](../README.md#status-key).

Maestro's autonomous evolution unit is atomic: `evolve()` mints exactly one
`EvolutionPlan`, `implement()` binds it to exactly one `maestro/evolve/<plan_id>`
branch and one PR, and `_snapshot_version()` records exactly one
`version_archive.jsonl` row when it merges. There is no runtime entity that
represents *a single self-improvement that must land across several PRs*. Yet a
Thomas-greenlit "multi-phase evolution planning" initiative already recognised
this gap — its phase-1 data model (`EvolutionPlan.parent_plan_id` /
`phase_index`) landed on 2026-07-18 and then stalled, dormant, with no
`EvolutionProposal` record to point at and no code that reads the fields.

This proposal completes that initiative at the structural level. It introduces
`EvolutionProposal` as the single runtime owner of a multi-PR evolution: an
ordered chain of thin, individually-reviewed plan *slices* that share one goal,
implemented one at a time in dependency order, each advancing only after its
predecessor's PR settles. Generation (decompose a too-large intent into ordered
slices) and processing (one-slice-open sequencing) are exactly the two arms
`docs/self-evolution.md` names as "later, separately-approved phases." Human
approval is preserved per slice — the proposal is a sequencing wrapper, never a
merge or approval bypass.

The gap matters now because multi-PR work is the *norm*, not the exception: 67 of
103 proposals with an Implementation Plan carry ≥2 steps, and 13 of the 52 open
proposals are mid-rollout `partial`. Today that body of multi-PR orchestration lives in
human-driven slash commands (`/propose` → `/plan-proposal` → `/impl`) and
hand-curated README "tracks" the loop cannot invoke. When Maestro *itself* wants
a change too big for one PR, it has three bad options and no good one, all forced
by code: cram it into one plan (no splitter exists), have that plan shrunk or
demoted by the scope gate, or hand-split into sibling plans that
`submit_for_review`'s duplicate/cluster guards then mark `obsolete` — because no
guard can tell an intended sequence from a repetition. The end state: Maestro
authors and rolls out its own multi-PR evolutions autonomously, the dormant
slice-linkage fields become load-bearing, and the accreted anti-duplication pile
gains a legitimate structure to defer to instead of policing every related plan.

## Relevant Findings

- `maestro/memory.py` (`EvolutionPlan.parent_plan_id`, `EvolutionPlan.phase_index`)
  — slice-linkage fields commented "data model only, no behavior change"; the
  comment names a "parent `EvolutionProposal`" that **does not exist anywhere in
  the codebase**. The only read is `_parse_plan_line`'s float→int coercion; both
  are excluded from `compute_plan_content_hash`. Nothing sets or branches on them.
- `maestro/agent.py` (`evolve`) — the cadence minter proposes and submits exactly
  one plan per tick (`reflect` → `propose_plan` → `submit_for_review`); there is
  no fan-out and no split path.
- `maestro/evolution.py` (`propose_plan`) — emits one `EvolutionPlan`; its JSON
  schema has no slice/parent/sibling field. The only cross-artifact pointer is
  `source_task_id`, a base-branch *reuse* hint validated to one kept task branch,
  not a chain. The prompt pushes MINIMALISM ("smallest change that works"), and
  `_PLAN_CHANGE_ITEM_MAX_CHARS` caps each change item.
- `maestro/evolution.py` (`submit_for_review`) — a stack of anti-duplication
  guards (`is_source_task_recited`, `find_duplicate_pending_plan`,
  `find_theme_cluster_pending_plans`, `find_pending_plan_cluster`, exact-title
  dedup, rejection cooldown, and the `pending_plan_submit_block_threshold`
  saturation block). None consult `parent_plan_id`/`phase_index`. In particular
  `is_source_task_recited` *refuses* a second plan that re-cites a base branch a
  live plan already claims — the most direct anti-multi-PR mechanism.
- `maestro/consciousness.py` (`detect_restatement`, `RESTATEMENT_THRESHOLD`) — the
  single content-word Jaccard engine every plan-dedup guard reuses, comparing
  `title`+`rationale`. Two legitimately sequential slices that share vocabulary
  are indistinguishable from duplicates to this engine.
- `maestro/agent.py` (`_implement_approved_plans`) — implements each eligible plan
  independently, capped by `max_implementations_per_iteration` and
  `max_open_evolution_prs`; there is no ordering by "predecessor merged first."
- `maestro/planner.py` (`check_plan_scope_detailed`) — a multi-*file* gate, not a
  multi-*PR* one: it passes an arbitrarily large plan as long as each non-doc file
  carries verification language, and never decomposes.
- `maestro/agent.py` (`_replan_for_scope`) — on first scope rejection, one bounded
  rewrite that preserves the same file count and change count; if it still fails,
  the plan is demoted to `failed`. It explicitly does not split into siblings.
- `maestro/agent.py` (`_build_execution_groups`) and `maestro/memory.py`
  (`PlanStep.depends_on`) — the step dependency DAG runs only inside
  `MaestroAgent.run` on a single task branch producing one `TaskRecord`.
  `EvolutionPlan` holds `proposed_changes: list[str]`, not `PlanStep`s; the
  synthetic steps `implement_plan` builds exist only to feed the scope gate.
  Nothing in the DAG spans multiple PRs.
- `maestro/vcs.py` (`evolution_branch`), `maestro/evolution.py`
  (`check_merged_plans`), `maestro/agent.py` (`_snapshot_version`, `VersionRecord`)
  — one plan_id → one `maestro/evolve/<plan_id>` branch → one PR → one merge
  verdict → one archive row. `VersionRecord` has no parent/proposal/phase field.
- `maestro/agent.py` (`is_pr_settled`) — the AST-enforced sole merge-truth door
  (proposal 087). The sequencing oracle this proposal adds binds a slice's
  readiness to its predecessor's `is_pr_settled` read, reusing that chokepoint
  rather than inventing a rival merge check.
- `maestro/migration.py` (`_TRACKED_CLASSES`) — schema-tracked classes get a
  startup fingerprint check and backfill; a new `EvolutionProposal` store joins
  here so its file migrates like `evolution_plans.jsonl`.
- `maestro/flag_lifecycle.py` (`EXPERIMENTAL_FLAGS`, `ExperimentalFlag`) — every
  opt-in flag registers a dated `review_by` horizon guarded by
  `test_no_experimental_flag_is_overdue` (proposal 073); the new flag joins here.
- `tests/test_structural_guards.py` (`ChokepointGuard`) — the AST-enforced
  single-owner registry (population budget bumped per proposal) that the
  sequencing oracle's enforcement test joins.
- `docs/proposals/core/059-structured-proposal-steps.md` — gave *doc* proposals
  ordered checkbox steps but its Out-of-scope explicitly defers "a parser that
  materializes proposal steps directly into `PlanStep` objects" and "cross-proposal
  'track' structuring." This proposal is the runtime counterpart 059 deferred.
- `docs/self-evolution.md` ("Multi-phase planning slice-linkage") — states the
  intended design directly: the fields are "the *data-model foundation* for
  multi-phase planning … the chain-emission (`propose_plan()`) and one-slice-open
  sequencing (`_implement_approved_plans`) arms land in later, separately-approved
  phases."

## Implementation Plan

Ordered so an interrupted rollout leaves the tree working at every boundary:
Step 1 is an inert data model; Step 2 a pure predicate; Steps 3–4 gate all
behavior behind a default-off flag; Step 5 is read-only; Step 6 retires the
subsumed workarounds only after the replacement is proven; Steps 7–8 are the
human-gated graduation and decommission, deferred behind the burn-in observation
window.

1. [x] **`EvolutionProposal` entity + tracked store.** `maestro/memory.py`,
   `maestro/migration.py` — define the `EvolutionProposal` dataclass
   (`proposal_id`, `title`, `goal`, `status` ∈ {`slicing`, `rolling_out`,
   `complete`, `abandoned`}, `created_at`) and an `evolution_proposals.jsonl`
   store mirroring `save_evolution_plan`'s atomic-append persistence; slice
   content stays in the plan store (a proposal's slices are its `EvolutionPlan`s
   filtered by `parent_plan_id`, ordered by `phase_index`). Register the class in
   `_TRACKED_CLASSES` so `ensure_schema` migrates the new file.
   - *Depends on:* none.
   - *Acceptance:* a saved `EvolutionProposal` round-trips through
     `evolution_proposals.jsonl`, and a fingerprint change backfills the new
     tracked store on startup — data model only, no loop behavior reads it yet.
2. [x] **Sequencing oracle — the single door.** `maestro/agent.py`,
   `tests/test_structural_guards.py` — add `next_eligible_slice(proposal)`, a
   pure predicate returning the lowest-`phase_index` slice plan whose predecessor
   slice's PR is settled via `is_pr_settled` (the head slice when no predecessor
   exists; `None` while a predecessor is unsettled or the proposal is complete),
   and register a `ChokepointGuard` making it the sole site that decides slice
   readiness. No behavior wired to it yet.
   - *Depends on:* 1.
   - *Acceptance:* `next_eligible_slice` returns the head slice with no
     predecessor, the next slice once its predecessor's PR `is_pr_settled` holds,
     and `None` otherwise; the structural guard fails if any other site
     re-derives slice readiness.
3. [x] **Chain emission behind a flag.** `maestro/evolution.py` (`propose_plan`),
   `maestro/agent.py` (`evolve`), `maestro/flag_lifecycle.py`, `maestro.toml` —
   behind `[loop] multi_pr_proposals_enabled` (default off), when a reflection
   yields an intent too large for one PR, mint one `EvolutionProposal` plus an
   ordered set of slice plans (each an `EvolutionPlan` with `parent_plan_id` /
   `phase_index` set) and submit only the phase-0 slice for review; register the
   flag in `EXPERIMENTAL_FLAGS` with a `review_by` horizon. Off → byte-identical
   single-plan behavior.
   - *Depends on:* 1.
   - *Acceptance:* with the flag on, a too-large reflection produces one
     `EvolutionProposal` with N linked slice plans and exactly one `pending_review`
     plan (phase 0); with the flag off, `propose_plan` still emits one standalone
     plan and no proposal record.
4. [x] **Slice-aware submission + one-slice-open implementation.**
   `maestro/evolution.py` (`submit_for_review`), `maestro/agent.py`
   (`_implement_approved_plans`) — exempt two plans that share the *same*
   `parent_plan_id` (naming a real `EvolutionProposal`) from the cross-plan
   dedup/restatement/recited-source guards, since a slice is intentionally related
   to its siblings; and consult `next_eligible_slice` so slice N+1 is implemented
   only after slice N settles, minting and submitting the next slice as each
   predecessor settles, advancing the proposal to `complete` when all slices
   settle. Behind the same flag.
   - *Depends on:* 2, 3.
   - *Acceptance:* two slices of one proposal both reach review/implementation in
     order without either being recorded `obsolete` by a dedup guard, and slice
     N+1 is not implemented until `is_pr_settled` holds for slice N's PR; a plan
     whose `parent_plan_id` names no real proposal earns no exemption.
5. [x] **Rollout surface.** `maestro/cli.py`, `maestro/agent.py` — add a
   read-only `maestro list-proposals` that joins each `EvolutionProposal` to its
   slice plans and their per-slice PR/merge status, and inject a bounded
   (≤200-char) one-line in-flight-rollout summary into the contemplation prompt
   so the loop perceives its own multi-PR state.
   - *Depends on:* 1.
   - *Acceptance:* `maestro list-proposals` prints each proposal's ordered slices
     with per-slice status and the proposal's rollout state; a contemplation line
     (≤200 chars) names any proposal mid-rollout.
6. [x] **Retire the subsumed workarounds.** `maestro/agent.py`
   (`_replan_for_scope`), `maestro/evolution.py` (`submit_for_review`) — once
   Steps 3–4 are proven under the flag, route a genuinely-too-large intent to
   chain emission *before* `_replan_for_scope`'s rewrite-or-fail path, and drop
   the within-initiative firing of `is_source_task_recited` (now covered by the
   parent-link exemption). Cross-proposal dedup and single-PR multi-file scope
   handling are unchanged.
   - *Depends on:* 4.
   - *Acceptance:* a too-large intent produces a sliced `EvolutionProposal`
     instead of a `_replan_for_scope` demotion to `failed`, and
     `is_source_task_recited` no longer records `obsolete` for two slices of the
     same proposal.
7. [ ] **Graduate the flag after burn-in.** `maestro/flag_lifecycle.py`,
   `maestro.toml` — once chain emission and one-slice-open sequencing are proven
   under a production canary, cut `[loop] multi_pr_proposals_enabled` over to its
   target default and move its manifest entry off `EXPERIMENTAL_FLAGS`. Deliberately
   human-gated on burn-in evidence; lands via a separate review PR.
   - *Depends on:* 6.
   - *Acceptance:* multi-PR proposal minting and advancement run without an opt-in,
     and the flag is no longer registered as experimental.
8. [ ] **Decommission the superseded single-plan path.** `maestro/agent.py`,
   `maestro/evolution.py` — once the flag has graduated, remove the flag-off
   branches the sliced path made dead (the fallback routing a genuinely-too-large
   intent to `_replan_for_scope`'s rewrite-or-fail instead of slicing, and the
   within-initiative `is_source_task_recited` firing) and retire the
   `multi_pr_proposals_enabled` flag itself; the general single-file
   `_replan_for_scope` rewrite is unchanged.
   - *Depends on:* 7.
   - *Acceptance:* the subsumed single-plan workarounds are gone and the
     sliced-proposal path runs unconditionally.

## Testing Strategy

All tests use `mem_fs`, mock `is_pr_settled` / the notifier / git, and make no
real network, sleep, or subprocess calls, per CLAUDE.md.

- **Deterministic single-owner enforcement (the named guard).**
  `tests/test_structural_guards.py` — the `ChokepointGuard` for
  `next_eligible_slice` fails synthetically when a second site re-derives slice
  readiness and passes on a clean live scan; the registry population budget is
  bumped by one with provenance naming this proposal.
- **Store round-trip + migration.** `tests/test_evolution_proposals.py` — an
  `EvolutionProposal` round-trips `evolution_proposals.jsonl`; a fingerprint
  change backfills the tracked store; a legacy plan with no slice fields loads as
  `parent_plan_id=None`/`phase_index=0`.
- **Chain emission (both flag branches).** Flag on → a too-large reflection yields
  one proposal, N linked slice plans, exactly one phase-0 `pending_review`; flag
  off → one standalone plan and no proposal record (guards the default path).
- **Parent-link dedup exemption + its negative control.** Two sibling slices
  survive `submit_for_review` where, without the exemption, a
  duplicate/cluster/recited-source guard would record one `obsolete`; a plan
  carrying a *dangling* `parent_plan_id` (naming no real proposal) is denied the
  exemption and still gets deduped — proving the exemption cannot be abused to
  dodge dedup.
- **One-slice-open sequencing.** Slice N+1 is blocked from implementation until a
  mocked `is_pr_settled(slice N)` returns settled; an out-of-order attempt is
  refused by the oracle; the proposal flips to `complete` only when every slice
  settles.
- **Rollout surface.** `maestro list-proposals` renders ordered slices with
  per-slice status; the contemplation line appears for a mid-rollout proposal and
  is absent when none is in flight.

## Risk Assessment

**What could regress.** The blast radius is the evolution-minting and
implementation path. Every behavior change is gated behind
`[loop] multi_pr_proposals_enabled`, default off, so the loop is byte-identical
to today until the flag is flipped. Each slice remains an ordinary
`maestro/evolve/<plan_id>` PR, so Tend, the merge-truth chokepoint, CI diagnosis,
and the version archive are untouched — a slice merges exactly like any plan does.

**Trust boundary (approvals).** The proposal is a *sequencing wrapper*, not an
approval or merge bypass: every slice PR still requires the standard
dashboard/Telegram human approval before `_implement_approved_plans` can act on
it, and `EvolutionProposal` never merges anything itself. The parent-link
exemption is the one new trust-sensitive surface; it keys on two plans sharing
the *same* `parent_plan_id` that names a real `EvolutionProposal` record, and the
dangling-parent negative-control test proves an un-parented plan cannot forge a
link to escape the dedup guards.

**Migration.** One new tracked JSONL store, backfilled by `ensure_schema` exactly
like `evolution_plans.jsonl`; the `parent_plan_id`/`phase_index` fields were
already migrated in phase 1. No existing file schema changes.

**Rollback posture.** Revert Step 6 to restore the retired guards; flip the flag
off to restore single-plan minting; delete `evolution_proposals.jsonl` to drop the
store. Each step is independently revertible and the flag carries a `review_by`
horizon so a never-graduated experiment is surfaced by `maestro audit-flags`.

## What landed

- **Step 1 — `EvolutionProposal` entity + tracked store.** `maestro/memory.py`
  defines the `EvolutionProposal` dataclass
  (`proposal_id`/`title`/`goal`/`status`/`created_at`) plus the
  `evolution_proposals.jsonl` append-only store
  (`write_evolution_proposal`, `_load_all_evolution_proposals`,
  `_save_all_evolution_proposals`, `get_evolution_proposals`) with empty/duplicate
  `proposal_id` guards and corrupted/blank-row skipping; `maestro/types.py` adds
  the `EvolutionProposalStatus` literal; `maestro/migration.py` registers the class
  in `_TRACKED_CLASSES` and the file in `_STATE_FILES`, so `ensure_schema` backfills the new store on a
  fingerprint change (via lazy dataclass normalization, not a per-file migrate
  helper). Data model only — no loop behaviour reads a proposal yet.
  Tests in `tests/test_evolution_proposals.py` / `tests/test_migration.py`.
- **Step 2 — the `next_eligible_slice` sequencing oracle.** `maestro/agent.py`
  adds the module-level `next_eligible_slice(proposal, slices, *, is_settled)`
  pure predicate — the single owner of *"which slice of a multi-slice
  `EvolutionProposal` may be worked next?"*: it returns `None` for a terminal
  (`complete`/`abandoned`) proposal, otherwise walks the slices in ascending
  `phase_index` order and returns the lowest-`phase_index` unsettled slice,
  yielding `None` when every slice has settled or a settled slice sits above an
  unsettled predecessor. Settlement is injected (mirroring
  `check_reconcile_near_miss`'s `is_settled=` seam), so the predicate stays pure
  and unwired — no loop phase calls it yet. `tests/structural_guards.py` adds the
  `next_eligible_slice` `ChokepointGuard` making `agent.py` its sole definition
  site, and `tests/test_structural_guards.py` bumps `_MAX_STRUCTURAL_GUARDS`
  17→18. Behavioural cases in `tests/test_evolution_proposals.py`.
- **Step 3 — chain emission behind `[loop] multi_pr_proposals_enabled`.**
  `maestro/evolution.py` adds `propose_plan_chain` (decomposes a too-large
  reflection into an ordered set of slice specs, mints one `EvolutionProposal`
  with `status="rolling_out"`, and builds N slice `EvolutionPlan`s linked by
  `parent_plan_id` / `phase_index=0..N-1`; a chain of fewer than two valid
  slices collapses to the lone-plan path) and `submit_proposal_chain` (submits
  only the phase-0 head slice through `submit_for_review`, then — only once
  persisted truth confirms the head is live — writes the `EvolutionProposal` and
  persists every downstream slice dormant with the new `"sliced"` status,
  leaving no headless chain behind on a refused head). `maestro/agent.py`
  (`evolve`) routes reflection through `propose_plan_chain` when the flag is on
  and calls `submit_proposal_chain` on a returned tuple, falling back to
  `propose_plan` → `submit_for_review` byte-for-byte when off. A shared
  `_plan_from_spec` helper normalises single-plan and per-slice fields
  identically. `maestro/flag_lifecycle.py` registers
  `multi_pr_proposals_enabled` in `EXPERIMENTAL_FLAGS` with a `review_by`
  horizon; `maestro.toml` / `maestro/config.py` / `maestro/config_settings.py` add
  the default-off key; `maestro/types.py` adds the `"sliced"` `PlanStatus`.
  Tests in `tests/test_evolution.py` / `tests/test_agent.py` /
  `tests/test_config.py` / `tests/test_evolution_proposals.py`. One-slice-open
  sequencing (promoting a held slice as its predecessor settles) remains
  deferred to Step 4.
- **Step 4 — slice-aware submission + one-slice-open implementation.**
  `maestro/memory.py` adds `update_evolution_proposal_status` (a `file_lock`
  read-modify-write mirroring `update_document_record`'s hit-flag shape) and
  `get_slices_for_proposal` (the single accessor returning a proposal's slice
  plans, `parent_plan_id`-filtered and `phase_index`-sorted). `maestro/evolution.py`
  adds the `_sibling_slice_ids` helper and threads a sibling-slice dedup
  exemption through `submit_for_review`: the exact-title, rejection-cooldown,
  `is_source_task_recited`, `find_duplicate_pending_plan`,
  `find_theme_cluster_pending_plans`, and `find_pending_plan_cluster` guards are
  suppressed only when *every* colliding plan is a sibling slice of the same
  `EvolutionProposal` (a collision touching any non-sibling still refuses;
  `find_duplicate_pending_plan` gains an `exclude_ids` corpus filter so its
  first-match-only return cannot let a co-colliding non-sibling slip through),
  and adds `promote_slice` (a `"sliced"`→`pending_review` status flip reusing
  `submit_for_review`'s content-hash + notify tail via the shared
  `_compose_review_message` helper, never its guard stack). `maestro/agent.py`
  adds the `_slice_pr_settled` settlement closure, the `_advance_slice_rollouts`
  driver (promotes the next dormant slice once its predecessor's PR settles and
  advances a fully-settled proposal to `complete`), and a pre-cap
  `_slice_defers_implementation` filter in `_implement_approved_plans` so an
  out-of-order slice defers without consuming a per-iteration cap slot. All
  behind `[loop] multi_pr_proposals_enabled`; off → byte-identical. Tests in
  `tests/test_agent.py` / `tests/test_evolution.py` /
  `tests/test_evolution_proposals.py`.
- **Step 5 — the `maestro list-proposals` rollout surface + the MULTI-PR-ROLLOUT
  contemplation line.** `maestro/cli.py` adds the read-only `cmd_list_proposals`
  (`list-proposals` subcommand) that joins each `EvolutionProposal` to its
  `parent_plan_id`-linked slice plans and renders each slice's `phase_index`,
  status, and PR/merge state alongside the proposal's rollout state — offline, no
  host call. `maestro/stats.py` adds `format_multi_pr_rollout_summary`, a pure,
  self-suppressing MULTI-PR-ROLLOUT line (bounded to `_ROLLOUT_LINE_MAX` chars,
  the LLM title truncated last) naming the first `rolling_out` proposal, its
  landed/total slice count, and the next open slice; `maestro/agent.py`
  (`contemplate`) injects it into the measured-data block only when a proposal is
  mid-rollout. Read-only — no behaviour change beyond the two surfaces. Tests in
  `tests/test_cli.py` / `tests/test_stats.py` / `tests/test_agent_contemplate.py`.
- **Step 6 — retiring the subsumed workarounds.** `maestro/agent.py` adds
  `_slice_scope_rejected_plan`: when the plan-scope gate rejects a plan on its
  first attempt (`failure_count == 0`) and `[loop] multi_pr_proposals_enabled`
  is on, `implement_plan` offers the genuinely-too-large intent to
  `propose_plan_chain` *before* the `_replan_for_scope` rewrite-or-fail
  fallback. On an `(EvolutionProposal, slices)` chain whose head is accepted and
  persisted (`submit_proposal_chain` returns `True`), the original plan is
  retired as `obsolete` — superseded / self-pruned, never demoted to `failed`
  nor fed to rejection learning — via `maestro/memory.py`'s
  `mark_plan_obsolete(..., allow_approved=True)` opt-in (widening the accepted
  status set to `approved`, orthogonal to `allow_implemented`). On a lone plan,
  a refused head (nothing persisted), or a `ProposalParseError`, the helper
  returns `False` and the unchanged rewrite-or-fail path runs, so flag-off
  behaviour stays byte-identical. `submit_proposal_chain` now returns a `bool`
  so the caller retires the original only on a genuinely-minted chain. The
  within-initiative firing of `is_source_task_recited` was already covered by
  Step 4's parent-link sibling exemption; Step 6 adds a regression test locking
  that exemption in isolation (no new guard code). Tests in
  `tests/test_agent.py` / `tests/test_evolution.py` / `tests/test_memory.py`.

## Production Evidence

**No live production data was available for this investigation** — the loop
container's Docker socket was permission-denied and the only on-disk data
directories were a stale April dev instance (iteration 2, no history stores).
Evidence is therefore **code + git history + the proposal corpus**, measured on
2026-08-24. The one thing this leaves unmeasured is the live *rate* at which the
loop mis-refuses a legitimate sequence (that would need `evolution_plans.jsonl`
`obsolete_reason` data); the impact below is inferred from the code paths and the
corpus, not from a live obsolete-rate.

**The stalled initiative (git history).** The phase-1 data model landed and then
went dormant. From commit `056bb599` (2026-07-18), message excerpt:

```
feat(memory): add slice-linkage fields to EvolutionPlan (multi-phase planning, phase 1)

Adds parent_plan_id and phase_index to EvolutionPlan — the data-model
foundation for multi-phase evolution planning (Thomas-greenlit initiative,
msg 432c1921ee87). Phase 1 is data-model only, no behavior change
```

Five-plus weeks later there is still no `EvolutionProposal` class and no reader of
the fields (verified by grep across `maestro/`).

**Multi-PR work is the norm (proposal corpus, 2026-08-24).** Of 103 proposals
(open and shipped) carrying an `## Implementation Plan`, **67 have ≥2 checkbox
steps**; and **13 of the 52 open proposals are `partial`** — mid-rollout across
multiple PRs. The README encodes **4 hand-curated
"tracks"** (security 021–029, artifact-review 030–033, visitor-awareness 034–037,
income 047–052), each an ordered multi-PR rollout sequenced entirely in prose —
"fix them in order so later items can rely on earlier primitives," "each phase
ships behind its own config flag so the track can be … paused at any boundary."
That discipline is exactly what this proposal gives the loop, which today cannot
author or advance any of it.

**The intended design, already written down (`docs/self-evolution.md`).** The
"Multi-phase planning slice-linkage" subsection names the two unbuilt arms:

```
Phase 1 is data model only — no behavior consumes the fields yet; the
chain-emission (propose_plan()) and one-slice-open sequencing
(_implement_approved_plans) arms land in later, separately-approved phases.
```

**The anti-duplication pile that fills the vacuum (`maestro/evolution.py`,
`maestro/memory.py`).** `submit_for_review` runs ~11 guards plus two periodic
sweeps (`sweep_clustered_pending_plans`, `sweep_restatement_pending_plans`), all
built on one Jaccard engine (`detect_restatement`), and **none** consult
`parent_plan_id`/`phase_index`. `is_source_task_recited` refuses a second plan
sharing a base branch outright. The consequence, stated from the code: two
legitimately sequential slices that share vocabulary are caught as duplicates —
the newest kept, the rest landed `obsolete`.

## Prior Point Fixes (whack-a-mole ledger)

Each entry is a mechanism that either fakes multi-PR work through a single-PR
primitive or suppresses the related plans a multi-PR effort would produce. Three-
plus independent mechanisms around one missing structure is the signal for a
structural remedy rather than a twelfth guard.

| Identifier | What it patched | Why the class survived it |
|---|---|---|
| `056bb599` — slice-linkage fields | Added `parent_plan_id`/`phase_index` as the multi-phase data model | Stalled at "data model only"; no `EvolutionProposal` to point at and no consumer, so the linkage never became load-bearing |
| `is_source_task_recited` (`submit_for_review`) | Refuses a 2nd plan re-citing a live plan's `source_task_id` base branch | An intended sequence *reuses* a base branch on purpose; the guard must fire precisely because no sequence representation exists |
| `find_duplicate_pending_plan` / `find_theme_cluster_pending_plans` / `find_pending_plan_cluster` (proposal 100) | Refuse near-restatement / themed clusters of related plans as `obsolete` | Cannot distinguish intended slices from duplicates — they compare only title+rationale Jaccard |
| `sweep_clustered_pending_plans` / `sweep_restatement_pending_plans` | Periodically prune clustered/restated pending plans | A legitimately-sequenced backlog reads as a cluster; newest kept, rest pruned |
| `pending_plan_submit_block_threshold` / `generation_admission` | Cap total in-flight plan production content-agnostically | A multi-slice effort counts against the cap as if its slices were unrelated |
| `check_plan_scope_detailed` + `_replan_for_scope` (proposal 009 lineage) | Force per-file verification or rewrite-at-same-file-count, else demote to `failed` | Never splits a too-large intent — pushes plans smaller instead of representing the whole |
| 059 structured proposal steps (`partial`) | Gave *doc* proposals ordered checkbox steps | Deferred the runtime parser and cross-proposal track structuring in its Out-of-scope; the doc chain has no runtime counterpart |
| README tracks + Recommended Implementation Order | Sequence multi-PR rollouts by hand in prose | Human-only; the loop can neither author nor advance them |

**Retired or subsumed by this design:** row 1 is *completed* (the fields become
load-bearing); rows 2–4 lose their within-initiative firing to the parent-link
exemption (Steps 4, 6); row 6's rewrite-or-fail churn is bypassed for genuinely
large intents, which are sliced upstream (Step 6); and the *autonomous* portion of
rows 7–8 is replaced by Maestro owning its own rollout (Steps 3–5). Rows 2–5 keep
firing across *distinct* proposals and un-parented plans — this design scopes them,
it does not delete them.

## Out of scope

- **Doc↔runtime bridge.** Materializing a `docs/proposals/**` Markdown step list
  (proposal 059's shape) directly into runtime slice plans. The human-authored doc
  corpus and the autonomous runtime chain stay parallel; a bridge is a follow-on.
- **Restructuring the human README tracks** into runtime `EvolutionProposal`s. The
  hand-curated tracks and Recommended Implementation Order remain a human artifact.
- **Cross-proposal collision detection.** The dedup/cluster guards keep firing
  *between* distinct proposals and for un-parented plans; only *within-proposal*
  siblings are exempted.
- **Branching rollout DAGs.** This proposal ships a *linear* chain ordered by
  `phase_index`. A slice graph with parallel branches is a later phase.
- **Enforcing acceptance-criterion quality across slices.** Each slice keeps its
  own `acceptance_criteria`; a proposal-level rollup of criteria is not built here.
