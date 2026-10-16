# 090: Narration → Action Linkages — One Cue Vocabulary, Completed Escalations, and Durable Directive Delivery

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | burn-in:observing |
| **Affects** | new `maestro/narration_cues.py`, `maestro/audit_checks.py`, `maestro/outcome_grounding.py`, `maestro/escalation_bridge.py`, `maestro/agent.py`, `maestro/task_gates.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro.production.toml`, `docs/self-evolution.md`, tests |
| **What landed** | Step 1 — the shared leaf module `maestro/narration_cues.py` (`RESOLUTION_KEYWORDS` / `RESOLUTION_RE` / `RESOLUTION_MARKERS` / `ESCALATION_PROSE_MARKER` / `COMMITTED_SHA_RE` / `SELF_COMMITTED_SHA_RE`), with `maestro/outcome_grounding.py` and `maestro/audit_checks.py` re-pointed at it via aliased imports and the narrow⊆wide SHA-containment test in `tests/test_narration_cues.py`. Step 2 — rule 13 (`check_phantom_resolution` / `unmerged_resolution_claims`) now keys on the word-boundary `RESOLUTION_MARKER_RE` (`RESOLUTION_KEYWORDS` + the noun `resolution`), gaining `merged`/`done` and dropping substring-inside-word matches. Step 3 — the rule-12 delivery actuator `deliver_claimed_escalation` (`maestro/escalation_bridge.py`), behind `[audit] phantom_escalation_delivery_enabled` (default-off, `maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`), routed for each ≥2-lap phantom-escalation finding in `maestro/agent.py` (`_audit_self_model`); the durable issue-keyed standdown it records is aged out by `prune_expired_standdowns` (`maestro/escalation_bridge.py`), invoked from `maestro/loop/pr_remediation.py`. Step 4 — the rule-14 forced arm behind `[audit] unconfirmed_landing_forced_enabled` (default-off, `maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`): `check_unconfirmed_own_landing` (`maestro/audit_checks.py`) widened to admit PR-less SHA-only handoff claims (`pr_number = None`), and `_audit_self_model` (`maestro/agent.py`) completes each past-grace finding by routing a PR-citing one through `deliver_handoff` (`maestro/escalation_bridge.py`) and writing the exported `_SELF_COMMITTED_FIRE_OR_CLOSE_DIRECTIVE` (`maestro/outcome_grounding.py`) into an empty directive slot for a PR-less one. Step 5 — the dropped-directive ledger `maestro/directive_ledger.py` (`offer_directive` / `replay_dropped_directive`, appending `{text, source, pr/sha grounding, iteration}` to a bounded `dropped_directives.jsonl`), behind `[loop] dropped_directive_replay_enabled` (default-off, `maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`), re-offered by the lowest-precedence `dropped_directive_replay` gate (`_dropped_directive_replay_run`) in `maestro/task_gates.py` (`GATE_CHAIN`). Step 6 — the actuation-telemetry sensors `measure_narration_conversion` + `format_narration_conversion_summary` (`maestro/audit_checks.py`) rendering a `NARRATION-CONVERSION` line in `format_known_state_summary`, fed by `log_gate_activation` rows (actions `directive` / `handoff` / `dropped`) emitted from the narration actuators in `_audit_self_model` (`maestro/agent.py`). Step 7 (partial) — the graduation criteria are recorded in `docs/self-evolution.md` (the "Forced-arm graduation criteria (proposal 090)" subsection: observation window / firing threshold / zero-refutation / settlement rules), and the `[audit]` production canary in `maestro.production.toml` arms the two surviving forced arms (rules 12/14; the rule-13/15 arms were later retired by proposal 105) plus the 088 reality-refutation watchdog under `MAESTRO_ENV=production` only, opening the observation path without touching any base default. |
| **What is missing** | Step 7's evidence-gated flip, now scoped to two forced arms. Rules 13 and 15 (and their `phantom_resolution_forced_enabled` / `narration_resolved_must_settle_enabled` flags) were retired by proposal 105 — its mechanical outcome authority subsumes the pure phantom-*verdict* rules — so only the two forced-*delivery* arms remain to graduate: `phantom_escalation_delivery_enabled` (rule 12) and `unconfirmed_landing_forced_enabled` (rule 14) stay default-off in `maestro.toml` / `maestro/config.py` (armed only in the `maestro.production.toml` canary), and the observed window numbers have not yet been appended to the graduation subsection — both await the separate human-reviewed graduation PR. |
| **Derived from** | The FM-series false-throughput incidents (#2845, #2846, #2848, #2849, #2853) and the shipped grounding track — 087 (merge-truth chokepoint), 088 (perception-grounded beliefs), 089 (PR readiness) |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Maestro already has a rich pipeline that senses its own narration — resolution
keywords in a `result_summary`, "escalating #NNNN" prose, cited PR numbers and
committed SHAs — and, in the best cases, converts that narration into an
action: a record-time outcome downgrade, a fire-or-close
`next_task_directive`, or a merge-or-close handoff with a durable standdown.
But the linkages between sensing and acting are uneven and lossy. The cue
vocabulary is defined three times with divergent membership, so the sensors
disagree about what counts as a resolution claim. Two of the four
narration-mismatch audit rules (12 and 14) have no actuator arm at all — their
findings dead-end as journal entries and `[audit]` questions. The
audit→directive bridge routes only the first finding of a cycle, and a
fire-or-close directive minted at record time is silently discarded whenever
the one-shot directive slot happens to be occupied. Nothing measures how often
a detected narration problem actually became an action, so the default-off
forced arms (FM2, FM5) have no evidence path toward graduation.

This proposal tightens each linkage without adding any LLM cost. One shared
cue module makes every sensor and binder key on the same prose. Rule 12's
phantom escalation gets the most direct fix possible: instead of merely
flagging that a claimed escalation message was never sent, the loop
mechanically *sends it* — the narration's claimed action is completed, not
re-litigated. Rule 14 gains the forced arm that rules 13 and 15 already have,
completing the matrix. Directives that lose the race for the one-shot slot
land in a small grounded ledger and are re-offered while their grounding is
still live. Finally, every narration-grounded actuation is logged through the
existing gate-activation trace so a new conversion sensor can report
detections → actuations → settlements, giving the operator the evidence needed
to flip the forced-arm defaults on.

End state: a narrated claim anywhere in a task record either matches reality,
is mechanically corrected at write time, or is converted — durably, at zero
SDK cost — into exactly one action (a directive, a delivered message, or a
standdown), with a per-sensor conversion line in contemplation showing the
pipeline working.

## Relevant Findings

- `maestro/outcome_grounding.py` (`_RESOLUTION_KEYWORDS`, `_RESOLUTION_RE`)
  and `maestro/audit_checks.py` (`_NARRATION_RESOLUTION_KEYWORDS`,
  `_NARRATION_RESOLUTION_RE`) — two byte-identical five-keyword tuples
  (`resolved`, `landed`, `merged`, `fixed`, `done`) with independently
  compiled word-boundary regexes. No shared definition site; a keyword change
  must be made twice or the bind and rule 15 silently diverge.
- `maestro/audit_checks.py` (`_RESOLUTION_MARKERS`, rule 13) — a *third*
  vocabulary (`resolved`, `resolution`, `landed`, `fixed`), substring-matched.
  It omits `merged` and `done`, so a `success` narrating "merged #NNNN" never
  ages into a phantom-resolution finding even though rule 15
  (`check_narration_verdict_mismatch`) and `ground_landing_claim` both treat
  "merged" as a resolution claim; substring matching also fires inside larger
  words, which the other two vocabularies deliberately exclude.
- `maestro/audit_checks.py` (`_COMMITTED_SHA_RE`, rule 14) vs
  `maestro/outcome_grounding.py` (`_SELF_COMMITTED_SHA_RE`, FM4) — a
  deliberate, documented widening (the `committed as \`<sha>\`` cue), but the
  containment relationship (wide accepts everything narrow accepts) is
  asserted nowhere; independent edits can split sensor and binder coverage
  without any test failing.
- `maestro/audit_checks.py` (`check_phantom_escalation`, rule 12) — detects
  "escalating #NNNN" prose in `partial` laps with no matching outbound message
  and emits a finding whose statement instructs "Deliver the merge-or-close
  message or stop re-stamping the skip" — but no code path delivers it. The
  idempotent delivery machinery the finding asks for already exists:
  `maestro/escalation_bridge.py` (`deliver_handoff`, the `pr_number`-keyed
  standdown ledger, `prune_resolved_standdowns` auto-release).
- Forced-arm asymmetry — rule 13 has `unmerged_resolution_claims` behind
  `[audit] phantom_resolution_forced_enabled` and rule 15 has
  `narration_resolution_unlanded_claims` behind
  `[audit] narration_resolved_must_settle_enabled` (both default-off, both
  routing through `deliver_handoff`); rules 12 and 14 have no forced arm.
  Rule 14's `partial` on-branch handoff claims (`_ON_BRANCH_HANDOFF_MARKERS` +
  `_COMMITTED_SHA_RE`) dead-end in the journal, while
  `bind_self_committed_success_to_landing` (FM4) covers only newly-stamped
  `success` records at write time — an aged, already-recorded handoff claim
  has no actuator.
- `maestro/agent.py` (`_audit_self_model`) — the audit→directive bridge routes
  only `findings[0]` into an *empty* `next_task_directive`; the cycle's other
  findings (up to `MAX_FINDINGS_PER_CYCLE`) reach only the journal and
  `[audit]` open questions.
- `maestro/agent.py` (`run`, §5c-iii/§5c-iv) — the fire-or-close directives
  returned by `bind_diagnostic_success_to_landing` and
  `bind_self_committed_success_to_landing` are written only when
  `next_task_directive` is empty. The bind is a one-shot record-time event: a
  drop is silent, unrecorded, and never retried, so the demanded disposition
  ("land #N or close it") evaporates whenever another bridge won the slot that
  iteration.
- `maestro/audit_checks.py` (`measure_directive_compliance`,
  `log_gate_activation`) — directive compliance is measured by correlating
  `gate_activations.jsonl` rows with subsequent task keywords, but the
  bind-time directive writers in `run()`, the audit→directive bridge, and the
  forced arms never call `log_gate_activation` — narration-grounded
  actuations are invisible to the compliance sensor.
- `maestro/lever_capture.py` (`fire_and_capture`) — the restatement sweep
  already demonstrates the capture pattern for zero-SDK actuator firings
  (`lever_attempts.jsonl`); this proposal extends the same observability
  posture to the narration actuators via the gate-activation trace.
- `maestro/task_gates.py` (`GATE_CHAIN`, `run_gate_chain`) and
  `tests/test_task_gates.py`
  (`test_gate_order_matches_documented_precedence`) — the declarative gate
  registry and its order-pinning test are the established slot for adding a
  new lowest-precedence directive gate.

## Implementation Plan

1. [x] **Shared narration-cue vocabulary.** new `maestro/narration_cues.py`,
   `maestro/outcome_grounding.py`, `maestro/audit_checks.py` — move the
   resolution keyword tuple + word-boundary regex, rule 13's substring marker
   tuple, `_ESCALATION_PROSE_MARKER`, and both committed-SHA regexes into one
   module; re-point every importer. Pure relocation — matching behaviour is
   byte-identical, and the deliberate narrow/wide SHA split is preserved but
   now co-located and cross-tested.
   - *Depends on:* none.
   - *Acceptance:* each cue has exactly one definition site (grep finds no
     duplicate tuple); a new test asserts every `_COMMITTED_SHA_RE` match on a
     cue corpus is also matched by `_SELF_COMMITTED_SHA_RE`.
2. [x] **Close rule 13's vocabulary blind spot.**
   `maestro/narration_cues.py`, `maestro/audit_checks.py` — rule 13
   (`check_phantom_resolution` and its forced arm
   `unmerged_resolution_claims`) keys on the shared word-boundary resolution
   regex plus its `resolution` extra, gaining `merged`/`done` and dropping
   substring matches inside larger words. Blast radius is bounded by the
   existing grace-age sidecar and per-rule findings cap.
   - *Depends on:* 1.
   - *Acceptance:* a `success` summary "merged #4242" with #4242 unmerged past
     the grace age yields a `phantom_resolution` finding; a summary whose only
     cue is a substring inside a larger word yields none.
3. [x] **Deliver the claimed escalation — rule 12 actuator.**
   `maestro/escalation_bridge.py`, `maestro/agent.py`, config surfaces — new
   `deliver_claimed_escalation(data_dir, issue_number, lap_count)` reusing the
   message + standdown-ledger substrate of `deliver_handoff`; behind new
   `[audit] phantom_escalation_delivery_enabled` (default-off),
   `_audit_self_model` routes each rule-12 finding with ≥2 prose laps through
   it, mechanically sending the merge-or-close message the narration claimed.
   Rule 12 self-extinguishes the next cycle because the delivered message now
   satisfies its cross-reference; the ledger auto-releases via
   `prune_resolved_standdowns` for PR-shaped numbers. Rule 12 keys on
   `extract_issue_numbers`, which can cite a plain issue rather than a PR — a
   number `is_pr_resolved` can never settle — so issue-keyed entries instead
   carry an age-based expiry, ensuring no standdown durably blocks Tend-route
   minting for a number that will never merge or close as a PR.
   - *Depends on:* none (1 recommended first).
   - *Acceptance:* flag-on, a synthetic history of two "escalating #N"
     `partial` laps with an empty message store produces exactly one
     `messages.jsonl` row and one standdown; a second audit cycle is a no-op
     and rule 12 emits no finding.
4. [x] **Rule 14 forced arm — complete the 12/13/14/15 matrix.**
   `maestro/audit_checks.py`, `maestro/agent.py`, config surfaces — behind new
   `[audit] unconfirmed_landing_forced_enabled` (default-off), an
   unconfirmed-own-landing finding past grace whose cited PR is still
   git-unlanded routes through `deliver_handoff`; a claim citing a SHA but no
   PR instead offers the FM4 fire-or-close directive (the
   `_SELF_COMMITTED_FIRE_OR_CLOSE_DIRECTIVE` template, exported from
   `maestro/outcome_grounding.py`) into the directive slot. The PR-less arm
   requires widening rule 14's detector: today `check_unconfirmed_own_landing`
   skips any summary whose `extract_issue_numbers` is empty, so SHA-only
   handoff claims must be admitted as candidates — a sensor-behaviour change,
   called out like step 2's, bounded by the same grace-age sidecar and
   per-rule cap. Covers the aged, already-recorded handoff claims FM4's
   write-time bind cannot reach.
   - *Depends on:* 1.
   - *Acceptance:* flag-on, an aged rule-14 finding with an unlanded SHA and a
     cited PR yields one message + standdown; the PR-less variant sets the
     fire-or-close directive when the slot is empty.
5. [x] **Dropped-directive ledger and replay gate.** new helper in
   `maestro/consciousness.py` (or a small `maestro/directive_ledger.py`),
   `maestro/agent.py`, `maestro/task_gates.py`, config surfaces — all
   narration-grounded directive writers route through one
   `offer_directive(model, text, source, grounding)` helper: it fills an empty
   slot, else appends `{text, source, pr/sha grounding, iteration}` to a
   bounded `dropped_directives.jsonl`. A new lowest-precedence `GATE_CHAIN`
   directive gate re-offers the oldest entry whose grounding is still live
   (re-verified through `is_pr_settled` / branch ancestry at offer time) and
   prunes settled or expired entries; behind
   `[loop] dropped_directive_replay_enabled` (default-off).
   - *Depends on:* none.
   - *Acceptance:* a fire-or-close directive dropped while the slot is
     occupied is re-offered on a later iteration while its PR is unlanded, and
     is pruned without re-offer once the PR merges.
6. [x] **Actuation telemetry — measure narration → action conversion.**
   `maestro/agent.py`, `maestro/audit_checks.py`, `maestro/task_gates.py` —
   every narration-grounded actuation (audit→directive bridge, §5c bind
   directives, forced arms, steps 3–5) logs through `log_gate_activation`
   (actions `directive` / `handoff` / `dropped`); new
   `measure_narration_conversion()` reports, per sensor, detections →
   actuations → post-actuation settlement, and
   `format_narration_conversion_summary()` adds a `NARRATION-CONVERSION` line
   to `format_known_state_summary` following the existing sensor-summary
   pattern. Zero SDK cost.
   - *Depends on:* 3, 4, 5 (degrades gracefully if only some landed).
   - *Acceptance:* a bind-time directive write appends a
     `gate_activations.jsonl` row, and the known-state summary renders a
     `NARRATION-CONVERSION` line with non-zero counts from synthetic history.
7. [ ] **Graduation — flip the forced-arm defaults on evidence.**
   `maestro.toml`, `maestro/config.py`, `docs/self-evolution.md` — once the
   step-6 telemetry shows a documented threshold of firings with zero
   reality-refuted escalations (the 088 watchdog) over an observation window,
   flip the two surviving forced-delivery flags — `phantom_escalation_delivery_enabled`
   (step 3) and `unconfirmed_landing_forced_enabled` (step 4) — to
   default-on, recording the criteria and the observed numbers in
   `docs/self-evolution.md`. (The original `phantom_resolution_forced_enabled`
   and `narration_resolved_must_settle_enabled` arms were retired with rules 13
   and 15 by proposal 105, so only the rule-12/14 delivery arms remain to
   graduate.)
   - *Depends on:* 6.
   - *Acceptance:* the two surviving defaults are `true` in `maestro.toml`,
     config tests updated, and the graduation criteria + evidence are recorded
     in `docs/self-evolution.md`.

## Testing Strategy

All unit tests use `mem_fs` and mocked git/host oracles (`is_pr_settled`,
ancestry callables); no subprocess, network, or sleep.

- **Step 1** — golden parity: the pre-move and post-move regexes produce
  identical match sets over a cue corpus (resolution phrases, negations like
  "not yet fixed", SHA cue variants); the narrow⊆wide SHA containment test;
  import-site checks that `outcome_grounding` and `audit_checks` consume the
  shared module.
- **Step 2** — rule 13 fires on "merged"/"done" claims past grace (happy
  path), does not fire within grace (guard), no longer fires on
  substring-inside-word matches (regression); forced-arm
  `unmerged_resolution_claims` covered for the same delta.
- **Steps 3–4** — flag-off short-circuits (no message, no standdown, no
  finding change); flag-on single delivery; idempotence on the second cycle;
  ledger auto-release via a mocked `is_pr_resolved`; age-based expiry prunes
  an issue-keyed entry `is_pr_resolved` cannot settle; the widened rule-14
  detector admits a SHA-only handoff claim past grace (and still skips it
  within grace); rule-14 PR-less variant writes the directive only into an
  empty slot.
- **Step 5** — occupied-slot drop appends a ledger row; replay gate re-offers
  only live groundings (mocked settled → pruned, unlanded → re-offered);
  bounded-ledger cap; `GATE_CHAIN` order re-pinned (extend the existing
  order-pinning test pattern).
- **Step 6** — activation rows appended for each writer; conversion
  measurement over synthetic `gate_activations.jsonl` + task history; summary
  line golden-string test; empty-history renders no line (suppression guard).
- **Step 7** — config default tests updated; a docs test is not required (the
  criteria live in prose), but `tests/test_config.py` pins the new defaults.

## Risk Assessment

- **Refactor drift (steps 1–2).** Mitigated by golden parity tests before any
  behavioural step lands; step 2 and the step-4 detector widening are the only
  sensor-behaviour changes, each bounded by the existing grace-age sidecar and
  per-rule caps. Rollback:
  revert the import re-pointing; no state-file changes.
- **Wrong or noisy mechanical escalations (steps 3–4).** Both actuators are
  default-off, idempotent by standdown key, auto-released on merge/close, and
  watched by the 088 reality-refutation loop, which escalates if a mechanical
  claim is itself contradicted by git. The ≥2-lap threshold in step 3 keeps a
  single stray "escalating" phrase from sending mail.
- **Directive replay loops (step 5).** The replay gate is default-off, sits at
  the lowest precedence (never preempts a fresher bridge), re-verifies
  grounding at offer time so a settled PR can never be re-demanded, and the
  ledger is bounded with age-based pruning — a stale entry expires rather
  than recirculating. `dropped_directives.jsonl` is a new sidecar; no schema
  migration is needed.
- **Telemetry volume (step 6).** `gate_activations.jsonl` already exists and
  is tail-read; the added rows are one per actuation, well under the file's
  existing growth rate. If proposal 072 (bounded JSONL growth) lands first,
  the new sidecar adopts its rotation.
- **Premature graduation (step 7).** The flip is a separate, human-reviewed PR
  gated on recorded evidence; each flag can be reverted independently in
  `maestro.toml` without code changes.

## Out of scope

- Widening `TaskOutcome` with a first-class `investigation` value (noted as
  out of scope in `bind_diagnostic_success_to_landing`'s contract).
- LLM-based narration classification — every linkage here stays keyword/regex
  + git-fact, zero SDK cost, consistent with the grounding track.
- Redesigning `GATE_CHAIN` precedence or merging the standdown ledgers
  (`escalation_handoffs.json` vs `stand_down_verdicts.jsonl`) — worth its own
  proposal if the replay gate surfaces contention.
