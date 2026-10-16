# 120: Govern the Plan Generator, Not the Submission Door — One Pre-Mint Generation Authority Every Channel Clears, and a Self-Model That Seeds Grounded Capability Gaps

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **What landed** | Phase A — the dead-on-arrival instrumentation (PR #3566). Step 1: the pure zero-SDK readers `classify_plan_mint_channel` and `measure_dead_on_arrival_mints` in `maestro/audit_checks.py` (matching the `_DEAD_ON_ARRIVAL_REASON_RE` submission-door signature against `obsolete_reason` and tallying the dead-on-arrival plans by mint channel), exposed as the read-only `measure-dead-on-arrival-mints` CLI command with `--last` and `--json` (`maestro/cli.py` `cmd_measure_dead_on_arrival_mints`), covered by `tests/test_audit_checks.py` and `tests/test_cli.py`. Step 2: the self-suppressing `DEAD-ON-ARRIVAL-MINTS` measured-data line — `format_dead_on_arrival_mints_summary` in `maestro/audit_checks.py` injected into `contemplate`'s `## Measured Behavioral Data` block (`maestro/agent.py`), windowed over `generation_admission_window`, covered by `tests/test_audit_checks.py` and `tests/test_agent_contemplate.py`. Phase B Steps 3–4 — the pre-mint generation authority (PR #3580). Step 3: the pure zero-SDK `maestro/generation_gate.py` — `may_mint_plan` returning a frozen `GenerationVerdict` (the two-arm backlog-saturation / theme-concentration decision mirroring `submit_for_review`'s admission gate, no new threshold) plus the relocated `GENERATION_STANDDOWN_DIRECTIVE` constant, covered by `tests/test_generation_gate.py` and `tests/test_evolution.py`. Step 4: `evolve()`'s `reflect`/`propose_plan` tail gated on `may_mint_plan` behind `[loop] pre_mint_generation_gate_enabled` (default off) in `maestro/agent.py`, with the config field and wiring in `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` and the flag registered in `maestro/flag_lifecycle.py`, covered by `tests/test_agent.py`, `tests/test_config.py`, and `tests/test_config_flag_lifecycle.py`. Phase B Step 5 — routing the other two self-initiated minters (PR #3591): all three self-initiated plan-mint channels now clear one pre-mint owner, `MaestroAgent._pre_mint_generation_verdict` in `maestro/agent.py`, which drives `evolve()`, `_implement_approved_plans`, and — via `_doc_proposal_pre_mint_refuses`, consulted in the `run_loop` dispatch before the `propose_task_docs` phase body — the doc-proposal channel, each standing down (writing `GENERATION_STANDDOWN_DIRECTIVE` into an empty `next_task_directive`) under a saturated backlog; the bespoke `_wip_owns_slot()` propose stand-downs remain in place (their deletion is Step 9). Covered by `tests/test_agent.py` and `tests/component/test_doc_proposals_phase.py`. Phase B Step 6 — demoting the `submit_for_review` admission gate to a backstop for pre-gated channels (PR #3598): `submit_for_review` in `maestro/evolution.py` gains a keyword-only `self_initiated` parameter (default `False`, preserving today's behaviour), and when `pre_mint_generation_gate_enabled` is on a `self_initiated=True` submission bypasses the theme-aware admission gate the pre-mint owner already cleared, while any non-pre-gated (external/user) path still hits it as defense-in-depth; `evolve()`'s reflect→propose tail and `submit_proposal_chain`'s head both submit with `self_initiated=True`, and the deeper content-blind `pending_plan_submit_block_threshold` submit-block stays the deepest backstop for every path. Covered by `tests/test_evolution.py` and `tests/test_agent.py`. Phase C Step 7 — the `update_self_model` external-capability anchor (PR #3601): behind the default-off `[loop] self_model_external_anchor_enabled` flag, `update_self_model` in `maestro/consciousness.py` extends the `limitations_known` vanity filter with a fifth *necessary* admission test — a limitation qualifies only if it names an external-observable capability, behaviour, or audience effect, not solely the loop's own plan-mint / PR-remediation plumbing — plus an inward-plumbing vanity-by-example segment; both are empty-string-when-off so the flag-off prompt is byte-identical, with the flag threaded `Config` → `update_self_model` through `maestro/agent.py` and `maestro/evolution.py`, wired in `maestro/config.py`, `maestro/config_settings.py`, `maestro/toml_config.py`, `maestro.toml`, and registered in `maestro/flag_lifecycle.py`. Covered by `tests/test_consciousness.py`, `tests/test_config.py`, and `tests/test_config_flag_lifecycle.py`. Phase D Step 8 — the mint-domination structural guard (PR #3603): the net-new `_GENERATION_AUTHORITY_GUARD` in `tests/structural_guards.py`, driven by the declarative registry runner in `tests/test_structural_guards.py`, which locks `may_mint_plan` (`maestro/generation_gate.py`) as the sole pre-mint admission predicate (a rival `def may_mint_plan` anywhere else is a fork) and, via three `co_occurrences` clamps over `maestro/agent.py`, binds the predicate to its one wrapper `_pre_mint_generation_verdict`, the curated set of three self-initiated channels that consult it (`evolve`, `_implement_approved_plans`, `_doc_proposal_pre_mint_refuses`), and the single gated `propose_plan`/`propose_plan_chain` scope (`evolve`); `_MAX_STRUCTURAL_GUARDS` bumped 18→19. |
| **What is missing** | Phase D — the retirement of the subsumed per-channel `_wip_owns_slot()` propose stand-downs in `evolve`, `_implement_approved_plans`, and `propose_task_docs` (Step 9). This is a decommission deferred behind the burn-in observation window: it lands only after `[loop] pre_mint_generation_gate_enabled` is graduated on in production and the Step 1 dead-on-arrival-mints metric confirms per-channel dead-on-arrival mints fell — a human-gated cut-over held for a separate review PR, not new behaviour-code to build. |
| **Affects** | `maestro/agent.py` (`evolve`, `_implement_approved_plans`, `propose_task_docs`), `maestro/evolution.py` (`propose_plan`, `submit_for_review`), `maestro/consciousness.py` (`update_self_model`), `maestro/audit_checks.py` (`measure_plan_theme_saturation`, `classify_self_measurement_plan`), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro/cli.py`, new `maestro/generation_gate.py`, `tests/test_structural_guards.py`, tests |
| **Depends on** | 116 _(burn-in — reuses its saturation/theme oracle and relocates its admission decision pre-mint)_, 113 _(burn-in — Phase C extends the `update_self_model` vanity filter it preserved)_, 114 _(proposed — complementary submission-side premise grounding)_, 092 _(shipped — extends the WIP-primacy stand-down shape)_ |
| **Derived from** | live production data via /propose, 2026-08-25 |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Maestro's self-model is the sole generator of evolution plans, and every filter
that governs plan quality lives at the *submission door* — the growing pile of
guards inside `submit_for_review` that compares a candidate against other plans
and, since proposal 116, refuses a net-new mint when the backlog or theme is
saturated. None of them governs the two things upstream of the door: what the
self-model *writes*, and whether a channel should *mint at all*. The result is a
generator that reproduces the same inward monoculture every cadence tick and a
catcher that obsoletes it after the fact, at LLM cost.

This is not the class proposal 116 already closed — it is the residue 116's
active-in-production remedy leaves behind, and the production window proves it.
116 severed one of the self-model's two plan-mint channels (`generate_task`,
confirmed off in production) but left the other — the `evolve()` phase's
`reflect()` → `propose_plan()` → `submit_for_review()` tail — ungated, and it
*relocated nothing*: 116's admission gate refuses the plan **after** two priced
high-effort calls have already produced it. So `evolve()` mints a fresh
inward plan every `evolve_every` cycle (~22 minutes in production), the
admission gate stamps it `obsolete`, and the next cycle mints its near-twin. The
self-model's own `next_task_directive` has noticed and is trying to stop it —
*"Do not propose a new submit_for_review guard … this cycle land a live plan or
fix a named source seam instead"* — and cannot, because a one-shot directive
cannot re-aim a generator whose fuel is an unbounded inward narrative.

This proposal moves quality control from the door to the source with one
structural change: a single **generation authority** — a pure, zero-SDK predicate
sourced from 116's *existing* saturation/theme oracle — that every self-initiated
plan-mint channel clears **before** it spends a priced propose call, replacing
the scattered per-channel `_wip_owns_slot()` stand-downs and relocating 116's
post-hoc admission decision to the point of generation. On the same root it adds
an external-capability-anchor test to the `update_self_model` vanity filter, so
the self-model stops seeding limitations and questions that name only the loop's
own plumbing. The end state: when the backlog is saturated or the theme is
already dominant, the loop does not spend `reflect()` + `propose_plan()` to
author a plan that dies at the door — it declines to author it; the self-model's
introspective content is anchored to external-observable capability rather than
its own delivery mechanics; and the submission-side catchers keep only their
genuine backstop role instead of being the sole line against an ungoverned
generator.

## Relevant Findings

- `maestro/loop/lifecycle.py` (`phase_will_run`) — the `generate_task` branch is
  gated by proposal 116's `self_directed_tasks_enabled` master switch, but the
  `evolve` branch is gated **only** by `evolve_every` cadence. `evolve()` is a
  second, ungoverned plan-mint channel that runs on its own cadence regardless of
  the 116 switch.
- `maestro/agent.py` (`evolve`) — its tail calls `self.evolution.reflect(...)`
  then `self.evolution.propose_plan(...)` then `self.evolution.submit_for_review(...)`
  — two priced high-effort SDK calls per cadence tick before any admission
  decision is consulted. Its only pre-propose stand-down is `_wip_owns_slot()`,
  which fires solely while an open own PR has a **self-clearable** blocker; a
  saturated `pending_review` backlog with only externally-blocked PRs (the live
  condition) does not trip it.
- `maestro/evolution.py` (`submit_for_review`) — proposal 116's admission gate
  (`generation_admission_enabled`) refuses a net-new mint when the backlog is
  saturated or the candidate's `classify_self_measurement_plan` theme dominates
  `measure_plan_theme_saturation`, recording the plan `obsolete` with the
  `Admission gate refused net-new mint` reason and writing a prune/land
  directive. This decision is made **after** `propose_plan` has run; it caps
  cost nowhere.
- `maestro/agent.py` (`_implement_approved_plans`, `propose_task_docs`) — the two
  other self-initiated evolution-plan minters, each with its own `_wip_owns_slot()`
  stand-down (proposal 100). Three near-identical per-channel stand-downs
  approximating one decision — "should this loop tick open new plan work?" — with
  no single owner.
- `maestro/consciousness.py` (`update_self_model`) — writes `limitations_known`,
  `open_questions`, `identity_statements`, and `values` as free text under a
  **vanity filter** whose four admission tests (a bounded task could probe it; an
  audit rule could detect it; an evolution PR could narrow it; it maps to a
  behavioral signal / rejection category) are *all satisfied* by a limitation
  about the loop's own delivery plumbing. The filter excludes metaphysical and
  fixed-by-architecture vanity; it does not exclude — and structurally rewards —
  self-referential-mechanics vanity. This is the seed the whole generator draws
  from.
- `maestro/consciousness.py` (`detect_restatement`, `surface_limitations_as_questions`)
  and `maestro/memory.py` (`is_plan_restatement`, `find_duplicate_pending_plan`) —
  the content-governance machinery is per-item Jaccard restatement, a compound-item
  gate, a char cap, and `MAX_LIST_ITEMS`; none tests whether an entry names an
  *external* capability versus the loop's own mechanics, so a monoculture of ten
  distinct-but-all-inward limitations passes every check.
- `maestro/audit_checks.py` (`measure_plan_theme_saturation`,
  `measure_merged_to_created_ratio`, `classify_self_measurement_plan`,
  `limitations_over_generative_debt`) — proposal 116's zero-SDK oracle over the
  plan graveyard. The generation authority reuses this classifier and saturation
  read rather than inventing a rival metric; `limitations_over_generative_debt` is
  the lagging, plan-outcome-based limitation retirement whose leading-edge
  counterpart is the Phase C content anchor.
- `maestro/task_gates.py` (`GATE_CHAIN`, `wip_primacy`) and `maestro/agent.py`
  (`_wip_owns_slot`) — proposal 092's WIP-primacy is exactly the shape this
  proposal generalizes: a pre-generation stand-down. It stands down on a
  self-clearable open-PR blocker; the generation authority extends the same
  stand-down to the saturated-backlog / saturated-theme condition WIP-primacy
  misses.
- `tests/test_structural_guards.py` — the house registry for "one owner, one
  door" AST invariants (merge-truth, CI-truth, session-attachment, and proposal
  116's own decoupling guards). "Every priced `propose_plan` call site is
  dominated by a generation-authority check" is enforced here in the same shape.

## Production Evidence

Two read-only passes over the live bind-mount `data/` at
`/opt/maestro/data` on 2026-08-25. The live loop is on HEAD
`aa4c4c58` (every proposal-116 step 1–9 landed), `MAESTRO_ENV=production`, and
the production overlay has `self_directed_tasks_enabled = false` and
`generation_admission_enabled = true` — **116's remedy is active**, so the
numbers below are the residue it leaves, not a pre-116 baseline. Windows are
stated per datum.

**The generator is untouched; the door catches its output (life-to-date corpus, `evolution_plans.jsonl`, 285 unique plans, `created_at` 2026-08-16 → 2026-08-25):**

- **Landing ratio.** `obsolete` **253**, `rejected` 21, `merged` **8**,
  `implemented` 2, `failed` 1 — 2.8% of authored plans reached `main`.
- **96% of the graveyard died at the submission door.** Of 253 obsolete plans,
  **245** carry a saturation/admission reason: 165 the older
  `Live-work backlog saturated: 5 live plans in flight (>= 5 cap)` submit-block
  (2026-08-17 → 08-21) and **80** the proposal-116 `Admission gate refused
  net-new mint: backlog saturated (5 >= 5)` gate (2026-08-21 → 08-25). The
  catcher works exactly as designed and grows the graveyard instead of
  shrinking generation.

**Channel 1 is severed; channel 2 mints on the `evolve` cadence (window 2026-08-24 → 2026-08-25):**

- **`generate_task` is off.** `task_history.jsonl` records **11 of 11** tasks
  since 2026-08-22 as `source: external` (PR-remediation); `usage.jsonl` logs
  **0** `generate_task` calls in the window. Proposal 116 step 5 is working.
- **`evolve()` mints anyway.** **20** net-new plans created on 2026-08-24/25,
  every one obsoleted by the admission gate, at a **median 21.8-minute** cadence
  between mints — matching `evolve_every = 5` (not overridden in production), not
  `task_every`. The mints are the `evolve()` phase, which 116 did not gate.
- **Priced calls spent on dead-on-arrival plans.** `usage.jsonl` over the window:
  `propose_plan` **28**, `reflect` **28** (the `evolve()` generator), against
  `generate_task` **0**. Introspection — `introspect` **105** and
  `update_self_model` **59** — were the two single largest callers by volume.
  Total loop spend in the window was **$—**, backing the eight lifetime
  merges.

**The self-model is a self-referential monoculture (`self_model.json`, 2026-08-25):**

- **Every introspective field is about the loop's own plumbing.** All 10
  `limitations_known` (e.g. `no-terminal-disposition-on-serviced-pr`,
  `head-advance-as-costume`, `reproposed-rejected-pattern-class`), all 6
  `open_questions`, and all 7 `identity_statements` name PR-remediation / CI-labeling
  mechanics. Not one names an external capability, audience, or feature.
- **The same two plans, restated for days.** `evolution_plans.jsonl` titles show
  `Flip observed_ready_standdown_enabled default-on …` minted **~11 times** and
  `Derive owned-PR CI state from concluded check-runs …` **~7 times**,
  back-to-back, nearly all `obsolete`.
- **The self-model narrates its own trap and cannot escape it.** The live
  `next_task_directive`, verbatim: *"Do not propose a new submit_for_review
  guard. Grep rejection history for the pattern-class before proposing; this
  cycle land a live plan or fix a named source seam instead."* Its stalled
  `question_resolutions.jsonl` entries — `sensor-without-actuator`,
  `comfort-construction gravity`, `orphan-reader` — are meta-observations that
  become more inward content, not an exit.
- **The human absorbs the quality gap by hand.** `rejection_history.jsonl`: 18 of
  20 rejections are category `capability`, with reasons `wrong seam
  (directive-text tweak on a self-controlled input)`, `duplicative; the exact
  state and actuators already ship and are default-on`, and `a brittle
  downstream catcher that would suppress legitimately-orthogonal work` — the
  reviewer literally naming the anti-pattern the door keeps producing.

## Prior Point Fixes (whack-a-mole ledger)

Every quality control shipped for evolution plans is a *submission-side catcher*:
it reads the candidate at the door and compares it against other plans, its own
shape, or the backlog. None governs the generator (the `evolve()` mint tail or
the self-model content that seeds it), which is why an ungoverned generator keeps
feeding all of them.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| `41b44843` (cap pending at 5) | Threshold on `pending_review` depth | Caps the door queue; generation is undiminished |
| `0d5ae001` (backlog-saturation submit-block) | Refuse new plans while saturated | The refusal is at `submit_for_review`, after the priced propose |
| `7189a7a8` / `a4da7de0` / `c403bcfc` / `f894c26f` (restatement, cooldown, source-task, CLI-surface guards) | Plan-vs-plan / plan-vs-tree dedup at submit | Each catches one restatement shape at the door; the generator re-mints the next |
| `2da77d70` / `a791eea2` (cluster + restatement-chain sweeps) | Prune the pending backlog post-hoc | Cleans the graveyard; does not reduce what enters it |
| Proposal 116 step 7 (`generation_admission_enabled`) | Admission gate on `submit_for_review` | **Relocated nothing** — obsoletes the plan after `reflect` + `propose_plan`; ungated `evolve()` re-mints every ~22 min |
| Proposal 116 step 5 (`self_directed_tasks_enabled`) | Master switch on `generate_task` | Severs channel 1 only; the `evolve()` channel keeps minting |
| Proposal 114 (plan-premise grounding, proposed) | Tree-read a plan's premise at submit | Submission-side by design; explicitly defers the generator to 112/113 |
| Proposal 113 (self-model as instrument, partial) | Cadence-gate priced introspection; retire purpose/stage | Preserves `limitations_known`/`open_questions` content and its identity-prompt injection; defers content quality as "a separate testable question" |

**Retired / subsumed by this design:** the three per-channel `_wip_owns_slot()`
propose stand-downs in `evolve()`, `_implement_approved_plans`, and
`propose_task_docs` collapse into the one generation authority (Step 5); proposal
116's submission-side admission decision is *relocated* to the pre-mint owner and
kept in `submit_for_review` only as a thin backstop for the external/user
channels (Step 6). Nothing is deleted before its replacement is proven.

## Implementation Plan

Each behavior change is flag-gated with the default preserving today's behavior
and is independently mergeable; an interrupted rollout leaves the loop running as
it does now. The metric lands first so every later step is measured; the
generation authority lands before the channels route through it; the self-model
content anchor and the retirements land last.

### Phase A — Instrument the surviving channel

1. [x] **Attribute each net-new mint to its channel.** `maestro/audit_checks.py`
   (new `classify_plan_mint_channel`, `measure_dead_on_arrival_mints`),
   `maestro/cli.py` — a zero-SDK reader over `evolution_plans.jsonl` that labels
   each plan's originating channel (`evolve` / `generate_task` / `implement` /
   `docs`, inferred from the persisted `source_task_id`/`parent_plan_id`/creation
   context already on the record) and counts, per channel, the mints obsoleted by
   a saturation/admission reason within the recent window. Composes with
   `measure_plan_theme_saturation` / `measure_merged_to_created_ratio`; exposed
   via CLI with `--json`.
   - *Depends on:* none.
   - *Acceptance:* a unit test seeds a plan set with known per-channel mint and
     dead-on-arrival counts and asserts the reader reproduces them; the CLI prints
     them.

2. [x] **Surface the `DEAD-ON-ARRIVAL-MINTS` sensor in contemplation.**
   `maestro/agent.py` (`contemplate`, the `## Measured Behavioral Data` block) —
   a self-suppressing zero-SDK line reading Step 1's per-channel dead-on-arrival
   count, so the loop confronts the cost of minting-then-obsoleting rather than
   re-learning it as a fresh inward limitation.
   - *Depends on:* 1.
   - *Acceptance:* with seeded activation/plan rows, the measured-data block
     renders one `DEAD-ON-ARRIVAL-MINTS` line; with none, the line is absent.

### Phase B — One pre-mint generation authority

3. [x] **Add the generation-authority owner.** New `maestro/generation_gate.py`
   — a pure `may_mint_plan(pending_count, theme_saturation, candidate_theme, …)
   -> GenerationVerdict` returning `admit` or `refuse(reason)`, sourced entirely
   from proposal 116's `pending_plan_submit_block_threshold` and
   `measure_plan_theme_saturation` / `generation_admission_theme_fraction` — the
   *same* inputs `submit_for_review`'s admission gate reads today, with no new
   threshold. This is the single owner of the "may a net-new plan be minted this
   tick?" decision.
   - *Depends on:* none.
   - *Acceptance:* a truth-table test asserts `refuse` on a saturated backlog and
     on an over-concentrated theme, and `admit` otherwise, over in-memory inputs
     with no I/O.

4. [x] **Gate `evolve()`'s propose tail on the owner (the surviving leak).**
   `maestro/agent.py` (`evolve`), `maestro/config.py`, `maestro/toml_config.py`,
   `maestro.toml` — behind `[loop] pre_mint_generation_gate_enabled` (default
   `false`), consult `generation_gate.may_mint_plan` **before** `reflect()` and
   `propose_plan()`; on `refuse`, skip both priced calls and write the same
   bounded prune/land directive proposal 116's gate writes into an empty
   `next_task_directive`. The `_wip_owns_slot()` short-circuit and the flag-off
   path are byte-identical to today.
   - *Depends on:* 3.
   - *Acceptance:* with the flag on and a saturated backlog, an `evolve()` cycle
     issues **zero** `reflect`/`propose_plan` calls and writes the prune/land
     directive; with the flag off, both calls fire as today.

5. [x] **Route the other self-initiated minters through the same owner; retire
   their bespoke stand-downs.** `maestro/agent.py` (`_implement_approved_plans`,
   `propose_task_docs`) — replace each channel's `_wip_owns_slot()` propose
   stand-down with a call to `generation_gate.may_mint_plan`, so one owner makes
   the admit/refuse decision for every channel. Behind the same
   `pre_mint_generation_gate_enabled` flag.
   - *Depends on:* 4.
   - *Acceptance:* with the flag on, each channel consults `may_mint_plan` and
     opens no new plan work under a saturated backlog; the removed per-channel
     stand-down branches have no remaining callers.

6. [x] **Demote the submission-side admission gate to a backstop.**
   `maestro/evolution.py` (`submit_for_review`) — when
   `pre_mint_generation_gate_enabled`, the pre-mint owner is authoritative for
   self-initiated channels, so the in-`submit_for_review` admission check runs
   only as a defense-in-depth backstop for plans reaching submission by another
   path (external/user resubmission); its `obsolete` reason and directive are
   unchanged. No behavior change with the flag off.
   - *Depends on:* 4, 5.
   - *Acceptance:* with the flag on, a self-initiated mint is refused pre-propose
     (no `submit_for_review` obsoletion needed); an externally-supplied saturated
     plan still hits the backstop and is recorded `obsolete` as today.

### Phase C — Anchor the self-model seed to external capability

7. [x] **Add an external-capability-anchor test to the vanity filter.**
   `maestro/consciousness.py` (`update_self_model`), `maestro/config.py`,
   `maestro/toml_config.py`, `maestro.toml` — behind `[loop]
   self_model_external_anchor_enabled` (default `false`), extend the
   `limitations_known` vanity-filter prompt with a fifth test: a limitation
   qualifies only if it names an **external-observable** capability, behavior, or
   audience effect — not solely the loop's own plan-mint / PR-remediation
   plumbing — and the instruction calls the inward-plumbing case out as vanity by
   example, mirroring the existing metaphysical/architectural exclusions. Prompt
   edit plus the flag; no schema change. Depends on proposal 113, which preserved
   this filter and deferred content quality.
   - *Depends on:* none.
   - *Acceptance:* `tests/test_consciousness.py` asserts the extended prompt
     contains the external-anchor test and the inward-plumbing exclusion example
     with the flag on, and is byte-identical to today with it off; the
     `MAX_LIST_ITEMS` / restatement / compound guards stay green.

### Phase D — Enforce and retire

8. [x] **Structural guard — every priced mint is dominated by the authority.**
   `tests/test_structural_guards.py` — an AST guard asserting that on the
   `pre_mint_generation_gate_enabled` branch each priced `propose_plan` call site
   (`evolve`, `_implement_approved_plans`, `propose_task_docs`) is dominated by a
   `generation_gate.may_mint_plan` call, and that `generation_gate` is the sole
   owner of the mint-admission predicate (no rival ad-hoc saturation read at a
   mint site). Same synthetic-parity pattern as the merge-truth / CI-truth /
   proposal-116 decoupling guards.
   - *Depends on:* 4, 5.
   - *Acceptance:* the guard fails if a future edit adds an un-gated priced
     `propose_plan` call site or a second module re-derives the admission
     predicate, and passes on the Step 4–6 tree.

9. [ ] **Retire the subsumed stand-downs (after Steps 4–8 proven).**
   `maestro/agent.py`, `CLAUDE.md` — delete the now-unreferenced per-channel
   `_wip_owns_slot()` propose stand-down branches in `evolve()`,
   `_implement_approved_plans`, and `propose_task_docs` once the generation
   authority owns the decision, and update the WIP-primacy / plan-saturation
   invariant lines to name the single owner. Lands last, only after the Step 1
   metric records that per-channel dead-on-arrival mints fell.
   - *Depends on:* 1, 4, 5, 6, 7, 8.
   - *Acceptance:* the removed branches are absent, the Step 8 guard and full
     suite (incl. `tests/test_no_line_citations_in_docs.py`) stay green, and the
     Step 1 metric shows dead-on-arrival mints and themed fraction fell while
     merged:created held or rose.

## Testing Strategy

All unit tests use the `mem_fs` fixture with mocked SDK/provider, VCS, and
notifier seams — no real network, subprocess, `sleep`, or filesystem — per
CLAUDE.md testing principles.

- **Step 1 (`tests/test_audit_checks.py`, `tests/test_cli.py`):** a seeded
  `evolution_plans.jsonl` with known per-channel mint counts and known
  saturation/admission-obsoleted subsets; assert `classify_plan_mint_channel` and
  `measure_dead_on_arrival_mints`, and a `--json` CLI smoke test.
- **Step 2 (`tests/test_agent_contemplate.py`):** the measured-data block renders
  the `DEAD-ON-ARRIVAL-MINTS` line from stubbed rows and suppresses it when zero.
- **Step 3 (`tests/test_generation_gate.py`):** `may_mint_plan` truth table —
  refuse on saturated backlog, refuse on over-concentrated theme, admit
  otherwise — over in-memory inputs, asserting no I/O.
- **Step 4 (`tests/test_agent.py`):** with the flag on and a saturated backlog,
  `evolve()` issues zero `reflect`/`propose_plan` queries (mocked usage recorder)
  and writes the prune/land directive; with the flag off, both fire and the
  directive path is unchanged; cover the `_wip_owns_slot()` short-circuit branch.
- **Step 5 (`tests/test_agent.py`):** `_implement_approved_plans` and
  `propose_task_docs` consult `may_mint_plan` under the flag and open no plan work
  when refused; the old stand-down branch is unreferenced.
- **Step 6 (`tests/test_evolution.py`):** with the flag on a self-initiated mint
  never reaches the `submit_for_review` admission obsoletion; an
  externally-supplied saturated plan still hits the backstop and is recorded
  `obsolete`; flag-off parity.
- **Step 7 (`tests/test_consciousness.py`):** the extended vanity-filter prompt
  contains the external-anchor test and the inward-plumbing exclusion with the
  flag on and is byte-identical with it off; restatement/compound/`MAX_LIST_ITEMS`
  guards stay green.
- **Step 8 (`tests/test_structural_guards.py`):** a synthetic fault (an un-gated
  priced `propose_plan` site; a second module re-deriving the predicate) is
  flagged, and the live tree passes — the deterministic enforcement anchor.
- **Step 9:** absence tests for the removed stand-down branches; a full-suite
  green run at 100% coverage.

The deterministic enforcement anchors are Step 8's structural guard and Step 1's
metric. The proposal is falsified if, after Steps 3–7 activate, the
`evolve()`-driven net-new mint cadence and per-channel dead-on-arrival count do
not fall while the backlog is saturated, or if the merged:created ratio
regresses.

## Risk Assessment

- **Starving legitimate self-improvement.** The pre-mint gate could refuse a
  genuinely novel plan when the backlog happens to be saturated. Mitigation: it
  keys on the *existing* proposal-116 saturation threshold and theme fraction (no
  new limit), writes a prune/land redirect rather than silently dropping, and
  fires only when a slot is genuinely unavailable; the loop retains PR tending /
  remediation, contemplation, dreaming, audit, and — when a slot opens — evolution
  planning. Every behavior change is flag-gated default-off and reverts by flag.
- **Pre-mint theme arm assumed a themed candidate (resolved by #4151).**
  `evolve()`'s pre-mint gate originally called `may_mint_plan` with
  `candidate_is_self_measurement` hard-set to `True`; with no candidate pre-mint,
  a theme-dominated obsolete graveyard refused every mint at any backlog depth,
  and with nothing new ever obsoleted the window never diluted — a latch with no
  release. Resolved by narrowing every pre-mint channel to the
  candidate-independent backlog-saturation arm and judging theme concentration
  per-candidate at the `submit_for_review` door, including for `self_initiated`
  submissions, so a non-themed plan is always mintable.
- **Over-tight self-model anchor (Step 7).** The external-anchor test is the more
  speculative half; an over-strict prompt could suppress a legitimately-internal
  limitation (e.g. a real correctness defect in a loop mechanism). Mitigation:
  it is a separate default-off flag, edits an existing constant, is trivially
  revertible, and is measured by the Step 1 themed-fraction metric before Phase D
  retires anything. It complements — does not replace — proposal 116's lagging
  `limitations_over_generative_debt` fuel-starve.
- **Relocation regression.** Steps 4–6 move the admission decision earlier;
  proposal 116's submission gate stays as a backstop, so no path loses the check.
  The Step 8 structural guard proves the pre-mint check cannot be bypassed on the
  flag-on branch, and Step 6's backstop test proves the external/user path is
  still covered.
- **Trust chain.** Untouched — no approval, sandbox, tool-gating, or public-API
  surface changes; the change only *withholds* a priced generation call and
  never approves, merges, or writes code. The human approval gate for any plan
  that does reach review is unchanged.
- **Rollback posture.** All behavior is behind `[loop]
  pre_mint_generation_gate_enabled` and `[loop] self_model_external_anchor_enabled`,
  both defaulting to today's behavior; Phase D deletions are a normal revert. No
  state-file schema change beyond additive config fields.

## Out of scope

- **Activating proposal 116's already-shipped machinery further, or flipping any
  116/113 flag.** Rollout posture is proposal 117's domain; this proposal adds the
  *pre-mint* control 116 deliberately left at the submission door, and does not
  re-tune 116's thresholds.
- **The submission-side premise-falsity leak** (a plan whose premise the tree
  refutes) — proposal 114 owns the door-side grounding oracle; this proposal is
  its generation-side complement and reuses none of its probe vocabulary.
- **Deleting `generate_task`, the purpose engine, or the stage ladder** —
  proposals 116 and 113 own those retirements; this proposal governs the surviving
  `evolve()` channel and the self-model content, and touches neither.
- **Re-tuning `evolve_every` or `contemplate_every`.** Changing a cadence is a
  threshold tweak, not a structural remedy; the design gates *whether* a mint
  happens on backlog state, not *how often* the phase ticks.
- **Curating the accumulated obsolete-plan graveyard or the inward self-model
  content already on disk.** Operator data actions; the design stops the graveyard
  growing and re-aims new content, it does not retro-clean existing rows.
