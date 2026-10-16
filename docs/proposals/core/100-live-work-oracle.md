# 100: Live-Work Oracle — one owner for "is this concern already being worked?"

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | new `maestro/live_work.py`, `maestro/memory.py`, `maestro/evolution.py`, `maestro/agent.py`, `maestro/remediation_tracker.py`, `maestro/audit_checks.py`, `maestro/pr_salvage.py` (docstring), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, new `tests/test_live_work.py`, new `tests/test_live_work_chokepoint.py`, `tests/test_memory.py`, `tests/test_evolution.py`, `tests/test_agent.py`, `tests/test_remediation_tracker.py`, `tests/test_config.py` |
| **What landed** | All eight steps (1–8). Step 1 (PR #3198) — `maestro/live_work.py`: the frozen `LivePlan` dataclass (`plan_id`/`status`/`title`/`rationale`/`pr_url`/`pr_number`) and the pure `select_live_plans(plans)` oracle returning only the three live statuses (`pending_review`/`approved`/`implemented`), each provenance-stamped, plus `tests/test_live_work.py`. Step 2 (PR #3198) — `Memory.get_live_plans()` (`maestro/memory.py`) delegating to `select_live_plans(self._load_all_plans())`, and the deletion of `is_plan_restatement`'s internal `if plan.status != "pending_review": continue` re-filter and the docstring paragraph codifying that exclusion, so the predicate now compares exactly the corpus it is handed. Step 3 — the `[loop] live_work_corpus_enabled` flag (`maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`, default `false`), the `LIVE_STATUSES` tuple (the single named owner) and `Memory.get_live_plan_records()` (the `EvolutionPlan`-typed sibling of `get_live_plans()` the guards consume), and the `use_live` corpus switch routing `find_duplicate_pending_plan`, `find_theme_cluster_pending_plans`, `find_pending_plan_cluster` and the backlog-saturation counter (`select_live_plans(all_plans_full)`) through the live-work corpus, threaded from `submit_for_review`. Flag off is byte-identical to today. Step 4 (PR #3218) — `tests/test_live_work_chokepoint.py`, the AST chokepoint asserting no module outside `maestro/live_work.py` open-codes an in-flight plan-status corpus filter, with the design-retained sites allowlisted by name. Step 5 (PR #3224) — `_pr_disposability` (`maestro/agent.py`) now builds its sibling corpus from `get_live_plans()`, making `approved` siblings visible to `classify_pr_disposability` and retiring the open-coded `status != "implemented" or not pr_url` filter. Step 6 (PR #3228) — `evolve()`'s reflect/propose/submit tail is gated on `_wip_owns_slot()` (placed before `reflect`), standing the third self-initiated minter down under owned WIP. Step 7 (PR #3236) — `measure_live_plan_collisions` (`maestro/remediation_tracker.py`) and the `LIVE-PLAN-COLLISION` line in `format_known_state_summary` (`maestro/audit_checks.py`). Step 8 — the default flip: `live_work_corpus_enabled = true` in `maestro.toml` and the `Config` field default (`maestro/config.py`), with `tests/test_config.py`'s default/committed-TOML/base-parse assertions re-pinned to the on default and the overlay round-trip reversed to overlay `false`; `tests/test_evolution.py`'s pending-only cluster-wording test pins the flag `false` to keep exercising that branch. The operational precondition — step 7's `LIVE-PLAN-COLLISION` sensor reading low across a full week of live iterations — was operator-judged satisfied before the flip; setting the flag `false` restores the `pending_review`-only corpus exactly. |
| **What is missing** | Nothing — fully shipped. Step 8's default flip is applied and the operational precondition (the `LIVE-PLAN-COLLISION` sensor reading low across a full week of live iterations) was operator-judged satisfied; rollback remains the flag (`live_work_corpus_enabled = false`), then `git revert`. |
| **Depends on** | 092 _(shipped — step 6 builds on 092's `_wip_owns_slot()` WIP-primacy stand-down)_, 094 |
| **Derived from** | live production data via /propose, 2026-07-20 |

## Description

Maestro mints new evolution plans for concerns that already have live work. Six
independent guards run at plan-submission time to prevent exactly this, and all
six missed the same duplicate: a plan minted at 13:36 on 2026-07-20 restating
work already sitting in open PR #3145, minted seven hours earlier the same day.
This proposal gives the contested fact — *what work is live right now?* — a
single owner, and routes every guard through it.

The guards did not miss it by a narrow margin of judgment. They missed it
because their corpus is defined by exclusion rather than by the question being
asked. Four of the six compare only against `pending_review` plans; the other
two are corpus-disjoint from live work by construction — exact-title dedup spans
every status but `rejected`/`failed`/`obsolete` yet compares by equality, and the
rejection cooldown scans only `rejected` plans. The moment
Thomas approves a plan it becomes `approved`, then `implemented` with a PR — and
it leaves every corpus in the codebase. The plans most likely to be duplicated,
because they represent work visibly in progress, are precisely the ones no guard
can see. `approved` plans are in no corpus at all. This is the same shape
proposal 087 retired for merge-truth: a fact re-derived at many call sites, each
with its own filter, none of them owning it.

The end state is `maestro/live_work.py` — a pure, zero-I/O module that answers
"which plans are live, and what backs each one?" — reached through one
`Memory.get_live_plans()` accessor, with an AST test proving no module
open-codes a rival plan-status filter. Every submission guard, the backlog
saturation counter, and the PR-salvage sibling corpus read from it. Separately,
the one self-initiated PR minter that proposal 092's WIP-primacy stand-down did
not enumerate — `evolve()` — is brought under the same `_wip_owns_slot()`
predicate as the other two, so the loop stops minting rival plans while it has
owned work open.

This proposal deliberately does **not** try to solve semantic concern-identity.
The measurements below show why: the two duplicate plans share only 17.5% of
their content words, well under the 0.5 restatement threshold, so no amount of
corpus-widening makes a lexical detector recognize them. The design closes the
class by removing the *opportunity* to mint a rival, not by perfecting the
detector. See `## Out of scope`.

## Production Evidence

All figures from `$DATA_DIR = /opt/maestro/data` (live loop bind-mount),
read-only, on 2026-07-20.

**The reported incident.** `evolution_plans.jsonl` — plan `7eaf90cc0d67`
("Deterministic brevity-cap conflict auto-resolver — dissolve the tend-path
conflict magnet", created 2026-07-20T13:36) restates plan `d524e548d4eb`
("Mechanical reconcile-and-land actuator — deterministic brevity-cap conflicts
land without a session", created 2026-07-20T06:19, PR #3145). *As of the
2026-07-20 read* the first was `approved` and the second `implemented` with PR
#3145 still open — those are the statuses the guard table below is reconstructed
against. Both have since merged (#3152 and #3145 respectively, confirmed
2026-07-21), which sharpens rather than softens the finding: the loop spent two
review-and-merge cycles on one concern.

**Why every guard passed**, reconstructed against the live thresholds in
`maestro.toml` and recomputed with the shipped `detect_restatement` /
`_content_tokens` machinery:

| Guard | Corpus | Value measured | Threshold | Fired? |
|---|---|---|---|---|
| Exact-title dedup | active = all *but* `rejected`/`failed`/`obsolete` | titles differ | equality | no |
| Rejection cooldown | `rejected` plans only | sibling never `rejected` | 7-day window | no |
| `find_duplicate_pending_plan` | `pending_review` only | Jaccard **0.175** | 0.5 | no |
| `find_theme_cluster_pending_plans` | `pending_review` only | containment **0.2625** | 0.35 | no |
| `find_pending_plan_cluster` | `pending_review` only | containment 0.2625 | 0.35 | no |
| Backlog-saturation submit block | `pending_review` count = **19** | 19 | 20 | no |

The in-flight plan was not in any corpus but the first, and the first compares
titles by equality. The rejection cooldown is corpus-disjoint by construction —
it scans `rejected` plans, and the sibling was `implemented` — which is why the
corpus fix below leaves it alone.

**The decisive number.** At the moment the duplicate was minted, counted from
`evolution_plans.jsonl`: `pending_review` = 19, `approved` = 0,
`implemented`-with-open-PR = 1 — **live total = 20**. The saturation submit
block refuses at ≥ 20. Counting *live* plans instead of `pending_review` plans
would have refused this submission outright. The corpus defect alone, with no
change to any threshold or detector, accounts for the reported incident.

**The class, not the instance.** Filtering `evolution_plans.jsonl` for the
brevity-cap concern (title or rationale naming the brevity cap, the conflict
engine, cap headroom, or `tests/test_claude_md_brevity.py`) returns **27
distinct plans**, the earliest 2026-06-25, 24 of them now `obsolete`. Five were
live simultaneously on 2026-07-20: `5371308734ac`, `13fe145067fa`,
`20c857f016d1` (`pending_review`), `d524e548d4eb` (`implemented`, PR #3145) and
`7eaf90cc0d67` (`approved`).

**Minting volume vs. yield.** Plans created since 2026-07-01, by final status
(n = 313): `obsolete` 266, `pending_review` 19, `merged` 14, `rejected` 10,
`failed` 2, `implemented` 1, `approved` 1. **85% of minted plans are
self-retired without ever landing.** On 2026-07-19 and 2026-07-20 alone the loop
minted 42 plans and merged 2.

**Why corpus-widening is necessary but not sufficient.** Recomputing the two
duplicate plans' `files_affected`: the shared entries are `CLAUDE.md` and
`maestro/evolution.py`. `CLAUDE.md` appears in **27 of 27** brevity-concern
plans, so it carries no discriminating information; each plan proposes a
*differently named* new module (`maestro/mechanical_reconcile.py`,
`maestro/brevity_conflict.py`, `maestro/reconcile_actuator.py`). Neither prose
similarity nor file overlap identifies this pair. That is the evidence behind
`## Out of scope`.

## Prior Point Fixes (whack-a-mole ledger)

| Identifier | What it patched | Why the class survived it |
|---|---|---|
| (original) exact-title dedup in `submit_for_review` | byte-identical titles, across all active statuses | the only cross-status guard, and one reworded word defeats it |
| `find_duplicate_pending_plan` / `is_plan_restatement` | pairwise content-word Jaccard vs the pending pile | corpus is `pending_review`-only; `is_plan_restatement` re-filters internally, so even a mixed-status list is dropped |
| `564b64d0` Cluster-aware duplicate-pending-plan guard | sub-threshold themed plans accumulating one at a time | same `pending_review`-only corpus |
| `d925bfd9` Floor directional containment in theme-cluster guard | short novel plans over-matching | tuning of the same pending-only lexical instrument |
| `2da77d70` Mechanical pending-plan cluster sweep | retires clustered backlog after the fact | `pending_review`-only, and post-hoc — the duplicate is already minted |
| `a791eea2` Restatement-chain pending-plan sweep | retires pairwise restatement chains | `pending_review`-only, post-hoc |
| `0d5ae001` Backlog-saturation submit block | refuses new plans while the backlog is saturated | counts `pending_review` only — saw 19, missed the 20th live plan |
| proposal 092 WIP-primacy minter stand-down | defers `_implement_approved_plans` and `propose_task_docs` under owned WIP | enumerates two of the three self-initiated minters; `evolve()` → `propose_plan` is ungated |
| proposal 094 `classify_pr_disposability` sibling corpus | compares an open PR against `implemented` sibling plans | lives in the *salvage/abandon* path, reached only once the duplicate exists; `approved` still invisible; `superseded_by_merged` is hardcoded unknown |

Steps 3, 4 and 6 below retire the corpus half of entries 2–4 and 7 (the guards
keep their detectors but stop owning their corpora), retire entry 9's open-coded
filter outright, and complete entry 8's enumeration. Entries 5 and 6 — the two
post-hoc sweeps — keep their `pending_review` corpus; they retire backlog rather
than prevent minting, so they are out of scope (see `## Out of scope`).

## Relevant Findings

- `maestro/memory.py` (`get_pending_plans`) — the filter is literally
  `p.status == "pending_review"`. It is the corpus for
  `find_duplicate_pending_plan`, `find_theme_cluster_pending_plans`,
  `find_pending_plan_cluster`, `sweep_clustered_pending_plans`,
  `sweep_restatement_pending_plans`, the restatement gate inside
  `save_evolution_plan`, and `select_distinct_obsolete_target` in
  `maestro/lever_capture.py`.
- `maestro/memory.py` (`is_plan_restatement`) — already takes its corpus as a
  parameter (`existing_pending: list[EvolutionPlan]`), but then re-filters it
  with `if plan.status != "pending_review": continue` and codifies that
  exclusion in its docstring ("Filtering happens here (not just at the call
  site) so the predicate is self-contained"). Widening a *caller's* corpus is
  therefore not enough — the internal re-filter silently drops the widened rows.
- `maestro/memory.py` (`get_all_plans`) — an unfiltered passthrough. There is no
  `get_approved_plans`, no `get_implemented_plans`, and no "plans backing an
  open PR" accessor anywhere in `Memory`.
- Open-coded plan-status filters are spread far wider than the guards suggest —
  **24 sites across four modules**, enumerated here because step 4's allowlist
  must classify each one:

  | Module | Sites | Where |
  |---|---|---|
  | `maestro/evolution.py` | 8 | `submit_for_review` ×3 (active-plan complement, `rejected` cooldown corpus, `pending_review` saturation count), `implement` (`not in ("approved", "failed")`), `check_merged_plans`, `tend_evolution_prs` (`implemented` + `pr_url`), `_reconcile_orphan_branches` ×2 |
  | `maestro/agent.py` | 8 | `_implement_approved_plans` ×3 (selection, sort key, dispatch), `_pr_disposability`, `implement_plan`, `_demote_plan_to_failed`, `_on_approve`, `_on_reject` |
  | `maestro/memory.py` | 5 | `get_pending_plans`, `is_plan_restatement`'s internal re-filter, the orphaned-cure sweep's `_retire`, `refine_plan`, `touch_plan_curated` |
  | `maestro/audit_checks.py` | 3 | `check_owned_act_drought` (backlog count), `select_curation_target`, `check_orphaned_cure` |

  Two of these are byte-identical and neither knows about the other:
  `check_merged_plans` and `_pr_disposability` both open-code
  `if plan.status != "implemented" or not plan.pr_url:` — the same corpus
  question answered twice in two modules, which is the duplication this
  proposal's accessor retires.

  Not every site is a similarity corpus, and step 4 must not treat them alike.
  `_implement_approved_plans` in particular is a *lifecycle selector*, not a
  corpus: its predicate is two-status —
  `p.status == "approved" or (p.status == "failed" and p.failure_count <
  max_attempts)` — so an allowlist entry describing it as a `status ==
  "approved"` selection will not match the code it is meant to exempt.
- `maestro/pr_salvage.py` (`classify_pr_disposability`) — its docstring already
  names the gap: the sibling corpus exists to reach "the `implemented` open-PR
  backing plans that `is_plan_restatement`'s `pending_review`-only filter cannot
  see." The workaround was added locally instead of fixing the filter.
- **None of the three re-pointed guards takes its corpus as an argument.** All
  three resolve it internally, so re-pointing each one is an accessor swap
  inside its body — there is no call site to change and no corpus parameter to
  pass:
  - `Memory.find_duplicate_pending_plan(title, problem, threshold)` calls
    `self.get_pending_plans()` and hands the result to `is_plan_restatement`.
  - `Memory.find_theme_cluster_pending_plans(title, description, threshold,
    cluster_fraction)` iterates `self.get_pending_plans()` directly.
  - module-level `find_pending_plan_cluster(memory, new_plan_text, threshold)`
    takes the `Memory` object rather than `self` and iterates
    `memory.get_pending_plans()`.

  The only corpus-as-list function in the family is module-level
  `is_plan_restatement(title, description, existing_pending, threshold)` — and
  it discards the widened rows anyway via the internal re-filter above, which is
  why step 2 must delete that re-filter before step 3's swaps mean anything.
- `maestro/evolution.py` (`submit_for_review`) — holds all six guards;
  `propose_plan` does no similarity checking at all. Guards route through
  `_record_obsolete_duplicate` except the cluster-count stop, which inlines an
  equivalent save/`mark_plan_obsolete`/journal sequence.
- `maestro/agent.py` (`_wip_owns_slot`) — its docstring enumerates its consumers
  as the audit→directive bridge, `_implement_approved_plans`, and
  `propose_task_docs`. `evolve()`, which calls
  `EvolutionEngine.propose_plan` and is the sole minter of evolution plans, is
  absent from that list and from every call site.
- `maestro/types.py` (`PlanStatus`) — seven statuses; `approved` is produced by
  the Telegram and dashboard approval paths and consumed by
  `_implement_approved_plans`, and is referenced by no similarity guard.
- `tests/test_merge_truth_chokepoint.py` — the house AST-enforcement pattern this
  proposal reuses for the corpus chokepoint.

## Implementation Plan

1. [x] **Pure live-work oracle.** new `maestro/live_work.py` — add a frozen
   `LivePlan` dataclass (`plan_id`, `status`, `title`, `rationale`, `pr_url`,
   `pr_number`) and a pure `select_live_plans(plans)` returning every plan whose
   status is `pending_review`, `approved`, or `implemented`, each provenance-
   stamped with the status that admitted it. No I/O, no SDK, no host calls.
   - *Depends on:* none.
   - *Acceptance:* `select_live_plans` returns exactly the three live statuses
     and excludes `merged`/`rejected`/`failed`/`obsolete`, proven over a fixture
     covering all seven `PlanStatus` values.
2. [x] **Single accessor.** `maestro/memory.py` — add `Memory.get_live_plans()`
   delegating to `select_live_plans(self._load_all_plans())`. `is_plan_restatement`
   already takes its corpus as a parameter, so it needs no new one: delete its
   internal `if plan.status != "pending_review": continue` re-filter and the
   docstring paragraph codifying that exclusion as contract, so the caller's
   corpus is honoured exactly as passed. `get_pending_plans` is retained
   unchanged for the display callers (`maestro/cli.py`,
   `maestro/dashboard/routes.py`) and for the two post-hoc sweeps that keep
   their `pending_review` corpus (see `## Out of scope`).
   - *Depends on:* 1.
   - *Acceptance:* `get_live_plans()` returns `approved` and `implemented` plans
     that `get_pending_plans()` omits, and `is_plan_restatement` matches a
     non-`pending_review` plan when one is passed in its corpus — a case that
     returns `None` today for the same input.
3. [x] **Route the submission guards through the oracle.** `maestro/evolution.py`,
   `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` — behind
   `[loop] live_work_corpus_enabled` (default `false`), point
   `find_duplicate_pending_plan`, `find_theme_cluster_pending_plans`,
   `find_pending_plan_cluster` and the backlog-saturation counter at the live
   corpus. (The guards are `EvolutionPlan`-typed and one consumer reads
   `.created_at`, so they route through the `EvolutionPlan`-preserving
   `get_live_plan_records()` sibling of `get_live_plans()` rather than the
   `LivePlan` projection.) Detectors and thresholds are untouched; only the corpus
   changes. Flag off reproduces today's behaviour exactly. As `## Relevant
   Findings` records, all three guards resolve their corpus internally — the
   first two off `self.get_pending_plans()`, the third off the `Memory` object
   it is handed — so each is an accessor swap inside its own body. There is no
   call site to update and no corpus parameter to hunt for; the guards' callers
   in `submit_for_review` are untouched by this step.
   - *Depends on:* 2.
   - *Acceptance:* with the flag on and a fixture reproducing the measured
     2026-07-20 state (19 `pending_review` + 1 `implemented` open-PR plan,
     `pending_plan_submit_block_threshold = 20`), `submit_for_review` refuses the
     candidate; with the flag off it admits it.
4. [x] **AST chokepoint.** new `tests/test_live_work_chokepoint.py` — assert that
   no module outside `maestro/live_work.py` compares an `EvolutionPlan` status
   against `"implemented"`, `"approved"`, or `"pending_review"` to build a
   similarity or backlog corpus, mirroring `tests/test_merge_truth_chokepoint.py`.
   Sites retained by design are allowlisted **by name** with a comment giving the
   reason. Work from the 24-site inventory in `## Relevant Findings` — every site
   there is either retired by steps 3/5 or allowlisted here, and the test should
   fail if a site is neither. The allowlist must include, at minimum:

   - `Memory.get_pending_plans` — retained by step 2 for the display callers and
     the two post-hoc sweeps. Without this entry steps 2, 4 and `## Out of scope`
     are mutually unsatisfiable: step 2 retains a function whose whole body is
     the comparison step 4 forbids.
   - `memory.py`'s orphaned-cure sweep filter (`status != "implemented"`),
     `refine_plan` and `touch_plan_curated` — single-plan lifecycle guards.
   - `_implement_approved_plans` — a lifecycle selector, not a corpus. Allowlist
     it by its **two-status** predicate (`approved`, plus `failed` under the
     retry cap) together with its sort key and dispatch check; an entry written
     against `status == "approved"` alone will not match the code.
   - `evolution.py`'s `check_merged_plans` — the `implemented`-with-`pr_url`
     lifecycle sweep that advances plans to `merged`/`rejected`. Note it reads
     the same predicate step 5 retires from `_pr_disposability`, so the test must
     distinguish the two by name rather than by predicate text.
   - The remaining lifecycle transitions in `agent.py`/`evolution.py`
     (`update_plan_status` paths, `implement`, `_demote_plan_to_failed`, the
     `_on_approve`/`_on_reject` dashboard handlers, the orphan-reset pass), and
     `mark_plan_obsolete`'s `accepted_statuses` guard.
   - *Depends on:* 3.
   - *Acceptance:* the test fails when a rival open-coded in-flight status filter
     is introduced into any non-allowlisted module, proven by an in-memory AST
     fixture rather than by editing a real module.
5. [x] **Retire the open-coded salvage corpus.** `maestro/agent.py`,
   `maestro/pr_salvage.py` — replace `_pr_disposability`'s hand-rolled
   `status != "implemented" or not plan.pr_url` loop with `get_live_plans()`,
   which also makes `approved` siblings visible to `classify_pr_disposability`
   for the first time. `classify_pr_disposability` itself is unchanged.
   - *Depends on:* 4.
   - *Acceptance:* `_pr_disposability` builds its sibling corpus from
     `get_live_plans()` and includes an `approved` sibling plan that the previous
     filter dropped; the chokepoint test in step 4 covers the retired site.
6. [x] **Close the minter gap.** `maestro/agent.py` — gate the plan-minting tail
   of `evolve()` on `_wip_owns_slot()`, so all three self-initiated minters stand
   down under owned WIP. The phase mints nothing and authors no directive; the
   `wip_primacy` gate stays the sole `Finish PR #N` writer. Inert while
   `[loop] wip_primacy_enabled` is off, since `_wip_owns_slot` short-circuits on
   the flag.

   Unlike `_implement_approved_plans` and `propose_task_docs` — which return
   before their bodies — `evolve()` cannot simply return: its steps 1–4 (quality
   gates, `introspect`, purpose evaluation, stage assessment) are not minting and
   must keep running. **Place the gate immediately before step 5's
   `self.evolution.reflect(...)` call, not before step 6's `propose_plan`.**
   `evolution_reflection` has exactly one consumer — `propose_plan` — so gating
   only the minting call would leave a priced SDK round-trip running each
   iteration purely to discard its result.
   - *Depends on:* none.
   - *Acceptance:* with an open own PR carrying a self-clearable blocker,
     `evolve()` calls none of `reflect`, `propose_plan`, or `submit_for_review`,
     and writes no `next_task_directive`, while `introspect` and the stage-
     assessment path still run; with no such PR it proposes as it does today.
7. [x] **Duplicate-live-concern sensor.** `maestro/remediation_tracker.py` — add
   `measure_live_plan_collisions(data_dir, n)` reporting, over the last *n*
   minted plans, how many were admitted while a live plan shared ≥ 2
   non-ubiquitous `files_affected` entries, then surface it as
   `LIVE-PLAN-COLLISION` in `format_known_state_summary` — which lives in
   `maestro/audit_checks.py`, not in `remediation_tracker.py`, so this is a
   two-file step following the house sensor/surfacing split. This is the
   falsifiable readout for the whole proposal.
   - *Depends on:* 3.
   - *Acceptance:* the sensor reports a non-zero collision count over a fixture
     reproducing the five simultaneously-live brevity-cap plans, and zero when
     each plan's `files_affected` are disjoint.
8. [x] **Flip the default.** `maestro.toml` — set `live_work_corpus_enabled = true`
   once step 7's sensor shows the collision count falling across at least one
   full week of live iterations. Rollback is the flag, then `git revert`.
   - *Depends on:* 7.
   - *Acceptance:* the default is `true` and the shipped test suite passes with
     the flag on, including the step-3 refusal test.

## Testing Strategy

All tests use `mem_fs`, mocked hosts, and no real network, subprocess, or
`sleep`, per CLAUDE.md.

- **`tests/test_live_work.py`** (new) — `select_live_plans` over a fixture
  carrying all seven `PlanStatus` values: the three live statuses admitted, the
  four terminal ones excluded, provenance stamped, and an empty input returning
  an empty list. Pure, no fixtures beyond dataclasses.
- **`tests/test_memory.py`** — `get_live_plans()` returns `approved` and
  `implemented` rows that `get_pending_plans()` omits; `is_plan_restatement`
  matches against an injected non-`pending_review` corpus and no longer
  self-filters.
- **`tests/test_evolution.py`** — the step-3 refusal test is the regression
  anchor: a fixture reproducing the measured 2026-07-20 state (19
  `pending_review` plans plus one `implemented` open-PR plan, threshold 20)
  asserts `submit_for_review` refuses with the flag on and admits with it off.
  Guard-by-guard tests confirm each of the four re-pointed guards reads the live
  corpus, and that thresholds and detector outputs are byte-identical to today
  for a pending-only fixture.
- **`tests/test_live_work_chokepoint.py`** (new) — the deterministic AST
  enforcement named in step 4. It parses each `maestro/` module and fails on a
  non-allowlisted in-flight status comparison used to build a corpus. Fault
  injection is an in-memory AST fixture, never a real edit, matching
  `tests/test_merge_truth_chokepoint.py`.
- **`tests/test_agent.py`** — step 5: `_pr_disposability` sources siblings from
  `get_live_plans()` and picks up an `approved` sibling. Step 6: `evolve()`
  stands down under `TendOwnedPR` (no `propose_plan` call, no directive
  written), proposes normally with no owned WIP, and is unaffected while
  `wip_primacy_enabled` is off — including the assertion that `_wip_owns_slot`'s
  `_current_task_focus` probe is never called on the flag-off path.
- **`tests/test_remediation_tracker.py`** — the step-7 sensor over both a
  colliding and a disjoint fixture, plus its `format_known_state_summary`
  surfacing.

## Risk Assessment

**Over-blocking is the main regression risk.** Widening the corpus from
`pending_review` (19 rows measured) to live (20 rows measured) raises every
guard's sensitivity, and the backlog-saturation block in particular becomes
strictly harder to satisfy — a large open-PR backlog could suppress legitimately
novel plans. Mitigations: the change is corpus-only, so thresholds stay
operator-tunable; `pending_plan_submit_block_threshold` can be raised in the same
commit that flips the default; and step 8 gates the flip on a week of the step-7
sensor rather than on a single observation. Rollback is `live_work_corpus_enabled
= false`, which restores today's corpus exactly.

**Step 6 changes loop cadence.** Standing `evolve()`'s minter down under owned
WIP means iterations with a stuck own PR produce no new plans. Given the measured
85% obsolete rate that is the intent, not a cost — but it does mean a
permanently stuck PR would halt plan generation entirely. This is bounded by the
existing WIP machinery rather than by new mechanism: `_wip_owns_slot()` only
reports `True` for *self-clearable* `CONFLICTING`/`THREADS_UNRESOLVED` blockers,
and proposal 092's escalation and stand-down paths already dispose of PRs the
loop cannot clear. Rollback is `wip_primacy_enabled = false`, which also reverts
092's existing stand-downs.

**No trust-chain surface.** Nothing here touches approvals, the sandbox, the
action gate, or any public API. Plans still reach Thomas through the unchanged
`notify_plan_for_review` path with their content hash intact; the change only
refuses *more* submissions, never admits or auto-approves one.

**No data migration.** `EvolutionPlan` gains no field and
`evolution_plans.jsonl` is unchanged; `select_live_plans` is a read-side filter
over records that already exist, so no `ensure_schema()` fingerprint changes and
old records are readable unmodified.

**Steps are independently mergeable.** Steps 1–2 add unreached code; step 3 is
flag-gated off; step 4 is a test; steps 5–7 are behaviour-preserving refactors
and a read-only sensor; step 8 is a one-line default flip. An interrupted
rollout leaves the loop running today's behaviour.

## Out of scope

- **Semantic concern-identity.** The measurements above show the two duplicate
  plans share 17.5% of their content words and no discriminating
  `files_affected` entry. No corpus change makes a lexical detector recognize
  them, and this proposal does not add an LLM-scored similarity call — that
  would put a priced, drifting judgment on the minting hot path. The design
  instead removes the opportunity to mint a rival (steps 3 and 6). A future
  proposal may stamp plans with a durable driver key — the audit finding,
  directive, or focus PR that motivated them — which would be stable across
  rewordings in a way prose is not; that requires a schema change and is
  deliberately deferred.
- **The 27-plan brevity-cap concern itself.** This proposal stops the loop from
  minting rival plans about it; whether the brevity cap should be restructured
  is the subject of PR #3145 and its predecessors, not of this document.
- **`superseded_by_merged`.** `classify_pr_disposability` still receives it as
  unknown. Precise merged-sibling detection remains the future work proposal 094
  named, though step 5 makes the `merged` corpus reachable if a later proposal
  wants it.
- **The two post-hoc sweeps.** `sweep_clustered_pending_plans` and
  `sweep_restatement_pending_plans` keep their `pending_review` corpus. The
  reason is scope, not capability: they retire backlog *after* the duplicate is
  already minted, whereas this proposal's whole thesis is prevention at
  submission time. (`mark_plan_obsolete` would not block a widening in any case
  — its `allow_implemented=True` opt-in already accepts
  `{pending_review, implemented}`, and `sweep_orphaned_cures` calls it that way
  today; only `approved` is genuinely refused.) Widening the sweeps is a
  reasonable follow-up once step 7's sensor shows whether post-hoc retirement
  still has work left to do after step 3 lands.
