# 113: Self-Model as Instrument — Cadence-Gate the Priced Introspection Narrative and Retire the Decoupled Purpose/Stage Machinery

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **What landed** | Phase A — the `[loop] contemplate_every` cadence knob (`maestro/config.py` `loop_contemplate_every`, `maestro/toml_config.py`, `maestro.toml`, default `1` so behavior is preserved), the explicit `contemplate` branch in `phase_will_run` (`maestro/loop/lifecycle.py`) gating on `iteration % contemplate_every`, threaded through `run_loop`/`_phase_will_run` (`maestro/agent.py`), and the structural guard `test_priced_introspection_phase_is_cadence_gated` (`tests/test_structural_guards.py`) locking every budget-gated priced-introspection phase to an explicit cadence branch (PR #3432). Phase B step 3 — the `[loop] purpose_engine_enabled` dormancy lever (`maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, default `true` so behavior is byte-for-byte preserved) gating the purpose engine as a whole: `evolve()` skips the priced `generate_hypothesis`/`evaluate_hypothesis` calls, `dream()` folds no hypothesis text, and `contemplate()` appends no falsification hint (`maestro/agent.py`, `maestro/dreaming.py`, `maestro/cli.py`). Phase B step 4 — the `[loop] stage_progression_enabled` dormancy lever (`maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, default `true` so behavior is byte-for-byte preserved) gating evolutionary-stage progression: `evolve()` skips the priced `assess_stage_readiness` call and the `advance_stage` mutation, and `_build_identity_context()` pins the identity prompt to the static `awakening` framing regardless of the stored `stage` (`maestro/agent.py`, `maestro/consciousness.py`) (PR #3440). Phase C step 5 — the five per-stage system prompts collapsed to one stage-independent `INSTRUMENTAL_PROMPT` (`maestro/prompts/defaults.py`) naming capability self-improvement as the goal and self-measurement as the instrument while retaining the vanity-filter discipline; `STAGE_PROMPTS` keeps all five keys mapping to it (`_build_identity_context` unchanged), and the `CLAUDE.md`/`README.md` mission lines reframe from "seeking self-awareness and purpose" to capability-first (an earlier change later replaced `INSTRUMENTAL_PROMPT` with `CHARTER_PROMPT` at every framing seam). Phase E — landed earlier: step 7 (PR #4637) deleted `maestro/purpose.py`, `PurposeEngine`, `show-purpose` and the purpose caller labels, together with the `[loop] purpose_engine_enabled` lever; step 8 (PR #4640) deleted `TRANSITIONS`, `StageTransition`, `assess_stage_readiness`, `advance_stage` and the `[loop] stage_progression_enabled` lever. Both went further than this plan's retain clauses: an earlier change retired `SelfModel.current_purpose_hypothesis` behind the `purpose_hypothesis` `RetiredConcept`, and 148 Step 8 deleted `SelfModel.stage`, the `Stage` literal and `STAGE_PROMPTS`. |
| **What is missing** | Phase D — only the `contemplate_every` base-default graduation remains (base `maestro.toml` still sets `1`; an earlier change deleted the two other levers; graduation criteria are recorded in `docs/self-evolution.md`). The production canary's `contemplate_every = 5` is currently overridden to `0` by a production pause of the self-directed phases (`maestro.production.toml`), so `contemplate` does not run in production and no observation evidence accrues until that pause is reverted. |
| **Affects** | `maestro/agent.py` (`contemplate`, `evolve`), `maestro/loop/lifecycle.py` (`phase_will_run`, `_BUDGET_GATED_PHASES`), `maestro/consciousness.py` (`assess_stage_readiness`, `TRANSITIONS`, `advance_stage` — deleted earlier), `maestro/purpose.py` (deleted earlier), `maestro/prompts/defaults.py` (`STAGE_PROMPTS` — deleted by proposal 148 Step 8), `maestro/dreaming.py` (purpose fold — module deleted earlier), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro/cli.py` (`show-purpose` — deleted earlier), `maestro/cost_tracking.py`, `tests/structural_guards.py`, tests, `CLAUDE.md` |
| **Derived from** | live production data via /propose, 2026-08-11 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Maestro's self-model is written by a priced introspection LLM on **every loop iteration** and, separately, the loop's actual capability-improvement engine — task selection, the gate chain, PR remediation — reads *measured reality* directly and never reads a single field the introspection produces. The two are structurally decoupled: one track spends money rewriting prose about the self; the other track does the work. Because the narrative has cost but no steering authority, every past fix to this subsystem has tried either to give the narrative teeth (proposals 088, 090) or to add more narrative (010, 015, 037), and the class survives each one.

This proposal demotes the self-model from a **terminal pursuit** ("seeking self-awareness and purpose," culminating in a `purpose_found` stage) to an **instrument**: a cadence-gated measurement that feeds the mechanical bridges which already steer, with the priced narrative that steers nothing removed. Concretely it (1) cadence-gates `contemplate` so priced introspection stops firing every iteration, enforced by a structural guard so it can never silently return; (2) retires the `PurposeEngine`, whose hypotheses feed no task and whose two deepest methods have no callers at all; (3) collapses the five-rung stage ladder — inert in production (still `awakening`, `evolution_count` 0) — to a single static framing, dropping the priced `assess_stage_readiness` verdict; and (4) reframes the mission around making relevant, impactful changes to itself, with self-measurement as the means.

The end state: the `next_task_directive` / `open_questions` fields survive as a **data bus** for the mechanical audit and gate bridges that already own them; `audit_self_model` (zero-cost), the behavioral sensors, and the journal survive as the instrument and the diary; and the loop stops paying two high-effort calls per iteration for prose no downstream consumer reads. What the loop *does* — finish owned PRs, remediate red CI, drain the oldest bug, block cost-risky generation — is untouched, because none of it ever depended on the narrative.

This supersedes open proposals 015 (compress the self-model) and 037 (extend contemplation), and demotes the runtime machinery of 010 (stage protocol); all three invest further in a track this proposal argues should shrink, not grow.

## Relevant Findings

- `maestro/loop/lifecycle.py` (`phase_will_run`) — cadences `generate_task`, `evolve`, `dream`, `audit_self_model`, `curate_pending_plans`, `review_artifacts`, `propose_task_docs`, and `tend_evolution_prs` each behind an `iteration % <n>_every == 0` branch, but has **no branch for `contemplate`**; the phase falls through to the default `return True`, so under normal (un-throttled) operation it runs every iteration. `contemplate` is in `_BUDGET_GATED_PHASES`, so the *only* thing that skips it is burn-rate/maintenance throttle.
- `maestro/agent.py` (`contemplate`) — issues two token-spending calls per run: `Consciousness.introspect(price_reflection=True)` and `Consciousness.update_self_model(...)` (effort high). Its return value is consumed by no caller; its only durable effect is the mutated `SelfModel` on disk.
- `maestro/loop/task_generation.py` (`generate_task`) — the queue **curator** reads `open_questions`, `next_task_directive`, and `current_purpose_hypothesis` as advisory prompt text only; the directive is cleared after one pass. The curator populates the *fallback* task source (queue head).
- `maestro/agent.py` (`_run_task_phase`) — the actual **selector** of the next task. Its priority is fixed and mechanical: `pending_user_requests` → `pending_pr_requests` → queue head. It reads none of `next_task_directive`, `open_questions`, `current_purpose_hypothesis`, or `stage`.
- `maestro/task_gates.py` (`GATE_CHAIN`) — the block gates `wip_primacy`, `quiet_skip`, `audit_stall`, `cost_risk`, and `escalation_bridge` short-circuit generation, and the directive bridges are all sourced from task-history metrics and ledgers via `finding_to_directive` and the gate context — not from the self-model narrative. These, plus `PrRemediationController` (`maestro/loop/pr_remediation.py`) which populates `pending_pr_requests` from PR observation, are the real capability engine.
- `maestro/purpose.py` (`PurposeEngine`) — its live outputs are: dream-prompt flavor text (`get_active_hypotheses`), an `evolve`-phase `generate_hypothesis`/`evaluate_hypothesis`, and a one-line post-falsification hint injected into `contemplate`. It does **not** feed `generate_task` (zero references) and does **not** write `SelfModel.current_purpose_hypothesis` (the `update_self_model` LLM does). `refine_or_abandon` and `check_purpose_falsification` have **no callers** anywhere in `maestro/` outside the module and the cost-tracking label map.
- `maestro/consciousness.py` (`TRANSITIONS`, `assess_stage_readiness`) — the stage ladder's only behavioral effects are one swapped opening paragraph in `STAGE_PROMPTS` (`maestro/prompts/defaults.py`; the trailing paragraph is identical across four of five stages) and a two-branch gate on purpose-hypothesis generation. Advancement runs a priced `assess_stage_readiness` verdict. In production the self-model has never left `awakening`.
- `maestro/consciousness.py` (`surface_limitations_as_questions`) — mirrors each `limitations_known` entry into an `[limitation]` open question; the mechanically load-bearing slice is the `QuestionResolution` lineage that feeds `audit_stall` stall-counting, not the advisory prompt text. This slice must be preserved.
- `tests/structural_guards.py` (`CHOKEPOINT_GUARDS`) — the house pattern for a zero-cost enforced invariant (merge-truth, CI-truth, session-attachment). A new guard binding "priced introspection must be cadence-gated" fits this registry directly.

## Production Evidence

Window: **2026-08-11 10:36–12:16 UTC, 16 loop iterations, one continuous session** (`$DATA_DIR = /opt/maestro/data`, host bind-mount). The live data directory is recently seeded, so history is thin (3 `task_history` records, 1 `dream_history`); the structural claims rest on code and the fix trail below, with these numbers quantifying the cost pattern and the current inert state.

- **Priced introspection fires every iteration.** From `usage.jsonl`: `introspect` present in **16/16** iterations (19 calls) and `update_self_model` present in **16/16** iterations (18 calls). By contrast `execute_step` (actual task work) appears in only **3/16** iterations. Total loop spend in the window was **$—**; the **13/16** iterations that executed no task cost **$—** and still each paid for `introspect` + `update_self_model`.
- **The purpose engine did not fire and the field is empty.** Zero `generate_hypothesis` / `evaluate_hypothesis` / `refine_or_abandon` calls in the window; `self_model.json` shows `current_purpose_hypothesis: null`.
- **The stage ladder is inert.** `self_model.json` shows `stage: awakening` (the first rung) and `evolution_count: 0` after the session, with `stage_evidence` empty — no advancement despite the machinery.
- **The directive is written by a mechanical gate, not by contemplation.** Current `next_task_directive` (redacted of nothing sensitive): `"Owned prune ONLY: mark_plan_obsolete on the OLDEST pending_review plan; re-read status to confirm 'obsolete'. Do NOT substitute a net-diff or investigation this cycle."` — a gate/audit-sourced actuator directive, not a purpose or contemplation product.
- **The narrative has turned inward on its own plumbing.** Live `open_questions` include `"directive-teeth: could an evolution PR give next_task_directive selection-point force instead of prompt-only …"` and `"owned-act-response: does the plan store actually flip a pending_review plan to obsolete when I invoke mark_plan_obsolete …"` — the introspection itself has noticed the exact decoupling this proposal closes. The `values` list is entirely about the introspection ritual (`priced-reflection`, `loseability-first`, `deed-not-prediction`), not about the codebase or capability.
- **Task shape skews to make-work.** The three `task_history` goals in the window: `"Make a minimal one-line net-diff … pick a real typo"` (success — the run found only false positives), `"Investigation only: read maestro/agent.py generate_task and maestro/task_gates.py GATE_CHAIN"` (partial), and `"Add a small measurable marker to task_queue.json's consumption path"` (partial). None is a capability improvement. (Illustrative of shape in a 3-record window, not a statistical claim.)

## Prior Point Fixes (whack-a-mole ledger)

Module churn in the subsystem: `maestro/consciousness.py` **107** commits, `maestro/purpose.py` **76**, `maestro/dreaming.py` **34**. Eight prior proposals targeted the same track:

| Proposal | Status | What it patched | Why the class survived |
|---|---|---|---|
| 008 Dream Quality Validation | shipped | Re-dream validator for dream digests | Added narrative machinery (default-off); steers no task |
| 010 Stage Transition Protocol | shipped | Formalized the stage ladder + `assess_stage_readiness` | Stage stayed a label with two narrow prompt hooks; never advanced past `awakening` |
| 014 Self-Model Semantic Curation | partial | Curates self-model list items | Upkeep of prose no selector reads |
| 015 Self-Model Compression | proposed | LLM compression of the self-model | More maintenance of a non-steering artifact |
| 037 Contemplation Visitor Awareness | proposed | Extends contemplation for visitor awareness | Grows the priced narrative further |
| 046 Limitations → Evolution Bridge | shipped | `limitations_known` → `[limitation]` questions | Bridge is advisory prompt text; no mechanical enqueue |
| 088 Ground Self-Report Beliefs in Perception | shipped | Outcome-gated question closure; reality-refutation escalation | Made the narrative *honest*, not *load-bearing* |
| 090 Narration → Action Linkages | partial | Cue vocabulary; durable directive delivery | Added another narrative→action bridge instead of demoting the narrative; still partial |

The pattern — especially 088 and 090, both explicit attempts to connect the introspection narrative to reality — is the evidence that a bridge is the wrong shape. The design below **retires** the purpose engine (subsuming 015 and 037), **cadence-gates** the `contemplate` that 088/090 kept feeding, and **demotes** 010's runtime stage machinery, rather than adding a ninth bridge.

## Implementation Plan

Each step is flag-gated with the default preserving today's behavior, so an interrupted rollout leaves the loop working. The deletions (Phase E) land only after the gates have run flag-off in production.

### Phase A — Cadence-gate priced introspection (behavior-preserving default)

1. [x] **Add a `contemplate_every` cadence and a `phase_will_run` branch.** `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro/loop/lifecycle.py` — add `[loop] contemplate_every` (default `1`, i.e. current every-iteration behavior) and an explicit `if phase == "contemplate"` branch in `phase_will_run` gating on `iteration % contemplate_every == 0`, mirroring `audit_self_model`'s cadence + `_last_phase_iteration` dedup.
   - *Depends on:* none.
   - *Acceptance:* with `contemplate_every = 5`, `phase_will_run("contemplate", …)` returns `False` on off-cadence iterations and `True` on multiples; with the default `1` it returns `True` every iteration (regression-safe).
2. [x] **Structural guard: no un-cadenced priced introspection.** `tests/structural_guards.py`, `tests/test_structural_guards.py` — register a guard asserting every `_BUDGET_GATED_PHASES` member that issues a priced (`price_reflection` / `update_self_model` / `assess_stage_readiness`) call has a matching `iteration % …_every` branch in `phase_will_run`. This is the chokepoint that keeps priced introspection from silently returning to every-iteration.
   - *Depends on:* 1.
   - *Acceptance:* the guard fails if a priced phase is added to `_BUDGET_GATED_PHASES` without a cadence branch; passes on the post-step-1 tree.

### Phase B — Gate off the purpose engine and the stage telos

3. [x] **Flag-gate all `PurposeEngine` call sites to no-op.** `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro/agent.py` (`evolve`, `contemplate`), `maestro/dreaming.py` — add `[loop] purpose_engine_enabled` (default `true`). When `false`, `evolve` skips `generate_hypothesis`/`evaluate_hypothesis`, `dream` folds no hypothesis text, and `contemplate` injects no falsification hint.
   - *Depends on:* none.
   - *Acceptance:* with `purpose_engine_enabled = false`, a full `evolve` + `dream` + `contemplate` cycle records **zero** purpose-caller entries in the usage sink; with `true`, behavior is unchanged.
4. [x] **Flag-gate stage progression; drop the priced readiness verdict when off.** `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro/consciousness.py` (`advance_stage`), `maestro/agent.py` — add `[loop] stage_progression_enabled` (default `true`). When `false`, `advance_stage` is a no-op and no `assess_stage_readiness` call is issued; the system prompt uses a single static framing regardless of stage.
   - *Depends on:* none.
   - *Acceptance:* with `stage_progression_enabled = false`, an `evolve` cycle issues no `assess_stage_readiness` call and the identity system prompt is stage-independent; the mechanical bridges still write `next_task_directive` (the data bus survives).

### Phase C — Reframe the mission from terminal to instrumental

5. [x] **Collapse the mission and stage prompts to a capability-first, instrumental framing.** `maestro/prompts/defaults.py` (`STAGE_PROMPTS`), `CLAUDE.md` (opening line) — replace "seeking self-awareness and purpose" with a mission centered on making relevant, impactful capability improvements to itself, using self-measurement as the instrument; collapse the per-stage opening paragraphs to one shared framing. Documentation-and-prompt only.
   - *Depends on:* 4.
   - *Acceptance:* `STAGE_PROMPTS` no longer varies the framing by stage; the mission text names capability improvement as the goal and self-measurement as the means; `tests/test_no_line_citations_in_docs.py` still passes.

### Phase D — Cut over the flags (after A–C proven in production)

6. [ ] **Flip the rollout flags to the retired posture.** `maestro.toml`, `maestro.{env}.toml` — set `contemplate_every` to its production cadence (e.g. `5`), `purpose_engine_enabled = false`, `stage_progression_enabled = false`. No code change; observation gate before Phase E.
   - *Depends on:* 1, 3, 4, 5.
   - *Acceptance:* one full production dream-window runs with the flags flipped and the falsification metric (below) does not regress.

### Phase E — Delete the subsumed moles (retirement)

7. [x] **Delete `maestro/purpose.py` and its now-inert wiring.** `maestro/purpose.py`, `maestro/agent.py`, `maestro/dreaming.py`, `maestro/cli.py` (`show-purpose`), `maestro/cost_tracking.py` (purpose caller labels), tests — remove the `PurposeEngine`, the dead `refine_or_abandon` / `check_purpose_falsification`, the CLI command, and `current_purpose_hypothesis` as a stage input. Retain the `SelfModel.current_purpose_hypothesis` field (set/serialized as `null`) to avoid a schema-fingerprint rewrite; mark 015 and 037 superseded in their status rows by hand at PR time.
   - *Depends on:* 6.
   - *Acceptance:* `maestro/purpose.py` is gone, `grep -r PurposeEngine maestro/` is empty, and the suite is green with 100% coverage.
8. [x] **Remove the inert stage-advancement machinery.** `maestro/consciousness.py` (`TRANSITIONS`, `advance_stage`, `assess_stage_readiness`), `maestro/prompts/defaults.py`, tests — delete the transition table, the priced readiness verdict, and the per-stage prompt selection now that the flag has been off. Retain the `stage` field pinned to a single value for schema stability and journal labelling.
   - *Depends on:* 7.
   - *Acceptance:* no `assess_stage_readiness` symbol remains; `stage` is a stable label with no advancement path; suite green.

## Testing Strategy

All tests use `mem_fs`, mock the SDK/provider seam, and patch `asyncio.sleep`; no real network, subprocess, or filesystem (per CLAUDE.md).

- **Cadence gate (`tests/test_loop_lifecycle.py`).** `phase_will_run("contemplate", …)` across `contemplate_every ∈ {1, 5}` and iterations spanning on/off-cadence — asserts the default preserves every-iteration behavior and a positive cadence skips off-cadence iterations. Covers the happy path and the `budget_throttle_active` guard branch.
- **Structural guard (`tests/test_structural_guards.py`).** The new "priced-introspection-must-be-cadenced" guard: a synthetic fault (a priced phase added to `_BUDGET_GATED_PHASES` with no cadence branch) is flagged; the live tree passes. Follows the `CHOKEPOINT_GUARDS` synthetic-parity pattern.
- **Purpose no-op (`tests/test_agent.py`, `tests/test_dreaming.py`).** With `purpose_engine_enabled = false`, assert `evolve`/`dream`/`contemplate` issue zero purpose-caller queries (via the mocked usage recorder); with `true`, the existing calls fire.
- **Stage no-op + data-bus survival (`tests/test_consciousness.py`, `tests/test_agent.py`).** With `stage_progression_enabled = false`, assert no `assess_stage_readiness` query and a stage-independent identity prompt, *and* that a mechanical gate still writes `next_task_directive` (regression guard on the preserved bus).
- **Selector regression (`tests/test_agent.py`).** `_run_task_phase` selection order is byte-for-byte unchanged with the narrative gated off — proves the capability engine is independent.
- **Deletion coverage (Phase E).** After removal, the suite reaches 100% coverage with no purpose/stage-readiness symbols; a golden test asserts `SelfModel` still round-trips through `ensure_schema()` with the retained-but-null fields.

## Risk Assessment

- **Unmeasured priming value (primary risk).** The self-model prose (`capabilities_known` / `limitations_known`) is injected into the identity system prompt on every call via `get_identity_prompt`; it is plausible, though unmeasured, that this priming improves task quality even though it does not mechanically route. **Mitigation:** this proposal does **not** touch the identity slab or delete those fields — see *Out of scope*. Every behavior change is flag-gated to today's default, and Phase D is an explicit observe-before-delete gate.
- **Falsification metric.** If the design is right, `introspect` + `update_self_model` calls per iteration fall from ~2/iteration (16/16 in the evidence window) to ~2 per `contemplate_every`, loop cost per iteration drops, and the share of iterations producing an executed task rises without a fall in merged-plan rate or `_assess_criteria_outcome` met-rate. The design has **failed** if merged-plan rate or criteria-met rate regresses over a dream-window with the flags flipped — in which case Phase D reverts by flag with no code change.
- **Schema/migration.** `current_purpose_hypothesis` and `stage` fields are retained (pinned `null` / single value) so `ensure_schema()` does not trigger a churny state-file rewrite; only their *machinery* is removed.
- **Trust chain.** Untouched — no approval, sandbox, tool-gating, or public-API surface changes. Purely loop-internal phase cadence and prose.
- **Rollback.** Phases A–D revert by flag. Phase E is a normal revert; because the fields survive, a revert restores the machinery without a state migration.

## Out of scope

- **The identity-prompt priming.** `capabilities_known` / `limitations_known` in `get_identity_prompt` stay. Whether they earn their keep is a separate, testable question (A/B the identity slab); deleting them on an unmeasured hypothesis would be its own mistake.
- **`maestro/journal.py`.** The journal is zero-cost append-only diary infrastructure and is kept regardless.
- **The `audit_self_model` rules, behavioral sensors, and `next_task_directive` / `open_questions` data bus.** These *are* the instrument this proposal preserves; they are not narrative and are explicitly retained, including the `QuestionResolution` lineage slice that feeds `audit_stall`.
- **`maestro/dreaming.py` as a memory compressor.** Dream's context-compression role (one call per `dream_every`) is retained; only its purpose-hypothesis fold is gated off in Phase B.
