# 082: Surface Acceptance Criteria Across Every Code-Writing Branch and PR Operation

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/evolution.py`, `maestro/loop/pr_remediation.py`, new `maestro/prompts/acceptance.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro/cli.py`, `maestro.toml`, tests |
| **Depends on** | none |
| **Derived from** | Audit of how `EvolutionPlan.acceptance_criteria` threads through the branch/PR lifecycle |

## Description

> Status updated to shipped on 2026-06-21 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

`EvolutionPlan.acceptance_criteria` is the plan's binding success contract — 1–5
testable invariants extracted by `EvolutionEngine.propose_plan` before any code is
written. Today that contract is surfaced unevenly across the lifecycle: it is
*born* at plan time, *enforced* at review time (`_self_review`) and outcome time
(`_assess_criteria_outcome`), and *published* in the PR body and the
remediation prompt — but the two phases that actually **write code**,
`Evolution.implement()` and `Evolution.auto_fix()`, never receive it. The
implementer codes against `proposed_changes` alone and first meets the binding
criteria at self-review, where each unmet criterion becomes a blocking defect
that costs an extra fix round; if a criterion is still unmet at completion the
plan is silently downgraded to `partial` for a contract the writer was never
shown.

This proposal makes the acceptance criteria visible at every operation on a
branch or PR that an LLM performs against a plan, so the writer aims at the
contract from the first commit rather than discovering it during review. It also
collapses the four near-identical, hand-rolled renderings of the criteria block
(self-review, PR body, remediation prompt, and the new implement/auto-fix sites)
into one shared renderer, so the contract reads identically everywhere and a
fifth call site cannot drift.

The end state: `implement()` and `auto_fix()` inject the same binding
criteria block self-review already uses, framed so the writer adds an asserting
test per criterion up front; the criteria render through a single helper; and a
regression test asserts that every code-writing prompt built from a plan carries
its criteria when present. The behavioural change is gated behind one
default-on flag so it is trivially revertible.

## Relevant Findings

- `maestro/evolution.py` (`Evolution.implement()`) — the implementation prompt
  is composed from `plan.title`, `plan.rationale`, `plan.proposed_changes`, and
  `plan.files_affected`; `acceptance_criteria` is **absent**. The writer never
  sees the contract its work will be judged against.
- `maestro/evolution.py` (`Evolution.implement()` source-task rebase path) —
  when `plan.source_task_id` nominates a kept task branch as the base, the
  existing commits were written for the *task*, before this plan's criteria
  existed; the refinement query is exactly where the criteria most need to be
  surfaced, and exactly where they are currently missing.
- `maestro/evolution.py` (`Evolution.auto_fix()` / `_run_auto_fix_query`) —
  the quality-gate fix prompt threads the gate failures and the round budget but
  not the acceptance criteria, so an auto-fix round can satisfy ruff/mypy/pytest
  while drifting away from a binding invariant.
- `maestro/evolution.py` (`_self_review`) — builds a `## Acceptance Criteria
  (binding)` block from `plan.acceptance_criteria` demanding, per criterion, a
  cited diff hunk **and** an asserting test, plus a checklist item. This is the
  desired pattern; the proposal reuses it verbatim at the earlier phases.
- `maestro/evolution.py` (PR-body composition in the create-PR path) — renders
  an ad-hoc `### Acceptance Criteria` section from the same list. A second
  hand-rolled rendering of the identical data.
- `maestro/loop/pr_remediation.py` (`_compose_pr_remediation_prompt`) — renders
  a third copy under `## Original Evolution Plan`, with a `None`-plan fallback
  note for archived plans, and reminds the agent the criteria "remain the
  success contract." Comment-addressing is already aligned; it is the model the
  earlier phases should match, and a fourth renderer to fold into the shared one.
- `maestro/agent.py` (`run()` criteria-anchored override / `_assess_criteria_outcome`) —
  when criteria exist, the outcome is gated per-criterion MET/UNMET and an
  all-met run is required for `success`. The contract decides the grade, yet the
  implementer was never shown it — the core asymmetry this proposal removes.
- `maestro/evolution.py` (`EvolutionEngine.propose_plan` / `validate_success_criteria`)
  — criteria are validated for non-vacuity at proposal time and capped at five
  short strings, so injecting them downstream adds bounded prompt cost.
- `tests/test_evolution.py` — covers that `_self_review` surfaces the criteria
  block; no test asserts `implement()` or `auto_fix()` do, which is why the gap
  went unnoticed.

## Implementation Plan

Steps are ordered so an interrupted run leaves the tree consistent: the shared
renderer (1) is a pure refactor that changes no prompt text; the config flag (2)
is inert until a later step reads it; the two injection points (3–4) are gated
behind it; the regression test (5) lands once the call sites are conformant.

1. [x] **Extract a shared acceptance-criteria renderer.** new
   `maestro/prompts/acceptance.py` — add `format_acceptance_criteria_block(criteria, *, style)`
   returning one of the three existing shapes (and `""` for empty criteria):
   `style="binding"` emits self-review's `## Acceptance Criteria (binding)` header,
   the "cite hunk + asserting test" contract language, and a numbered list;
   `style="plain"` emits the PR body's `### Acceptance Criteria` header and a flat
   `- ` list; `style="remediation"` emits the remediation prompt's header-less
   `- Acceptance criteria:` label followed by a 2-space-indented `  - ` sub-list (the
   structurally distinct shape embedded under `## Original Evolution Plan`). A single
   `binding` bool would cover only two of these three shapes, so the parameter is a
   small style enum, not a flag. Re-point `_self_review`, the PR-body composition, and
   `_compose_pr_remediation_prompt` at it so the three existing renderings produce
   byte-identical output. Place the helper under `maestro/prompts/` so both
   `evolution.py` and `loop/pr_remediation.py` import it without an import cycle.
   - *Depends on:* none.
   - *Acceptance:* the three existing call sites render through the shared
     helper and a golden-string test shows their output is unchanged from before
     the refactor.
2. [x] **Add the alignment config flag.** `maestro/config.py`,
   `maestro/toml_config.py`, `maestro/cli.py`, `maestro.toml` — introduce
   `[loop] criteria_alignment_enabled` (`Config.criteria_alignment_enabled`,
   default `True`) wired through the TOML loader and CLI the same way
   `uncalled_symbol_gate_enabled` is.
   - *Depends on:* none.
   - *Acceptance:* `Config` exposes `criteria_alignment_enabled` defaulting
     `True`, and a config test asserts the TOML key round-trips.
3. [x] **Inject criteria into the implementation prompt.** `maestro/evolution.py`
   (`Evolution.implement()`) — when `criteria_alignment_enabled` and the plan has
   criteria, append the binding block (via step 1's helper) with an instruction
   to implement each criterion and add a test that would fail if it were
   violated. Note in the prompt that on the source-task rebase path the existing
   commits predate these criteria and must be reconciled to them.
   - *Depends on:* 1, 2.
   - *Acceptance:* the implement prompt contains every criterion string when the
     plan has criteria and the flag is on, and omits the block when the plan has
     none or the flag is off.
4. [x] **Inject criteria into the auto-fix prompt.** `maestro/evolution.py`
   (`_run_auto_fix_query`) — under the same flag, append the binding block so a
   quality-gate fix round keeps the invariants in view alongside the gate
   failures.
   - *Depends on:* 1, 2.
   - *Acceptance:* the auto-fix prompt contains every criterion string when the
     plan has criteria and the flag is on, and omits the block otherwise.
5. [x] **Regression test for cross-phase criteria coverage.** `tests/test_evolution.py`
   — assert that for a plan with criteria, the prompts built by `implement()`,
   `auto_fix()`, and `_self_review()` each contain every criterion string; and
   that with the flag off the implement/auto-fix prompts omit the block while
   self-review (always binding) still carries it. Add a negative case proving a
   removed criterion would be caught.
   - *Depends on:* 3, 4.
   - *Acceptance:* the test passes on the aligned call sites and fails if any of
     the three prompts drops a criterion.

## Testing Strategy

All surfaces are prompt strings and pure rendering — no new I/O, network, or
sleep; tests read `EvolutionPlan` fixtures and assert on the composed prompt
text per CLAUDE.md's `mem_fs`/mocked-externals rules.

- **Shared-renderer golden test (step 1).** Assert `format_acceptance_criteria_block`
  produces the exact binding block self-review emitted before the refactor, and
  `""` for empty criteria. Further assertions cover the plain (PR-body) and
  remediation (header-less indented sub-list) variants against their captured
  baselines.
- **Refactor-equivalence (step 1).** For a fixed plan, assert the self-review,
  PR-body, and remediation renderings are unchanged versus a captured baseline,
  proving the dedup is behaviour-preserving.
- **Injection presence/absence (steps 3–4).** Parametrize over
  `criteria_alignment_enabled ∈ {True, False}` and `criteria ∈ {present, empty}`;
  assert the implement and auto-fix prompts include each criterion exactly when
  flag-on and non-empty, and never otherwise.
- **Cross-phase coverage (step 5).** The single guard test above; plus a
  negative case where one criterion is dropped from a prompt to confirm the
  assertion fails rather than passing vacuously.
- **Unaffected invariants.** `_assess_criteria_outcome`, `validate_success_criteria`,
  and the remediation-routing tests in `tests/test_pr_feedback_routing.py`
  continue to pass — the criteria data and outcome gating are untouched; only
  where the data is *shown* changes.

## Risk Assessment

**What could regress.** Step 1 rewrites three live prompt renderings; a careless
refactor could alter the self-review contract wording and weaken the gate.
Mitigation: the golden/equivalence tests pin the output byte-for-byte before any
new call site is added, so the dedup must be exactly behaviour-preserving to
merge.

**Prompt-cost.** Each injection adds at most five short strings
(`validate_success_criteria` bounds the list), so the marginal token cost on the
implement and auto-fix calls is negligible and bounded.

**Behavioural change to the implement phase.** Showing the writer the binding
contract could, in principle, shift what it produces. This is the intended
effect — fewer self-review fix rounds and fewer silent `partial` downgrades —
but it is gated behind `[loop] criteria_alignment_enabled` (default on) so a
single flag flip reverts to the current implement/auto-fix prompts. The shared
renderer (step 1) is unconditional but inert by construction.

**Rollback posture.** Steps 3–4 are flag-gated; flipping
`criteria_alignment_enabled` to `False` restores today's behaviour. Steps 1, 2,
and 5 are pure refactor / config / test with no runtime behaviour change and are
independently revertible. No schema or state-file migration.

## Out of scope

- **Per-criterion verdicts fed back into auto-fix.** Surfacing the self-review's
  MET/UNMET assessment of each criterion into the next fix round (a closed-loop
  repair signal) is a natural follow-on but is not built here; this proposal only
  ensures the criteria *text* is present at each code-writing phase.
- **Criteria on remediation-only tasks.** PR-remediation tasks carry no
  `acceptance_criteria` of their own (`_assess_criteria_outcome` never fires for
  them); synthesising criteria from review findings is a separate change.
- **Changing how criteria are extracted or validated.** `propose_plan` and
  `validate_success_criteria` are untouched — this proposal moves where existing
  criteria are *shown*, not how they are produced or judged.
