# 046: Limitations → Evolution Bridge — Synthetic `[limitation]` Open Questions

| Field | Value |
|-------|-------|
| **Impact** | LOW (zero new SDK cost, zero schema change, zero new config flag) |
| **Status** | shipped |
| **Affects** | `maestro/consciousness.py`, `maestro/agent.py` (`contemplate`), `CLAUDE.md`, `docs/self-evolution.md` |
| **Depends on** | 044 (shipped) — establishes the pattern; PR #1616 (vanity filter) — guarantees only growth-frameable limitations reach this surface |

## Description

> Status updated to shipped on 2026-04-27 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Maestro identifies "what I can't do" — `limitations_known` on the self-model — but those entries never enter the task-generation pipeline. The recently-merged vanity filter ensures every limitation is *growth-frameable* (probeable by a task, detectable by an audit rule, narrowable by an evolution PR, or mapped to an error pattern), and proposal 044 established the pattern of converting soft self-knowledge into mechanical loop input. But limitations themselves still terminate at the identity prompt: they shape *how* Maestro thinks, not *what* it picks to work on. `TASK_GENERATION_PROMPT` takes only `{stage}`, `{open_questions}`, `{purpose_hypothesis}`, `{recent_tasks}`, `{task_type_distribution}`, `{next_task_directive}` — no `{limitations}`. There is no closure path turning "can't do X" into a task that builds the missing capability.

This proposal surfaces each entry in `limitations_known` as a synthetic `[limitation] <text>` open-questions row, mirroring the existing `[audit]` pattern. The existing fuzzy-match anchoring in `generate_task` (`match_open_question`), the existing REMOVE/REFINE/KEEP closure in `update_self_model`, and the existing audit-stall actuator all then apply for free. When the LLM removes a `[limitation] X` question because the finding showed progress, the prompt also instructs it to revise the matching `limitations_known` entry in the same `update_self_model` call — otherwise surfacing re-adds the row next cycle and the disposition records as `kept`. The vanity filter has already paid for the safety: only growth-frameable limitations reach this pipeline.

The outcome is first-class evolution candidacy for every limitation. "I can't reliably parse YAML" becomes a synthetic question, picks up a self-directed task, the task ships a parser, the next contemplate cycle removes the question and narrows the limitation. Soft self-knowledge becomes mechanical loop input — the same bridge proposal 044 built for audit findings, now extended to limitations.

## Design — simplest viable

- **Mirror the audit pattern exactly.** New constants `_LIMITATION_PREFIX = "[limitation"` (no trailing char so future variants like `[limitation narrowed]` count against the same sub-cap) and `MAX_LIMITATION_OPEN_QUESTIONS = 3`, plus an `_is_limitation_question` classmethod that mirrors `_is_audit_question`.
- **Cap composition.** `_enforce_open_questions_caps` extends from a two-bucket split (audit / non-audit) to three buckets — audit (cap 4, unchanged), limitation (cap 3, new), other (fills slots up to `MAX_LIST_ITEMS = 10`). With both sub-caps full, 3 slots remain for self-generated questions; the seed has 4 default questions, so under maxed load one of the four would be evicted — acceptable cosmetic churn given that mature agents replace the seed with self-generated questions anyway.
- **Surfacing helper.** `Consciousness.surface_limitations_as_questions(model)` is a pure classmethod: for each entry in `model.limitations_known`, build the synthetic question `f"[limitation] {entry[:160]}"` (trim leaves room for the prefix under the 200-char per-item ceiling), normalise via `_normalize_question`, append to `model.open_questions` only if no existing entry — regardless of provenance prefix — normalises to the same key. Returns the same model (mutated). No I/O, no SDK cost.
- **Insertion point.** In `agent.py contemplate`, immediately after `update_self_model` returns and before `_record_question_resolutions`. The freshly-updated model — including any limitations the LLM just added — is passed through the helper before the resolution diff is computed, so dispositions reflect whether the limitation persisted (not whether the LLM remembered to rewrite the open-question list).
- **Persistence.** `update_self_model` saves the model before returning, so an explicit `save_self_model(updated_model)` call is added after surfacing to persist the freshly-surfaced rows. The save also runs the rows back through `_enforce_open_questions_caps`, applying the new sub-cap.
- **Closure prompt.** The `update_self_model` prompt now ends the self-directed-pairs section with one paragraph: when a `(question, finding)` pair starts with `[limitation]` and the finding shows the limitation has been narrowed or eliminated, also revise or remove the matching `limitations_known` entry in the same response. No new code path — the LLM already returns `limitations_known` in every update; this tightens the prompt's instructions for *when* to revise it.
- **No new actuator.** The audit-stall actuator (044) only matches `[audit]` prefix, so `[limitation]` rows don't trigger it. Stalled limitations remain a soft signal for now — an explicit follow-up actuator could mirror the stall gate for limitations once we have lived data on whether they stall in practice.

## Self-referential proof-of-concept

The architecture's catalogue of self-knowledge surfaces gains a new bridge: `limitations_known` (a self-model field) is mirrored into `open_questions` (a task-generation surface) by deterministic code. The LLM cannot route around the surfacing — every `contemplate` cycle pushes every limitation into the anchor pool. The same fuzzy-match plumbing that closes audit findings now closes limitation questions; the same identity-prompt budget that holds limitations now also funds their evolution candidacy. One soft surface, two hard wirings.

## Risks & edge cases

1. **Deadlock — limitation never narrows.** If a limitation passes the vanity filter but is genuinely outside Maestro's reach, the synthetic question fires repeatedly, generates a task that fails, and the LLM may KEEP both the limitation and the question. *Mitigation:* the existing `check_stalled_question` audit rule already detects questions targeted ≥ 3× with disposition `kept` — it sees `[limitation]` questions identically to any other. The audit-stall actuator (044) does not gate on `[limitation]` directly, but the audit-finding it produces will gate via 044 if `audit_stall_block_enabled = true`.
2. **Cap pressure.** Audit (4) + limitation (3) + total (10) leaves 3 slots for self-generated questions. The seed has 4 default questions, so under maxed load one of the four would be evicted. Mature agents naturally rotate past the seed, so the impact is bounded to the awakening stage.
3. **Normalisation collisions.** Two limitations that normalise to the same key (whitespace, casing) produce one question, not two. Acceptable: dedup is the goal.
4. **Limitation revised mid-cycle.** If the LLM rephrases a limitation, the old `[limitation] OLD` row persists until removed; the surfacing helper sees `NEW` and creates `[limitation] NEW`. Next cycle's sub-cap may evict one. Acceptable cosmetic churn; the cap prevents runaway.
5. **Identity-prompt budget.** Limitations are already injected into the identity prompt; they now also appear in `open_questions`. Mild duplication, but `open_questions` only enters task-generation context, not every API call. No identity-prompt growth.
6. **Empty-limitations init.** The default `SelfModel.limitations_known` is `[]`, so the helper is a no-op for fresh agents — surfacing only kicks in once contemplation populates limitations.
7. **Closure prompt drift.** If the LLM removes the synthetic question but does not revise `limitations_known`, surfacing re-adds the row next cycle and the resolution records as `kept`. Self-healing via existing `check_stalled_question`.
8. **Phase-resume idempotency.** The surfacing helper is idempotent and pure (no I/O, only the explicit `save_self_model` writes); safe under `_LOOP_PHASES` resume.

## Future bridge candidates (out-of-scope)

The same template — soft surface mirrored into a hard surface, no schema change, idempotent — opens further bridges:

- **Limitation-stall actuator.** A `[limitation]`-aware extension of the 044 stall gate, once lived data shows whether limitation rows stall in practice.
- **Capability surfacing.** Mirror `capabilities_known` as `[capability] X` in `open_questions` so capabilities also have a generation anchor — would need a different finding shape (probe-to-confirm rather than probe-to-narrow).
- **Value tension.** Two `values` entries that contradict each other surface as a `[value tension] X vs Y` question.

Each follows the template established here: deterministic code-level surfacing, idempotent dedup, and reuse of the existing closure path rather than a parallel mechanism.
