# 044: Actuator Gap — Audit-Finding Stall Escalation

| Field | Value |
|-------|-------|
| **Impact** | LOW (opt-in, zero new SDK cost, zero schema change) |
| **Status** | shipped |
| **Affects** | `maestro/audit_checks.py`, `maestro/memory.py`, `maestro/agent.py` (`generate_task`), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `CLAUDE.md`, `docs/self-evolution.md` |
| **Depends on** | none — closes a gap inside the existing `audit_self_model` contract |

## Description

> Status updated to shipped on 2026-04-27 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Maestro has a long catalogue of self-knowledge surfaces (self-model fields, audit findings, open questions, dream digests, rejection patterns, health summaries, purpose hypothesis, question resolutions, task-type distribution, version archive, journal narrative, recent self-directed pairs) and a separate catalogue of hard actuators (burn-rate throttle, rejection cooldown, executor loop detection, secret-scan hard-block, `expect_status` TOCTOU, retry caps, dedup early-outs, budget gating, standby window, stage-specific prompts, the audit-cap subcap, `_LOOP_PHASES` resume, `_BUDGET_GATED_PHASES`).

Every self-knowledge surface is injected into prompts as **advisory text** the LLM may or may not act on. Not one of the actuators is currently driven by self-knowledge. **Self-knowledge generates no mechanical force.**

The audit subsystem already exposes this gap. From `audit_checks.py` module docstring: rules "self-extinguish" *only* when the LLM voluntarily INTEGRATEs them. A `KEPT`-but-unintegrated finding fires every cycle with zero behavioral cost — the contract is that integration happens, but nothing enforces it.

This proposal builds the *first* hard actuator driven by a self-knowledge surface. When an `[audit]` open question survives N consecutive `audit_self_model` cycles without being INTEGRATEd, `generate_task` mechanically returns `(None, None)` — exactly mirroring the existing issue-dedup early-out at `agent.py`. The block clears automatically when the LLM acts (the `[audit]` entry disappears from `open_questions`, dropping the count). Ship behind an opt-in flag so we get lived data before defaulting on. Frame as a bug fix (audit's existing self-extinguishing contract is currently unenforced), not a feature.

## Soft-vs-Hard Taxonomy

A **soft surface** is text rendered into a prompt: it influences the next LLM response only if the model chooses to weight it. Soft surfaces fail open — under any kind of LLM drift, distraction, or context overflow they silently lose force. Examples in Maestro: `## Health Summary` injection, `recent_audit_findings` block, `analyze_rejection_patterns` aggregate, `next_task_directive`, dream digest body.

A **hard actuator** is a code-level branch that changes program control flow regardless of what the LLM produces. Hard actuators fail closed — they execute even when the LLM is silent, miscalibrated, or wrong. Examples: `_BUDGET_GATED_PHASES` skip, secret-scan `has_hard_block`, `expect_status` TOCTOU flip, `_LOOP_PHASES` resume, executor loop detection, dedup early-outs.

The two are not equivalent. An LLM can route around a soft surface; it cannot route around a hard actuator. Today every self-knowledge surface in Maestro is soft, which makes the architecture's introspection a *suggestion* and not a *constraint*. The audit-stall actuator is the first place we wire a soft surface into a hard actuator.

## Design — simplest viable

- **No persisted schema change.** Stall count is computed-on-read from `audit_history.jsonl` rows (already records `findings: list[AuditFinding]` and zero-finding cycles). No `AuditFinding` / `AuditResult` field changes; no `_TRACKED_CLASSES` change; no `compute_schema_fingerprint()` bump. Rollback = flip the flag.
- **Counter semantics.** For each `[audit]`-prefixed entry in `model.open_questions`, walk recent `AuditResult` rows newest→oldest and count the *consecutive* cycles whose `findings` reproduce the same `f"[audit] {finding.kind}: {finding.statement[:160]}"` string (matched via `_normalize_question` for case/whitespace drift). The first cycle without the match resets the count. Consecutive (not cumulative) is correct: any cycle without the finding is evidence of integration or condition resolution.
- **Gate.** Inserted in `generate_task` between the `load_self_model()` call and the prompt build, so the SDK call is skipped entirely when the gate trips. Returns `(None, None)` — same shape as the issue-dedup branch. The `next_task_directive` clear-on-success runs *after* the SDK call and is therefore preserved across gate firings, which is the desired one-shot semantics. Directives are not a bypass — directives are LLM-set, and an LLM ignoring integration in favour of self-directed work is exactly the failure mode the gate addresses.
- **Configuration.** Two new keys under a new `[audit]` TOML section: `stall_threshold` (default 5, ≥ 1) and `stall_block_enabled` (default `false` for safe rollout). Threshold is in *audit cycles*, not loop iterations — at default `[loop] audit_self_every=10` that is ≈50 iterations of grace; called out in the TOML comment, in CLAUDE.md, and in `docs/self-evolution.md`.

## Self-referential proof-of-concept

The audit subsystem now actuates the audit subsystem. Repeat-firings of an `[audit]` finding convert from soft warning into real progress cost. The pattern (computed-on-read from existing JSONL, code-level branch in a phase the LLM cannot route around, reversible config flag) is the template for every future bridge.

## Risks & edge cases

1. **Deadlock — the LLM cannot integrate.** Mitigations: opt-in flag, `contemplate` still runs every iteration and re-presents the finding, Thomas can clear the entry via the dashboard self-model editor, a future bridge (auto-removal at 2× threshold) is the planned backstop.
2. **`MAX_FINDINGS_PER_CYCLE` clip resets the counter.** When >3 rules fire simultaneously and a finding is dropped by the cap, the cycle won't surface it and the counter resets. Acceptable: rare and conservative.
3. **Cycle-count ≠ iteration-count.** Threshold 5 ≈ 50 iterations at `audit_self_every=10`. Documented in TOML, CLAUDE.md, and `docs/self-evolution.md`.
4. **`check_stalled_question` interaction.** Once the gate engages, no new tasks are generated → no new `question_resolutions.jsonl` rows → `check_stalled_question` does not feedback-couple. The stall counter reads from `audit_history.jsonl` only.
5. **Phase-resume idempotency.** The gate is pure-read + pure-decision before any state mutation — safe under `_LOOP_PHASES` resume.
6. **Threshold change mid-flight.** Lowering immediately trips the gate (desired); raising relaxes (desired). No persistence migration needed.

## Future bridge candidates (out-of-scope for this proposal)

The pattern this proposal establishes opens the door for further self-knowledge → actuator bridges:

- **Auto-removal at 2× threshold.** When an `[audit]` entry survives 2× the stall threshold, mechanically rewrite it to `[audit refuted] …` so the rule self-extinguishes. Backstop against deadlock (#1 above).
- **Health-gated stage transitions.** When `is_health_noteworthy()` reports failure-streak ≥ 5, refuse stage advancement until the streak clears.
- **Stalled-question auto-removal.** When `check_stalled_question` keeps firing on the same key for N cycles, mechanically prune the entry from `open_questions`.
- **Rejection-pattern policy gates.** When `analyze_rejection_patterns` shows scope-too-large dominating, mechanically clamp `Planner.decompose`'s step budget for the next K plans.

Each follows the same template: computed-on-read from existing JSONL, code-level branch the LLM cannot route around, reversible config flag, lived-data validation before defaulting on.
