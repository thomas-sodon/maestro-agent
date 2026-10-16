# 135: Refutable Stand-Down — Re-Validate the Handoff Latch Against the Live `pr_facts` Reading That Minted It, So a Refuted Claim Releases Itself

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | new `maestro/standdown_validity.py`, `maestro/escalation_bridge.py` (`StanddownRecord`, `deliver_handoff`, `prune_resolved_standdowns`, `prune_expired_standdowns`, `unserviced_comment_ids`, `mark_comment_ids_serviced`, `reconcile_serviced_comment_ids`), `maestro/loop/pr_remediation.py` (`PrRemediationController.route_feedback`), `maestro/pr_owed_act.py` (`classify_pr_owed_act`), `maestro/remediation_tracker.py` (new `measure_contradicted_standdowns`, `format_pr_lap_summary`), `maestro/agent.py` (`_audit_self_model`, `contemplate` sensor wiring, `mark_comment_ids_serviced` call site retired in step 10), `maestro/config.py`, `maestro/config_settings.py`, `maestro/toml_config.py`, `maestro/flag_lifecycle.py`, `maestro.toml`, `tests/structural_guards.py`, new `tests/test_standdown_validity.py`, tests |
| **What landed** | Steps 1–7. Step 1 — the validity oracle `maestro/standdown_validity.py`: `StanddownClaim` (four members, one per `escalation_bridge` deliverer), `StanddownValidity` (`HOLDS` / `REFUTED`), the tolerant `parse_standdown_claim`, and `assess_standdown_validity`, which reads `REFUTED` iff a `HANDOFF_READY` claim meets a live `OwedAct.CODE_OWED`. An unclaimed (legacy) row parses to `None` and reads `HOLDS` rather than defaulting to the handoff claim, so a pre-135 record is never refuted. Step 2 — `StanddownRecord.claim` (`NotRequired[str]`), carried through `_parse_standdown_entry` when present and a string, and stamped by all four deliverers (`deliver_handoff` → `"handoff-ready"`, `deliver_reality_refutation`, `deliver_claimed_escalation`, `deliver_retirement_disposition`); the ledger projections `standdown_claims` and `standdown_delivery_times` expose the raw strings without coupling the ledger to the oracle. Step 3 — the contradiction sensor: `measure_contradicted_standdowns` and the composing `measure_refuted_standdowns` (both in `maestro/standdown_validity.py`, not `remediation_tracker`, because `pr_owed_act` already imports the tracker and the dependency must run one way), reading each PR's live owed act off the persisted `pr_readiness_snapshot` via `owed_act_from_readiness_snapshot` and `latest_pr_readiness_snapshots` bounded below by the latch timestamp; `format_pr_lap_summary`'s `refuted_prs` parameter renders `[standdown-refuted]` (sharing no substring with `_CHOSEN_QUIET_MARKER`), wired through the new `maestro/loop/known_state.py::compose_pr_lap_context`. Step 4 (sole-owner half) — `_STANDDOWN_VALIDITY_GUARD` in `tests/structural_guards.py` pins `StanddownClaim` / `StanddownValidity` / `assess_standdown_validity` as the sole owner, with two `ModulePurity` specs keeping the module a leaf that neither imports the ledger nor reaches the filesystem or network; covered by `tests/test_standdown_validity.py` and `tests/test_standdown_contradiction_wiring.py`. Step 4's second half — `_STANDDOWN_RELEASE_SITES_GUARD` in `tests/structural_guards.py` enumerates the four functions in `escalation_bridge.py` that may drop a record (`clear_standdown`, `prune_resolved_standdowns`, `prune_expired_standdowns`, `prune_refuted_standdowns`), detecting a `del`/`pop`/`popitem`/`clear` on a `standdown`-named container, so a fifth release path cannot appear unregistered. Step 5 — `prune_refuted_standdowns(backend, live_owed_act)`, the third release path: it parses each record's `claim` through `parse_standdown_claim`, consults `assess_standdown_validity` against the injected live reading, and drops the `REFUTED` records inside one atomic `mutate` closure, fail-loud on a raising predicate and short-circuiting on an empty ledger; an `expires_at`-bearing claimed-escalation record survives even when its PR reads `CODE_OWED`, because selection keys on the claim. Step 6 — the pruner is wired into `PrRemediationController.route_feedback`'s standdown-upkeep block under the same `lap_budget > 0` guard as the merge/close pruner and behind `[loop] refutable_standdown_enabled` (default off; registered in `config.py`, `config_settings.py`, `maestro.toml`, the `test_config_integration.py` fixture and `EXPERIMENTAL_FLAGS`), reading each PR's owed act off `TendPRsResult.owed_acts_observed` so the release costs no host call — a PR the lap did not walk keeps its latch for that pass — and logging one `standdown_refuted` gate activation per release. Step 7 — the release is spoken and budgeted: `prune_refuted_standdowns` gains `data_dir` / `blockers` / `release_cap` / `message_store` / `now` and returns a `RefutedReleaseOutcome(released, capped)` rather than a bare list. Each refuted record first appends one id-keyed `standdown-withdrawn-<pr>-<n>` message naming the PR, the blocker that refuted the claim (carried out of the tend walk on the new `TendPRsResult.owned_blockers_observed`, which `_observe_owed_act` now returns alongside the act instead of discarding), and the owed act the same selection pass measured; the record is released only once a post-write `get_thread` re-read confirms that message landed, so a silent retraction cannot happen. `count_refutation_releases` over the append-only message ledger is the cap authority — the release deletes the record, so a counter on it would be destroyed by the event it counts — and past `[loop] standdown_refutation_release_cap` (default 2, `ge=1`) the latch becomes permanent, the record stays, and one terminal `standdown-refutation-capped-<pr>` message is written once and never again. `StanddownRecord.refutation_releases` is the read-only projection of that count, stamped by `deliver_handoff`'s new `release_cap` argument. Every production call site reaches it through the new config-aware door `deliver_handoff_cfg(config, …)`, the one place that derives both `memory_dir` and the budget from the single-owner `Config.active_refutation_release_cap` (0 while the master flag is off, so the default-off path skips the probe and stays byte-identical) — a site that threaded the cap by hand could silently omit a `NotRequired` field, so passing a config instead makes the omission impossible to express. The capped id — but deliberately not the withdrawal id — is registered in `audit_checks._HANDOFF_MESSAGE_ID_PREFIXES`, since a terminal cap notice is a durable merge-or-close escalation whereas a withdrawal is the *un*-banking of one, and `route_feedback` logs a `standdown_refuted`/`capped` gate activation at WARNING per capped PR. |
| **What is missing** | Step 8's observation half is running — the canary in `maestro.production.toml` (a `[loop]` line, declared as a `PROD_ONLY_CANARIES` entry in `maestro/flag_lifecycle.py`) enables `refutable_standdown_enabled` in `MAESTRO_ENV=production` only since this PR's production merge, so the third release path now runs in production while every base default stays off and byte-identical; the window, tripwires and reversion live in the "Refutable stand-down canary (proposal 135)" subsection of [self-evolution.md](../../self-evolution.md). What remains of step 8 is graduating the flag to a base default. All of Phase 3: the #2798 review-thread carve-out and the `serviced_comment_ids` / `unserviced_comment_ids` / `mark_comment_ids_serviced` / `reconcile_serviced_comment_ids` machinery are all still live (steps 9–10). |
| **Depends on** | 080, 087, 089, 092, 094 _(composes with — reuses the durable standdown ledger, the merge-truth chokepoint, the `PRReadiness` fact, the WIP owner, and the salvage oracle)_ |
| **Derived from** | live production data via /propose, 2026-09-18 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

A handoff stand-down is a **claim about a fact**: "this PR is merge-ready, only
Thomas can act on it." The fact is measured live — rule 22
(`green_ready_handoff_prs`) reads the `pr_facts` `PRReadiness` observation and
fires the instant a PR reads green and unblocked. The claim, however, is latched:
`escalation_bridge`'s `pr_number`-keyed record releases only when the host reports
the PR merged or closed. Nothing ever re-asks whether the fact that earned the
latch still reads the same way. When a sibling PR merges and turns the stood-down
PR `CONFLICTING`, the claim becomes false — the owed act flips from *hand off* back
to *write code* — and the latch keeps suppressing the remediation that would clear
it. The loop can measure the contradiction but cannot act on it.

This proposal makes the latch **refutable**. A stand-down record gains the claim
that earned it; a third pruner, sibling of the existing `prune_resolved_standdowns`
(terminal release) and `prune_expired_standdowns` (age release), re-reads the live
owed act each tend pass through the already-shipped `pr_owed_act` composer and
releases any latch the live reading refutes. The oracle deciding "does this claim
still hold?" is one pure, AST-enforced owner, mirroring how `is_pr_settled` owns
merge truth and `assess_pr_salvage` owns give-up permission. A released handoff
also *withdraws* its outbound message, because the message it sent — "only you can
land it" — is no longer true.

The end state: a stand-down means "I have measured that this is not mine to act
on," re-measured every lap, rather than "I have stopped looking at this PR." A
refuted claim releases itself within one tend pass and the ordinary conflict route
mints the remediation it always would have. Nothing about the treadmill 080 closed
returns: the latch still holds against every reading that is *not* owned work —
CI still running, awaiting approval, a mechanical catch-up Tend owns — so a PR
genuinely waiting on Thomas stays handed up and silent. Only the reading that says
*I* owe code releases it, and the release is budgeted so a flapping PR surfaces as
a bounded, visible failure rather than a message storm.

## Production Evidence

Live data read read-only from the loop container's bind-mount at
`/opt/maestro/data` (resolved via `docker inspect maestro-loop`).

**1. The deadlock, live, one contradictory pair per iteration.** PR #3911 was
handed off at `2026-09-18T00:16:33Z` (`escalation_handoffs.json`,
`stand_down_verdicts.jsonl`, `deed_ledger.jsonl`). Sibling PR #3910 merged at
`00:42`, which turned #3911 `CONFLICTING`. Over the window `2026-09-18 00:42:19`
→ `10:01:23` — **130 loop iterations, 9h 19m** — `logs/maestro.log` and
`logs/maestro.log.1` carry, anchored to a log timestamp:

```
2026-09-18 09:06:31 [INFO] maestro: WIP primacy: owned work is pending
  (TendOwnedPR(pr_number=3911, owned_blocker='CONFLICTING'))
  — deferring evolution-PR implementation this iteration
2026-09-18 09:08:39 [INFO] maestro: [lap-budget] PR #3911 under active standdown
  — suppressing remediation minting until it merges/closes
```

156 WIP-primacy deferrals naming #3911 (across the two sites that log it —
evolution-PR implementation and evolve reflect/propose/submit), **every one**
reading `owned_blocker='CONFLICTING'`, against 130 stand-down suppressions — one
per iteration, a 100% contradiction rate. One oracle says the code is mine; the
latch says hands off.

**2. The cost of the deadlock.** Same window, `usage.jsonl`: 134 metered
iterations, **$—**. `task_history.jsonl`'s most recent record is
`2026-09-17T23:38:48Z` and `task_queue.json` is `[]` — **zero tasks executed** for
the entire window, because WIP-primacy correctly refuses discretionary work while
owned WIP is pending and the latch refuses the owed remediation.

**3. The blocked work is trivial and owned.** `git merge-tree` over
`origin/main`…`origin/maestro/evolve/59ecbf545d3d` reports **one conflict hunk in
one file** — `CLAUDE.md`, one insertion, one deletion. Every CI check on the head
concluded `SUCCESS`. `MERGE_BLOCK_CONFLICTING` classifies
`BLOCKER_OWNERSHIP_OWNED` in `maestro/remediation_tracker.py`.

**4. The outbound message is now false.** `messages.jsonl`, id `handoff-3911-nohead`:

```
Handoff — PR #3911 carries an implemented cure that has not landed after 1
unlanded remediation lap(s), and git-merge-truth shows it is not merged into
main. Only you can land it: please merge PR #3911 or close/retire it. I am
standing down — no further remediation laps will be minted for this PR until
it merges or closes.
```

Thomas cannot merge a `CONFLICTING` PR. The claim was true when sent and false
28 minutes later, and nothing revises it.

**5. This is the second instance, not the first.** PR #3528 was handed off
`2026-08-22T19:15:31Z` and released `2026-08-24T12:55:10Z` — 41.7 hours. The
retained log (`maestro.log.3`, which only begins `2026-08-24 06:31`) already shows
**69 anchored WIP-primacy deferrals naming #3528 with `owned_blocker='CONFLICTING'`**
between `06:49:16` and `12:55:09` — the last of them one second before the release.
`task_history.jsonl` shows the first `Remediate PR #3528:` task at `13:33:56`, i.e.
only *after* the latch released. Across all four retained logs (`2026-08-24` →
`2026-09-18`) there are 238 WIP-primacy deferrals in total; **226 of them (95%) name
these two latched PRs** (#3911: 156, #3528: 70).

**6. The handoff arm cannot see its own claim expire.** All seven `pr_number`-keyed
handoff records in `stand_down_verdicts.jsonl` (`2026-08-22` → `2026-09-18`)
released after 41.7h, 8.2h, 4.3h, 8.8h, 1.9h and 0.4h respectively, with #3911 still
unreleased at 9.8h. **Every release was a merge.** Not one was a refutation, because
no code path can produce one.

**7. The claim is minted in batches that refute each other.** `deed_ledger.jsonl`
records `handoff_delivered` for #3910 **and** #3911 at the identical timestamp
`2026-09-18T00:16:33`. Rule 22 hands off every simultaneously-green owned PR in one
cycle; when two of them touch the same file, merging either necessarily refutes the
other's claim. The batch mint is a systematic refutation generator.

**8. The loop spends priced calls grading the deadlock as a deed.** Since the
handoff: 54 `reflection`, 27 `realization` and 56 `audit_observation` journal entries,
10 naming #3911. Three consecutive priced `introspect` bets scored the *non*-re-admission
of #3911 as the act performed — from `journal.jsonl`:

```
Naming that #3911 is stood-down is not the same as keeping it stood-down; the
deed is the not-re-admitting, and the sensor confirms I performed it, not just
narrated it. That's what makes this cycle count — the act happened, not merely
the read.
```

The measurement instrument is reporting a livelock as a terminal act, because the
sensor set has no vocabulary for "this latch is contradicted."

## Prior Point Fixes (whack-a-mole ledger)

Each entry patched one symptom of a latch that cannot be refuted; none made the
latch re-read its own grounds.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| `4801a6cd` (proposal 080 Phase 3) | Re-keyed the standdown from `(pr, head_sha)` to `pr_number`, releasing only on merge/close | Fixed re-knocking on every fresh head and **created** the permanent latch: a refuted claim now has no release at all |
| `270b42d1` (#2798) | Carved out one tend lap for genuinely-new review threads on a stood-down PR | Symptom-scoped to *review threads*, keyed on `databaseId`; a conflict or CI-red refutation still escapes nothing |
| `b9fd1833` (#2967) | Stopped the handoff re-knocking a **closed** PR | Widened the *terminal* release from merged to merged-or-closed; still terminal-only |
| `fb771ba9` + `e78107e8` + `8d725a32` (proposal 090) | Added `expires_at` / `prune_expired_standdowns` so issue-keyed claimed escalations age out | Added a second release axis for one record class and **explicitly denied it** to every PR handoff |
| `c36b6412` (proposal 091 steps 4–5) | Self-healed falsely-`serviced` comment ids so the #2798 valve reopens | A fix *to the escape valve* — the valve had itself latched shut |
| `2719d5a1` (#2968) | Exempted comment-review goals from `gate_stood_down` dequeue eviction | Same shape one store over: a per-goal exemption instead of re-reading the fact |
| `833595f0` (#2760) | Recorded a repetition-gate no-op on an honored stand-down eviction | Accounting for a suppression that should not have held |
| `fd09d3d5` (proposal 094 step 6) | Re-based `deliver_handoff` on `measure_no_progress_laps` consecutive no-progress laps | Tightened *minting*; release semantics untouched |
| `34f2263e` | Flipped `observed_ready_standdown_enabled` default-on | Gave the **dequeue** gate live recomputation — the ledger latch above it, which suppresses before the dequeue gate is ever reached, did not get it |

**Retired by this design:** the #2798 carve-out and its 091 self-heal (steps 9–10) —
an un-answered review thread promotes to `MERGE_BLOCK_THREADS_UNRESOLVED`, classifies
owned, and refutes the latch through the one general path, so the bespoke
`serviced_comment_ids` machinery and the reply-completion force-admit branch have
nothing left to do.

## Relevant Findings

- `maestro/escalation_bridge.py` (`StanddownRecord`) — carries `head_sha`
  (informational only), `delivered_at`, `serviced_comment_ids` and an optional
  `expires_at`. It records **no claim**: nothing on the row says what fact earned
  it, so nothing can check whether that fact still reads the same way.
- `maestro/escalation_bridge.py` (`prune_resolved_standdowns`, `prune_expired_standdowns`)
  — the only two release paths. The first consults a merged/closed predicate; the
  second an ISO deadline. Neither reads the PR's live state. `clear_standdown` is
  the third door and is called from exactly one place outside tests.
- `maestro/loop/pr_remediation.py` (`PrRemediationController.route_feedback`) — its
  standdown-upkeep block runs both pruners ahead of the early return on every tend
  pass, which is why the third pruner belongs in the *same function* — the lap that
  detects the conflict is the lap that would release the latch. It does **not** belong
  at the same line: that block runs before any per-PR fact is gathered and holds only
  `is_pr_settled`, so composing an owed act there would buy a host read per stood-down
  PR (step 6).
- `maestro/loop/pr_remediation.py` (`route_feedback`) — the docstring is explicit
  that the suppressed set includes conflicts: the #2798 carve-out admits one lap
  for un-serviced review threads while "the treadmill the standdown exists for —
  conflicts, quality-gate re-fires, already-serviced threads — stays suppressed."
  A conflict introduced *after* a merge-ready handoff is not that treadmill; it is
  fresh owned work misfiled as the thing the latch was built to stop.
- `maestro/loop/pr_remediation.py` (`route_user_pr_message`) — the only refutation
  release that exists today is a **human** typing the PR number: it calls
  `release_stand_downs` and `clear_standdown` before routing the demand. A latch
  whose sole non-terminal release is a human message is not self-correcting.
- `maestro/pr_owed_act.py` (`classify_pr_owed_act`, `OwedAct`) — already composes
  `classify_blocker_ownership` + `ci_diagnosis.classify_ci_conclusions` + a
  caller-supplied settled set into exactly one of `CODE_OWED` / `HANDOFF_READY` /
  `WAIT_CI` / `EXTERNAL_BLOCKED`. Pure, no network, zero SDK cost — and consumed by
  **nothing but `maestro/cli.py` (`cmd_pr_owed_act`)**. The oracle that names the
  latch's claim and its refutation is built, tested, and unwired from every actuator.
- `maestro/wip_primacy.py` (`select_task_focus`) — returns `TendOwnedPR` with the
  owned blocker for the first open owned PR whose blocker classifies owned. It is
  the verdict the log prints 156 times for #3911. It is wired as a **veto** in
  `_wip_owns_slot` (standing down `_implement_approved_plans`, `propose_task_docs`,
  `evolve`, the audit→directive bridge) and as the `wip_primacy` task gate — which
  has fired **0 times all-time** in `gate_activations.jsonl`, because remediation
  reaches `_run_task_phase` through `pending_pr_requests` and never walks the
  discretionary generation path the gate lives on.
- `maestro/agent.py` (`_gate_pr_remediation_ready`) — states the correct pattern in
  its own docstring: "the readiness fact is recomputed live each pop, so a blocker
  reappearing on the same head (a sibling merge, a new comment) re-admits the PR
  immediately — churn handled by construction." The house already knows recompute
  beats latch; it applied that at the dequeue gate and not at the ledger above it.
- `maestro/audit_checks.py` (`green_ready_handoff_prs`) — self-extinguishes on
  `has_standdown`, so once the latch exists rule 22 never re-evaluates the PR. This
  is correct for re-minting and is exactly why the arm cannot notice its own claim
  expiring.
- `maestro/pr_facts.py` (`observe_pr_readiness`, `PRReadiness.merge_block_reason`,
  `assess_pr_salvage`) — the shared, memoized readiness observation every PR-servicing
  face already reads per iteration, and the salvage oracle that already vetoes the
  *other* give-up actuators. `assess_pr_salvage` does **not** gate `deliver_handoff`,
  so a handoff is the one give-up the salvage chokepoint does not cover.
- `maestro/remediation_tracker.py` (`measure_handoff_relapse`) — counts tasks that
  re-engage an already-handed-off lineage, and documents itself as "the precise
  signature of standing-down-because-exhausted rather than standing-down-because-done."
  Under the current design it is pinned near zero by construction: the latch prevents
  the re-engagement it measures.
- `tests/structural_guards.py` (`_MERGE_TRUTH_GUARD`, `_SALVAGE_ORACLE_GUARD`,
  `_BLOCKER_OWNERSHIP_GUARD`, `_ATTEMPT_DISPOSITION_GUARD`) — the registry pattern a
  new single-owner oracle plugs into, run by `tests/test_structural_guards.py`
  regardless of any feature flag.

## Implementation Plan

### Phase 1 — Name the claim and measure the contradiction (no behaviour change)

1. [x] **The standdown-validity oracle.** new `maestro/standdown_validity.py` — a pure
   leaf owning the vocabulary: `StanddownClaim` (the fact a record asserts; today only
   `HANDOFF_READY`), `StanddownValidity` (`HOLDS` / `REFUTED`), and
   `assess_standdown_validity(claim, live_act)` returning `REFUTED` iff the live
   `OwedAct` is `CODE_OWED` — owned work has reappeared. `WAIT_CI`, `EXTERNAL_BLOCKED`
   and `HANDOFF_READY` all read `HOLDS`, so the latch keeps suppressing everything it
   was built to suppress. No I/O, no imports beyond `maestro/pr_owed_act.py`.
   - *Depends on:* none.
   - *Acceptance:* `assess_standdown_validity` returns `REFUTED` for `CODE_OWED` and
     `HOLDS` for each of the other three `OwedAct` members, with no filesystem or
     network access in the module.

2. [x] **Stamp the claim on the record.** `maestro/escalation_bridge.py` — add
   `claim: NotRequired[str]` to `StanddownRecord`, stamped `"handoff-ready"` by
   `deliver_handoff`; `_parse_standdown_entry` tolerates its absence and
   `_load_standdowns` defaults a missing claim to `HANDOFF_READY`, the only fact the
   two PR-keyed writers ever asserted. No reader consumes it yet.
   - *Depends on:* 1.
   - *Acceptance:* a record written by `deliver_handoff` round-trips with
     `claim == "handoff-ready"`, and a legacy row lacking the key loads as
     `HANDOFF_READY` without a rewrite of the ledger file.

3. [x] **The contradiction sensor.** `maestro/remediation_tracker.py` —
   `measure_contradicted_standdowns(standdowns, owed_acts)`, pure, returning the
   `{pr_number: OwedAct}` subset whose latched claim the live reading refutes;
   `format_pr_lap_summary` — which lives in this same module, *not* in
   `maestro/audit_checks.py` — annotates such a PR `[standdown-refuted]` instead of
   `[stood-down]`, wired from `maestro/agent.py`'s existing measured-data assembly off
   the memoized readiness reads. Zero SDK cost, zero new host calls. Landing this
   **before** the actuator gives the flip a baseline.

   The annotation must **not** be spelled as a `[stood-down…]` variant. The same module
   owns `_CHOSEN_QUIET_MARKER` (`[stood-down]`), a load-bearing marker documented as one
   *only I* emit and which `classify_noop` matches by **substring** over
   `result_summary` / `key_findings` to score a stand-down as chosen-rather-than-forced.
   A refutation annotation sharing that prefix invites a future substring or
   `startswith` reader to score a *contradicted* latch as a deliberate quiet act —
   precisely the miscount finding 8 already documents. `[standdown-refuted]` shares no
   prefix with the marker, and the step pins the distinction in a test.
   - *Depends on:* 1.
   - *Acceptance:* a stood-down PR whose live owed act is `CODE_OWED` renders
     `[standdown-refuted]` in the PR-LAPS line while a `WAIT_CI` one still renders
     `[stood-down]`; and `classify_noop` over a summary carrying only
     `[standdown-refuted]` does **not** score it as chosen quiet.

4. [x] **Structural guard.** `tests/structural_guards.py` — a `_STANDDOWN_VALIDITY_GUARD`
   registry entry asserting `maestro/standdown_validity.py` is the sole producer of
   `StanddownValidity` / sole definer of `assess_standdown_validity`, and that every
   standdown-release site in `maestro/escalation_bridge.py` is named in one enumerated
   set — so a fourth release path cannot appear unregistered. Runs regardless of the flag.
   - *Depends on:* 1.
   - *Acceptance:* a seeded rival `StanddownValidity` producer, and a seeded unlisted
     release function, each red the guard.

### Phase 2 — Release the refuted latch (flagged)

5. [x] **The third pruner.** `maestro/escalation_bridge.py` —
   `prune_refuted_standdowns(backend, live_owed_act)`, the sibling of
   `prune_resolved_standdowns` / `prune_expired_standdowns`: for each record whose
   **`claim` reads `HANDOFF_READY`** — the key step 2 stamps, and the only fact this
   oracle knows how to re-read — consult the injected owed-act callable through
   `assess_standdown_validity` and drop the `REFUTED` ones inside one atomic `mutate`
   closure. Fail-loud on a raising predicate, matching its two siblings. Not yet called.

   Selection keys on the claim, **not** on the absence of `expires_at`. Those are not
   the same set: `known_standdown_numbers`'s own docstring records that `expires_at`
   marks a *claimed-escalation* record, not an *issue-keyed* one, and that rule 12 keys
   "overwhelmingly on real PR numbers" — so an `expires_at`-bearing record is routinely
   a real PR, and an absence-of-`expires_at` filter would both sweep in claimed
   escalations this oracle has no claim to re-read and re-derive, badly, the membership
   step 2 exists to record. Records carrying any other claim, or none after the legacy
   default, are skipped by construction.
   - *Depends on:* 1, 2, 4.
   - *Acceptance:* the function releases exactly the `HANDOFF_READY`-claim records whose
     live reading is `REFUTED`; leaves a claimed-escalation record untouched **even when
     it is keyed on a real, `CODE_OWED` PR number**; and returns the sorted released
     numbers.

6. [x] **Wire it into the tend upkeep block.** `maestro/loop/pr_remediation.py`
   (`route_feedback`), `maestro/config.py`, `maestro/config_settings.py`,
   `maestro/toml_config.py`, `maestro.toml`, `maestro/flag_lifecycle.py` — call the new
   pruner behind `[loop] refutable_standdown_enabled` (default **off**, registered in
   `EXPERIMENTAL_FLAGS` with `review_by = 2027-09-30`). Each release appends a
   `standdown_refuted` row to `gate_activations.jsonl`.

   **The slot is not free where the two sibling pruners sit.** The standdown-upkeep
   block runs *ahead of `route_feedback`'s early return*, before any per-PR fact is
   gathered: the only oracle in scope there is `is_pr_settled`, and
   `classify_pr_owed_act` additionally requires `blocker`, `check_runs` and
   `unresolved_threads`, none of which that block holds — nor does it hold the head
   branch `observe_pr_readiness` is keyed on. Placing the call immediately after
   `prune_resolved_standdowns` would therefore buy a fresh host read per stood-down PR,
   not zero. The step instead wires the pruner at the point in the tend lap where the
   open-PR walk has already observed each PR's readiness and checks, passing those facts
   in, so the composition reads the per-iteration `observe_pr_readiness` /
   merge-truth memos and adds no host call. A stood-down PR the lap does not walk keeps
   its latch for that pass and is re-read on the next — acceptable, because the release
   is a latency question, not a correctness one. Confirming this is the step's own
   acceptance test, not an assertion.
   - *Depends on:* 5.
   - *Acceptance:* with the flag on, a tend pass over a stood-down `CONFLICTING` own PR
     clears its ledger record and logs one `standdown_refuted` activation, with a
     counting `CodeHost` mock showing **no** call beyond those the same lap makes with
     the flag off; with the flag off the upkeep block is byte-identical to today.

7. [x] **Withdraw the message, budget the release.** `maestro/escalation_bridge.py`,
   `maestro/config.py` / `maestro.toml` — on a refuted release, append exactly one
   id-keyed withdrawal message to `messages.jsonl` naming the PR, the blocker that
   refuted the claim, and that the act is being taken back; and persist a
   `refutation_releases` counter on the successor record so a PR may be released at most
   `[loop] standdown_refutation_release_cap` times (default 2) before the latch becomes
   permanent and one final message says so. A flapping PR then surfaces as a bounded,
   named failure rather than a message storm.
   - *Depends on:* 6.
   - *Acceptance:* the first refuted release sends one withdrawal message; a second
     handoff→refutation cycle on the same PR sends the second; the third leaves the latch
     in place, sends the terminal message once, and is idempotent across tend passes.

8. [ ] **Flip on, observe, tripwire.** `maestro/config.py`, `maestro.toml` — flip
   `refutable_standdown_enabled` to `true` everywhere in one reversible step after a
   bounded observation window against written tripwires: `measure_contradicted_standdowns`
   trending to zero, and per-PR `measure_no_progress_laps` **not** climbing back toward
   `pr_remediation_lap_budget`.
   - *Depends on:* 7.
   - *Acceptance:* the flag defaults `true` in both `Config` and the committed TOML, and
     the tripwire metrics are named in the proposal's observation subsection.

### Phase 3 — Retire the moles

9. [ ] **Retire the #2798 review-thread carve-out.** `maestro/loop/pr_remediation.py`
   (`route_feedback`) — delete the `has_standdown` force-admit branch and the
   reply-completion standdown gate: an un-answered thread promotes to
   `MERGE_BLOCK_THREADS_UNRESOLVED`, classifies owned, and refutes the latch through the
   one general path. Lands only after step 8 has proven the general path in production.
   - *Depends on:* 8.
   - *Acceptance:* a stood-down PR carrying a genuinely-new review thread is admitted for
     remediation with the carve-out code deleted, via the refutation release alone.

10. [ ] **Retire the serviced-id machinery and collapse the flag.**
    `maestro/escalation_bridge.py` — delete `serviced_comment_ids` from `StanddownRecord`
    plus `unserviced_comment_ids`, `mark_comment_ids_serviced` and
    `reconcile_serviced_comment_ids` (the 091 self-heal for a valve that no longer
    exists); move `refutable_standdown_enabled` to `CUTOVER_FLAGS` and delete the
    off-branch.

    The symbols have call sites **outside** `escalation_bridge.py`, and the step is not
    done until each is removed with them: `maestro/agent.py` imports and calls
    `mark_comment_ids_serviced` on the recorded-reply path (stamping the ids it just
    answered), and `maestro/loop/pr_remediation.py` calls
    `reconcile_serviced_comment_ids` and `unserviced_comment_ids` in the #2798 carve-out
    step 9 deletes. `maestro/agent.py`'s stamp is the one step 9 does not touch, so it
    must be dropped here or the build breaks on the deleted import.
    - *Depends on:* 9.
    - *Acceptance:* no module — `escalation_bridge.py`, `agent.py` or
      `loop/pr_remediation.py` — references the retired symbols; legacy rows carrying the
      dropped key still load; and `tests/test_config_flag_lifecycle.py` reports the flag
      collapsed.

## Testing Strategy

- **new `tests/test_standdown_validity.py`** — the oracle's full truth table across all
  four `OwedAct` members and every `StanddownClaim`; purity (no `open`, no host seam)
  asserted by `ast` inspection in the house style.
- **`tests/test_escalation_bridge.py`** — `prune_refuted_standdowns` over a `mem_fs`
  ledger: a refuted PR released, a `HOLDS` PR retained, an `expires_at`-bearing
  issue-keyed record untouched, an empty ledger short-circuiting without a `mutate`, and
  a raising predicate propagating (fail-loud, no swallow). Record round-trip for the new
  `claim` and `refutation_releases` keys including the legacy-row default. Withdrawal
  message idempotence across repeated passes, and the release-cap terminal.
- **`tests/test_pr_remediation.py`** — `route_feedback` with the flag off (byte-identical
  upkeep) and on (release + one `standdown_refuted` activation), asserting the owed act is
  composed from the memoized reads with **no additional `CodeHost` call** via a counting
  mock; plus the post-release path where the conflict route mints the remediation.
- **`tests/test_remediation_tracker.py`** — `measure_contradicted_standdowns` over
  fixture ledgers, and the `[standdown-refuted]` vs `[stood-down]` rendering in
  `format_pr_lap_summary` (both live in `maestro/remediation_tracker.py`, so one test
  module covers them), including the no-contradiction and empty-ledger branches, plus
  the `classify_noop` negative control proving the new marker is not scored as the
  chosen-quiet `[stood-down]`.
- **`tests/test_structural_guards.py`** — the `_STANDDOWN_VALIDITY_GUARD` entry with a
  seeded rival producer and a seeded unregistered release site as negative controls, run
  with the flag both on and off.
- **`tests/test_config.py` / `tests/component/test_config_integration.py`** — the new
  flag and cap defaults, the committed-TOML parse, the overlay round-trip, and the
  `EXPERIMENTAL_FLAGS` `review_by` registration.
- **Regression pin for the live incident** — a `tests/test_pr_remediation.py` case
  reproducing the #3911 shape end to end: handoff delivered on a green PR, a sibling
  merge flips readiness to `CONFLICTING`, one tend pass releases the latch and the PR
  becomes remediation-eligible in the same pass. Fails on today's tree.

All tests use `mem_fs`, mock the code host and VCS seams, patch `asyncio.sleep`, and make
no real network or subprocess call, per CLAUDE.md.

## Risk Assessment

**The treadmill 080 closed could return.** This is the central risk and the design
bounds it three ways: only `CODE_OWED` refutes (a PR awaiting approval, awaiting CI, or
behind `main` keeps its latch); releases are capped per PR by
`standdown_refutation_release_cap`; and a released PR that re-greens earns a *fresh*
rule-22 handoff rather than silence. The tripwire is explicit —
`measure_no_progress_laps` per PR climbing back toward `pr_remediation_lap_budget` means
the design failed and the flag reverts it in one edit.

**Message churn to Thomas.** Each refuted release sends one withdrawal. A PR that
flip-flops green↔conflicting could generate a pair of messages per cycle; the release cap
bounds this at `cap` withdrawals plus one terminal message per PR lineage.

**Migration.** None required. `claim` and `refutation_releases` are optional keys on
`StanddownRecord`; `_load_standdowns` defaults them, matching how `expires_at` was added
by proposal 090 and how the legacy `"<pr>@<head>"` keys are collapsed on read. No state
file is rewritten on upgrade.

**Trust chain.** Unchanged. The new pruner only *deletes* suppression records; it holds
no push, merge or approval capability, opens no SDK session, and adds no host call. The
act it unblocks is the ordinary conflict remediation, which still runs under the existing
action gate and human-in-the-loop invariants. Releasing a latch strictly *increases* the
work routed through those gates rather than bypassing any of them.

**Rollback.** `[loop] refutable_standdown_enabled = false` restores today's behaviour
exactly for steps 5–8. Steps 9–10 delete code and their rollback is `git revert`, which
is why they are sequenced after the general path has been observed in production.

**Falsifiability.** If the design is right: `measure_contradicted_standdowns` (step 3)
trends to zero; `measure_handoff_relapse` — pinned near zero today by construction —
reports a small positive count reflecting genuine post-handoff re-engagement; and the
single-PR WIP-primacy deferral rate, 130 of 130 iterations in the measured window, falls
to the length of one tend pass. If it is wrong: `standdown_refuted` fires repeatedly for
the same PR against a rising `measure_no_progress_laps`, which the release cap makes
visible and bounded rather than unbounded.

## Out of scope

- **`stand_down_verdicts.jsonl` (the other stand-down store).** Its eviction already
  holds only while git-unlanded with a Tend-clearable blocker and auto-releases on a new
  head, so it is not latched in the same way. The `2719d5a1` per-goal exemption in the
  ledger above belongs to that store and is left alone; extending the validity oracle to
  it is a later step if the sensor shows it needs one.
- **Issue-keyed claimed-escalation records.** A plain issue number has no PR from which
  to read an owed act; they keep their `expires_at` age release from proposal 090, and
  the new pruner skips them by construction.
- **The batch-mint refutation generator (finding 7).** Rule 22 handing off every
  simultaneously-green owned PR in one cycle, at most one of which can merge cleanly, is
  a *landing-serialization* concern that belongs to proposal 128's merge-queue domain.
  This design makes the resulting refutation **recoverable**; it does not stop the batch
  from producing it.
- **The WIP owner as an actuator.** `select_task_focus` can veto discretionary work but
  cannot mint the remediation it names, and the `wip_primacy` gate has fired zero times
  all-time because remediation never walks the discretionary-generation path. Once the
  latch releases, the ordinary tend conflict route mints the work, so closing this class
  does not require it — but the gap is real and worth its own proposal.
- **Rule 22's readiness threshold.** Whether a PR should be handed off the instant it
  reads green, or only after a settling window that would have caught the #3910/#3911
  collision, is a minting-policy question this proposal deliberately does not reopen.
