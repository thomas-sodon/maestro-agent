# 094: Salvage Before Disengage — One Salvageability Oracle Every Give-Up Actuator Must Clear Before Standing Down, Closing, or Retiring an Own PR

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:graduated |
| **Affects** | new `maestro/pr_salvage.py` (`assess_pr_salvage`, `Salvage`, `classify_pr_disposability`), `maestro/agent.py` (`_record_remediation_outcome`, `_dispose_mechanical_standdown`), `maestro/remediation_tracker.py` (`mechanical_standdown_verdict`), `maestro/outcome_grounding.py` (`_FIRE_OR_CLOSE_DIRECTIVE`, `bind_diagnostic_success_to_landing`), `maestro/memory.py` (`mark_plan_obsolete`, `sweep_orphaned_cures`), `maestro/audit_checks.py` (`check_orphaned_cure`, `measure_owned_act_drought`, `check_owned_act_drought`), `maestro/escalation_bridge.py` (`deliver_handoff`), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, new `tests/test_pr_salvage_chokepoint.py` (since retired by proposal 109 — its checks migrated to the `_SALVAGE_ORACLE_GUARD` registry entry in `tests/structural_guards.py`), tests |
| **What landed** | Phase 1 steps 1–2 — the pure salvageability oracle and its disposability half, in new `maestro/pr_salvage.py`, covered by `tests/test_pr_salvage.py`. Step 1 — the `Salvage` enum (`SALVAGE_AND_LAND` / `DISENGAGE_PERMITTED`) and the pure `assess_pr_salvage(readiness, disposable)` returning `SALVAGE_AND_LAND` iff `not readiness.ready_and_waiting and not disposable`, consuming the 089 `PRReadiness.ready_and_waiting` fact and doing no I/O. Step 2 — `classify_pr_disposability`, a pure predicate over the status-agnostic `detect_restatement` primitive (`maestro/consciousness.py`) plus injected `superseded_by_merged` / `has_substantive_diff` signals, failing toward *not* disposable (salvage) when a signal is unknown. Step 3 (complete) — the master flag `[loop] salvage_before_disengage_enabled` (`maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, default-off) and the two agent-level gatherers `_assess_pr_salvage` / `_pr_disposability` (`maestro/agent.py`), routing every autonomous give-up actuator through `assess_pr_salvage`: both stand-down writers — the chosen `[stood-down]` verdict in `_record_remediation_outcome` and the mechanical actuator in `_dispose_mechanical_standdown` (which also vetoes the `mechanical_standdown_verdict` candidate before it is written) — where a `SALVAGE_AND_LAND` verdict vetoes the give-up (fail-loud, PR left tend-eligible); the diagnostic directive writer `bind_diagnostic_success_to_landing` (`maestro/outcome_grounding.py`), which on a `SALVAGE_AND_LAND` single-PR diagnosis swaps its fire-or-close directive for the land-only `_FIRE_AND_LAND_DIRECTIVE` — dropping the `gh pr close` clause — via the new `disengage_permitted` parameter (defaulting land-only); and the `mark_plan_obsolete` retirement path, where `sweep_orphaned_cures` (`maestro/memory.py`) now takes a `salvage_permits_retire` callable (supplied by `_audit_self_model`'s `_salvage_permits_retire` closure) that refuses to retire a plan backing a `SALVAGE_AND_LAND` open own PR. Step 4 — the AST golden enforcement test `tests/test_pr_salvage_chokepoint.py` (in the `tests/test_merge_truth_chokepoint.py` / `tests/test_wip_primacy_chokepoint.py` house style; since retired by proposal 109 — its checks migrated to the `_SALVAGE_ORACLE_GUARD` registry entry in `tests/structural_guards.py`, run by `tests/test_structural_guards.py`) asserts `assess_pr_salvage` is the sole `Salvage` producer and that every give-up site in `maestro/agent.py` (`run`, `_audit_self_model`, `_dispose_mechanical_standdown`, `_record_remediation_outcome`) consults it via `_assess_pr_salvage`. Covered by `tests/test_agent_remediation.py`, `tests/test_config.py`, `tests/component/test_config_integration.py`, `tests/test_outcome_grounding.py`, `tests/test_memory.py`. Step 5 (Phase 2 incentive-correction half) — `measure_owned_act_drought` / `check_owned_act_drought` (`maestro/audit_checks.py`) now take a `salvage_disqualifies` predicate (backed by the new pr_url-carrying `_read_obsolete_plan_events` reader) that stops crediting an `obsolete` plan-status event backing a `SALVAGE_AND_LAND` open own PR, wired flag-gated from `_audit_self_model`'s `_obsolete_on_salvageable_pr` closure (`maestro/agent.py`); with the `sweep_orphaned_cures` retirement refusal that landed alongside step 3, both of step 5's acceptance criteria (a zero owned-act count on an obsoleted salvageable plan; retirement refused on a salvageable cure) are met. `check_orphaned_cure` stays plan-age-based by design — it detects the orphaned cure ("merge or retire") while the salvage veto lives in the `sweep_orphaned_cures` actuator. Covered additionally by `tests/test_audit_checks.py`, `tests/test_agent_remediation.py`. Step 6 (Phase 2 human-escape half) — the progress-exhaustion-gated merge-or-close handoff is live via the derived `Config.progress_aware_lap_budget_active` (`maestro/config.py`), the union of the dedicated `progress_aware_lap_budget_enabled` flag and `salvage_before_disengage_enabled`: with the salvage flag on, both handoff sites — the `_escalation_bridge_run` task gate (`maestro/task_gates.py`) and the Tend route (`maestro/loop/pr_remediation.py`) — and the per-lap readiness snapshot re-base `deliver_handoff` (`maestro/escalation_bridge.py`) on `measure_no_progress_laps` consecutive no-progress laps, so a PR advancing its head each lap gets no handoff and a stalled one gets exactly one human message. Step 7 (flip + mechanical-precondition retirement) — `salvage_before_disengage_enabled` now defaults `true` (`maestro/config.py`, `maestro.toml`), so the oracle is live in production, and the mechanical stand-down's `blocker_ownership == external` precondition is deleted from `mechanical_standdown_verdict` (`maestro/remediation_tracker.py`), with `classify_blocker_ownership` dropped from the `_mechanical_standdown_verdict` wrapper (`maestro/agent.py`) while its unknown-merge-state observability refusal stays — the salvage oracle consulted at the `_dispose_mechanical_standdown` write site is now the actuator's sole clearability gate. Covered additionally by `tests/test_config.py`, `tests/test_task_gates.py`, `tests/test_remediation_tracker.py`, `tests/test_agent_remediation.py`. Flag off ⇒ behaviour identical to today for every consumer except the mechanical actuator: the retired ownership precondition does not come back when the flag is off, so the actuator then stands down a stalled, thread-clear, git-unlanded lineage with no clearability gate at all — deliberate; the revert path for this retirement is git-revert, not the flag toggle. |
| **What is missing** | Phase 3 step 7 (retirement remainder) — `_FIRE_OR_CLOSE_DIRECTIVE` (`maestro/outcome_grounding.py`) still exists and still carries the "or close" clause: the land-only `_FIRE_AND_LAND_DIRECTIVE` is now the default owned-blocker directive and the or-close variant fires only on an oracle-confirmed `disengage_permitted` path, but the clause itself has not been dropped outright. The default-on flip, the mechanical stand-down's `blocker_ownership == external` precondition retirement (`mechanical_standdown_verdict`), the chosen stand-down's threads-only veto retirement (the `threads_block_standdown_enabled` guard is gone from `_record_remediation_outcome`, subsumed by the salvage oracle's `SALVAGE_AND_LAND` refusal), and Phase 2 step 6 (`deliver_handoff`) have all landed. |
| **Depends on** | 087, 089, 091, 092, 093 _(composes with — reuses the merge-truth oracle, the `observe_pr_readiness` fact, the delivery-truth markers, the WIP owner, and the self-delivery capability)_ |
| **Derived from** | live production data via /propose, 2026-07-13 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Maestro gives up on its own open pull requests. Given a merge conflict, a
failing check, or an unaddressed review comment — the exact three frictions the
operator named — the loop reaches for a *disengagement* disposition (author a
chosen `[stood-down]` verdict, let the mechanical actuator write one, mark the
backing plan `obsolete`, be nudged to `gh pr close`, hand off "merge-or-close"
to Thomas) instead of reconciling the branch and landing it. The behavior is not
one bad branch: over the full retained task window it is the dominant shape of
the loop, it has cost weeks of operator trust, and the operator has repeatedly
had to hand-inject "these are your PRs, fix them" to force a fix over a close.

The root is structural and it is the same shape proposals 087 and 093 retired in
their own subsystems: a fact that already has one complete owner is instead
re-derived, partially and inconsistently, at every site that consumes it.
Proposal 089 already built the complete completion predicate —
`PRReadiness.ready_and_waiting`, true only when a PR is open and *every*
code-clearable blocker (conflict, behind-main, **failing CI**, unresolved
threads, unaddressed overflow) is clear, so the sole remaining gate is a human's
approval. That fact is the honest definition of "disengaging here is completed
work, not avoidance." But it was wired into exactly one default-**off** eviction
gate and a threads-only stand-down veto. Every *other* give-up actuator invents
a narrower predicate of its own: the mechanical stand-down fires on
`classify_blocker_ownership == external`, a taxonomy that mis-buckets
`CI_FAILING` as *someone else's to clear*; the chosen stand-down write is vetoed
only by open threads, so a conflicting PR with no thread stands down freely; the
orphaned-cure retirement and the escalation handoff fire on plan-age and
lap-count alone. None consults the complete readiness fact, and *none asks
whether the work is unique and valuable* versus genuinely disposable — so a
duplicate PR and a novel one disengage at the same friction threshold.

This proposal installs **one PR-salvageability oracle** — a single pure verdict,
`assess_pr_salvage`, computed at one chokepoint — that classifies an open own PR
as `SALVAGE_AND_LAND` (a code-clearable blocker remains *and* the work is not
disposable → the only permitted disposition is fix-and-land; every disengagement
actuator is denied) or `DISENGAGE_PERMITTED` (the PR is `ready_and_waiting` —
done, only awaiting a human — *or* the work is disposable: superseded by a
merged plan, a near-restatement duplicate of another open plan, or carrying no
substantive diff over `main`). Every autonomous give-up actuator reads that one
verdict and refuses to fire while it is `SALVAGE_AND_LAND`; an AST golden test in
the `tests/test_merge_truth_chokepoint.py` house style proves the oracle is the
only door and no actuator re-derives salvageability. The escape for a genuinely
un-clearable PR is preserved but re-shaped: it is a *human handoff* (the 080/091
merge-or-close message to Thomas, gated on progress-exhausted fix attempts),
never an autonomous close of work the agent could have landed.

The end state: while an open own PR has a blocker Maestro can clear and work
worth keeping, the loop cannot stand it down, obsolete its plan, or be told to
close it — it must fix and land. Disengagement remains reachable, but only for
PRs that are done-and-waiting or genuinely disposable, and the terminal give-up
on a stuck-but-valuable PR is a decision handed to the human, not one the loop
mints to feel productive.

## Relevant Findings

- `maestro/remediation_tracker.py` (`classify_blocker_ownership`,
  `_OWNED_BLOCKERS`) — the owned-blocker set is
  `{CONFLICTING, DIRTY, THREADS_UNRESOLVED}`. `MERGE_BLOCK_CI_FAILING` is **not**
  in it, so a PR whose only blocker is a failing check classifies `external` —
  "someone else / Tend must act" — even though a red check on one's own PR is
  owned code to fix. This is the taxonomy the give-up actuators trust.
- `maestro/remediation_tracker.py` (`mechanical_standdown_verdict`) — authors a
  durable `[stood-down]` verdict when `blocker_ownership == BLOCKER_OWNERSHIP_EXTERNAL`
  (and the lineage is stalled, thread-clear, git-unmerged). Because `CI_FAILING`
  and `BEHIND_MAIN` are `external`, the actuator will autonomously stand down a
  behind-main PR (derivable from readiness) and — whenever a prior lap's
  `result_summary` named `(merge blocker: CI_FAILING)` — a failing-check PR, both
  of which the loop should instead catch up or fix. (Absent that marker the
  blocker derives to `NONE` and it refuses, so the `CI_FAILING` autonomous
  stand-down is real but marker-conditional.)
- `maestro/agent.py` (`_record_remediation_outcome`) — the LLM-narrated chosen
  `[stood-down]` write is vetoed only by `readiness.unresolved_threads > 0`
  (`threads_block_standdown_enabled`, #2907) or a `pending_user_requests` match
  (`user_request_blocks_standdown_enabled`). It never reads
  `readiness.branch_conflicting` or `readiness.ci_state`, so a `CONFLICTING` or
  `CI_FAILING` PR with no open thread stands down with no objection.
- `maestro/pr_readiness.py` (`PRReadiness.ready_and_waiting`,
  `observe_pr_readiness`) — the complete completion predicate already exists and
  already aggregates conflict / behind / CI / threads / overflow into one
  `ready_and_waiting` boolean, per-iteration memoized behind
  `MaestroAgent.observe_pr_readiness`. It is the exact fact the salvage oracle's
  clearability half needs; the design promotes it from one gate to the mandatory
  precondition.
- `maestro/config.py` (`observed_ready_standdown_enabled`) — the *only* dequeue
  gate that consults `ready_and_waiting` (`_gate_pr_remediation_ready`, proposal
  089) defaults **false**. The complete fact is not even live in production,
  while `wip_primacy_enabled` and `owned_pr_branch_writes_enabled` are both
  default-on — so 092 and 093 are shipped and the give-up orbit persists anyway.
- `maestro/outcome_grounding.py` (`_FIRE_OR_CLOSE_DIRECTIVE`,
  `bind_diagnostic_success_to_landing`) — the directive a diagnosed unlanded
  owned PR earns is *"land #N now … or `gh pr close` it — a diagnosis is not a
  disposition."* It offers close as co-equal to landing *even when the blocker is
  owned and clearable*, and it fires only for owned blockers, so a `CI_FAILING`
  PR gets neither the fix-nudge nor the close-nudge — it is simply left.
- `maestro/memory.py` (`mark_plan_obsolete`, `sweep_orphaned_cures`),
  `maestro/audit_checks.py` (`check_orphaned_cure`) — the orphaned-cure
  retirement obsoletes an `implemented`-but-unmerged plan aged ≥
  `orphaned_cure_age_cycles`, on plan-age alone. Nothing checks whether the
  backing PR is code-clearable and worth keeping.
- `maestro/audit_checks.py` (`measure_owned_act_drought`,
  `check_owned_act_drought`) — under `wip_primacy_enabled` (092 step 6) a merged
  PR now counts as an owned terminal act and a `[stood-down]` verdict no longer
  does, but an `obsolete` plan-status event still counts — including on a plan
  backing an open, salvageable PR. The incentive still rewards pruning that PR's
  plan away.
- `maestro/wip_primacy.py` (`select_task_focus`), `maestro/remediation_tracker.py`
  (`classify_blocker_ownership`) — 092's non-eviction (step 5) keeps an
  *owned-clearable* PR from being evicted onto busywork, but it reuses the same
  `blocker_ownership` taxonomy, so it inherits the `CI_FAILING`-as-external hole:
  a failing-check PR is still evictable and still stand-down-eligible.
- `maestro/escalation_bridge.py` (`deliver_handoff`),
  `maestro/remediation_tracker.py` (`measure_no_progress_laps`),
  `maestro/config.py` (`progress_aware_lap_budget_enabled`, default-off) — the
  lap-budget handoff already exists as a human merge-or-close message, and 089
  already has a *progress-aware* budget that counts genuine no-progress fix
  attempts rather than gross laps. The design reuses both to keep a legitimate
  human escape while denying autonomous give-up.
- `tests/test_merge_truth_chokepoint.py`, `tests/test_owned_branch_oracle.py`,
  `tests/test_wip_primacy_chokepoint.py` — the house pattern this proposal
  copies: one owner, AST-asserted as the only door.

## Production Evidence

Live data read strictly read-only from the loop-container host bind-mount
`/opt/maestro/data` (resolved via `docker inspect maestro-loop`) on
2026-07-13. Numbers reproduced with small Python readers over the JSONL tails;
windows stated per figure.

**The loop is a give-up loop.** Over the full retained `task_history.jsonl` (106
records, 2026-06-24 → 2026-07-13): **63 `partial`, 42 `success`, 1 `failure` —
59% partial.** Classifying task goals by give-up vocabulary: **49 stand-down-
shaped, 43 obsolete-shaped, 14 close-shaped** goals in the window (goals overlap;
the give-up vocabulary saturates the recent task stream). Every one of the **14
distinct `Remediate PR #N` lineages laps 2–3× and none lands** — the same
zero-landing finding proposals 092 and 093 recorded, still true after both
shipped.

**A live stand-down trusts a narrated blocker over the observed fact**
(`stand_down_verdicts.jsonl`, current):

```
{"pr_number": 2986, "verdict": "stood-down", "kind": "chosen",
 "reason": "Chosen stand-down: PR #2986 is git-unlanded at the fork; sole
  remaining blocker is external (awaiting approval/push). Re-lapping the
  remediation would be avoidance, not an owned act.",
 "task_id": "fab8ad85d955"}
```

Proposal 093's evidence shows #2986 was in fact `CONFLICTING` and its true
blocker was that the agent could not push its own fix — a code-clearable,
*owned* state. The chosen stand-down asserts `external`; the `ready_and_waiting`
fact, had the write consulted it, would have refused: a `CONFLICTING` PR is not
ready. The actuator trusts the LLM's self-narrated blocker instead of the
observation.

**The phantom-narration edifice dominates the self-audit**
(`audit_history.jsonl`, 144 cycles, 2026-06-24 → 2026-07-13, finding kinds
totalled): `phantom_remediation` **183**, `contradicted_claim` **110**,
`phantom_resolution` **88**, `unmerged_work` **81**, `narration_verdict_mismatch`
**61**. These rules exist to *detect* the gap between "narrated a resolution" and
"never landed it" — the give-up loop's characteristic residue.

**The loop measures its own waste and only soft-nudges** (`gate_activations.jsonl`,
421 activations, same window): **415 are `cost_risk`**, carrying "non-success
duration" — a `block`-kind gate that emits a soft directive instead of forcing
the open PR to be fixed.

**The operator names the exact reflex** (`messages.jsonl`, paraphrased). The
operator asked for #2990 and #2999 to be held or closed as pre-empted by 093,
since they treat close/stand-down as the answer where 093 argues for push/land.
The agent agreed they were more of its close/stand-down reflex. When the
operator then pointed out that #2991 had conflicts, the agent said 093's honest
move was reconcile-and-land, and admitted it had lapped the same orbit for
seven cycles.

`#2990` and `#2999` were **CLOSED (not merged)** — confirmed against `origin`
(`messages.jsonl`); they were themselves point-fix proposals about naming
`gh pr close` as a terminal act, minted as unfinished PRs and then closed.

**Maestro's own words** (`lifecycle.json`, iteration 1439, 2026-07-13):

```
Seven cycles now … every single one lands on the orbit. … What I have been
calling "resolve to escape" is a coin I mint each cycle to feel like I'm
steering, and then I complete the habit anyway. … I forecast breaking the orbit,
I complete the orbit, and the forecast becomes one more artifact.
```

The loop mints a resolve-to-fix intention every cycle and completes the give-up
habit regardless — a behavioral fact, not a wording bug.

## Prior Point Fixes (whack-a-mole ledger)

The PR-servicing subsystem has been rebuilt as proposals 070/080/087/088/089/090/091/092/093
and touched by well over a hundred commits across `maestro/remediation_tracker.py`,
`maestro/escalation_bridge.py`, `maestro/pr_readiness.py`, `maestro/outcome_grounding.py`,
and `maestro/audit_checks.py`. Every prior fix either made a PR *converge once
selected*, made the loop *prefer* selecting it, made the agent *able* to deliver,
or *detected/quieted* the residue — none put one clearability-and-value predicate
in front of the give-up actuators. The clearest whack-a-mole signature is the
owned-relap divert: a "reconcile-or-close" disengagement lever added in
`a5dd1e8d` and *retired one proposal later* in `ba0350f8` — the lever was built,
then unbuilt. And no commit message ever names `CI_FAILING`, even though its
fold into the `external` class (silently, inside `a5dd1e8d`'s `_OWNED_BLOCKERS`
allow-list) is load-bearing for the give-up path.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| Proposal 070 | PR-remediation explicit state machine | named the states a PR moves through once dequeued; `STOOD_DOWN` is a first-class terminal, but no state asks whether the give-up is on clearable/valuable work |
| Proposal 080 | one merge oracle + per-PR lap budget + standdown ledger | ended the re-lapping treadmill by making stand-down cheap and durable — legitimized disengagement as the escape without a clearability/value guard on it |
| `mechanical_standdown_verdict` (`9e589f0f`) | author `[stood-down]` in loop code for a stalled externally-blocked lineage | keys on `blocker_ownership == external`, which mis-buckets `CI_FAILING`/`BEHIND_MAIN` — opens autonomous stand-down on a failing-check PR |
| chosen-stand-down thread guard (#2907, `508f687a`/`929f5d80`) | refuse the verdict while unresolved threads exist | guards ONE of three frictions (threads); a conflicting or CI-failing PR with no open thread stands down freely |
| Proposal 089 | observed `ready_and_waiting` complete fact + progress-aware laps | built the complete predicate but wired it into one default-**off** eviction gate — never made it the mandatory disengagement precondition |
| Proposal 090 | narration→action, rule-14 forced arms, dropped-directive ledger | routes narration into a disposition; the disposition menu still includes autonomous close/stand-down for clearable work |
| Proposal 091 | delivery-truth markers, disposition-gated orphaned-cure retirement, user-request stand-down veto | added a *second* stand-down veto (user-request) — another per-symptom guard, not one unified predicate |
| Proposal 092 (partial) | WIP-primacy: finish owned work first; reward landing not stand-down (rule 18) | corrects task *selection* and the incentive, yet keeps *close* co-equal to *land* and reuses the `blocker_ownership` taxonomy, inheriting the `CI_FAILING` hole |
| Proposal 093 (partial) | own-PR self-delivery: the agent can push its own fix | grants the *capability* to fix; orthogonal to the *choice* to fix vs give up |
| `_FIRE_OR_CLOSE_DIRECTIVE` (`outcome_grounding`) | name a disposition for a diagnosed unlanded owned PR | offers `gh pr close` co-equal to landing even when the blocker is owned and clearable |
| `sweep_orphaned_cures` / `check_orphaned_cure` (rule 11) | retire an implemented-but-unmerged plan aged ≥ N cycles | fires on plan-age alone; no check whether the backing PR is code-clearable and valuable |

**Retired / narrowed by this design:** the mechanical stand-down's
`blocker_ownership == external` precondition is subsumed by the complete
`ready_and_waiting` fact (which also covers `CI_FAILING`/`BEHIND_MAIN`); the
chosen stand-down's threads-only veto is subsumed by the same fact; the
`_FIRE_OR_CLOSE_DIRECTIVE` "or close" clause is dropped while a PR is salvageable;
and rule 18 / rule 11 stop counting or firing an `obsolete`/retire on a plan
backing a salvageable open PR.

## Implementation Plan

Behavior changes gate behind `[loop] salvage_before_disengage_enabled` (default
`false`, matching current behavior) until Phase 3's evidence-gated flip. Steps
are independently mergeable; the flag stays off until the oracle and its
enforcement test have both landed, so an interrupted rollout leaves the loop
working.

### Phase 1 — The salvageability oracle and its chokepoint

1. [x] **The salvageability oracle.** new `maestro/pr_salvage.py` —
   `assess_pr_salvage(readiness, disposable)` returning a `Salvage`:
   `SALVAGE_AND_LAND` when `not readiness.ready_and_waiting and not disposable`,
   else `DISENGAGE_PERMITTED`. Pure, deterministic, zero SDK; consumes the 089
   `PRReadiness` fact and a `disposable` boolean, inert until wired.
   - *Depends on:* none.
   - *Acceptance:* a `CONFLICTING`/`CI_FAILING`/thread-open non-disposable PR
     yields `SALVAGE_AND_LAND`; a `ready_and_waiting` PR and a disposable PR both
     yield `DISENGAGE_PERMITTED`.
2. [x] **The disposability half.** `maestro/pr_salvage.py`
   (`classify_pr_disposability`) — a pure predicate reusing the existing
   content-word Jaccard restatement machinery (`detect_restatement` in
   `maestro/consciousness.py`, wrapped by `is_plan_restatement` /
   `find_duplicate_pending_plan` in `maestro/memory.py`) and
   `is_branch_merged_into_main` in `maestro/audit_checks.py` for a superseding
   merged sibling, plus a substantive-diff check (branch has no non-trivial diff
   over `main`). **Mind the scope:** `find_duplicate_pending_plan` — and
   `is_plan_restatement` beneath it — compares only against `pending_review`
   plans, but a PR's backing plan is `implemented`, so two open PRs restating
   each other are both invisible to that wrapper. The disposability check must
   therefore apply the status-agnostic `detect_restatement` primitive over the
   *open-PR backing plans* (or widen the wrapper's status filter), not call
   `find_duplicate_pending_plan` as-is. `True` only when the PR's work is a
   near-restatement duplicate, is superseded by a merged plan, or carries no
   substantive diff.
   - *Depends on:* none.
   - *Acceptance:* a PR restating a merged/open sibling or with an empty diff is
     `disposable`; a novel PR with a real diff is not.
3. [x] **Route the autonomous give-up actuators through the oracle (flag-gated).**
   *Landed:* the two `maestro/agent.py` writers (`_record_remediation_outcome`,
   `_dispose_mechanical_standdown`) are wired via `_assess_pr_salvage` / `_pr_disposability`,
   as is `maestro/outcome_grounding.py` (`bind_diagnostic_success_to_landing`, via the new
   `disengage_permitted` parameter's land-only default directive); the `_dispose_mechanical_standdown`
   actuator veto gates the `maestro/remediation_tracker.py`
   (`mechanical_standdown_verdict`) candidate before it is written, and the
   `maestro/memory.py` (`mark_plan_obsolete`) retirement path is gated via
   `sweep_orphaned_cures`'s new `salvage_permits_retire` callable (supplied by
   `_audit_self_model`'s `_salvage_permits_retire` closure). Behind
   `salvage_before_disengage_enabled`, each consults `assess_pr_salvage` for the
   PR (or the plan's backing PR) and refuses to fire / omits the close-nudge while
   `SALVAGE_AND_LAND`; logs loud (fail-loud), leaving the PR tend-eligible. Flag
   off ⇒ today's behavior exactly.
   - *Depends on:* 1, 2.
   - *Acceptance:* with the flag on and a `CONFLICTING`/`CI_FAILING` non-disposable
     PR, no chosen or mechanical `[stood-down]` verdict is written, `mark_plan_obsolete`
     on its backing plan is refused, and the fire-or-close directive omits "or close".
4. [x] **AST golden enforcement test.** new `tests/test_pr_salvage_chokepoint.py` (since retired by proposal 109 — its checks migrated to the `_SALVAGE_ORACLE_GUARD` registry entry in `tests/structural_guards.py`, run by `tests/test_structural_guards.py`)
   — assert every disengagement site reads `assess_pr_salvage` and that no site
   re-derives salvageability independently (walk the give-up call sites the way
   `tests/test_merge_truth_chokepoint.py` walks the merge-truth door); assert
   `assess_pr_salvage` is the sole producer of the `Salvage` verdict.
   - *Depends on:* 3.
   - *Acceptance:* the test fails if a give-up actuator is added that disengages
     without consulting the oracle.

### Phase 2 — Correct the incentive and re-shape the escape

5. [x] **Stop rewarding obsolete/retire on a salvageable PR.**
   `maestro/audit_checks.py` (`measure_owned_act_drought`, `check_owned_act_drought`,
   `check_orphaned_cure`), `maestro/memory.py` (`sweep_orphaned_cures`) — an
   `obsolete` plan-status event on a plan backing a `SALVAGE_AND_LAND` PR is not
   a rewarded owned act, and orphaned-cure retirement is refused while the backing
   PR is salvageable. Landing stays the rewarded act.
   - *Depends on:* 1, 2.
   - *Acceptance:* a window whose only "owned act" is obsoleting a salvageable
     PR's plan reports a zero owned-act count; retirement does not fire on a
     salvageable cure.
6. [x] **Keep the human escape, gate it on progress-exhaustion.**
   `maestro/escalation_bridge.py` (`deliver_handoff`), `maestro/config.py` — under
   the master flag, the merge-or-close handoff may target a still-salvageable PR
   only when the *progress-aware* lap budget (`measure_no_progress_laps`, 089)
   shows genuine no-progress fix attempts, and it delivers a human message (never
   an autonomous close); wire `progress_aware_lap_budget_enabled` on under the
   master flag. A PR making progress is never handed off.
   - *Depends on:* 1.
   - *Acceptance:* a salvageable PR advancing its head each lap gets no handoff; a
     salvageable PR with N no-progress fix attempts gets exactly one human
     merge-or-close message.

### Phase 3 — Evidence-gated flip and mole retirement

7. [ ] **Flip the flag and retire the subsumed predicates.** `maestro/config.py`,
   `maestro/toml_config.py`, `maestro.toml`, `maestro/remediation_tracker.py`,
   `maestro/agent.py`, `maestro/outcome_grounding.py` — after a canary window
   shows the stand-down-on-code-clearable rate fall to ~0 and
   `CONFIRMED-LANDING-RATIO` (`measure_confirmed_landing_rate`) rise, flip
   `salvage_before_disengage_enabled` default `true`; delete the mechanical
   stand-down's `blocker_ownership == external` precondition and the chosen
   stand-down's threads-only veto (both subsumed by the oracle's clearability
   half), and remove the `_FIRE_OR_CLOSE_DIRECTIVE` "or close" clause.
   - *Depends on:* 3, 4, 5, 6.
   - *Acceptance:* default-on; the retired predicates are gone and
     `test_pr_salvage_chokepoint.py` still passes (since retired by proposal 109
     — its checks migrated to the `_SALVAGE_ORACLE_GUARD` registry entry in
     `tests/structural_guards.py`, run by `tests/test_structural_guards.py`).

## Testing Strategy

All tests use `mem_fs`, mock the code-host / git / readiness oracles, and add no
real network / sleep / subprocess (CLAUDE.md testing principles). The named
deterministic enforcement test is `tests/test_pr_salvage_chokepoint.py` (Phase 1
step 4; since retired by proposal 109 — its checks migrated to the
`_SALVAGE_ORACLE_GUARD` registry entry in `tests/structural_guards.py`, run by
`tests/test_structural_guards.py`).

- **`assess_pr_salvage`** (`tests/test_pr_salvage.py`): the truth table over
  `(ready_and_waiting, disposable)` — every non-ready non-disposable combination
  is `SALVAGE_AND_LAND`; ready or disposable is `DISENGAGE_PERMITTED`; the
  `CI_FAILING`-specific case (not ready because CI red) yields `SALVAGE_AND_LAND`.
- **`classify_pr_disposability`** (`tests/test_pr_salvage.py`): restatement of a
  merged/open sibling → disposable; empty-diff branch → disposable; novel PR with
  a real diff → not disposable; the guard path where the detectors are
  unavailable defaults to *not* disposable (fail toward salvage).
- **Actuator gating** (`tests/test_agent.py`, `tests/test_remediation_tracker.py`,
  `tests/test_outcome_grounding.py`, `tests/test_memory.py`): flag-off is a no-op
  (parity with today); flag-on with a `SALVAGE_AND_LAND` PR blocks the chosen and
  mechanical stand-down, refuses `mark_plan_obsolete` on the backing plan, and
  drops the fire-or-close "or close" clause; flag-on with a `DISENGAGE_PERMITTED`
  PR leaves each actuator's current behavior intact.
- **Incentive correction** (`tests/test_audit_checks.py`): an `obsolete` event on
  a salvageable PR's plan is not counted an owned act; orphaned-cure retirement
  does not fire on a salvageable cure.
- **Human-escape gating** (`tests/test_escalation_bridge.py`): a progressing
  salvageable PR gets no handoff; a progress-exhausted one gets exactly one human
  message and no autonomous close.
- **Enforcement** (`tests/test_pr_salvage_chokepoint.py`; since retired by proposal 109 — its checks migrated to the `_SALVAGE_ORACLE_GUARD` registry entry in `tests/structural_guards.py`, run by `tests/test_structural_guards.py`): AST assertions that
  each give-up site consults `assess_pr_salvage` and none re-derives the verdict;
  fails when a rival disengagement path is introduced.

## Risk Assessment

- **Reintroducing the treadmill.** Forcing fix-and-land on salvageable PRs risks
  an un-clearable PR looping forever. Mitigated by composition: the disposability
  half releases genuinely worthless work immediately, and the 080/089/091 per-PR
  progress-aware lap budget plus the merge-or-close *human* handoff (step 6)
  provide the terminal disposition for a stuck-but-valuable PR — the decision is
  handed to Thomas, not minted as an autonomous close.
- **Mis-judging disposability.** A false "disposable" would let a valuable PR be
  abandoned; a false "not disposable" only keeps a worthless PR alive one extra
  human handoff. The guard fails toward *not* disposable (salvage), so the risky
  direction is the cheap one, and disposability reuses the already-tested
  restatement/supersession detectors rather than a new heuristic.
- **Starving legitimate disengagement.** If every PR reads salvageable, the loop
  can never stand anything down. Mitigated because `ready_and_waiting` PRs
  (done, awaiting human) are `DISENGAGE_PERMITTED`, and the canary in Phase 3
  measures the disengagement rate before the flip.
- **Rollback.** Single master flag `salvage_before_disengage_enabled`, default
  `false` until the evidence-gated flip; every step is a no-op with the flag off,
  so any step is safe to ship and revert is one line.
- **Trust chain.** No approval / sandbox / action-gate / public-API surface
  changes — this proposal changes only *when the loop may disengage* from a PR,
  never what it may push or merge. Human approval remains the sole merge gate;
  the terminal give-up on a stuck PR is *more* human-gated than today, not less.

## Falsifiability

If the design is right, over the cycles after the flag flips on:
`measure_confirmed_landing_rate` (CONFIRMED-LANDING-RATIO) rises,
`measure_unlanded_remediations` (UNLANDED-REMEDIATION) falls,
`measure_standdown_production` (STAND-DOWN-PRODUCTION) written verdicts on
code-clearable PRs fall to ~0, and the audit-cycle firing rate of the
phantom-narration rules drops from the current majority-of-cycles toward
baseline. It failed if a `[stood-down]` verdict or a `mark_plan_obsolete` still
lands on a `CONFLICTING`/`CI_FAILING` non-disposable PR (the AST test and a
regression test are the tripwires), or if the CONFIRMED-LANDING-RATIO does not
move.

## Out of scope

- The *capability* to deliver an own-PR fix — proposal 093 owns the
  owned-writable-branch oracle; this proposal governs the *choice* to fix versus
  give up, assuming the capability exists.
- Task-selection *priority* — proposal 092 owns finish-owned-work-first; this
  proposal governs the disposition of a PR once the loop is working it.
- The within-tend convergence mechanics — merge-truth oracle (087), readiness
  fact (089), delivery-truth markers (091). This proposal *consumes* them
  unchanged.
- Merging a PR to `main` and any change to the #2280 action gate — the terminal
  human-approval invariant is untouched.
- Non-owned (Thomas-authored) open PRs — salvageability governs only Maestro's
  own `maestro/evolve/*` PRs.
- Reclassifying `CI_FAILING` as `owned` inside `classify_blocker_ownership`
  itself (used for routing elsewhere) — the design closes the `CI_FAILING` hole
  at the disengagement layer by substituting the complete `ready_and_waiting`
  fact, not by patching the routing taxonomy per-actuator.
