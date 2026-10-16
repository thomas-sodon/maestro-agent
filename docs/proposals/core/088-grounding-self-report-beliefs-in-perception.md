# 088: Ground Self-Report Beliefs in Perception — Outcome-Gated Question Closure and Reality-Refutation Escalation

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/audit_checks.py`, `maestro/agent.py`, `maestro/escalation_bridge.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `CLAUDE.md`, tests |
| **Depends on** | none |
| **Derived from** | The merge-truth grounding layer (`MERGE_TRUTH_FIRST_PREAMBLE`, `_git_pr_merged_default`, `is_branch_merged_into_main`) and the audit-stall actuator (`compute_audit_stall_counts`, `[audit] stall_block_enabled`); an epistemic audit of which Maestro beliefs are reality-grounded vs. self-report-only |

## Description

> Status updated to shipped on 2026-06-22 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Maestro has built one excellent sensory organ — the merge-truth oracle (`git fetch` + `--is-ancestor` + `git log --grep` for the PR subject line) — and wired it tightly into one belief domain: PR remediation. Inside that domain, narration cannot outrun reality: the fetch-before-word contract forces a git check before any verdict word, and audit rules 10/13/14 (`check_phantom_remediation`, `check_phantom_resolution`, `check_unconfirmed_own_landing`) re-fire whenever a stamp claims a landing git cannot confirm. Every *other* belief Maestro holds is brain-only: reasoned and self-reported, watched by deterministic sensors that *measure and display* but never *bind or veto*.

Two consequences of that asymmetry are load-bearing. First, **the brain can declare victory the eyes never saw**: `_classify_disposition` closes an `open_question` purely from the before/after diff of the list, and the probing task's `outcome` is recorded on the `QuestionResolution` row but never consulted — a question a *failed* task probed can be marked answered. Second, **the brain can overrule the eyes indefinitely**: a git-grounded audit finding can be `REFUTE`d (the `[audit]` entry is rewritten to `[audit refuted] <counter-claim>`), and the finding simply re-fires next cycle with no escalation. When narration and git disagree forever, nothing makes git win.

This proposal closes both gaps with the cheapest possible mechanisms — deterministic, zero-SDK-cost checks over data that already exists — installing one principle in both directions. **Phase 1** makes the eyes able to veto a closure: a question removal whose only probe did not succeed is re-surfaced as an audit finding rather than silently trusted. **Phase 2** makes the eyes win a sustained dispute: when a *reality-grounded* finding is refuted across a threshold of cycles while git still contradicts the claim, Maestro delivers one durable escalation and stands the lineage down, reusing the `escalation_bridge` standdown shape.

The end state is that the grounding discipline already proven in the PR domain stops being domain-locked: a closed question must point at a successful probe, and a claim that disputes git cannot dispute it forever. Both phases are config-gated and self-extinguishing, so each step is safe to ship and reversible by flag.

## Relevant Findings

- `maestro/agent.py` (`_classify_disposition`) — derives `removed` / `refined` / `kept` / `unresolved` purely from the before/after diff of `open_questions`; the probing task's outcome plays no part. A question vanishing from the list with no prefix-overlapping successor is `removed` regardless of whether the task that probed it succeeded or failed.
- `maestro/agent.py` (`QuestionPair`) — already carries `outcome: TaskOutcome` alongside `question` / `finding` / `task_id`, and `_record_question_resolutions` already stamps that outcome onto each `QuestionResolution`. The signal needed to gate closure is present at the call site; nothing reads it.
- `maestro/memory.py` (`QuestionResolution`) — the persisted row holds both `outcome` and `disposition`, so groundedness is computable on read from existing rows with **no schema change** and no migration.
- `maestro/consciousness.py` (`update_self_model`) — the audit-findings section offers the LLM `INTEGRATE` / `REFUTE` / `KEEP`; `REFUTE` rewrites the `[audit]` open question to `[audit refuted] <position>`. The docstring states a finding "will re-appear in the next cycle if the underlying condition persists" — true, but for a git-grounded finding that means narration can dispute reality every cycle with no cost.
- `maestro/audit_checks.py` (`check_phantom_remediation`, `check_phantom_resolution`, `check_unconfirmed_own_landing`) — the three rules whose ground truth is git (via `_git_pr_merged_default` / `is_branch_merged_into_main`). Each carries a stable `AuditFinding.rule` name and `cites` the offending `task_id`(s), so a meta-check can select exactly the reality-grounded findings.
- `maestro/audit_checks.py` (`AuditFinding`) — fields `kind`, `statement`, `cites`, `rule`; `rule` is documented as the "stable string for telemetry and round-robin ordering," making it the natural key for a reality-grounded allowlist.
- `maestro/audit_checks.py` (`compute_audit_stall_counts`) — already computes, zero-SDK, how many cycles an `[audit]` question has survived; it is the established template for "count persistence across the audit history and actuate at a threshold," which Phase 2's sensor mirrors for reality-grounded rules.
- `maestro/audit_checks.py` (`format_known_state_summary`) — the single composition point where measured grounds become contemplation context (`UNLANDED-REMEDIATION`, `PROSE-VERDICT-GAP`, prediction accuracy, etc.); both new sensors surface here as one line each.
- `maestro/escalation_bridge.py` — the no-escalation-bridge already delivers exactly one merge-or-close message plus a durable `pr_number`-keyed standdown that blocks Tend-route minting until the PR merges/closes. Phase 2's actuator is a second trigger into this same durable-standdown shape, not a new delivery path.
- `maestro/config.py` (`audit_stall_block_enabled`) and `maestro/toml_config.py` (`("audit", "stall_block_enabled")`) — the canonical pattern for a default-off, `[audit]`-namespaced bool gating an actuator; the two new flags follow it exactly.

## Implementation Plan

### Phase 1 — The eyes veto a closure (outcome-gated question closure)

1. [x] **Add a pure groundedness predicate for question closures.** `maestro/audit_checks.py` — add `is_grounded_closure(resolution: QuestionResolution) -> bool` returning `False` only when `disposition == "removed"` **and** `outcome != TaskOutcome.SUCCESS`; every other disposition (`kept` / `refined` / `unresolved`) and every success-backed removal is grounded. Pure function over an existing row, so it works on historical resolutions without migration.
   - *Depends on:* none.
   - *Acceptance:* a `removed`+`failed` row is ungrounded, a `removed`+`success` row is grounded, and `kept`/`refined`/`unresolved` rows are grounded regardless of outcome.

2. [x] **Add the ungrounded-closure audit rule.** `maestro/audit_checks.py` — add `check_ungrounded_question_closure(...)` reading the recent `question_resolutions` tail, emitting one `AuditFinding` (kind `contradicted_claim`, `cites=[task_id]`) per ungrounded removal, gated behind a new default-off `[audit] ungrounded_closure_enabled`. The finding re-surfaces the closed question through the existing audit → `open_questions` path, so the LLM must re-open it or justify the closure; it self-extinguishes once the row scrolls out of the window or the question is re-resolved by a successful probe.
   - *Depends on:* 1.
   - *Acceptance:* a resolution tail with one `removed`+`failed` row yields exactly one finding citing that `task_id`; the same tail with the flag off yields none; a `removed`+`success` row yields none.

3. [x] **Register the rule and flag.** `maestro/agent.py` (`_audit_self_model` registry), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` — wire `check_ungrounded_question_closure` into the registered-check registry behind `audit_ungrounded_closure_enabled` (TOML `[audit] ungrounded_closure_enabled`, default `false`), matching the `stall_block_enabled` plumbing.
   - *Depends on:* 2.
   - *Acceptance:* with the flag on, `_audit_self_model` includes the rule's findings in a cycle that has an ungrounded removal; with it off, the registry omits the rule and behavior is byte-identical to today.

### Phase 2 — The eyes win a sustained dispute (reality-refutation escalation)

4. [x] **Add the reality-refutation sensor.** `maestro/audit_checks.py` — add a `_REALITY_GROUNDED_RULES` frozenset of the over-claim-on-unmerged git-grounded checks `check_phantom_resolution` / `check_unconfirmed_own_landing` (`check_phantom_remediation` is excluded — it fires only when git shows the PR *merged*, the opposite polarity the merge-or-close escalation serves), and `measure_reality_refutation(data_dir, n)` reading the `audit_history.jsonl` tail, grouping findings by `(rule, target)` over that set, and counting consecutive cycles each recurred. Mirrors `compute_audit_stall_counts`; zero SDK cost.
   - *Depends on:* none.
   - *Acceptance:* an audit-history tail with `check_phantom_resolution` firing for `#N` across three cycles reports a count ≥3 for `#N`; a single-cycle firing reports below threshold; a non-reality rule (e.g. `check_stability_claim_vs_failure_streak`) is excluded from the result.

5. [x] **Surface the sensor in contemplation.** `maestro/audit_checks.py` (`format_known_state_summary`) — add a `REALITY-REFUTATION` line when any `(rule, target)` count reaches a read threshold and the self-model carries a matching `[audit refuted]` open question naming the target. Pure display, no behavior change, safe to enable independently of the actuator.
   - *Depends on:* 4.
   - *Acceptance:* given a refuted, thrice-recurring git-grounded finding for `#N`, the summary contains one `REALITY-REFUTATION` line naming `#N`; with no matching `[audit refuted]` entry, no line appears.

6. [x] **Add the actuator: one escalation + durable standdown.** `maestro/escalation_bridge.py`, `maestro/agent.py` — when `measure_reality_refutation` crosses `[audit] reality_refutation_threshold` for a target **and** a matching `[audit refuted]` entry exists **and** git still contradicts the claim, deliver exactly one merge-or-close-style message ("you have asserted `#N` resolved N times; git shows it unmerged") and record a durable target-keyed standdown via the existing bridge. Idempotent per target; releases when git shows the PR merged/closed. Gated behind a new default-off `[audit] reality_refutation_enabled`.
   - *Depends on:* 5; reuses the `escalation_bridge` standdown store.
   - *Acceptance:* crossing the threshold with the flag on delivers exactly one message and writes one durable standdown for the target; a second cycle re-delivers nothing; a git result showing the PR merged clears the standdown and suppresses further escalation.

7. [x] **Document both grounds.** `CLAUDE.md` (one line each under the self-evolution / audit invariants), `maestro.toml` (`[audit]` keys), and the audit-rules count in `docs/self-evolution.md` — cite symbols only, no `file.py:line`.
   - *Depends on:* 3, 6.
   - *Acceptance:* `tests/test_no_line_citations_in_docs.py` and `tests/test_claude_md_brevity.py` pass with the two grounds described; the documented audit-rule count matches the registry.

## Testing Strategy

All tests use `mem_fs`, mock git via the same seams the existing merge-truth rules mock (`_git_pr_merged_default` / `is_branch_merged_into_main`), and make no real I/O, network, subprocess, or `sleep`.

- **`is_grounded_closure` (Step 1)** — table over the four dispositions × {success, partial, failed, investigation}; assert only `removed`+non-success is ungrounded.
- **`check_ungrounded_question_closure` (Step 2)** — happy path (one ungrounded removal → one finding citing the task), guard path (flag off → no findings), exemption paths (`removed`+`success`, `kept`+`failed`, empty log), and a malformed resolution row skipped without raising. Assert finding `cites` exactly the offending `task_id`.
- **Registry wiring (Step 3)** — assert the rule appears in `_audit_self_model`'s findings only when `audit_ungrounded_closure_enabled` is true, and that the off-path leaves the existing finding set unchanged.
- **`measure_reality_refutation` (Step 4)** — multi-cycle audit-history tails: a recurring git-grounded finding accumulates the count; a non-reality rule is excluded; a finding that lapses for a cycle resets its run. Assert per-target counts.
- **`format_known_state_summary` (Step 5)** — the `REALITY-REFUTATION` line appears iff count ≥ threshold *and* a matching `[audit refuted]` entry exists; assert it is absent when either condition fails, and that no other summary line changes.
- **Actuator (Step 6)** — patch the `escalation_bridge` store and the git oracle: crossing the threshold delivers exactly one message and one standdown; idempotency across a second cycle; release on a merged/closed git result; flag-off path delivers nothing. Assert message count and standdown-store contents exactly.
- **Cross-rule sync** — a test pinning `_REALITY_GROUNDED_RULES` to the actual `AuditFinding.rule` names of the two over-claim-on-unmerged git-grounded checks, so renaming a rule without updating the set fails loudly rather than silently dropping it from escalation.

## Risk Assessment

- **Both phases are default-off and config-gated.** `audit_ungrounded_closure_enabled`, `reality_refutation` display, and `reality_refutation_enabled` (actuator) are independent flags; rollback is flipping a flag, and an interrupted rollout leaves the loop byte-identical to today.
- **No schema migration.** Groundedness is computed on read from fields already on `QuestionResolution`; the reality sensor reads `audit_history.jsonl` as it already exists. No state-file fingerprint change, so `ensure_schema()` is untouched.
- **Closure-thrash risk (Phase 1).** Re-surfacing every ungrounded removal could re-open questions the LLM legitimately closed from a partial-but-conclusive finding. Mitigated by routing through the existing audit → `open_questions` path (the LLM can re-close with justification rather than being hard-blocked) and by the default-off flag; a tightening option — treat a finding that cites a verified artifact (merged SHA / passing-test reference) as grounded even on a non-success outcome — is noted for a follow-up rather than built now.
- **False escalation to Thomas (Phase 2).** An over-eager actuator could page on a benign refutation. Mitigated by three conjuncts (threshold count **and** a live `[audit refuted]` entry **and** a fresh git contradiction at fire time), per-target idempotency, and automatic release when git shows the PR merged/closed — the same conservative shape the `escalation_bridge` already proves.
- **Rule-name drift.** The reality-grounded set keys off `AuditFinding.rule` strings; a rename would silently drop a rule from escalation. Mitigated by the cross-rule sync test (Testing) that fails when the set and the live rule names diverge.
- **Trust chain.** Phase 2's only outward effect is one Telegram/message escalation through the existing bridge and a stand-down that *slows* the remediation loop — it grants no new mutating capability, opens no approval bypass, and posts to no public surface.

## Out of scope

- **Evidence-bound capability claims (feature #3).** Extending dream-style provenance (`task_source_ids`) to `capabilities_known` so each claim cites the `task_id` / `VersionRecord` SHA that earned it, with an audit rule flagging stale or git-contradicted claims, is the most architecturally significant follow-on but is a `SelfModel` schema change; deferred until these cheaper guards prove the pattern.
- **Prediction-calibration actuator (feature #4).** `measure_prediction_accuracy` already scores per-shape calibration but only displays it; turning sustained miscalibration into a directive/gate (mirroring the stall actuator) is a separate, self-contained increment.
- **Generalized action-effect oracle (feature #5).** Lifting the fetch-before-word contract into a reusable "claimed outward effect → cheap existence check" abstraction (with merge-truth and `check_phantom_escalation`'s message check as its first instances) is a refactor best done after a second concrete instance exists to generalize from.
