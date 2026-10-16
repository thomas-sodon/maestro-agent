# 137: Owed-Act Chokepoint — One Owner of "What Does This PR Owe Me Right Now?", So a Self-Clearable Red Can Never Be Read as a Handoff

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | `maestro/pr_owed_act.py` (`classify_pr_owed_act`, `OwedAct`, `check_runs_from_conclusion`), `maestro/evolution.py` (`_observe_owed_act`, `_in_quality_fix_cooldown`, `_in_conflict_cooldown`, `_tend_single_pr`, `tend_evolution_prs`, `TendPRsResult`), `maestro/loop/pr_remediation.py` (the independent `refutable_standdown_enabled` guard step 5 must leave standing), `maestro/loop/task_delivery.py` (its reference to the `#2825` hatch step 15 deletes), a `maestro/loop/` collaborator for step 13's sensor wiring (kept out of `agent.py`'s body), `maestro/memory.py` (`EvolutionPlan` tend fields, `mark_plan_obsolete`), `maestro/wip_primacy.py` (`select_task_focus`), `maestro/audit_checks.py` (`check_reconcile_near_miss`), `maestro/pr_facts.py` (`assess_pr_salvage`), `maestro/prompts/remediation.py` (size-pin cure hint), `maestro/remediation_tracker.py` (owed-act divergence sensor), `maestro/agent.py` (`_record_remediation_outcome` anchor-consumption gate, `contemplate` sensor call-out), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro/flag_lifecycle.py`, new `tests/test_owed_act_chokepoint.py`, updates to `tests/test_structural_guards.py`, `tests/test_evolution_tend.py`, `tests/test_wip_primacy.py`, `tests/test_audit_checks.py`, `tests/test_remediation_tracker.py`, `tests/test_pr_salvage.py`, `tests/test_prompts.py`, `tests/test_config.py` |
| **What landed** | Steps 4 and 5 (PR #3978). Step 4 — `maestro/evolution.py` (`tend_evolution_prs`) resolves the PR's check rollup once per PR per lap into a lap-scoped `check_conclusion` local (a `VCSError` degrading to `""`), placed beside the staleness reading and ahead of the `is_current` and conflict-cooldown short-circuits, and threads it into `_observe_owed_act`, `_tend_single_pr` and `_confirm_ready_for_review` so the two sites that previously fetched it themselves now read the hoisted value; `check_runs_from_conclusion` maps an unreadable or in-flight rollup to the empty sequence, so such a PR still reads `WAIT_CI` and never green. `TendPRsResult.owed_acts_observed` is therefore populated from a measured CI conclusion rather than an empty check-run sequence. Step 5 — the `[loop] refutable_standdown_enabled` guard was dropped from the *measurement* in `tend_evolution_prs`; `prune_refuted_standdowns` keeps its own independent guard at its call site in `maestro/loop/pr_remediation.py`, so the observation now runs in production while the release path stays flag-gated. Step 6 — `[loop] cooldown_signature_keying_enabled` (default-off, registered in `EXPERIMENTAL_FLAGS` with a dated `review_by`); six new `EvolutionPlan` tend fields (`last_conflict_resolution_head_sha` / `_ci_conclusion`, `last_quality_fix_head_sha` / `_base_sha` / `_ci_conclusion`) plumbed through `update_plan_tend_fields`; the lap's already-measured `(origin/<branch>` head, `origin/main` base, CI rollup) locals threaded into `_tend_single_pr` and stamped unconditionally at all four cooldown-anchor writes; and both predicates consulting that signature ahead of the hours comparison under the flag. The conflict cooldown keys on the full triple (generalising its proposal-099 base release); the quality-fix one keys on `(head, CI rollup)` only, since an `origin/main` advance — several a day — says nothing about whether this PR's own red changed and would collapse the 24h throttle before step 7's backoff exists to bound it. Only a *proven* difference (both sides known and unequal) releases, so a legacy anchor or an unmeasurable reading keeps today's window. Step 7 — `[loop] cooldown_backoff_base_hours` (1.0, registered alongside the step-6 flag); two new `EvolutionPlan` attempt counters (`conflict_resolution_signature_attempts`, `quality_fix_signature_attempts`) plumbed through `update_plan_tend_fields` and stamped unconditionally at all four anchor writes via `_next_cooldown_attempts` (reset to `0` on a provably-changed component, `prior + 1` otherwise, `0` on a first-ever stamp); and `_cooldown_window_hours` decaying each cooldown's flat ceiling into a clamped doubling ladder from the base, consulted by both predicates and falling back to the ceiling when `_signature_comparable` reports no component known on both sides. The flag still governs only what the predicates *do* with the count — the counting itself is unconditional bookkeeping. Step 9 — `maestro/prompts/remediation.py` gained `SIZE_GUARD_MARKER` and the pure `size_pin_cure_hint`, appended by `maestro/loop/pr_remediation.py::_compose_pr_remediation_prompt` directly beneath the rendered `## CI Failure` section; unflagged, stateless, no extra log fetch, and `""` on every non-matching text so any other failing gate's directive is byte-identical. The marker carries the `.py::` pytest failure-identifier suffix, so a ruff/mypy diagnostic *about* the guard file does not trigger it. The acceptance criterion is met narrowly: only the head-CI route carries the hint, since the local `quality_gates_routed` gate summary is filename-free — deriving the hint from that route stays out of scope. Step 8 (PR #3998) — `_next_cooldown_attempts` gained a `consumed` parameter, evaluated ahead of the provably-changed difference scan, so a rung is spent only by a lap the cooldown actually governed against a concluded rollup on a resolved head; three reachable laps leave the count untouched (a lap an escape hatch admitted while the cooldown held, a lap whose own ahead-of-origin push advanced `origin/<branch>` before it fell through to the conflict route, and a lap whose head or rollup was unmeasurable). `_cooldown_window_hours` reads the reciprocal — a lap that cannot advance the ladder does not read a position off it either, so an unmeasurable head holds the ceiling rather than re-routing at the first rung forever. "Concluded" is decided by `check_runs_from_conclusion`, not a second local truthiness rule. The tend walk's compound cooldown condition was also split so the cooldown verdict and each hatch evaluate exactly once; skip behaviour and the `conflicts_cooldown_skipped` counter are unchanged, and with the flag off every window is still the flat ceiling. Step 12 — the AST chokepoint pinning `classify_pr_owed_act` as the sole owner of the owed-act partition landed as a registered `_OWED_ACT_GUARD` entry in `tests/structural_guards.py` plus its controls in `tests/test_structural_guards.py`, rather than the planned new `tests/test_owed_act_chokepoint.py`: the house meta-guard forbids a standalone chokepoint file that re-implements the bespoke skeleton, so the rule is expressed as a `ChokepointGuard` with a `no_scope_re_derives_the_owed_act` `PartitionCoOccurrence` (any *function scope* reaching `classify_ci_conclusions` plus an ownership classifier must consult the oracle in that same scope) and a production detector flagging an `OwedAct.<MEMBER>` reference in a value-producing position, leaving `Compare` operands and the sanctioned `OwedAct(raw)` round-trip clean. The co-occurrence proved to be a scope property, not a module one — `pr_facts.py` names both classifiers only in docstrings and `agent.py` in two unrelated methods — so no re-pointing audit was owed and the allowlist is empty. `_MAX_STRUCTURAL_GUARDS` rose 28 → 29 with a recorded justification. Runs regardless of any flag. Step 10 (PR #4013) — `[loop] owed_act_binding_enabled` (default-off, registered in `EXPERIMENTAL_FLAGS`) plus the new `maestro/loop/owed_act_binding.py` collaborator (`owed_act_for_readiness`, `make_owed_act_seam`, `readiness_is_code_owed`, the flag-aware `OwedActBinding` façade and the `owed_act_binding(config)` reader), which keeps the derivation out of the size-pinned `agent.py` body: `check_reconcile_near_miss` gained an optional `owed_act` seam that suppresses the `or gh pr close #N` clause while the cited PR reads `CODE_OWED`, and `assess_pr_salvage` gained a `code_owed` keyword holding `SALVAGE_AND_LAND` on a code-owed PR inside the existing reconcile budget. Neither helper mints an `OwedAct` — the value comes from `owed_act_from_readiness_snapshot` — so the step-12 guard stays clean, and with the flag off the seam is never built, so the finding text and the salvage verdict table are byte-identical and no extra readiness observation is paid for. Step 11 — `select_task_focus` gained an optional `owed_act_by_pr` map as a second, weaker arm: when the branch-flag ownership arm does not fire and that map reports `CODE_OWED`, the PR is owned WIP, reported under the code `_code_owed_blocker` reads back off the *same* readiness the owed act was derived from (a measured `FAILING` CI, else `THREADS_UNRESOLVED` for review work, else the branch flag `merge_block_reason` already carries) rather than a fixed `CI_FAILING`, so the focus never names a red the observation did not measure. The member is only compared against, never minted, so the step-12 guard stays clean. The agent gatherer's body moved into `maestro/loop/owed_act_binding.py::gather_task_focus` (with `OwedActBinding.focus_map` deriving the map from the observations the gatherer already made), leaving `_current_task_focus` a shim that owns only the enumeration snapshot and lowering `agent.py`'s size pin. `maestro/task_gates.py::_wip_primacy_code_red_directive_text` also takes the failing check names — resolved lazily by `failing_checks_for` through the CI-truth owner, only on the code-red branch and only while the flag is on — and appends them as a `(failing: …)` clause still bounded by `truncate_directive`. Flag off: no map is built, no `gh` read is paid for, and both the focus verdict and the directive text are byte-identical. Step 13, cooldown-skip leg (PR #4027) — both tend cooldown skips (`conflict` in `tend_evolution_prs`, `quality_fix` in `_tend_single_pr`) now stamp a `tend_cooldown_skip` gate-activation row through `EvolutionEngine._stamp_cooldown_skip`, classified `diverged`/`aligned` against the owed act already in `TendPRsResult.owed_acts_observed` and rendered by the shared `format_cooldown_skip_detail`; `maestro/remediation_tracker.py` (`measure_owed_act_divergence` / `format_owed_act_divergence`) reads those rows back into an `OWED-ACT-DIVERGENCE` line surfaced through `format_known_state_summary`, self-suppressing below its data floor and at zero divergence. Unflagged, measurement only. Step 13, disposal-offer and handoff-directive legs (PR #4033) — `_OWED_ACT_DECISION_KINDS` in `maestro/remediation_tracker.py` now spans `cooldown`, `disposal` and `handoff`, with disposal rows stamped from `maestro/loop/owed_act_binding.py` (`owed_act_disposal_offer`) and handoff rows from `maestro/task_gates.py` (`format_handoff_directive_detail`). Step 1 — the stranded branch's pin-headroom restore merged (PR #3971). Canary — `maestro.production.toml` sets `cooldown_signature_keying_enabled = true` and `owed_act_binding_enabled = true` in `MAESTRO_ENV=production` only, each declared as an `EnvOverlayCanary` in `maestro/flag_lifecycle.py` (`PROD_ONLY_CANARIES`), with the window and graduation criteria in the "Owed-act chokepoint canary (proposal 137)" subsection of `docs/self-evolution.md`, so the observation window was running at archival. |
| **What is missing** | No behaviour-code remains. Steps 2-3 are operator actions on live `data/` state, not verifiable from the tree. Steps 14-16 are the human-gated graduation and decommission: both flags are still base default-off (`maestro.toml`, `Config`) and registered in `EXPERIMENTAL_FLAGS`, live only through the production canary; the three conflict-cooldown escape hatches remain in `maestro/evolution.py`; and the flags are not collapsed. |
| **Depends on** | 135 _(burn-in:observing — reuses `_observe_owed_act`, the tend-walk owed-act observation seam this proposal un-blinds)_; 099 _(shipped — the base-advance cooldown release this proposal generalises)_; 094 _(burn-in:graduated — the salvage veto whose disposal gate this proposal extends)_; composes with 124, 095 |
| **Derived from** | live production data via /propose, 2026-09-19 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Maestro has a correct, pure, zero-cost oracle for the question every PR-servicing
decision actually turns on — *what act does this PR owe me right now?* It lives in
`maestro/pr_owed_act.py` (`classify_pr_owed_act`), it is already imported by the tend
engine, and the read-only `maestro pr-owed-act` CLI renders its verdict faithfully. No
decision path consults it. Every decision site instead re-derives its own partial,
CI-blind answer from whatever facts happen to be nearby: a wall-clock timestamp, a
git distance, a set of branch flags. This proposal makes the oracle a chokepoint —
the same move proposal 087 made for merge truth and the `ci_diagnosis` owner made for
CI truth — and re-keys the two tend cooldowns from wall-clock onto the observation
they are actually bouncing off.

It matters now because the divergence is not theoretical. At the time of writing,
`maestro pr-owed-act` reads `#3971 code-owed: write code — owned blocker or CI red`.
Simultaneously the tend walk logs `in quality-fix cooldown` on that PR every lap and
routes nothing; audit rule 21 offers `Reconcile+push #3971 or gh pr close #3971`; and
`SelfModel.next_task_directive` has settled on *"gh pr close #3971: retire the
7-behind, git-unlanded cure PR I keep re-servicing."* The loop is steering toward
closing its only open PR, whose entire blocker is 186 bytes over a shrink-only size
pin — work it can finish in one lap. Six consecutive priced reflections have narrated
the PR as a delivered head awaiting Thomas's hand, and the generator has begun minting
an evolution plan to stop servicing it, which would harden the wrong read into
shipped behaviour. The blindness has started producing work.

The structural root is that "owed act" is a *fact with an owner and no authority*.
`_observe_owed_act` deliberately passes an **empty** check-run sequence, documented as
"a check conclusion this walk does not hold" — yet twenty lines later `_tend_single_pr`
fetches exactly that conclusion into a local variable and then discards it into a
wall-clock cooldown. `_in_quality_fix_cooldown` is a bare timestamp comparison; its
explicit mirror sibling `_in_conflict_cooldown` has accreted three separate escape
hatches (#2472, #2821, #2825) plus proposal 099's base-advance release, each one a
point fix re-answering "has the thing I was bouncing off changed?" for one more case.
The quality-fix cooldown, being the newer copy, inherited the shape and none of the
hatches.

The end state: one `OwedAct` verdict per open own PR per iteration, composed from the
CI conclusion the walk already holds, stamped into `TendPRsResult`, and binding at
four decision sites — the cooldown skip, the disposal-directive vocabulary, the
WIP-primacy focus, and the dequeue gate. A cooldown becomes a bounce on an *unchanged
observation signature* `(origin head, base head, CI conclusion)` rather than an
unexpired clock, which subsumes all four conflict-cooldown hatches into one rule that
both cooldowns share. On an *unchanged* signature the bounce decays instead of
holding flat: a bounded exponential backoff re-routes the same red head again, so a
lap that did nothing is retried in an hour, not a day, and the lineage is still
capped. And a PR classified `CODE_OWED` is never offered for closure while its
reconcile budget lasts, and never described to Maestro as someone else's move.

## Production Evidence

Live data read read-only from the loop container's bind-mount at
`/opt/maestro` (`data/`), resolved via
`docker inspect maestro-loop`. Windows stated per figure.

- **The oracle and the loop disagree, right now.** `maestro pr-owed-act` (read-only,
  composes `classify_blocker_ownership` + `ci_diagnosis`) prints exactly one line:
  `#3971 code-owed: write code — owned blocker or CI red [maestro/evolve/69cdba8847d6]`.
  In the same minute, `data/self_model.json` (`next_task_directive`) reads
  `"gh pr close #3971: retire the 7-behind, git-unlanded cure PR I keep re-servicing —
  one genuine chosen owned terminal act, not a phantom prune of a live pending plan."`
  One shipped oracle says *write code*; the self-model says *close it*.

- **The dead window, measured.** CI run 35452898536 concluded `failure` at
  2026-09-19T15:52:20Z. `data/logs/maestro.log` (window 2026-09-18T04:13Z →
  2026-09-19T19:40Z) contains **41** occurrences of
  `Tend PR #3971: head CI failing (quality) — deferring main-merge, in quality-fix cooldown`
  and exactly **1** of the sibling `routing quality-gate remediation` line. Over those
  41 iterations `data/usage.jsonl` records 43 SDK calls and **$—** spent, with zero
  work performed on the only open PR. `data/task_queue.json` is `[]`. The cooldown
  anchor in `data/evolution_plans.jsonl` for plan `69cdba8847d6` is
  `last_quality_fix_attempt_at: 2026-09-19T15:41:02Z`; at
  `quality_fix_cooldown_hours = 24.0` the next route cannot fire until
  2026-09-20T15:41Z — a further ~20h at the same burn.

- **The cooldown was stamped against a CI result that no longer exists.** The anchor at
  15:41:02Z was written while the branch head was `f06c91c9` (run 35441435023, red at
  12:00:46Z). Head `ac98dc77` was pushed at 15:47:18Z and produced its own red at
  15:52:20Z. `data/pr_ledger.json` shows the walk *observed* the advance
  (`observed_head: "ac98dc771e…"`, `attempts_on_head: 0`) — but the head-advance release
  nulls only `last_conflict_resolution_attempt_at`, so the quality-fix cooldown kept
  bouncing on a verdict about a head that had been replaced six minutes earlier.

- **The lap raced the run it was judging.** `data/task_history.jsonl` records the sole
  remediation lap `c462b45aed78` completing `partial` at 15:48:07Z — 4m13s *before* the
  run it was reporting on concluded. `data/gate_activations.jsonl` stamps
  `mechanical_outcome_authority … pr=#3971 narr=partial mech=partial` at the same
  second. `data/pr_ledger.json` reads `last_outcome: "noop"`. No lap has read the red.

- **The audit rule offers closure on a code-owed PR, on a cadence.**
  `data/audit_history.jsonl` records `reconcile_near_miss` firing at iterations 5530,
  5540, 5550 and 5560 (17:12Z → 19:29Z), all four on #3971, all with the identical
  statement `Reconcile+push #3971 or gh pr close #3971: it is 7 commits behind
  origin/main (≤10, your reconcile lane) and git-unlanded…`. Over the life of
  `data/journal.jsonl` this rule has cited exactly one PR — this one — and every firing
  re-offers disposal.

- **The blind read is now generating work.** `data/logs/maestro.log` at 17:49:28Z:
  `Proposing evolution: Evict a handoff-already-delivered, still-handoff-ready PR from
  the remediation dequeue instead of re-servicing it`. The premise — that #3971 is
  handoff-ready — is refuted by the oracle in the same tree.

- **The same class, one cooldown over.** `data/logs/maestro.log.3` (window
  2026-08-24T06:31Z → 2026-09-01T23:45Z) contains **56** lines of
  `Tend PR #3528: in conflict cooldown until 2026-08-24T22:15:20…; skipping`, all on a
  single PR. Different cooldown, identical shape: a wall-clock window outliving the
  world it was stamped against.

- **Two of three size pins have zero headroom against `main`.** Measured against
  `origin/main` at `cb9ac902`: `maestro/agent.py` pin 725,345 / actual 725,262
  (**83 bytes** — under one line); `tests/test_agent.py` pin 697,771 / actual 697,771
  (**0**); `tests/test_audit_checks.py` pin 609,693 / actual 609,693 (**0**). Any
  contributor adding a single test to either test module produces an unavoidable CI
  red, which is precisely how #3971 became red: +103 bytes in `agent.py` and +3,005 in
  `tests/test_audit_checks.py`, everything else green at 100% coverage.

## Prior Point Fixes (whack-a-mole ledger)

Each entry re-answered "has the thing this cooldown is bouncing off changed?" for one
more case, at one more site, without giving the question an owner.

| Fix | What it patched | Why the class survived |
|---|---|---|
| #2110 | Introduced `quality_fix_cooldown_hours` to stop re-routing a persistently-broken head every lap | Throttles by clock, not by whether the head or its CI changed — the defect in evidence |
| #2472 | Local-commits escape hatch: a stale-red head with unpushed local commits falls through to merge+push | Closes once the commits reach origin; a *fresh* red on a *new* head is still bounced |
| #2821 | Head-advance release nulls `last_conflict_resolution_attempt_at` when `origin/<branch>` moves | Wired to the conflict anchor only; `last_quality_fix_attempt_at` is untouched by the same advance |
| #2825 | `_has_unconsumed_awaiting_push_verdict` — a third conflict-cooldown hatch for deferred pushes | A third special case for a third shape of "the world moved"; conflict-only again |
| 099 (shipped) | `last_conflict_resolution_base_sha` releases `_in_conflict_cooldown` on base advance | Correct and load-bearing — but scoped to one of the two cooldowns by construction |
| #2440 | Named the real failing check(s) so the directive stops misattributing the gate | Improves the *message*; no decision reads the check names |
| #2836 | Logged the CI-failure merge-deferral on every lap so a stranded PR is greppable | Makes the strand visible, not reachable — the 41 log lines in evidence are this fix working as designed |
| 8 extraction refactors (`7539f00f`, `eaf5fdf9`, `5dde762c`, `c0274f58`, `c164a5bf`, `57b09212`, `513a9bb0`, `05889228`) | Each carved a sibling module out of `agent.py` to get back under the size pin | Buys bytes; the cure is known to humans but named nowhere the remediation lap reads, so a size-pin red looks uncurable to the loop |

Steps 6, 7 and 15 below retire rows 1–5 (the four hatches and the duplicated cooldown
predicate collapse into one signature rule with a bounded backoff). Rows 6 and 7 are
kept: they are observability, and step 11 finally gives their output a consumer. Row 8
is answered by step 9 — the cure is put where the lap can read it — while the pins'
zero headroom itself is split out (see Out of scope).

## Relevant Findings

- `maestro/pr_owed_act.py` (`classify_pr_owed_act`) — already returns the right answer.
  Its documented tree consults `classify_blocker_ownership` first, then hands the CI
  axis to `ci_diagnosis.classify_ci_conclusions`, so an *external* blocker such as
  `MERGE_BLOCK_BEHIND_MAIN` with a concluded-red rollup resolves to `OwedAct.CODE_OWED`
  — behind-ness cannot mask a red. Pure, deterministic, zero SDK cost, no network.
- `maestro/evolution.py` (`_observe_owed_act`) — proposal 135's tend-walk observation,
  which passes a **deliberately empty** check-run sequence, documented as: *"with no
  concluded CI in hand every non-owned blocker classifies `WAIT_CI` … Refuting on a
  concluded-red rollup needs a check conclusion this walk does not hold and is left for
  a later increment."* This is the single line that blinds the loop. It is also gated
  behind `[loop] refutable_standdown_enabled`, which is `false` in `maestro.toml`, so
  in production the observation does not run at all.
- `maestro/evolution.py` (`_tend_single_pr`) — calls
  `self._host.get_pr_check_conclusion(pr.number)` into a local `conclusion`, and
  `self._host.get_pr_failing_check_names(pr.number)` into `failing`, at the exact
  branch that then defers to `_in_quality_fix_cooldown`. The conclusion the walk
  "does not hold" is in hand, one stack frame away from the oracle that needs it.
- `maestro/evolution.py` (`_in_quality_fix_cooldown`) — reads
  `EvolutionPlan.last_quality_fix_attempt_at` and compares hours against
  `Config.quality_fix_cooldown_hours`. No head input, no base input, no CI input. Its
  own docstring describes it as mirroring `_in_conflict_cooldown` "so the two cooldowns
  do not interfere" — the mirror copied the shape and none of the releases.
- `maestro/evolution.py` (`_in_conflict_cooldown`) — the sibling, carrying 099's
  base-advance release inline plus, elsewhere in `tend_evolution_prs`, the #2821
  head-advance null-out and, in `_has_unconsumed_awaiting_push_verdict`, the #2825
  hatch. Four release conditions, three call sites, one anchor field.
- `maestro/evolution.py` (`TendPRsResult.owed_acts_observed`) — already typed
  `dict[int, str]` mapping PR number to `OwedAct.value`, and already populated in the
  walk. The transport exists; only the CI input and the consumers are missing.
- `maestro/wip_primacy.py` (`select_task_focus`) — derives its blocker from
  `PRReadiness.merge_block_reason()` (branch flags) → `classify_owned_blocker` →
  `classify_blocker_ownership`, with no CI axis. It is the sole writer of the
  `Finish PR #N` / `Hand off PR #N` directive, so a red PR is described to the
  generator by its *staleness*, never by its failing gate.
- `maestro/audit_checks.py` (`check_reconcile_near_miss`, rule 21) — takes
  `is_settled`, `commits_behind` and `is_ancestor` seams only. It has no CI input and
  no owed-act input, so its directive text `Reconcile+push #N or gh pr close #N`
  offers disposal as a co-equal option on a PR that is provably code-owed. Classified
  `mechanical_fact` in `RULE_AUTHORITY`, so its findings are treated as non-refutable.
- `maestro/remediation_tracker.py` (`classify_merge_block`) — ranks
  `MERGE_BLOCK_BEHIND_MAIN` (rung 2) above `MERGE_BLOCK_CI_FAILING` (rung 3), with the
  documented rationale that a behind branch "must catch up before CI on the merged tree
  is meaningful". Correct for a stale green; the reason `classify_pr_owed_act`'s
  separate CI axis is load-bearing and must be the thing decisions read, rather than
  the merge-block rung alone.
- `maestro/pr_facts.py` (`assess_pr_salvage`) — proposal 094's single veto over give-up,
  already the gate every disposal actuator clears. It is the natural place to refuse a
  `gh pr close` directive on a `CODE_OWED` PR, and it already has the
  `DISENGAGE_PERMITTED` / `SALVAGE_AND_LAND` vocabulary to express it.
- `tests/test_source_file_size.py` (`_SIZE_BASELINE`) — asserts exact-byte
  non-growth for the upper-band files against a hand-maintained constant map. The
  invariant it defends (the 1 MiB tool-result frame cap) is real, and because the
  pinned files sit at or near the derived ceiling there is no room to carve slack
  *below* it: any slack is growth toward the frame cap. The red it produces is
  self-clearable (extract to a sibling module) but no prompt the remediation lap reads
  says so.
- `maestro/pr_owed_act.py` (`check_runs_from_conclusion`) — already maps `""` /
  `"pending"` / anything unrecognised to the empty sequence, which
  `classify_ci_conclusions` reads as `RUNNING`, so an unmeasurable or pending rollup
  classifies `WAIT_CI` and can never be read as green.
- `maestro/loop/pr_remediation.py` — proposal 135's `prune_refuted_standdowns` call is
  guarded by its own `refutable_standdown_enabled` check, independent of the guard
  around the observation in `tend_evolution_prs`, so un-gating the measurement does
  not switch the release path on.
- `tests/test_structural_guards.py` — the house pattern this proposal reuses: it
  already pins `is_pr_settled` as the sole merge-truth door and `ci_diagnosis` as the
  sole CI-truth owner, both regardless of any flag. An owed-act guard is the missing
  third member of that family.

## Implementation Plan

### Phase 0 — Clear the live strand

1. [x] **Restore pin headroom on the stranded branch.** `maestro/agent.py`,
   `tests/test_audit_checks.py` on `maestro/evolve/69cdba8847d6` — move the branch's
   added test cases into a sibling test module and the added wiring out of `agent.py`'s
   body, following the eight-commit extraction precedent, then merge `origin/main` and
   push so CI re-runs green. This is the ordinary fix the loop would have made had it
   read its own oracle; it is listed as step 1 so the PR stops being a live example
   while the structural work lands.
   - *Depends on:* none.
   - *Acceptance:* `gh pr checks 3971` reports `quality` passing, and both pinned files
     measure at or below their pinned sizes on the branch head.
2. [ ] **Release the wall-clock strand and the disposal directive.**
   `data/evolution_plans.jsonl` (plan `69cdba8847d6` `last_quality_fix_attempt_at`),
   `data/self_model.json` (`next_task_directive`) — an operator-run, documented
   one-shot clear so tend can route on the next lap and the generator stops carrying
   `gh pr close #3971`. Run with the loop container **stopped**: both files are
   read-modify-written by the live loop under `file_lock()`, and a hand edit that does
   not take that lock can be lost to, or clobber, a concurrent rewrite. No code change;
   recorded here so the manual step is not lost.
   - *Depends on:* none.
   - *Acceptance:* the next tend lap logs a routing decision rather than
     `in quality-fix cooldown`, and `next_task_directive` no longer names a disposal
     act on a `code-owed` PR.
3. [ ] **Dispose the plan minted from the blind read.** Dashboard / `mark_plan_obsolete`
   — the plan titled *"Evict a handoff-already-delivered, still-handoff-ready PR from
   the remediation dequeue…"* rests on a premise the oracle refutes. Mark it `obsolete`
   (disposal, not rejection, so it feeds no rejection learning) before a human approval
   or an implementation attempt is spent on it.
   - *Depends on:* none.
   - *Acceptance:* the plan's persisted status re-reads `obsolete`, and no
     `pending_review` / `approved` plan cites #3971 as handoff-ready.

### Phase 1 — Give the oracle the fact it is missing

4. [x] **Feed the real CI conclusion into the tend-walk owed-act observation.**
   `maestro/evolution.py` (`_observe_owed_act`, `tend_evolution_prs`,
   `_tend_single_pr`), `maestro/pr_owed_act.py` (`check_runs_from_conclusion`) — hoist
   the `get_pr_check_conclusion` fetch to the observation point (before the
   `is_current` and conflict-cooldown short-circuits, where proposal 135 deliberately
   placed the observation), memoize it per PR per iteration, and have `_tend_single_pr`
   and `_confirm_ready_for_review` read the memo instead of re-fetching. **Cost, stated
   plainly:** PRs that short-circuit before `_tend_single_pr` today pay no check fetch,
   so this adds at most one host call per open own PR per lap (bounded by
   `max_open_evolution_prs`); PRs that already reached the fetch pay nothing extra. A
   `VCSError` or empty conclusion maps through `check_runs_from_conclusion` to the
   empty sequence → `RUNNING` → `WAIT_CI`, never green. Retires the "left for a later
   increment" caveat in that docstring.
   - *Depends on:* none.
   - *Acceptance:* for a PR whose rollup concluded `failure` and whose blocker is
     `MERGE_BLOCK_BEHIND_MAIN`, `TendPRsResult.owed_acts_observed` records
     `OwedAct.CODE_OWED`; a fetch raising `VCSError` records `WAIT_CI`; and the mocked
     host sees exactly one `get_pr_check_conclusion` call per PR per lap.
5. [x] **Observe the owed act unconditionally.** `maestro/evolution.py`
   (`tend_evolution_prs`) — move the owed-act observation out from behind
   `[loop] refutable_standdown_enabled`. That flag gates proposal 135's *release path*,
   which keeps its own independent guard in `maestro/loop/pr_remediation.py`; the
   measurement is pure composition and leaving it flag-gated means the chokepoint's
   input is absent in production.
   - *Depends on:* 4.
   - *Acceptance:* with `refutable_standdown_enabled = false`, `owed_acts_observed` is
     populated for every open own PR the walk visits, and `prune_refuted_standdowns`
     is provably not called.

### Phase 2 — Make the retry dynamic

6. [x] **One observation signature, shared by both cooldowns.**
   `maestro/evolution.py` (`_in_quality_fix_cooldown`, `_in_conflict_cooldown`),
   `maestro/memory.py` (`EvolutionPlan` tend fields) — stamp each cooldown anchor with
   the signature it was taken against: `(origin_head, base_head, ci_conclusion)`. A
   cooldown holds only while the current signature is *provably equal* to the stamped
   one; any measured change releases it, and an unmeasurable component leaves the
   existing time window intact (the fail-toward-current-behaviour posture 099
   established). The CI component is only ever a *concluded* value — tend already
   defers on a pending rollup, so an anchor is never stamped against `""`. This alone
   covers the #3971 race: the anchor was stamped against head `f06c91c9`, the lap
   pushed `ac98dc77`, so the first concluded red on the new head is a changed
   signature and routes. Behind default-off
   `[loop] cooldown_signature_keying_enabled`, registered in `EXPERIMENTAL_FLAGS` with
   a dated `review_by`.
   *As landed,* the release width differs per cooldown: the conflict cooldown keys on
   the full triple (a base advance is precisely the race it guards — the proposal-099
   release this generalises), while the quality-fix cooldown keys on
   `(origin_head, ci_conclusion)` only. `origin/main` advances several times a day
   independently of the PR, so admitting the base there would release the 24h window on
   a move that says nothing about whether this PR's own red changed — collapsing the
   throttle before step 7's backoff exists to bound it. The base is still stamped on the
   quality-fix anchor as a record.
   - *Depends on:* 4.
   - *Acceptance:* a plan whose anchor was stamped against CI conclusion `failure` on
     head `A` is out of cooldown once the head is `B`, on the same clock, with the flag
     on; byte-identical to today with the flag off.
7. [x] **Bounded backoff on an unchanged signature.** `maestro/evolution.py`
   (`_in_quality_fix_cooldown`, `_in_conflict_cooldown`), `maestro/memory.py`
   (`EvolutionPlan` — a per-signature attempt count), `maestro/config.py` — a lap that
   changed nothing (a `noop`, a failed push) leaves the signature identical, and a flat
   24h hold then strands a PR the loop can still fix. Replace the flat window, under
   the step-6 flag, with an exponential backoff over the attempts made *on this
   signature*: first re-route after `[loop] cooldown_backoff_base_hours` (1h),
   doubling, capped at the existing `quality_fix_cooldown_hours` /
   `conflict_resolution_cooldown_hours`. The count resets when the signature changes.
   This keeps the throttle — a persistently-broken head still converges to the old
   cadence — and the lineage caps downstream (repetition gate, lap budget, salvage
   oracle) are untouched. It deliberately does **not** exempt `CODE_OWED` from the
   cooldown: the quality-fix cooldown is only ever consulted on a concluded red, which
   is `CODE_OWED` by definition, so an exemption would be deletion of the throttle.
   - *Depends on:* 6.
   - *Acceptance:* on an unchanged signature the walk re-routes at 1h, 2h, 4h … and
     never more often than the backoff allows; the hold never exceeds the configured
     ceiling; a signature change resets the count to zero.
8. [x] **A lap that raced CI does not consume the route.**
   `maestro/evolution.py` (`_tend_single_pr` anchor stamping),
   `maestro/agent.py` (`_record_remediation_outcome`) — the backoff attempt count of
   step 7 increments only when a lap *ended against a concluded conclusion on the head
   it was routed for*. A lap that pushed a new head, or recorded its outcome while the
   rollup was still pending, leaves the count where it was, so the next concluded red
   routes at the base interval rather than one rung up. Same flag as step 6. As in step
   13, `agent.py`'s 83 bytes of pin headroom bind here: the `_record_remediation_outcome`
   change must be net-zero-or-negative bytes in `agent.py` — the gate itself belongs in
   `maestro/evolution.py` beside the anchor stamping, leaving a call-out at the
   `agent.py` site — or it lands in the `maestro/loop/` collaborator instead.
   - *Depends on:* 7.
   - *Acceptance:* the seeded #3971 sequence — anchor on head `A`, lap pushes `B` and
     records `partial` before `B`'s run concludes, `B` concludes `failure` — routes on
     the first lap after the conclusion with attempt count `0`.
9. [x] **Put the size-pin cure where the lap can read it.**
   `maestro/prompts/remediation.py`, `maestro/evolution.py` (`quality_gates_routed`
   gate summary) — when the failing check output names
   `tests/test_source_file_size.py`, append one bounded line to the remediation
   directive: the pin is shrink-only, the cure is extracting the added code/tests to a
   sibling module, never raising the baseline. The red that stranded #3971 was
   self-clearable; nothing told the lap how. Unflagged — prompt text only, capped.
   - *Depends on:* none.
   - *Acceptance:* a routed remediation whose failing-gate text contains
     `test_source_file_size` carries the extraction hint, within the directive's
     existing char cap; any other failing gate's directive is byte-identical.

### Phase 3 — Bind the decisions

10. [x] **Bind the disposal vocabulary to the owed act, with an exit.**
    `maestro/audit_checks.py` (`check_reconcile_near_miss`), `maestro/pr_facts.py`
    (`assess_pr_salvage`) — give rule 21 an `owed_act` seam (dependency-injected,
    consistent with its existing `is_settled` / `commits_behind` / `is_ancestor` shape)
    and suppress the `or gh pr close #N` clause when the act is `CODE_OWED`, emitting
    the reconcile-or-fix directive alone; extend 094's veto so a `CODE_OWED` reading
    holds `SALVAGE_AND_LAND` **only while the PR is within 094's existing reconcile
    budget**. Past that budget the verdict table is unchanged, so a genuinely
    uncurable PR can still be closed — the binding removes the *premature* close, not
    the exit. A missing seam short-circuits to today's text. Behind default-off
    `[loop] owed_act_binding_enabled` (its own `EXPERIMENTAL_FLAGS` row): these are
    decision changes, not cooldown keying, and must be reversible separately.
    - *Depends on:* 5.
    - *Acceptance:* for a `CODE_OWED` PR inside the reconcile budget, rule 21's finding
      contains no `gh pr close` token and `assess_pr_salvage` does not return
      `DISENGAGE_PERMITTED`; outside the budget both are byte-identical to today.
11. [x] **Name the real blocker in the WIP directive.** `maestro/wip_primacy.py`
    (`select_task_focus`) — take the observed `OwedAct` alongside the existing
    `PRReadiness` map and select focus from it, so `Finish PR #N` carries the failing
    check names `#2440` already extracts rather than the staleness rung. A PR with no
    owed-act observation keeps today's branch-flag derivation. Same flag as step 10.
    - *Depends on:* 5.
    - *Acceptance:* with a red rollup and failing check `quality`, the directive
      `select_task_focus` drives names `quality` as the blocker and never reads
      `Hand off`.
12. [x] **AST chokepoint.** `tests/test_structural_guards.py`, new
    `tests/test_owed_act_chokepoint.py` — a concrete, checkable rule: outside
    `maestro/pr_owed_act.py`, no module under `maestro/` may call **both**
    `classify_blocker_ownership` (or `classify_owned_blocker`) **and**
    `classify_ci_conclusions` — the co-occurrence is what re-deriving the partition
    looks like — and no module outside it may construct an `OwedAct` member except by
    value round-trip (`OwedAct(raw)`). Today `maestro/pr_facts.py` and
    `maestro/agent.py` both trip the co-occurrence; this step audits each — a site that
    is genuinely re-deriving the partition is re-pointed at `classify_pr_owed_act`, a
    site using the two classifiers for unrelated questions is split so the calls no
    longer share a module-level decision — before the guard lands. Runs regardless of any flag, mirroring the
    merge-truth and CI-truth guards. It depends on nothing: the audit it needs is of the
    *current* `pr_facts.py` / `agent.py` co-occurrence, so the guard lands ahead of the
    flag-gated decision changes and steps 10 and 11 are then written to satisfy it —
    a new co-occurrence introduced by step 10's `pr_facts.py` seam fails the guard when
    it is written, which is the ordering that makes the chokepoint load-bearing rather
    than retroactive.
    - *Depends on:* none.
    - *Acceptance:* the guard fails on a seeded module calling both classifiers, and
      the allowlist names only `pr_owed_act.py`; any existing module the rule trips
      is routed through `classify_pr_owed_act` rather than allowlisted.
13. [x] **Divergence sensor.** `maestro/remediation_tracker.py`
    (`measure_owed_act_divergence` / `format_owed_act_divergence`),
    `maestro/audit_checks.py` (`format_known_state_summary`), `maestro/agent.py`
    (`contemplate`) — a zero-SDK sensor counting laps whose decision disagreed with the
    observed owed act (a cooldown skip past the backoff, a disposal offer, or a handoff
    directive on a `CODE_OWED` PR), surfaced as `OWED-ACT-DIVERGENCE` and
    self-suppressing below a data floor. This is the falsifiability instrument for the
    whole proposal. `agent.py` has 83 bytes of pin headroom: the wiring lands in a
    `maestro/loop/` collaborator, not the `agent.py` body.
    - *Depends on:* 5.
    - *Acceptance:* the sensor reads a positive count against a seeded corpus
      reproducing #3971's shape, and zero once steps 6–8 and 10–11 are on.

### Phase 4 — Flip, retire, collapse

14. [ ] **Flip both flags on.** `maestro.toml` — after a bounded observation window
    with `OWED-ACT-DIVERGENCE` reading as predicted (positive with the flags off),
    flip `cooldown_signature_keying_enabled` and `owed_act_binding_enabled` on, each in
    its own reversible commit.
    - *Depends on:* 8, 11, 13.
    - *Acceptance:* `OWED-ACT-DIVERGENCE` reads zero over the following window with no
      rise in `PR-LAPS` without a matching rise in landings.
15. [ ] **Retire the three conflict-cooldown escape hatches.**
    `maestro/evolution.py` (`_has_unconsumed_awaiting_push_verdict`, the #2821
    head-advance null-out block, the #2472 local-commits fall-through) — with
    signature keying on by default, each hatch is a strictly narrower special case of
    "the signature changed" and is deleted, along with the now-dead
    `last_conflict_resolution_attempt_at` null-out. Placed after the replacement is
    proven, never before.
    - *Depends on:* 14.
    - *Acceptance:* the three hatch symbols are absent from `maestro/evolution.py`, and
      the pre-existing tests that covered each hatch's scenario pass unchanged against
      the signature rule.
16. [ ] **Collapse the flags.** `maestro.toml`, `maestro/config.py`,
    `maestro/flag_lifecycle.py` — register both in `CUTOVER_FLAGS` and collapse them,
    deleting the off-branches.
    - *Depends on:* 15.
    - *Acceptance:* both flags are absent from `Config` and `maestro.toml`, and
      `maestro audit-flags` no longer lists them.

## Testing Strategy

All tests use `mem_fs`, mock every host/VCS seam, and make no real network, subprocess
or `sleep` call, per CLAUDE.md.

- `tests/structural_guards.py` + `tests/test_structural_guards.py` — the AST guard
  (step 12, landed here rather than in a new `tests/test_owed_act_chokepoint.py`, which
  the house meta-guard forbids): asserts no scope outside `pr_owed_act.py` reaches both
  the ownership and the CI classifier without consulting the oracle, that no module
  outside it mints an `OwedAct` member as a produced value, and that the registry's
  seeded `synthetic_bypass` is detected while the `synthetic_ok` round-trip is clean.
  Runs flag-independently.
- `tests/test_evolution_tend.py` — the signature-keying table (step 6): held on an
  unchanged signature; released on head change, on base change, and on CI-conclusion
  change; held on an unmeasurable component; never stamped against a pending rollup;
  and byte-identical with the flag off.
- `tests/test_evolution_tend.py` — the backoff table (step 7): re-route at 1h/2h/4h on
  an unchanged signature, ceiling at the configured cooldown hours, count reset on a
  signature change; and the step-8 race case reproducing #3971 exactly — lap records
  before the run concludes, attempt count not incremented, first concluded red routes.
- `tests/test_evolution_tend.py` — step 4: exactly one `get_pr_check_conclusion` call
  per PR per lap (including a PR that short-circuits at `is_current`), and the
  `VCSError` → `WAIT_CI` branch.
- `tests/test_evolution_tend.py` — step 5: with `refutable_standdown_enabled` off the
  observation is populated and `prune_refuted_standdowns` is not called.
- `tests/test_prompts.py` — step 9: the size-pin hint appears only for a
  `test_source_file_size` failure and respects the directive cap.
- `tests/test_audit_checks.py` — step 10: rule 21 with an `owed_act` seam returning
  `CODE_OWED` emits no `gh pr close` token; returning `HANDOFF_READY` emits today's
  text verbatim; a `None` seam or the flag off is byte-identical to the current output.
- `tests/test_pr_salvage.py` — step 10's veto: `CODE_OWED` inside the reconcile budget
  refuses `DISENGAGE_PERMITTED`; outside the budget, and for every other act, the
  existing verdict table is preserved.
- `tests/test_wip_primacy.py` — step 11: a red rollup yields a `Finish` focus naming the
  failing check; a green merge-ready PR still yields `Hand off`; a PR with no owed-act
  observation falls back to the branch-flag derivation unchanged.
- `tests/test_remediation_tracker.py` — step 13: the divergence sensor over a seeded
  lap corpus, including the data-floor self-suppression branch and the zero case.
- `tests/test_config.py` — both new flags and `cooldown_backoff_base_hours`: defaults,
  committed-TOML parity, overlay round-trip, and `EXPERIMENTAL_FLAGS` registration with
  dated horizons.

## Risk Assessment

**A released cooldown re-routes too eagerly.** The cooldowns exist to stop a
persistently-broken head consuming a lap every iteration. Signature keying releases on
any measured change, and the backoff re-routes on an unchanged one. Both are bounded
by construction: a signature release requires a genuinely new observation (a new head,
a new base, or a newly-concluded rollup), and the backoff doubles to the *existing*
cooldown ceiling, so the worst steady-state cadence is today's. The throttle is kept,
not delegated — the downstream lineage caps (repetition gate, lap budget, salvage
oracle) are a second line, and leaning on them alone would trade a stranded PR for an
early stand-down, which is the outcome this proposal exists to prevent. The
`OWED-ACT-DIVERGENCE` sensor plus the existing `PR-LAPS` reading are the tripwires; a
rise in laps without a rise in landings is the abort signal.

**One more host call per open PR per lap.** Step 4 hoists the check-conclusion fetch
ahead of the short-circuits, so idle PRs now pay it. Bounded by
`max_open_evolution_prs`, memoized per iteration, and a fetch failure degrades to
`WAIT_CI` rather than raising; tend stays zero-SDK.

**Un-blinding shifts real behaviour on live PRs.** Steps 6–8 and 10–11 change what the
loop does with an open PR, which is the trust-sensitive surface. They sit behind two
default-off flags — keying/backoff and decision binding — flipped separately and
collapsed only after a clean observation window; steps 4, 5, 9, 12 and 13 are
measurement, prompt-text, enforcement and test-only and are safe to land ahead of
them. Rollback is the flag, then `git revert`.

**A `CODE_OWED` veto could make an uncurable PR unclosable.** Bounded by step 10's
reuse of 094's reconcile budget: the veto lapses with the budget, and a human close is
never gated at all.

**Deleting the three hatches loses a case the signature rule does not cover.** This is
the sharpest risk, and it is why step 15 lands after the flags are on by default
rather than alongside them. Mitigation: each hatch's existing test scenario is kept
and re-pointed at the signature rule, so a case the generalisation misses fails a test
that already exists rather than surfacing in production.

**Step 2 is a manual write to live state.** It touches `data/` on the loop's
bind-mount, which every other part of this proposal is forbidden from doing. It is
operator-run with the loop stopped, one-shot, and listed explicitly so it is reviewed
rather than improvised; nothing in the code depends on it having happened.

## Out of scope

- **Size-pin headroom.** Two of three `_SIZE_BASELINE` pins sit at zero headroom
  against `main`, which is how #3971 went red — but the pinned files are in the upper
  band at or near the derived ceiling, so slack cannot be carved below it, and a
  minimum-headroom assertion would red `main` on landing. The real fix is shrinking
  `tests/test_agent.py` and `tests/test_audit_checks.py` by extraction, a separate
  proposal. This one only makes the resulting red curable by the loop (step 9).
- **The `classify_merge_block` rung ordering.** `MERGE_BLOCK_BEHIND_MAIN` outranking
  `MERGE_BLOCK_CI_FAILING` is correct for its own consumers and is neutralised for
  decisions by `classify_pr_owed_act`'s separate CI axis. Re-ranking it would ripple
  through every merge-block consumer for no gain here.
- **The handoff-message redundancy** Maestro recorded as
  `[limitation] handoff-message-spam` — three arms emitting the same intent-less ping.
  PR #3971's own feature addresses part of it; the arm-consolidation question is
  genuinely separate from which arm is *allowed to fire*, which is what step 11 decides.
- **Rule 21's cadence and per-rule cap.** This proposal changes what the rule may say,
  not how often it says it.
- **The reflection loop's narration.** Six priced reflections converged on a wrong read
  because their inputs were wrong. Fixing the inputs is the remedy; auditing the
  narrative is proposal 129's ground.
