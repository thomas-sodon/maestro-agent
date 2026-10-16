# 109: Structural-Guard Registry — Consolidate the Chokepoint Guards, Budget Their Growth, and Auto-Flag Orphans

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `tests/test_merge_truth_chokepoint.py`, `tests/test_ci_truth_chokepoint.py`, `tests/test_pr_salvage_chokepoint.py`, `tests/test_wip_primacy_chokepoint.py`, `tests/test_live_work_chokepoint.py`, `tests/test_owned_branch_oracle.py`, `tests/test_session_attachment.py`, `tests/test_provider_session_handle.py`, `tests/test_tend_origin_grounded.py`, `tests/test_blocker_ownership_binding.py`, new `tests/support/structural_guard.py`, new `tests/structural_guards.py`, new `tests/test_structural_guards.py`, `docs/testing.md`, `CLAUDE.md` |
| **What landed** | All eight steps. Steps 1–5 — the shared harness `tests/support/structural_guard.py` (`ChokepointGuard` spec, `line_to_function` / `line_to_innermost_function`, the per-line `exempt_lines` hook, `scan_for_violations`, both synthetic controls, `assert_allowlist_entries_real`, `assert_subjects_exist`, plus the relational capabilities `required_presence` / `scoped_references` / `co_occurrences` / `module_purity` / `partition_co_occurrences`) with its unit test `tests/test_structural_guard_harness.py`; the registry `tests/structural_guards.py` (`CHOKEPOINT_GUARDS`) carrying all thirteen chokepoint-family guards — merge-truth, CI-truth, push-permission, the two WIP entries, live-work, session-attachment, salvage-oracle, the two provider-session-handle entries, origin-grounded, and blocker-ownership — plus the settlement-record entry (proposal 103); the parameterized runner `tests/test_structural_guards.py`; the subject-existence retirement trigger; and the machine-readable-provenance check (`test_provenance_is_present`). Step 6 — all ten bespoke chokepoint-family scan files retired at proven parity; no bespoke chokepoint-family file remains. Step 7 — the `_MAX_STRUCTURAL_GUARDS` population budget (`_population_within_budget`, `test_registry_population_within_budget`, `test_population_budget_bites_past_the_cap`) and the un-registered-skeleton meta-guard (`test_no_unregistered_skeleton_files` / `test_skeleton_allowlist_names_live_carriers`). Step 8 — the four repointed `CLAUDE.md` citations and the `docs/testing.md` "Structural-Guard Registry" how-to. |
| **What is missing** | Nothing — steps 1–8 all landed; the proposal is shipped. |
| **Derived from** | live production data via /propose, 2026-07-30 |

## Description

> Status updated to shipped on 2026-08-10 by /plan-proposal — see [README.md](../README.md#shipped) for the landing record.

The codebase defends its load-bearing invariants with a family of AST **structural guards** — tests that parse `maestro/` source and fail the build when a merge decision, a CI-classification, a salvage verdict, or a WIP directive is made outside its single sanctioned owner. Each guard is genuinely valuable: for a self-modifying agent they stop Maestro from re-introducing a bypass it was told not to make. But the *population* is an accreting one-way ratchet. Every guard is hand-authored per proposal by copy-pasting the previous one; none has ever been retired; and because each pins the *current* structure (defined-only-in-Y, route-through-X, head-of-chain), the guards themselves become part of the cost of every future deletion or refactor. This is a direct contributor to the codebase's monotonic growth — the shape a 2026-07-30 architecture review named as "the gates make addition cheaper than deletion."

This proposal converts that accreting ratchet into a **managed, bounded, self-pruning population** without weakening a single real defense. One shared harness replaces the ten copy-pasted guard skeletons; a declarative registry turns each guard into a data entry with explicit provenance and the subject symbols it protects; a population budget — the same trim-to-add idiom `tests/test_claude_md_brevity.py` already applies to `CLAUDE.md` — makes adding a guard a zero-sum decision (retire one or consciously raise the cap); and a subject-existence check turns the exact `Memory.get_resolution_attempts`-style orphaning (dead surface that survived because nothing re-evaluated it) into a red build that names the guard to retire.

The end state: the merge-truth, CI-truth, salvage, WIP-primacy, live-work, owned-branch-oracle, session-attachment, provider-session-handle, origin-grounded, and blocker-ownership guards each become one registry entry behind one parameterized runner; the ten bespoke files are deleted; the next structural guard cannot be added silently, and the next orphaned one cannot survive silently. Net test LOC falls, not rises. This composes with — and does not depend on — proposal 106, which consolidates the *production* PR-fact oracles these guards sit above; 106 unifies the sources, 109 unifies the guards.

## Relevant Findings

- `tests/test_merge_truth_chokepoint.py` and `tests/test_ci_truth_chokepoint.py` (`_line_to_function`, `_FUNCTION_ALLOWLIST`, `test_guard_flags_a_synthetic_bypass`, `test_allowlist_entries_name_real_functions`) — structurally near-identical: a copied line→function mapper, a `(module, function)` allowlist, a primitive/signature detector, a whole-AST scan asserting no violators, a synthetic-bypass negative control, a synthetic-OK positive control, and a staleness check. The CI-truth module's own docstring says it is "Modeled on `tests/test_merge_truth_chokepoint.py`," which is in turn "Modeled on `tests/test_session_attachment.py`" — the copy-paste lineage is self-documented.
- `_line_to_function` is **verbatim-duplicated across nine test files**: `test_merge_truth_chokepoint`, `test_ci_truth_chokepoint`, `test_pr_salvage_chokepoint`, `test_wip_primacy_chokepoint`, `test_live_work_chokepoint`, `test_owned_branch_oracle`, `test_session_attachment`, `test_blocker_ownership_binding`, and `test_no_silenced_exceptions`. The chokepoint-family guard files total **3,632 lines across ten files**.
- `tests/test_merge_truth_chokepoint.py::test_allowlist_entries_name_real_functions` already asserts that every allowlist entry names a function that still exists — the seed of a retirement trigger, but scoped only to allowlist staleness, not to the guard's *subject* symbol. When a guard's protected subject is deleted, no test notices.
- Each guard was minted by a numbered proposal and carries that provenance **only in module-docstring prose**: `is_pr_settled` (087), `ci_diagnosis` ownership (102), `assess_pr_salvage` (094), `select_task_focus` (092), the live-work accessors (100), the origin-grounded observer (095). There is no machine-readable provenance and no re-evaluation trigger.
- `tests/test_claude_md_brevity.py` (`_MAX_LINES`, `_MAX_WORDS`, `_MAX_BULLET_CHARS`) is the house precedent for a **population budget**: hard caps set just above post-compression size that force a trim-to-add discipline. No equivalent cap governs the structural-guard population. <!-- TODO: tests/test_claude_md_brevity.py retired by proposal 108 (brevity caps removed); no successor -->
- `maestro/audit_checks.py` (the twenty-two-rule registry) and `maestro/task_gates.py` (`GATE_CHAIN`) are the house precedent for a **declarative registry of self-extinguishing checks** dispatched from one place — the pattern this proposal applies to the structural guards, which are instead ten free-standing files.
- `docs/proposals/core/106-infrastructure-consolidation.md` (Phase C) consolidates the parallel *production* PR-fact oracles (`world_state`, `pr_readiness`, `pr_staleness`, `pr_salvage`) into one `pr_facts.py`. It does not touch the test-harness guards; the two proposals are complementary and order-independent.

## Production Evidence

Live data is available at the loop container's bind-mount (`data/lifecycle.json` present); this is nonetheless a codebase-structure concern whose decisive evidence is git history and the source tree, with live data corroborating the growth dynamic.

- **No structural guard has ever been retired.** `git log --diff-filter=D` over `tests/test_*chokepoint*.py`, `tests/test_*_oracle.py`, and `tests/test_session_attachment.py` (full history) returns **zero deletions**. The population is strictly monotonic.
- **The family is accreting, and accelerating.** Birth dates of the ten chokepoint-family guards (`git --diff-filter=A`, full history): `test_session_attachment` 2026-04-30, `test_provider_session_handle` 2026-05-30, `test_merge_truth_chokepoint` 2026-06-24, then **six in the seventeen days from 2026-07-11 to 2026-07-28** (`wip_primacy` 07-11, `owned_branch_oracle` 07-12, `pr_salvage` 07-14, `tend_origin_grounded` 07-17, `blocker_ownership_binding` 07-19, `live_work` 07-26, `ci_truth` 07-28).
- **Codebase-wide, addition beats deletion ~5.5:1.** Over the last 300 commits touching `maestro/*.py` (measured 2026-07-30): 10,200 lines added, 1,866 deleted (net +8,334). Over the last 200 commits, **only 4 (2%) are net-negative in LOC** — deletion essentially never wins.
- **The one recent deletion confirms the orphaning mechanism.** `refactor: remove unused Memory.get_resolution_attempts` (commit `047244e0`, 2026-07-30) removed a 23-line method that had been added to feed an audit rule which later evolved to decide stalled/non-stalled a different way — dead surface that survived until *external* AI review (#1611) flagged it, because nothing in-tree re-evaluated it. Removing it still required deleting 40 lines of tests.
- Live `data/version_archive.jsonl` records **46 merged changes**; `data/evolution_plans.jsonl` shows **380 `obsolete` vs 46 `merged`** plans (measured 2026-07-30) — the same add-cheaper-than-delete accretion visible in the plan queue. Context only; the guard evidence above is the crux.

## Prior Point Fixes (whack-a-mole ledger)

Each row is a proposal that "fixed" the risk of *one* invariant regressing by adding a bespoke AST guard copy-pasted from the previous one. The class — an unbounded, duplicated, un-retired guard population — survived every one because none introduced a shared harness, a budget, or a retirement path.

| Guard (born) | Minted by | What it pins | Why the class survived |
|---|---|---|---|
| `test_session_attachment` (04-30) | session-attach invariant | main-phase SDK calls session-attached | introduced the copied `_line_to_function` skeleton |
| `test_provider_session_handle` (05-30) | 058 | opaque `Session` handle, no vendor import | re-copied the skeleton |
| `test_merge_truth_chokepoint` (06-24) | 087 | `is_pr_settled` sole merge door | re-copied; "Modeled on test_session_attachment" |
| `test_wip_primacy_chokepoint` (07-11) | 092 | `select_task_focus` sole writer, head-of-chain | re-copied |
| `test_owned_branch_oracle` (07-12) | owned-branch oracle | push primitives only via the oracle | re-copied |
| `test_pr_salvage_chokepoint` (07-14) | 094 | `assess_pr_salvage` single-def + consulted everywhere | re-copied |
| `test_tend_origin_grounded` (07-17) | 095 | grounded observer used; retired flags absent | re-copied + absence asserts |
| `test_blocker_ownership_binding` (07-19) | blocker taxonomy | remediation preamble bound to classifier | re-copied |
| `test_live_work_chokepoint` (07-26) | 100 | live-work literals via accessors | re-copied |
| `test_ci_truth_chokepoint` (07-28) | 102 | `ci_diagnosis` sole owner | re-copied; "Modeled on test_merge_truth_chokepoint" |

**The design retires all ten** into registry entries and deletes the bespoke files (Step 6). The co-located *behavioral* tests in those files (e.g. the `is_pr_settled` live-git suite in `test_merge_truth_chokepoint.py`) are not structural guards and are relocated untouched, never deleted.

## Implementation Plan

1. [x] **Build the shared structural-guard harness.** new `tests/support/structural_guard.py` — one `line_to_function`, one `ChokepointGuard` spec dataclass (`name`, `provenance`, `subjects`, `scanned`, `detector`, `allowlist`, `synthetic_bypass`, `synthetic_ok`), and the generic operations `scan_for_violations(spec)`, `assert_flags_synthetic(spec)`, `assert_synthetic_ok(spec)`, `assert_allowlist_entries_real(spec)`, `assert_subjects_exist(spec)`. No registry yet; no existing test changes.
   - *Depends on:* none.
   - *Acceptance:* a unit test drives the harness over a hand-built spec and asserts the scan, both synthetic controls, the allowlist-real check, and the subject-existence check each fire correctly.
2. [x] **Stand up the registry + parameterized runner beside the bespoke guards.** new `tests/structural_guards.py` (the `CHOKEPOINT_GUARDS` list, seeded with the merge-truth and CI-truth entries) and new `tests/test_structural_guards.py` (parameterized over the registry). The bespoke files stay in place, so each invariant is now guarded twice — belt-and-suspenders during migration.
   - *Depends on:* 1.
   - *Acceptance:* a parity test feeds each seeded entry's own `synthetic_bypass` source and asserts the runner flags it, and its `synthetic_ok` source and asserts it does not — reproducing the bespoke guard's verdicts exactly.
3. [x] **Migrate the remaining eight chokepoint-family guards into registry entries.** `tests/structural_guards.py` — add entries for salvage, WIP-primacy, live-work, owned-branch-oracle, session-attachment, provider-session-handle, origin-grounded, and blocker-ownership; bespoke files still present.
   - *Depends on:* 2.
   - *Acceptance:* every chokepoint-family invariant has a registry entry whose `synthetic_bypass` control fails and whose `synthetic_ok` control passes.
4. [x] **Add the subject-existence retirement trigger.** `tests/support/structural_guard.py`, `tests/test_structural_guards.py` — assert every entry's `subjects` name symbols present in `maestro/`; on absence, fail with "guard `<name>` protects `<symbol>`, which no longer exists — retire this entry or update its subject." Generalizes `test_allowlist_entries_name_real_functions` from allowlist staleness to the guard's protected subject.
   - *Depends on:* 3.
   - *Acceptance:* an entry whose `subjects` name a deleted symbol makes the runner fail with the retirement message; a negative-control entry with a live subject passes.
5. [x] **Require machine-readable provenance.** `tests/test_structural_guards.py` — assert every entry carries a non-empty `provenance` naming its proposal or incident.
   - *Depends on:* 3.
   - *Acceptance:* an entry with empty `provenance` fails the runner; all migrated entries pass.
6. [x] **Retire the moles: delete the bespoke structural-guard files.** Delete the AST-scan portions of the ten files; relocate any co-located behavioral tests (notably the `is_pr_settled` behavioral suite) into the subject's behavioral test module unchanged.
   - *Depends on:* 2, 3, 4, 5.
   - *Acceptance:* the ten bespoke chokepoint-family scan files are gone and `grep -rl _line_to_function tests/ --exclude test_no_silenced_exceptions.py` returns only `tests/support/structural_guard.py` — the out-of-scope whole-tree exception scan (`test_no_silenced_exceptions.py`) keeps its own `_line_to_function` copy by design and is excluded from the grep; the full suite passes at 100% coverage. (`test_provider_session_handle` and `test_tend_origin_grounded` carry no `_line_to_function`, so their retirement is proven by their Step-3 registry-entry parity controls, not this grep.)
7. [x] **Add the population budget and the meta-guard.** `tests/test_structural_guards.py` — a `_MAX_STRUCTURAL_GUARDS` cap set just above the migrated count (current + 1), asserted against `len(CHOKEPOINT_GUARDS)`; plus a meta-guard asserting no standalone AST-scanning structural-guard test file exists outside the registry, so the budget cannot be bypassed by writing a new bespoke file.
   - *Depends on:* 6.
   - *Acceptance:* adding an entry past the cap fails until the cap is consciously raised or an entry retired; adding a new standalone `test_*_chokepoint.py`-shaped scan file fails the meta-guard.
8. [x] **Document the discipline and repoint the stale citations.** `docs/testing.md` gains the how-to for a registry entry, the budget, and the retirement trigger. In `CLAUDE.md`, repoint the four invariant lines that cite a now-deleted bespoke file (`tests/test_session_attachment.py`, `tests/test_merge_truth_chokepoint.py`, `tests/test_ci_truth_chokepoint.py`, `tests/test_pr_salvage_chokepoint.py`) to `tests/test_structural_guards.py` — a word-neutral filename swap, **not** a new line: CLAUDE.md sits at zero line and two words of headroom against `tests/test_claude_md_brevity.py`, so no invariant line is added and none is needed. Add a new invariant line only by pairing it with an offsetting cut. <!-- TODO: tests/test_claude_md_brevity.py retired by proposal 108 (brevity caps removed); the CLAUDE.md line/word/bullet caps no longer exist, so this headroom reasoning no longer applies -->
   - *Depends on:* 6, 7.
   - *Acceptance:* `docs/testing.md` describes the registry, budget, and retirement trigger; no `CLAUDE.md` invariant line still names a deleted guard file; `tests/test_no_line_citations_in_docs.py` passes.

## Testing Strategy

- **Harness unit tests** (Step 1) exercise `scan_for_violations`, both synthetic controls, `assert_allowlist_entries_real`, and `assert_subjects_exist` over hand-built specs whose source is in-memory strings parsed with `ast.parse` — no disk, no network, deterministic. Reading `maestro/` source under guard follows the existing pattern (`path.read_text`) and is the guards' purpose, not test-authored I/O.
- **Parity tests** (Steps 2–3) are the correctness floor for the migration: each registry entry carries its own `synthetic_bypass` (must flag) and `synthetic_ok` (must not) — the same two controls each bespoke guard ships today — so a subtly weaker entry is caught before the bespoke file is deleted.
- **Retirement-trigger tests** (Step 4) assert the subject-existence check fails on a nonexistent subject and passes on a live one — proving orphaning becomes a red build.
- **Budget + meta-guard tests** (Step 7) assert the cap bites past the limit and the meta-guard rejects a standalone scan file outside the registry.
- Whole-tree AST scans keep the component runtime budget (`@pytest.mark.timeout(15)`) the existing guards already use. All tests honour the project rules: no real network, no real `sleep`, no subprocess, `mem_fs` where any file write is involved.

## Risk Assessment

- **Parity gap — a registry entry silently weaker than the bespoke guard it replaces.** The gravest risk, because these guards defend the self-modifying agent's trust invariants. Mitigated by the belt-and-suspenders migration (Steps 2–3 keep both live), the per-entry `synthetic_bypass`/`synthetic_ok` parity controls, and deleting a bespoke file (Step 6) only after its entry is proven. The meta-guard (Step 7) then proves no invariant is left with *zero* enforcement.
- **The budget becomes a rubber stamp.** A cap can be reflexively raised. Mitigated because a cap bump is a visible one-line diff in a reviewed PR and each new entry requires a non-empty `provenance`; more importantly the retirement trigger keeps surfacing orphans, so the population is pushed down as well as capped. This is a social control, acknowledged as such.
- **Over-consolidation hurts readability.** One harness is less self-describing than ten narrated files. Mitigated by keeping each entry's docstring, provenance, and synthetic controls as a readable unit; only the copy-pasted skeleton is removed.
- **Trust-chain surface.** This edits the harness that enforces the merge, CI, salvage, and WIP invariants. The attack surface is a weakened guard. The controls that prove no control is lost are the parity tests, the belt-and-suspenders sequencing, and the meta-guard that forbids un-registered scan files.
- **Rollback.** Pure test infrastructure — no runtime flag, no data migration. Steps are independently mergeable and ordered so an interrupted rollout always leaves every invariant guarded by either the surviving bespoke file or its registry entry, never a gap. Reverting any step restores the prior guarding.

**Falsifiability.** If the design is right: the chokepoint-family test LOC drops materially from ~3,632 toward one harness plus small data entries; adding the next structural guard requires a registry entry and a conscious cap decision rather than a new file; and the next time a guard's subject is deleted, CI goes red naming the entry to retire. The design has failed if authors route around it by adding standalone scan files — which is precisely what the Step 7 meta-guard makes impossible.

## Out of scope

- **Harness-unifying the differently-shaped structural guards** — the content/format caps (`test_claude_md_brevity`, `test_no_line_citations_in_docs`, `test_vps_backup_cron_doc`), the package-isolation guard (`test_review_suite_isolation`), the slab guard (`test_identity_prompt_slabs`), the whole-tree exception scan (`test_no_silenced_exceptions`), and `test_fault_seeding`. They do not share the route-through/defined-only skeleton; the registry and budget are extensible to them, but this proposal unifies only the chokepoint family and registers that family.
- **The production-source oracle consolidation** (proposal 106 Phase C) and **the `maestro/loop/` shim removal** (a separate proposal completing 068). This proposal changes the guards, not the code they guard.
