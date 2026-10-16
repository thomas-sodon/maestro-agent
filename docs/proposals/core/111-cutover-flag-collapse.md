# 111: Cut-Over Flag Collapse — The Retirement Half of the Config-Flag Lifecycle

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | burn-in:graduated |
| **Affects** | `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro/audit_checks.py`, `maestro/cli.py`, the `EXPERIMENTAL_FLAGS` manifest introduced by proposal 073, new `tests/test_cutover_flag_collapse.py`, and (demonstration collapse) `maestro/evolution.py`, `maestro/memory.py`, `maestro/pr_facts.py`, `tests/test_evolution.py` |
| **What landed** | Steps 1–5. `maestro/flag_lifecycle.py` carries the `CutoverFlag` record and the `CUTOVER_FLAGS` manifest seeded from the three-flag census (`live_work_corpus_enabled` at stage `cutover`; `enable_session_reuse` and `structured_transcripts_enabled` at `retain` with their fallback reasons), plus `find_stage_violations`, which binds each entry's `stage` to whether its `off_path_symbol` is still defined under `maestro/` (a lingering `collapsed` entry, a `collapse_eligible` entry whose off-path is still wired, and any non-`collapsed` entry whose off-path was retired all red the build — 109's subject-existence self-prune idiom). `tests/test_cutover_flag_collapse.py` covers the manifest's structural honesty (`test_manifest_stage_consistency`, `test_stage_violations_negative_control`) and adds a growth budget (`_MAX_CUTOVER_FLAGS`, pinned to the current population) so the manifest cannot itself become an un-pruned ratchet. The registry-completeness guard (step 2, `test_every_default_on_flag_is_classified`) partitions every default-on boolean flag in `maestro.toml` into either the `CUTOVER_FLAGS` manifest or a documented `_MASTER_SWITCH_FLAGS` allowlist and reds the build on any flag in neither, so a new default-on cut-over flag cannot be added without declaring its collapse plan (or being documented as a feature master-switch). Step 3's burn-in report also shipped: `cutover_burn_in_report` (`maestro/flag_lifecycle.py`) joins each `cutover`-stage entry against `measure_gate_triggers` counts to emit burn-in age, a `past_floor` marker at its `review_by`, the measured off-path divergence count, and a `recommend collapse_eligible` suggestion only once past-floor with zero divergences; `cmd_audit_flags` (`maestro/cli.py`) renders it as a Cut-Over Burn-In section (with `--json`), covered by `test_burn_in_report_recommendation_matrix` over the below-floor/aged-clean/aged-noisy/unmeasured cases. Step 5's demonstration collapse also landed (PR #3413): `live_work_corpus_enabled`, its `use_live` off-path and wording ternaries, its five config sites, its `CUTOVER_FLAGS` entry, and the flag-off tests are all removed and the live corpus is unconditional — the net-negative-LOC runbook this mechanism exists to serve. |
| **What is missing** | Step 6 only, deferred behind the burn-in floor. As each remaining cut-over flag ages past its `review_by` with a clean divergence history, it is collapsed one PR at a time via the step-5 runbook (human-approved). No flag is collapse-eligible today: `cutover_burn_in_report` shows every surviving `cutover`-stage entry below its floor, so collapsing any now would be the premature deletion the floor exists to prevent (`242d4475`'s 2-day retirement is the cited antipattern). The floor is carried by `observed_ready_standdown_enabled` (089) and `owned_committed_push_actuator_enabled`, whose `review_by` is 2027-02-28; read `cutover_burn_in_report` over `CUTOVER_FLAGS` for the live floor rather than trusting this date. 113's stage-progression lever was deleted outright earlier and its registry entry dropped; 117's pair has already been collapsed to unconditional behaviour and its registry entries dropped, so it names no surviving target. |
| **Depends on** | 073 (shipped) _(composes with 106, 109)_ |
| **Derived from** | Code + git history via /propose (no live loop telemetry available), 2026-08-07 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Every behaviour change in this codebase is introduced behind a rollback flag
defaulted to match the old behaviour, then cut over to default-on once it proves
out — `docs/proposals/TEMPLATE.md` mandates exactly that introduction discipline
("gate behaviour changes behind a config flag where possible"). But the lifecycle
has no symmetric *retirement* half. Collapsing a cut-over flag — deleting the
dead flag-off path, removing the flag's config sites, deleting the two-branch
tests, and making the new path unconditional — is a discretionary, per-proposal
hand-step with **no burn-in criterion and no owner**. So it happens
inconsistently: proposal 093 retired its flag two days after cut-over (before any
real burn-in), while proposals 092/094/096/097/100/104 flipped their flags on
and simply retained the old path as a "rollback lever" with no scheduled decision
to ever collapse it. This is the config-flag instance of the exact ratchet a
2026-07-30 architecture review named for the AST guards in proposal 109: addition
is cheaper than deletion, and nothing re-evaluates whether a member can be retired.

Proposal 073 builds the *front* half of this lifecycle — a dead-flag reader guard,
a Config↔TOML symmetry guard, and an `EXPERIMENTAL_FLAGS` manifest with a
`review_by` horizon and a `maestro audit-flags` report over `measure_gate_triggers`
fire-rates. It is framed around *default-off experimental* flags and *orphaned*
(reader-less) fields. It does not cover the *default-on cut-over* flag whose
flag-off branch is live old logic: such a flag has readers (so the dead-flag guard
passes) and is not an experiment (so it never enters the experimental manifest).
This proposal — depending on 073 being shipped — extends 073's manifest into a full
staged lifecycle (`introduce → cutover → collapse_eligible → collapsed`), adds the
missing **burn-in floor** and **collapse trigger**, and demonstrates the loop by
collapsing one flag that has already burned in. The end state: a cut-over flag has
exactly one path from "flipped on" to "old path deleted"; neither premature
deletion nor indefinite retention can happen silently; and the discretionary
per-proposal retire-step is subsumed by an explicit, deterministic stage. Net LOC
falls, not rises.

This is a small, sharp surface by design. A consumption-site census (below) finds
only **three** genuine cut-over flags among the ~42 default-on `_enabled` flags —
the other ~40 are feature master-switches whose flag-off branch is an intended
escape hatch, not old logic, and are explicitly **out of scope**. The value here is
not a large one-time deletion; it is turning an implicit, inconsistent process into
a named lifecycle stage so the class cannot re-accrete as the self-evolution track
adds the next cut-over flag.

## Relevant Findings

- `docs/proposals/TEMPLATE.md` and `docs/proposals/README.md` (Implementation Plan
  guidance) mandate the *introduction* side ("gate behaviour changes behind a config
  flag ... default matching current behaviour") with no symmetric retirement mandate;
  the collapse is left to each proposal's discretion.
- `docs/proposals/core/073-config-flag-lifecycle.md` (`EXPERIMENTAL_FLAGS`,
  `maestro audit-flags`) — introduces the flag manifest, the `review_by` horizon, and
  the `measure_gate_triggers` fire-rate pairing, but scopes them to *default-off
  experimental* and *reader-less orphan* flags. A default-on cut-over flag with a
  live off-path is neither, so it falls through both guards.
- `maestro/audit_checks.py` (`measure_gate_triggers`, `log_gate_activation`,
  `MECHANICAL_OUTCOME_AUTHORITY_GATE`, `MECHANICAL_OUTCOME_DIVERGED_ACTION`) — a
  cut-over flag whose off-path is never needed shows a ~0 divergence rate in
  `gate_activations.jsonl`; the burn-in signal a floor would consume already exists.
- `maestro/config.py` (`Config`, `Config.validate`) and `maestro/toml_config.py`
  (`_TOML_TO_CONFIG`, `_deep_merge`) — every retained flag costs five edit sites
  (dataclass field, `validate` type-guard, the from-TOML constructor, the mapping
  row, the TOML default) that only a collapse recovers.
- `maestro/evolution.py` (`submit_for_review`, `use_live`), `maestro/pr_facts.py`
  (`select_live_plans`, `LIVE_STATUSES`), `maestro/memory.py` (`get_live_plans`) —
  `live_work_corpus_enabled` gates the live-work corpus against the older
  `pending_review`-only corpus plus ~6 wording ternaries; the cleanest deletable
  off-path, and proposal 100 (shipped) records the flag as its documented rollback.
- `maestro/agent.py` (`_get_or_create_session`, whose inline `enable_session_reuse`
  check drives the reuse decision) and `maestro/sdk.py` (`configure_sdk_transcripts`)
  — the off-paths of `enable_session_reuse` (the stateless per-call `query()` path)
  and `structured_transcripts_enabled` (raw-`.log` rendering) **double as fallbacks**
  (exception fallback / permanent pre-rollout rendering). Cut-over in shape, but
  *not* collapse targets; the lifecycle must mark them `retain` with a reason.
- `docs/proposals/core/109-structural-guard-registry.md` (`_MAX_STRUCTURAL_GUARDS`,
  `assert_subjects_exist`, the subject-existence retirement trigger) and
  `maestro/audit_checks.py` / `maestro/task_gates.py` (`GATE_CHAIN`) — the house
  precedents for a bounded, self-pruning declarative registry whose entries name the
  subject they protect and are retired when that subject dies. This proposal applies
  that idiom to cut-over flags; proposals 069/070 are the precedent for replacing a
  discretionary accreted process with a named state machine.

## Production Evidence

No live loop telemetry was available at authoring time (no running container; the
only on-disk `data/` is a stale dev tree — `lifecycle.json` at iteration 2, 0 calls,
last written 2026-04-20 — which the live loop does not write to). Evidence is the
source tree and git history, measured as of 2026-08-07.

- **Census (source tree, 2026-08-07).** `maestro.toml` declares **42** default-on
  `_enabled` flags. A consumption-site read classifies **3** as cut-over (flag-off
  runs a distinct older path): `enable_session_reuse` (`[sdk]`, not `_enabled`-suffixed),
  `live_work_corpus_enabled`, `structured_transcripts_enabled`. The remaining **~40**
  are feature master-switches (flag-off = feature disabled), out of scope.
- **Two of the three cut-over flags are not collapse targets.** `enable_session_reuse`'s
  off-path doubles as the `create_session`-failure exception fallback (plus a hard
  `validate` coupling with `sdk_phase_providers`); `structured_transcripts_enabled`'s
  raw-`.log` rendering is a permanent fallback for pre-rollout sessions. Both stay
  wired regardless of the flag.
- **The one genuine target is recently flipped and deliberately retained.** Git
  flip-on date: `live_work_corpus_enabled` 2026-07-28 (~10 days).
  `docs/proposals/core/100-live-work-oracle.md` (shipped) records the off-path as
  the intended rollback: "rollback remains the flag (`live_work_corpus_enabled = false`),
  then `git revert`." So even the cleanest target is intentionally kept — the current
  safely-deletable surface is ~0, which is the point: a lifecycle stage retires each
  flag *when it ages out*, not before.
- **Retention wording is the accretion signal.** 7 of the 44 default-on flag comments
  carry "default on"/"retained"/"rollback" language (`grep` over `maestro.toml`);
  `gate_strip_redirections_enabled`'s comment ends "rollback: flip to false" with no
  scheduled collapse.

## Prior Point Fixes (whack-a-mole ledger)

Every flag collapse in the repo's history is a bespoke, one-flag-at-a-time,
per-proposal hand-step. The class survives because none introduced a reusable
lifecycle stage, a burn-in criterion, or a retirement trigger — so the *next*
cut-over flag starts the cycle over, and the ones nobody remembered to retire
accrete.

| Fix (SHA) | What it patched | Why the class survived |
|---|---|---|
| `dd240f9c` (087) | retired `unified_merge_truth_enabled` once `is_pr_settled` proved sole door | one flag, hand-done; no reusable path |
| `c717d502` (080) | retired `unified_remediation_gate_enabled` "after parity proven" | parity judged by hand, per-flag |
| `242d4475` (093) | retired `owned_pr_branch_writes_enabled` **2 days** after flip-on | no burn-in **floor** — premature-deletion risk |
| `a9f974a7` (095) | retired proposal-095 flags, tend unconditional | per-proposal step; not systematized |
| `6ddb16ef` / `7a2a260f` (098) | retired `owned_push_marker_delivery` flag | next proposal starts over |
| `6c901862` / `f066e7f6` (102) | retired `ci_failure_diagnosis_enabled` + `red_read_oracle_scoped_enabled` | two more hand retirements |
| `3c2f3a2c` (108) | retired the brevity-cap machinery | a whole proposal to retire one mechanism |
| _(none)_ | 092/094/096/097/100/104 flags **retained** as rollback levers | no **ceiling** — indefinite retention |

`316ca137` ("Keep progress-aware lap budget OFF — revert counterproductive default
flip", 2026-07-03) is the counter-evidence that justifies a **floor**, not just a
ceiling: a flip-on reverted because the new path was worse. The rollback lever has
real value *during* burn-in, so the design must forbid collapse before the flag has
aged and shown a clean divergence history — the mistake `242d4475`'s 2-day
retirement courted.

**Retired by this design:** the discretionary per-proposal retire-step (rows 1–7)
becomes the deterministic `collapse_eligible → collapsed` stage; the indefinite-
retention row (row 8) is surfaced by the collapse trigger and the `audit-flags`
report before it can accrete further.

## Implementation Plan

Steps 1–4 are pure additions (manifest data, CI guards, an advisory report) with no
runtime behaviour change, so an interrupted rollout leaves the system working. The
actual deletions (5–6) come only after the mechanism is proven, and each is a single
independently-revertible PR (`git revert` restores the flag and its off-path).

1. [x] **Extend 073's manifest with a cut-over lifecycle stage.** `maestro/flag_lifecycle.py`,
   where 073's shipped `EXPERIMENTAL_FLAGS` manifest lives. Add each default-on cut-over
   flag as an entry carrying `proposal`, `cutover_cycle`, `off_path_symbol`,
   `review_by`, and `stage` ∈ {`cutover`, `collapse_eligible`, `collapsed`, `retain`};
   seed it from the three-flag census, marking `enable_session_reuse` and
   `structured_transcripts_enabled` `retain` with their fallback reason.
   - *Depends on:* 073 (shipped).
   - *Acceptance:* the manifest lists the three cut-over flags; a test asserts each
     entry names a real flag and (for non-`collapsed` entries) a real `off_path_symbol`.
2. [x] **Registry-completeness guard.** `tests/test_cutover_flag_collapse.py`
   (`test_every_default_on_flag_is_classified`) — partition every default-on boolean
   flag in `maestro.toml` into either a cut-over flag registered in `CUTOVER_FLAGS`
   or a documented `_MASTER_SWITCH_FLAGS` master-switch allowlist, and fail on any
   flag in neither, so a new cut-over flag cannot be added without declaring its
   collapse plan.
   - *Depends on:* 1.
   - *Acceptance:* seeding a cut-over flag with no manifest entry fails CI; the current
     tree passes.
3. [x] **Burn-in report (extend `maestro audit-flags`).** `maestro/cli.py`,
   `maestro/audit_checks.py`. For each `cutover`-stage entry print cut-over age
   (cycles), its divergence fire-rate from `measure_gate_triggers` /
   `gate_activations.jsonl`, and a promote-to-`collapse_eligible` recommendation only
   once age ≥ the floor **and** divergences ≈ 0. Advisory; no auto-mutation
   (human-in-the-loop).
   - *Depends on:* 1.
   - *Acceptance:* a `mem_fs` test asserts an age-below-floor entry yields no
     recommendation and an aged, zero-divergence entry does.
4. [x] **Collapse trigger + subject-existence retirement.**
   `tests/test_cutover_flag_collapse.py`. (a) a `collapse_eligible` entry whose
   `off_path_symbol` is still present in `maestro/` → red build (closes indefinite
   retention); (b) a `collapsed` entry whose symbol is gone → the entry must be removed
   (109's subject-existence idiom, so the manifest self-prunes and cannot itself become
   a ratchet).
   - *Depends on:* 1.
   - *Acceptance:* marking an entry `collapse_eligible` with the off-path still wired
     fails CI; a `collapsed` entry with a still-present symbol also fails.
5. [x] **Demonstrate the collapse loop.** `maestro/evolution.py`, `maestro/memory.py`,
   `maestro/pr_facts.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`,
   `tests/test_evolution.py`. For the flag the step-3 report shows past-floor with ~0
   divergences (candidate `live_work_corpus_enabled`, operator-confirmed via 100's
   `LIVE-PLAN-COLLISION` sensor): delete the `use_live=False` branch and wording
   ternaries, make the live corpus unconditional, remove the flag's five config sites,
   delete the flag-off tests, and mark the manifest entry `collapsed`.
   - *Depends on:* 2, 3, 4.
   - *Acceptance:* `live_work_corpus_enabled` no longer exists, the live corpus is
     unconditional, the step-4 guard passes with the entry removed, and the suite is
     green — a net-negative-LOC PR.
6. [ ] **Collapse remaining past-floor flags, one PR each.** As each cut-over flag
   ages past its floor with a clean divergence history, collapse it via the step-5
   runbook, human-approved. These retire the ledger's moles and come only after the
   mechanism is proven.
   - *Depends on:* 5.
   - *Acceptance:* each collapsed flag's off-path, five config sites, and flag-off tests
     are gone and its manifest entry removed.

## Testing Strategy

All guards live in `tests/test_cutover_flag_collapse.py` and read the repo tree on
disk (the pattern of `tests/test_no_line_citations_in_docs.py` and 073's
`tests/test_config_flag_lifecycle.py`), with `mem_fs` for the `audit-flags` activation
read and a **mocked iteration counter** for the floor check so no real clock or
subprocess is used. Negative controls: seed an unregistered default-on cut-over flag
and assert step 2 fires; mark an entry `collapse_eligible` with a live off-path and
assert step 4a fires; mark an entry `collapsed` with the symbol still present and
assert step 4b fires; set an entry's age below the floor and assert step 3 emits no
recommendation. The step-5 demonstration reuses the existing `tests/test_evolution.py`
corpus with the flag-off cases deleted. No SDK, no real network, no real I/O beyond
source reads.

## Risk Assessment

Steps 1–4 are CI + CLI only — no runtime surface, no state-file, no trust-chain change
— so their blast radius is the test suite, and the manifest is advisory: it reports
and gates, it never auto-deletes, keeping collapse a human decision consistent with
the human-in-the-loop evolution invariant. The real risk is in steps 5–6: collapsing a
flag removes a rollback lever, so a latent bug in the new path could no longer be
switched off in place. The burn-in **floor** (collapse only after the flag has aged
past `review_by` with a ~0 divergence rate) plus per-collapse human approval plus
`git revert` as the restore path are the mitigations — precisely the discipline
`242d4475`'s 2-day retirement lacked and `316ca137`'s reverted flip warns about.
Rollback of the mechanism itself is a revert of the guard files; rollback of any
collapse is a revert of that one PR.

The remedy must not become the next ratchet: the manifest is a small, bounded registry
whose subject-existence check (step 4b) forces a `collapsed` entry to be removed, so it
self-prunes the way 109's guard registry does. Falsifiability: the observable metric is
the count of `cutover`-stage entries past their `review_by` with a live off-path, which
the `audit-flags` report surfaces — if the design works it trends toward zero as each
ages out and is collapsed or consciously re-approved. The design has failed if
collapses routinely need reverting (the floor is too low) or if the manifest itself
grows monotonically without entries reaching `collapsed` (the stage is not being
worked).

## Out of scope

The ~40 default-on **master-switch** flags (flag-off disables a feature rather than
running old logic) are not cut-over flags; retiring their escape hatches is a distinct
decision with a different risk profile and is deferred. Grouping `Config` into nested
sub-dataclasses (073's deferred item) and the `pydantic-settings` migration (proposal
106 Phase A) are the config-*mechanism* changes this proposal composes with but does
not perform: 106 makes a *removed* key a load-time error, which cleanly backstops a
collapse's field deletion, but does not decide *when* a cut-over flag may be collapsed —
that decision is this proposal's contribution.
