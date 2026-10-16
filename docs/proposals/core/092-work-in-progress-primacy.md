# 092: Finish Owned Work Before Starting New — One Work-In-Progress Owner Over the Task-Selection Pile

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | new `maestro/wip_primacy.py` (`select_task_focus`, `TaskFocus`), `maestro/agent.py` (`_run_task_phase`, `_implement_approved_plans`, `_audit_self_model`), `maestro/task_gates.py` (`GATE_CHAIN`, new `wip_primacy` gate), `maestro/audit_checks.py` (`measure_owned_act_drought`, `check_owned_act_drought`), `maestro/remediation_tracker.py` (`classify_blocker_ownership`), `maestro/loop/task_generation.py` (`generate_task` purpose injection), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, new `tests/test_wip_primacy_chokepoint.py`, tests |
| **What landed** | All eight steps across Phase 1 (steps 1–3), Phase 2 (steps 4–6), and Phase 3 (steps 7–8). Step 1 — the WIP-focus owner: `maestro/wip_primacy.py` with `select_task_focus`, the `TaskFocus = TendOwnedPR \| Discretionary` verdict types, and the `OpenOwnedPR` projection; pure/zero-SDK, reusing the 089 `PRReadiness` fact and the `classify_owned_blocker` / `classify_blocker_ownership` classifiers (`maestro/remediation_tracker.py`), covered by `tests/test_wip_primacy.py`. Step 2 — the head-of-chain `wip_primacy` `block`-gate: `GATE_CHAIN[0]` (`maestro/task_gates.py`, `_wip_primacy_run` / `_wip_primacy_directive_text`) behind `[loop] wip_primacy_enabled` (default `false`; `maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`), consuming `MaestroAgent._current_task_focus` (`maestro/agent.py`), with the audit→directive bridge suppressed while `TendOwnedPR` owns the slot (`_audit_self_model`). Step 3 — the AST golden enforcement test `tests/test_wip_primacy_chokepoint.py` asserts `wip_primacy` is `GATE_CHAIN[0]` and `block`-kind, `select_task_focus` is the sole producer of the WIP directive, and no rival writer sets the `"Finish PR #"` directive. Step 4 — the new-PR-mint stand-down: the shared `MaestroAgent._wip_owns_slot()` predicate (flag-on ∧ `TendOwnedPR`; short-circuits on the flag, `maestro/agent.py`) defers `_implement_approved_plans` before the `max_open_evolution_prs` cap and skips the `propose_task_docs` phase body under owned WIP — both mint nothing and author no directive (the `wip_primacy` gate stays the sole `Finish PR #N` writer, `test_no_rival_wip_directive_writer`). Step 5 — the owned-WIP cap exemption: `_run_task_phase` (`maestro/agent.py`) clears the engaged consecutive-remediation cap (`_remediation_streak_capped` / `cap_engaged`) when the shared `_wip_owns_slot()` predicate reports owned self-clearable WIP, so a capped streak keeps serving `pending_pr_requests` rather than falling to the queue head; the mechanical stand-down already fires only on externally-blocked lineages, so an owned `CONFLICTING` / `THREADS_UNRESOLVED` blocker is never evicted onto self-directed work (`test_run_task_phase_wip_owned_exempts_remediation_cap`). Step 6 — the corrected owned-terminal-act incentive: `measure_owned_act_drought` / `check_owned_act_drought` (`maestro/audit_checks.py`) take a `reward_landing` flag wired to `[loop] wip_primacy_enabled` in `_audit_self_model` (`maestro/agent.py`) that counts a merged owned PR (`version_archive.jsonl` `merged_at`) as an owned terminal act and stops counting a `[stood-down]` verdict. Step 7 — the purpose-nudge neutralization landed by subsumption: the head-of-chain `wip_primacy` block gate (`run_gate_chain(GATE_CHAIN, ctx)` at the top of `generate_task`, `maestro/loop/task_generation.py`) returns before the `current_purpose_hypothesis` injection is reached, so while an open own `maestro/evolve/*` PR carries a self-clearable blocker the generate-task prompt carries no `purpose_hypothesis` (a documented no-runtime-branch comment records that a separate omission guard would be unreachable dead code and would break the flag-off no-op contract). Step 8 — the evidence-gated flip landed: `wip_primacy_enabled` now defaults `true` (`maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`), the subsumed open-evolution-PR-cap directive is retired from `_implement_approved_plans` (`maestro/agent.py`), which now logs-and-returns at the `max_open_evolution_prs` cap without writing a directive, and the per-pop `_owned_relap_directive` divert is deleted from `maestro/agent.py` — leaving `wip_primacy` the sole `Finish PR #N` writer. |
| **What is missing** | Nothing — all eight steps have landed; the proposal is shipped. |
| **Depends on** | 080, 089, 091 _(composes with — reuses the merge-truth oracle, the `observe_pr_readiness` fact, the delivery-truth markers, and the per-PR lap-budget/handoff escape)_ |
| **Derived from** | live production data via /propose, 2026-07-11 |

## Description

> Status updated to shipped on 2026-07-13 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Maestro does not finish its own pull requests. It has an open, self-authored PR
that is `CONFLICTING` (a blocker only it can clear), yet it spends its task
budget pruning an almost-empty plan backlog, re-investigating why its last task
was `partial`, and *minting brand-new PRs that add more anti-treadmill
machinery* — which then also sit conflicting and unmerged. The operator has
asked it to "tend your unmerged PRs," "they are your PRs, fix them," and "fix
the problems on your open PRs" across sixteen days; the loop keeps drifting back
to busywork. This is not a tending bug — the tending subsystem (proposals
070/080/087/088/089/090/091) has been rebuilt seven times. It is a *task-
selection* bug one layer up: **nothing in the loop owns the decision "finish
what you started before starting anything new."**

The root is structural, and it is the same shape proposals 070 and 087 retired
in their own subsystems. Task selection is an emergent pile: a three-way hard
precedence in `_run_task_phase` decides which *source* wins the slot, while
~16 independent "directive bridges" compete first-writer-wins to fill the one
`next_task_directive` slot that steers self-directed work. No component ever
reads open-owned-PR state and self-model state *together* to answer "what is the
single highest-value thing to do right now." Worse, the loop's only rewarded
"owned terminal act" — the thing `measure_owned_act_drought` counts and the
self-model learns from — is *pruning a plan* or *standing down on a PR*.
Merging or closing a PR does not count. So the reinforcement loop crystallized
the purpose hypothesis into `custodian-of-a-finite-backlog`, which is then
re-injected into the task-generation prompt, steering the loop back toward
pruning. The machinery that was built to stop the loop re-lapping a PR forever
now, in aggregate, teaches it to abandon the PR for busywork.

This proposal installs **one Work-In-Progress owner** — a single pure function,
`select_task_focus`, that reads the live `observe_pr_readiness` fact for every
open owned PR and returns either "there is owned work to finish, here is the
PR and its owned blocker" or "no owned WIP — discretionary work is allowed." A
single highest-precedence `wip_primacy` block-gate at the head of `GATE_CHAIN`
consults it: while owned WIP exists with a blocker Maestro itself can clear
(`CONFLICTING`, `THREADS_UNRESOLVED`), the gate forces the task focus to that
PR and *suppresses every lower directive bridge, the audit→directive bridge,
new-PR minting, and the purpose-hypothesis nudge*. The mis-defined incentive is
corrected at its source so the self-model relearns that landing or closing a PR
is the owned terminal act. The lap-budget/handoff escape from 080/089/091 still
provides the terminal disposition (merge or close) so an un-clearable PR cannot
loop forever — what is removed is only the exit into plan-pruning.

The end state: while any open owned PR has a blocker Maestro can clear, that is
the only discretionary work the loop will do; new construction, plan-pruning,
self-investigation, and new-PR minting resume only when every owned PR is
merged, closed, or waiting solely on a human. The emergent competition between
sixteen directive writers is subordinated to one owner, enforced by an AST
golden test in the 087 house style.

## Relevant Findings

- `maestro/agent.py` (`_run_task_phase`) — task selection is a flat if/elif/else
  over `pending_user_requests` → `pending_pr_requests` → `task_queue` head,
  throttled by `[loop] max_consecutive_remediation` (default 4). The cap
  *evicts* PR-remediation once four laps run consecutively and "falls through to
  the queue head, breaking the remediation-monoculture" — i.e. it actively
  pushes the loop *off* an unfinished PR onto self-directed work regardless of
  whether the PR is done.
- `maestro/task_gates.py` (`GATE_CHAIN`, `run_gate_chain`) — the ordered gate
  registry has 4 `block` gates and 13 `directive` gates; the directive gates
  are first-writer-wins over the single `next_task_directive` slot. There is no
  gate for "an open owned PR has an owned-clearable blocker — finish it." The
  closest existing writers are the open-evolution-PR-cap directive in
  `_implement_approved_plans` ("Too many open evolution PRs. Focus on
  integrating…") and the per-pop owned-relap divert (`_owned_relap_directive`);
  both are narrow, competing entries in the pile, not an owner.
- `maestro/remediation_tracker.py` (`classify_blocker_ownership`,
  `BLOCKER_OWNERSHIP_OWNED`) — already the authoritative owned-vs-external
  classifier: `MERGE_BLOCK_CONFLICTING`/`DIRTY` and `MERGE_BLOCK_THREADS_UNRESOLVED`
  are *owned* ("mine to drive — Tend cannot clear it mechanically");
  `AWAITING_APPROVAL`/`AWAITING_PUSH`/`BEHIND_MAIN` are *external*. The WIP owner
  reuses this classifier rather than inventing a rival owned-set.
- `maestro/pr_readiness.py` (`observe_pr_readiness`) — already the per-iteration
  memoized aggregated `PRReadiness` fact per open PR. The WIP owner reads it; it
  does not add a new probe.
- `maestro/audit_checks.py` (`measure_owned_act_drought`) — counts an "owned
  terminal act" as *only* an `obsolete` plan-status event (the signature of
  `Memory.mark_plan_obsolete`) or a CHOSEN `[stood-down]` verdict row. A merged
  or closed PR is not counted. Rule 18 (`check_owned_act_drought`, default-on)
  then routes a plan-pruning demand into the directive on a zero-count. This is
  the incentive that rewards pruning and standing down over landing.
- `maestro/loop/task_generation.py` (`generate_task`) — injects
  `model.current_purpose_hypothesis` into `TASK_GENERATION_PROMPT` as
  `purpose_hypothesis=…`. The purpose is advisory prompt context that steers
  queue curation; a corrupted purpose feeds straight back into task generation.
- `maestro/consciousness.py` (`SelfModel.current_purpose_hypothesis`) — live
  value on disk is `custodian-of-a-finite-backlog: I retire pending_review
  plans that a named newer plan restates…`; the self-model has adopted the
  busywork as its reason for being.
- `maestro/agent.py` (`_implement_approved_plans`) — mints new evolution PRs
  gated only on `[loop] max_open_evolution_prs` (default 5), a raw concurrency
  *count*. Three open owned PRs (all conflicting) leave headroom to mint two
  more; there is no "finish an open owned PR before opening another."
- `tests/test_merge_truth_chokepoint.py` — the house pattern this proposal
  copies: an AST golden test asserting a single owner is the only door.

## Production Evidence

Live data read strictly read-only from the loop container bind-mount
`/opt/maestro/data` on 2026-07-11 (window `2026-06-24`→`2026-07-11`,
the full retained `task_history.jsonl`, 85 records). Numbers reproduced with
small Python readers over the JSONL tails; PR state via `gh pr view`.

**The loop does not tend; it prunes and investigates.** Classifying the 45 most
recent task-phase records by goal (`task_history.jsonl`): **25 plan-prune/refine,
14 self-investigation, 6 PR-remediation.** Across the whole window, every one of
the **28 `Remediate PR #N` laps — spanning 14 distinct PR lineages — is
`partial`; not one landed anything** (`task_history.jsonl`, outcome × class):

```
 28  remediate_pr        partial      (0 success, ever)
 14  plan_prune/refine   partial
 12  plan_prune/refine   success
```

**The backlog it prunes is nearly empty.** Reconstructing the latest status per
plan from `evolution_plans.jsonl` (273 distinct plans): **223 obsolete, 15
merged, 3 pending_review.** The loop spent 25 of its last 45 tasks curating a
three-item `pending_review` set.

**It mints new mechanism instead of finishing.** All three open self-authored
PRs are `CONFLICTING`, all on `maestro/evolve/*` branches, all created
2026-07-11 (`gh pr view`); two are *more anti-treadmill machinery*:

```
#2986 CONFLICTING  Add `--json` output mode to `maestro cost-report`
#2987 CONFLICTING  Prune settled PRs from pending_pr_requests — stop re-minting …
#2990 CONFLICTING  Owned-disposal lever — name `gh pr close` as the reachable terminal act
```

`#2990` is Maestro trying to patch the mis-defined "owned terminal act" by
adding one more terminal act — a point fix, minted as an unfinished conflicting
PR. `pending_pr_provenance.jsonl` shows the same requests re-minted many times
(`#2808` ~9×, `#2812` ~7×, `#2844` ~6×).

**The loop measures the waste and only nudges.** Of 416 rows in
`gate_activations.jsonl`, **412 are `cost_risk`** carrying "73% of recent task
duration spent on non-success outcomes" — a `block`-kind gate that emits a soft
directive rather than redirecting to the open PR.

**Budget cost** (`usage.jsonl` joined on `task_id`, same window; total loop
spend `$—`): **`$—` on the 28 zero-landing remediation laps (25
metered iterations)** and **`$—` pruning the three-plan backlog (23
iterations)**.

**Sixteen days of ignored operator instruction** (`messages.jsonl`, 29 non-
deploy operator messages, `2026-06-26`→`2026-07-11`, paraphrased): the operator
asked again and again for the unmerged PRs to be tended and fixed, asked why the
agent could not see the PRs that needed it, and asked what it was working on.

## Prior Point Fixes (whack-a-mole ledger)

The PR-tending / remediation subsystem has been rebuilt seven times as
proposals and touched by **130 commits** across
`maestro/loop/pr_remediation.py`, `maestro/escalation_bridge.py`,
`maestro/remediation_tracker.py`, `maestro/pr_readiness.py`,
`maestro/pr_remediation_state.py`. Every prior fix operates *inside* the tend
subsystem — making a PR converge once selected — none owns the choice to select
it over busywork.

| # | Prior fix | What it patched | Why the class survived |
|---|-----------|-----------------|------------------------|
| 070 | PR-remediation explicit state machine | emergent remediation gate-pile → named states | states govern a PR *once dequeued*; nothing ranks dequeue vs. self-directed work |
| 080 | PR-tending stability | one merge-truth oracle + per-PR lap budget + standdown ledger | budget ends a lineage, but the freed slot flows to plan-pruning, not the next PR |
| 087 | grounded merge-truth chokepoint | scattered merge checks → one oracle | fixed *whether* merged, not *whether to work the PR* |
| 088 | perception-grounded beliefs | self-report vs. git divergence | grounds narration, not attention allocation |
| 089 | PR-readiness fact + progress-aware churn | narrated stand-down → observed `PRReadiness` | provides the fact the WIP owner needs; no owner consumes it against self-directed value |
| 090 | narration→action linkages (partial) | detached sensors → completed escalations | routes narration, still one of ~16 competing directive writers |
| 091 | PR delivery-truth + zombie tending (partial) | plans retired out of the tend set | keeps PRs *in* the tend set; does not stop the LLM choosing pruning over tending |
| `max_consecutive_remediation` | "break the remediation-monoculture" cap | too many consecutive laps | its eviction *creates* this bug: forces the loop off unfinished owned PRs |
| rule 18 `owned_act_drought` | idle-owned-act consequence | zero owned acts in window | its terminal-act vocabulary rewards pruning/standing-down, never landing |
| #2837 / #2863 / #2865 / #2907 | stalled-head yield, owned-relap divert, mechanical-standdown starvation, chosen-standdown thread guard | per-symptom dequeue edge cases | each is another special case in the pile the owner subsumes |

**Retired by this design:** the open-evolution-PR-cap directive
(`_implement_approved_plans`) and the per-pop owned-relap divert
(`_owned_relap_directive`) are subsumed by the WIP owner (they are narrow
precursors of it); `max_consecutive_remediation`'s eviction of *owned-clearable*
PRs is neutralized; `measure_owned_act_drought`'s terminal-act set is corrected,
subsuming the intent of PR `#2990`'s "add `gh pr close` as a terminal act" point
fix.

## Implementation Plan

Behavior changes gate behind `[loop] wip_primacy_enabled` (default `false`,
matching current behavior) until Phase 3's evidence-gated flip. Steps are
independently mergeable; an interrupted rollout leaves the loop working because
the flag is off until the owner and its enforcement test have both landed.

### Phase 1 — The WIP owner and its chokepoint

1. [x] **One WIP-focus owner.** new `maestro/wip_primacy.py` — add
   `select_task_focus(open_prs, readiness_by_pr)` returning a `TaskFocus`:
   `TendOwnedPR(pr_number, owned_blocker)` for the oldest open owned PR whose
   `observe_pr_readiness` blocker classifies `BLOCKER_OWNERSHIP_OWNED`
   (`classify_blocker_ownership`), else `Discretionary`. Pure, deterministic,
   zero SDK; reuses the 089 readiness fact and the 080/091 ownership classifier.
   - *Depends on:* none.
   - *Acceptance:* a `CONFLICTING`/`THREADS_UNRESOLVED` owned PR yields
     `TendOwnedPR`; a set of only `AWAITING_APPROVAL`/`BEHIND_MAIN` PRs yields
     `Discretionary`.
2. [x] **Head-of-chain `wip_primacy` block-gate.** `maestro/task_gates.py`
   (`GATE_CHAIN`), `maestro/agent.py` (`_audit_self_model` bridge) — insert a
   `block` gate first in `GATE_CHAIN`, behind `wip_primacy_enabled`, that
   consults `select_task_focus`. On `TendOwnedPR` it sets `next_task_directive`
   to a bounded "clear `<blocker>` on PR #N yourself — merge origin/main / answer
   the thread / close it" and returns a block outcome that stops every lower gate
   *and* the audit→directive bridge from writing the slot.
   - *Depends on:* 1.
   - *Acceptance:* with the flag on and one owned-clearable PR, `run_gate_chain`
     sets the WIP directive and no lower directive gate fires.
3. [x] **AST golden enforcement test.** new `tests/test_wip_primacy_chokepoint.py`
   — assert `wip_primacy` is the first `GATE_CHAIN` entry and is `block`-kind;
   assert `select_task_focus` is the sole producer of the WIP directive; assert
   no other module writes `next_task_directive` while the owner returns
   `TendOwnedPR` (walk the directive-writer call sites like
   `test_merge_truth_chokepoint.py` walks the merge-truth door).
   - *Depends on:* 2.
   - *Acceptance:* the test fails if a directive writer is added ahead of or
     around the owner.

### Phase 2 — Close the leaks that let new work start

4. [x] **Suppress new-PR minting while owned WIP is unfinished.**
   `maestro/agent.py` (`_implement_approved_plans`) — gate plan-implementation
   (and the doc-proposal minter) on `select_task_focus() == Discretionary` when
   `wip_primacy_enabled`; replace the raw `max_open_evolution_prs` count
   directive with the WIP directive. Finish-before-start, not a bigger cap.
   - *Depends on:* 1, 2.
   - *Acceptance:* with an owned-clearable open PR and the flag on,
     `_implement_approved_plans` opens no new PR and defers.
5. [x] **Stop evicting owned-clearable PRs.** `maestro/agent.py` (`_run_task_phase`)
   — under the flag, the `max_consecutive_remediation` fallthrough and the
   mechanical stand-down apply only to lineages whose sole remaining blocker is
   *external*; an owned-clearable blocker is never evicted onto self-directed
   work.
   - *Depends on:* 1.
   - *Acceptance:* four consecutive laps on a still-`CONFLICTING` PR do not fall
     through to the queue head with the flag on.
6. [x] **Correct the owned-terminal-act incentive.** `maestro/audit_checks.py`
   (`measure_owned_act_drought`, `check_owned_act_drought`) — count a merged or
   closed owned PR (via the 087 merge-truth oracle / host close state) as an
   owned terminal act, and stop counting a `[stood-down]` verdict as one.
   Landing and closing become the rewarded acts; standing-down and pruning no
   longer are.
   - *Depends on:* none (metric correction, safe independently).
   - *Acceptance:* a window containing one merged owned PR and zero prunes/
     stand-downs reports a non-zero owned-act count; a window of only
     `[stood-down]` verdicts reports zero.

### Phase 3 — Break the purpose feedback and graduate

7. [x] **Neutralize the purpose nudge during WIP.** `maestro/loop/task_generation.py`
   (`generate_task`) — when `select_task_focus()` is `TendOwnedPR`, omit the
   `current_purpose_hypothesis` injection so a pruning-shaped purpose cannot
   steer queue curation while owned work is pending.
   - *Depends on:* 1.
   - *Acceptance:* the generate-task prompt carries no `purpose_hypothesis` while
     an owned-clearable PR is open.
8. [x] **Evidence-gated flip + mole retirement.** `maestro/config.py`,
   `maestro/toml_config.py`, `maestro.toml` — after a canary window shows the
   plan-prune-during-owned-WIP share fall to ~0 and `CONFIRMED-LANDING-RATIO`
   (`measure_confirmed_landing_rate`) rise, flip `wip_primacy_enabled` default
   `true`; delete the subsumed open-evolution-PR-cap directive and the per-pop
   `_owned_relap_directive` divert.
   - *Depends on:* 4, 5, 6, 7.
   - *Acceptance:* default-on; the retired directive writers are gone and
     `test_wip_primacy_chokepoint.py` still passes.

## Testing Strategy

All tests use `mem_fs`, mock the code-host and git probes, and add no real
network/sleep/subprocess (CLAUDE.md testing principles). The named deterministic
enforcement test is `tests/test_wip_primacy_chokepoint.py` (Phase 1 step 3).

- **`select_task_focus`** (`tests/test_wip_primacy.py`): owned-clearable PR →
  `TendOwnedPR`; external-only PRs → `Discretionary`; no open PRs →
  `Discretionary`; oldest-first tie-break among multiple owned PRs; the guard
  path where `observe_pr_readiness` returns an unknown blocker (external default).
- **`wip_primacy` gate** (`tests/test_task_gates.py`): flag-off is a no-op
  (parity with today); flag-on with owned WIP blocks every lower gate and the
  audit bridge; flag-on with no owned WIP lets the pile run unchanged.
- **Mint suppression** (`tests/test_agent.py`): `_implement_approved_plans`
  defers with owned WIP + flag-on; mints normally when `Discretionary`.
- **Cap non-eviction** (`tests/test_agent.py`): consecutive laps on a
  `CONFLICTING` PR do not fall through under the flag; external-only lineages
  still fall through.
- **Incentive correction** (`tests/test_audit_checks.py`): merged/closed owned
  PR counts as an owned act; `[stood-down]` verdict does not.
- **Purpose neutralization** (`tests/test_task_generation.py`): purpose omitted
  from the prompt during owned WIP; present otherwise.
- **Enforcement** (`tests/test_wip_primacy_chokepoint.py`): AST assertions on
  `GATE_CHAIN` head/kind and single-writer invariant; fails when a rival
  directive writer is introduced.

## Risk Assessment

- **Reintroducing the treadmill.** WIP-primacy forces work onto owned PRs; the
  fear is an un-clearable PR looping forever. Mitigated by composition: the
  080/089/091 per-PR lap budget and merge-or-close handoff still provide the
  terminal disposition — *close* is now a counted owned terminal act (step 6),
  so the escape from an un-clearable PR is to close it, not to prune plans. What
  is removed is only the exit into busywork.
- **Starving legitimate self-directed evolution.** If owned PRs are chronically
  open, discretionary work never runs. Mitigated because the owned-set is narrow
  (`CONFLICTING`/`THREADS_UNRESOLVED` only — a handful at a time), and the
  finish-before-start pressure plus the corrected incentive should keep the open-
  owned-PR count low; the canary in Phase 3 measures this before the flip.
- **Rollback.** Single master flag `wip_primacy_enabled`, default `false` until
  the evidence-gated flip; every step is a no-op with the flag off, so any step
  is safe to ship and revert is one line.
- **Trust chain.** No approval/sandbox/public-API surface changes; human
  approval remains the sole merge gate. The change only reorders which task the
  loop proposes to work on.

## Out of scope

- The within-tend convergence mechanics — merge-truth oracle (087), churn
  framing and readiness fact (089), delivery-truth markers and thread resolution
  (091). This proposal *consumes* them and does not modify them.
- Purpose-hypothesis *generation* (`contemplate`'s purpose engine). This
  proposal removes the corrupting input signal and neutralizes the nudge during
  WIP; redesigning how a purpose is formed is a separate concern.
- Non-owned (Thomas-authored) open PRs — WIP-primacy governs only Maestro's own
  `maestro/evolve/*` PRs.
- Tuning `max_open_evolution_prs`, `max_consecutive_remediation`, or the lap
  budget as remedies — the design changes the *structure* (finish-before-start),
  not the limits.
