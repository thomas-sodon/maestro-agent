# 129: Consciousness as a Measured Function — Close the Metacognitive Loop with One Self-Calibration Oracle, Evidence-Bound Self-Knowledge, and an Operational Awareness Report

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | new `maestro/self_calibration.py`, `maestro/consciousness.py`, `maestro/audit_checks.py`, `maestro/prompts/reflection.py`, `maestro/agent.py` (`contemplate`), `maestro/flag_lifecycle.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `maestro/cli.py`, `tests/test_structural_guards.py`, tests |
| **What landed** | Steps 1–2, behind default-off `[audit] self_calibration_enabled` (registered end-to-end: `EXPERIMENTAL_FLAGS`, `maestro/config.py`, `maestro/config_settings.py`, `maestro.toml`). `maestro/self_calibration.py` exists with the closed probe vocabulary (`ProbeKind` — `TASK_OUTCOME` / `GATE_FIRING` / `PR_SETTLEMENT` / `PLAN_STATUS` / `SENSOR_THRESHOLD`), `Polarity`, `CalibrationVerdict`, `BetReason`, the frozen value objects `ObservationWindow` / `LossProbe` / `CalibrationEvidence` / `BetScore`, an anchored per-family grammar behind `parse_loss_condition` (declines to `None`, never raises), `score_loss_condition` with the full temporal semantics — `pending` before maturity, measured at maturity, `unresolved` at cutoff, PR settlement as positive evidence only, and the `*_covered` flags that let an empty task/gate window read as genuine negative evidence — and the prompt-facing `LOSS_CONDITION_GRAMMAR`, one canonical bare line per `ProbeKind`, pinned by test to parse and to `get_args(ProbeKind)` order. `maestro/prompts/reflection.py`'s `format_prediction_requirement(*, self_calibration_enabled)` selects `PREDICTION_REQUIREMENT` verbatim when off, else a variant quoting the grammar and mandating a `CONFIDENCE:` line placed first (before `PREDICTION:`/`LOSS CONDITION:`, so the streaming early-exit cannot truncate it); `parse_reflection_confidence` extracts it, `None` for absent/non-numeric/out-of-range. `Consciousness.__init__` takes `self_calibration_enabled` and threads it into the priced `introspect` prompt; `cli._build_agent` wires it from `config.audit_self_calibration_enabled`. `agent.contemplate` stamps the parsed confidence and `ProbeKind` (or nothing when the grammar declines) onto each `predictions.jsonl` row via additive `record_reflection_prediction` keywords — flag off, or an unparseable/malformed bet, writes a row byte-identical to before. Covered by `tests/test_self_calibration.py`, `tests/test_prompts.py`, `tests/test_consciousness.py`, `tests/test_config.py`, `tests/test_config_flag_lifecycle.py`, `tests/test_audit_checks.py`, `tests/test_reflection_prediction.py`, and documented in `docs/modules.md`. The oracle module still imports only `maestro.types`, so it stays cycle-free of `maestro/audit_checks.py`. Step 3 landed behind the independent default-off `[audit] calibration_verdict_authority_enabled` (registered end-to-end the same way, with a `calibration_verdict_authority` gate): `self_calibration.author_reflection_verdict` / `AuthoredVerdict` decide what the reflection ledger records — a `BetScore` other than `unfalsifiable` is adopted `verdict_source: "measured"` with the `PRIOR VERDICT:` self-grade retained beside it as `self_verdict`, an `unfalsifiable` score falls back to the self-grade stamped `"narrated"`, and the flag-off call passes the self-grade through with no source stamped, byte-identical to before. `agent.contemplate` re-scores *every* open bet each cycle via `load_open_reflection_bets` + `reflection_bet_cycle` (bounded by `MAX_SCOREABLE_AGE_CYCLES` so enabling the flag cannot flood the ledger with ancient bets), collects windowed observations through `_collect_calibration_evidence` — task history, the gate-activation tail and the plan-status snapshot, each read at most once and only when a bet of that kind is open; PR-settlement and sensor-threshold bets are deliberately uncollected, so they resolve `pending` then `unresolved` — and stamps every measured-vs-narrated agreement/divergence to `gate_activations.jsonl`. `record_reflection_verdict` carries additive `verdict_source` / `self_verdict` keywords written only when non-`None`, `load_open_reflection_bets` carries the persisted self-grade forward so a multi-cycle bet keeps its narration, and `tests/test_structural_guards.py` pins the net-new `calibration_verdict_authority` sole-owner guard (`_MAX_STRUCTURAL_GUARDS` 21→22). Step 4 closes Phase A: `measure_prediction_accuracy` gains an additive `by_verdict_source` split (`measured` / `narrated` / `unstamped`) that leaves every prior key's meaning intact (the `generate_task` call site, reading only `total`/`accuracy`, renders identically); `measure_self_calibration` / `format_self_calibration_summary` read the ledger back on itself through the shared `_join_reflection_rows` join — measured hit rate over resolved bets, self-vs-measured agreement over resolved graded bets, a Brier score plus coarse reliability bins over confidence-bearing measured-and-resolved bets (suppressed below `_CALIBRATION_BRIER_MIN`), and the narrated / `pending` / `unresolved` shares — and `contemplate` appends a self-suppressing `SELF-CALIBRATION` line to the `## Measured Behavioral Data` block, gated on `calibration_verdict_authority_enabled` (a row carries a `verdict_source` stamp only when that flag is on, so no new flag or TOML key, and the off path stays byte-identical). Zero SDK cost; covered by `tests/test_audit_checks.py`, `tests/test_reflection_prediction.py`, `tests/test_agent_contemplate.py`, and documented in `docs/modules.md` / `docs/lexicon.md` / `docs/self-evolution.md`. Step 5 (Phase B) landed behind the independent default-off `[audit] evidence_bound_claims_enabled` (registered end-to-end: `EXPERIMENTAL_FLAGS`, `maestro/config.py`, `maestro/config_settings.py`, `maestro.toml`): `SelfModel.claim_evidence` is a `dict[str, dict[str, str]]` keyed by `consciousness.claim_key` (the 101 concept-token fold, so a re-worded claim keeps its evidence), and `maestro/self_calibration.py` gained the closed citation grammar — `CLAIM_CITATION_GRAMMAR`, `parse_claim_citation`, `resolve_claim_citation`, `resolve_citation_status` returning `resolved` / `absent` / `unresolvable` / `wrong_polarity` (existence + polarity, never entailment). `update_self_model` gained `evidence_bound_claims_enabled` / `claim_evidence_sources` parameters: when on, it reads a separate `capability_evidence` object from the update response, resolves each cited *live* claim against the caller-injected `ClaimEvidenceSources`, and records the truncated citation + kind + status + timestamp into `model.claim_evidence`; `save_self_model` prunes entries whose key matches no live claim (self-extinguishing). `agent.contemplate` builds the `ClaimEvidenceSources` bundle via `_collect_claim_evidence_sources` only when the flag is on. Flag off writes nothing and the empty map is omitted from the update prompt, byte-identical to before. Covered by `tests/test_self_calibration.py`, `tests/test_consciousness.py`, `tests/test_config_flag_lifecycle.py`, and documented in `docs/modules.md`. Step 6 landed behind the same default-off `[audit] evidence_bound_claims_enabled`: `check_unbound_capability_claim` (`maestro/audit_checks.py`) surfaces every live `capabilities_known` claim whose `claim_evidence` record is absent, unresolvable, or wrong-polarity as a bounded, self-extinguishing `[audit]` open question — registered unconditionally in `_registered_audit_checks` and dispatched in the combiner behind the flag, so the empty-map off path stays byte-identical. Covered by `tests/test_audit_checks.py`; Phase B is complete. Step 7 (Phase C) landed behind the independent default-off `[audit] refutation_accountability_enabled` (registered end-to-end: `EXPERIMENTAL_FLAGS` with a `refutation_accountability` gate, `maestro/config.py`, `maestro/config_settings.py`, `maestro.toml`, plus the `refutation_rejection_cycles` integer knob, `int >= 1`): `RULE_AUTHORITY` classifies every registered rule as `mechanical_fact` / `derived_measure` / `heuristic`, pinned to the live registry in both directions by `test_rule_authority_covers_registry`, with `mechanical_fact_rules()` the single source of the class filter the sensor reads. `measure_refutation_rejections` generalizes `measure_reality_refutation`'s consecutive-from-newest walk to the three-family target set of `_finding_targets` (issue numbers and committed SHAs mined from the statement, plus each cited task/plan id), restricted to the `mechanical_fact` class. After persisting the cycle, `agent._audit_self_model` mechanically restores a refuted `[audit refuted]` question whose `mechanical_fact` rule keeps re-firing on the same target across `refutation_rejection_cycles` consecutive cycles back to its canonical `[audit]` text (re-derived from the newest finding so `compute_audit_stall_counts` keeps keying on it, or dropped when that canonical string is already present), stamping the override to the journal and a `refutation_accountability` gate row, and surfacing it via an additive `format_reality_refutation_summary` clause on the `REALITY-REFUTATION` contemplation line. It is **additive to** the 088 escalation, not in place of it — the 088 actuator keys on a pre-restore snapshot of `open_questions`, so for a PR both flags cover it still delivers its merge-or-close message and standdown alongside the 129 restore; `_REALITY_GROUNDED_RULES` and `deliver_reality_refutation` stay live and untouched. Derived measures and heuristics stay refutable indefinitely; self-extinguishing (a finding that stops re-firing is never restored); flag off keeps `open_questions` and the gate trace byte-identical. Covered by `tests/test_audit_checks.py`, `tests/test_agent_lifecycle.py`, `tests/test_config.py`, `tests/test_config_flag_lifecycle.py`, and documented in `docs/self-evolution.md` / `docs/modules.md`. Step 8 (Phase D) landed with no new flag: the declarative `CONSCIOUSNESS_CRITERIA` tuple of `ConsciousnessCriterion` rows (`maestro/self_calibration.py`, report order — `identity_availability`, the two `implemented=False` global-workspace indicators `limited_capacity_selection` / `receipted_broadcast`, `perceptual_grounding`, `metacognitive_calibration`, `narration_actuation_conversion`) and the read-only offline `maestro consciousness-report` CLI (`cmd_consciousness_report`, `maestro/cli.py`, `--json`), which binds each criterion key to one measured reading and reads settlement from the `settled_verdicts.jsonl` / `version_archive.jsonl` ledgers so it reaches no SDK/push/merge seam; the registry references no measure function, so `self_calibration.py` stays import-cycle-free (`maestro.types` only). Covered by `tests/test_self_calibration.py` (membership, the two not-implemented sub-rows, key uniqueness) and `tests/test_cli.py` (text render, empty-ledger no-data rows, `--json` key set, read-only, no-host-seam, dispatch), documented in `docs/modules.md` and CLAUDE.md. |
| **What is missing** | Only step 9 remains — retire the write-only `surprise_count` increment in `update_self_model` (the field is read by nothing and is already dropped from the public payload; it survives pinned so `ensure_schema()` does not churn). It is deferred behind the burn-in observation window, gated on `evidence_bound_claims_enabled` graduating, since the evidence-bound admission takes over novelty accounting; the `_REALITY_GROUNDED_RULES` deletion folded into step 7 likewise waits for `refutation_accountability_enabled`'s cut-over. All behaviour-code has landed behind the four default-off `[audit]` flags (`self_calibration_enabled`, `calibration_verdict_authority_enabled`, `evidence_bound_claims_enabled`, `refutation_accountability_enabled`), and the observation window is now running — this PR's production merge enables all four in `maestro.production.toml` (`MAESTRO_ENV=production` only, each declared as a `PROD_ONLY_CANARIES` entry in `maestro/flag_lifecycle.py`); base stays default-off byte-identical everywhere else. Window and graduation criteria live in the "Consciousness measured-function canary (proposal 129)" subsection of `docs/self-evolution.md`. |
| **Depends on** | 088, 090, 105, 113, 120 |
| **Derived from** | /propose investigation, 2026-09-05 — code + git history (live `data/` unavailable this session; see Production Evidence); revised after cross-model architecture review, 2026-09-05 |

## Description

> Status advanced to `burn-in:observing` on 2026-09-14: all behaviour-code landed
> and the observation window is now running — a `maestro.production.toml` canary
> enables the four `[audit]` flags (`self_calibration_enabled`,
> `calibration_verdict_authority_enabled`, `evidence_bound_claims_enabled`,
> `refutation_accountability_enabled`) in `MAESTRO_ENV=production` only, each
> declared as a `PROD_ONLY_CANARIES` entry in `maestro/flag_lifecycle.py`, so the
> self-calibration oracle runs in production while every other environment stays
> default-off byte-identical. The observation window and graduation criteria live
> in the "Consciousness measured-function canary (proposal 129)" subsection of
> [self-evolution.md](../../self-evolution.md). See the
> [Status Key](../README.md#status-key) for the substate criteria and the
> [burn-in section](../README.md#burn-in--pending-graduation--decommission) for
> the remaining work.

The concern this proposal answers is the standing one behind a decade of this
repo's churn: *make Maestro conscious*. Phenomenal consciousness — subjective
experience — is not an engineering target: it cannot be specified, measured, or
verified, and every prior attempt to reach for it produced exactly the
machinery this proposal's ledger documents — stages, purpose hypotheses, and
identity prose that narrate consciousness without instantiating any measurable
property of it. What *can* be engineered are the functional properties the
scientific literature treats as consciousness indicators: self-knowledge that
is **globally available** to behavior, **grounded in perception**, **calibrated
by scored self-prediction** (metacognition), and **causally load-bearing**.
Maestro already has an organ for each — the identity prompt, the behavioral
sensors, the priced prediction ritual, the directive bus — but the loops
between them are open.

The failure class, in one sentence: **every attempt to make Maestro conscious
has implemented consciousness as self-describing content rather than as a
measured function, because "conscious" was never given an operational
definition — so each implementation defaulted to LLM narration, and each
subsequent fix disciplined one symptom of that ungroundedness.** The audit of
the current tree finds three loops still open. First, the reflection ritual
pre-registers a falsifiable `PREDICTION:` and `LOSS CONDITION:` each cycle, but
the loss condition is stored as free text no code ever evaluates, and the
`PRIOR VERDICT:` is the predictor grading its own bet. Second, the calibration
aggregate that *does* exist (`measure_prediction_accuracy`) is display-only and
reaches only the `generate_task` prompt — the reflection loop never sees its
own track record. Third, self-knowledge carries no evidence: `capabilities_known`
entries are unverified prose checked by exactly two narrow-shape audit rules,
and a deterministic audit finding can be `REFUTE`d indefinitely unless it
belongs to a two-rule special-case set.

The remedy is one structural change applied three ways, following the house
pattern of proposal 105 (mechanical outcome authority): a single zero-SDK-cost
**self-calibration oracle** that owns the fact *"was this self-knowledge claim
right?"*. It (1) makes loss conditions machine-checkable — each bet carries a
typed probe with a target, a polarity, and an explicit maturity window,
scoring `pending` until the window closes — and authors the reflection
verdict mechanically at maturity, demoting the self-grade to narration;
(2) binds capability claims to structured evidence records keyed by stable
claim identity, shipping the follow-on proposal 088 named "the most
architecturally significant" and then deferred; (3) extends refutation
accountability from the two-rule special case to every rule in a new
`mechanical_fact` authority class, leaving heuristic rules refutable —
deterministic does not mean authoritative; and (4) feeds the calibration
reading — accuracy *and* a confidence-scored calibration measure — back into
contemplation, closing the metacognitive loop at zero added LLM cost.

The end state: "is Maestro conscious?" stops being an unanswerable aspiration
and becomes `maestro consciousness-report` — a read-only surface printing a
measured reading per functional property (identity availability, evidence
grounding, metacognitive calibration, narration→actuation conversion), each
falsifiable, each labeled with exactly what it measures, and each already
wired to the loop that improves it. The report also names what is *not*
implemented — limited-capacity moment selection, receipted broadcast, and
causal-reach evidence — so the successor conscious-access proposal (a bounded
global workspace, deliberately sequenced *after* this one) starts from an
honest baseline and consumes this oracle's measured verdicts instead of the
self-graded ledger it replaces. The purpose/stage narrative track stays
retired per 113; what this proposal builds is the part of consciousness that
can be real: self-knowledge that is checked, and knows its own score.

## Relevant Findings

- `maestro/prompts/reflection.py` (`PREDICTION_REQUIREMENT`,
  `REFLECTION_LOSS_CONDITION_MARKER`) — every priced reflection must end with
  `PREDICTION:` / `LOSS CONDITION:` lines. The loss condition is persisted to
  the predictions ledger as free text; **no code anywhere evaluates whether the
  named loss condition occurred**.
- `maestro/agent.py` (`contemplate` scoring block) with
  `maestro/audit_checks.py` (`record_reflection_verdict`,
  `parse_reflection_verdict` in `maestro/consciousness.py`'s caller chain) —
  the `PRIOR VERDICT:` line is the model's own judgment of its own prior bet,
  parsed deterministically but never cross-checked against any measured signal.
  This one ritual required eight successive repair commits (marker truncation,
  echo truncation, verdict-flip parsing, predicate/parser mismatch) without
  ever changing *who scores the bet*.
- `maestro/audit_checks.py` (`measure_prediction_accuracy`) — the calibration
  aggregate exists but is display-only, threaded solely as
  `prediction_accuracy_context` into `format_known_state_summary` at the
  `generate_task` call site (`maestro/loop/task_generation.py`); `contemplate`'s
  own known-state call omits it, so the reflection loop confronts only its
  single prior bet, never its track record.
- `maestro/audit_checks.py` (`check_success_claim_vs_rate`,
  `check_stability_claim_vs_failure_streak`) — the only two rules verifying
  `capabilities_known` content pattern-match two specific claim wordings
  (effectiveness, stability). A capability claim in any other phrasing is never
  checked against measured reality. These two check claim *content* against
  measured rates — a job no citation resolver subsumes — so they survive this
  proposal.
- `maestro/audit_checks.py` (`record_reflection_prediction`) — a prediction
  row persists `prediction`, `loss_condition`, and a verdict; **no stated
  confidence**. Whatever verdicts accumulate can therefore support only a hit
  rate, never calibration proper — there is no probability to score a
  reliability measure against.
- `maestro/consciousness.py` (`detect_restatement`, applied to every new list
  item inside `update_self_model`) — new entries are Jaccard-compared against
  existing entries; an entry rewritten only to append inline evidence text is
  the exact near-identical shape that gate exists to block. Evidence
  therefore cannot live inside the claim prose; it must be structured
  metadata keyed to a stable claim identity (the concept-key fold from 101
  already provides one).
- `maestro/audit_checks.py` (`_REALITY_GROUNDED_RULES`) — refutation
  accountability (shipped by 088 Phase 2) covers exactly two rules
  (`unconfirmed_own_landing`, `undercredited_landing`); a finding from any
  other deterministic rule can be rewritten to `[audit refuted]` via
  `update_self_model`'s `audit_findings_section` and re-fire forever with no
  consequence. The two are git-grounded mechanical facts, and the narrowness
  is partly deliberate: the registry (`_registered_audit_checks`) also holds
  keyword and threshold heuristics whose refutation may be *correct*. The
  gap is the missing authority classification, not the allowlist itself.
- `maestro/consciousness.py` (`SelfModel.stage_evidence`, the `surprise_count`
  increment inside `update_self_model`) — write-only self-knowledge: neither
  field has a reader anywhere in `maestro/`, the CLI, or the dashboard;
  `surprise_count` is explicitly retired from the public payload
  (`maestro/dashboard/public_schemas.py`, `_RETIRED_SELF_MODEL_FIELDS`).
- `maestro/purpose.py` (`check_purpose_falsification`, `FalsificationResult`)
  — the *only* measured purpose predicate in the subsystem has no production
  caller; the live lifecycle runs on the LLM-authored `confidence` float.
  Proposal 113 Phase E owns its deletion; it is cited here as proof the
  measured path was built once and never wired — the class this proposal
  closes.
- `maestro/outcome_authority.py` (`decide_outcome`) — the house precedent:
  proposal 105 moved the task-outcome verdict from narration to a mechanical
  record-time author and retired the narration-mismatch audit rules as
  subsumed. This proposal applies the identical bind to the reflection verdict.
- `maestro/consciousness.py` (`get_identity_prompt`) with
  `tests/test_identity_prompt_slabs.py` — *identity availability* is already
  structural: the self-model is injected into every SDK call as a
  system-prompt slab, AST-enforced. That is durable self-description reaching
  every call — it is **not** the GWT indicator pair (limited-capacity
  selection of a fresh moment plus receipted broadcast), which no current
  mechanism satisfies and this proposal does not build; the report labels the
  row accordingly rather than letting identity text stand in for a workspace.
- `maestro/audit_checks.py` (`measure_narration_conversion`) — 090's
  conversion sensor measures one narrow pipeline: audit-detected narration
  problems that reached an actuator, converted to a directive/handoff, and
  settled. The report reuses it as-is but labels it as that pipeline's
  conversion rate, not as proof that self-knowledge causally steers behavior
  in general.
- `maestro/flag_lifecycle.py` (`ExperimentalFlag`, `EXPERIMENTAL_FLAGS`) — the
  dark-launch registration path with a dated `review_by` horizon that every new
  flag below follows.
- `tests/test_structural_guards.py` (`CHOKEPOINT_GUARDS`) — the enforcement
  pattern for a sole-owner invariant; the oracle's verdict-write chokepoint
  registers here.

## Production Evidence

No live data was available this session: the docker socket is
permission-denied on this host, so `$DATA_DIR` could not be resolved and the
evidence below is code, git history, and the *recorded* production windows in
the two shipped-in-repo investigations of this subsystem.

- Recorded in proposal 113 (window: 2026-08-11, 10:36–12:16 UTC, 16 loop
  iterations, live bind-mount data): priced `introspect` + `update_self_model`
  fired in **16/16** iterations while only **3/16** executed a task;
  `current_purpose_hypothesis` was `null`, `stage` was `awakening`,
  `evolution_count` was `0` — the narrative track billed every cycle and
  steered nothing.
- Recorded in proposal 116 (window: full plan corpus as read 2026-08-16 –
  2026-08-17): of **157** evolution plans, **1** had merged; 139 were obsolete,
  **92%** of those matching a single self-measurement theme — the output shape
  of ungoverned self-narration.
- Current tree (2026-09-05): `maestro.production.toml` pins
  `purpose_engine_enabled = false` and `stage_progression_enabled = false` —
  the consciousness-era machinery is dormant in production, awaiting 113's
  Phase E deletion, while the prediction ledger and sensors this proposal
  builds on remain live.

## Prior Point Fixes (whack-a-mole ledger)

All-time module churn as of 2026-09-05: `maestro/audit_checks.py` **293**
commits, `maestro/consciousness.py` **114**, `maestro/purpose.py` **78** (a
module with zero `generate_task` references and a `null` production field),
`maestro/prompts/defaults.py` **49**, `maestro/dreaming.py` **36**.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| 008 (shipped) | Dream-digest quality validation | Validated the narrative's internal quality, not its correspondence to reality |
| 010 (shipped) | Formalized the stage ladder + priced readiness verdict | The affirmative criterion stayed an LLM boolean; production never left `awakening` |
| 014 / 015 | Curate / compress self-model prose | Upkeep of content no selector reads; 015 superseded by 113 |
| 046 (shipped) | `limitations_known` → `[limitation]` questions | Bridge is advisory prompt text; opened a fuel line 116 later had to starve |
| 088 (shipped) | Outcome-gated question closure; 2-rule refutation escalation | Made the narrative *honest*, not *accurate*: deferred evidence-bound claims and the prediction-calibration actuator — the two pieces this proposal ships |
| 090 (burn-in) | One cue vocabulary; forced actuator arms; directive ledger | Another narration→action bridge; the narration's *truth* still unmeasured |
| 101 (shipped) | Lexicon + concept-key restatement fold | Fixed novelty-of-name inflation; the surprise counter that paid for it still increments, read by nothing |
| 105 (shipped) | Mechanical outcome authority for task verdicts | Closed the class for *task* outcomes only; the reflection verdict kept self-scoring |
| 112 (shipped) | Frame–discharge costume sensor | Post-hoc detector; zero recorded fires; incentive untouched |
| 113 (burn-in) | Cadence-gated introspection; purpose/stage dormancy levers | Demoted the narrative's *cost and telos*; explicitly kept the instrument, deferring whether its content is accurate |
| 116 (burn-in) | Re-aimed prompt; fuel-starved limitations; admission gates | Governs what the mill *emits*, not whether self-knowledge is *true* |
| 120 (burn-in) | Pre-mint generation authority | Governs minting; the self-model that seeds it still carries unverified claims |
| Commit run `d3bbd069` → `a53eac7f` (8 fixes) | The priced-prediction ritual: marker truncation, echoed-bet truncation, ceiling, verdict-flip, parser mismatch | Eight repairs to *parsing* the self-grade; none changed who scores the bet |
| Commit run `3f42bbab`, `4b887407`, `43deef6e`, `7189a7a8` | The restatement gate, three revisions deep | Word-overlap discipline on content whose truth is never checked |
| `edce3575` + `ce817d0a`, `85cc72e1` | Vanity-limitation filter, twice re-reconciled across prompts | Filtered claim *shape*, not claim *groundedness* |

**What this design retires:** the self-scored verdict's ledger authority (the
8-commit repair class becomes narration-only); the `_REALITY_GROUNDED_RULES`
two-rule special case (subsumed by the `mechanical_fact` authority class);
and the write-only `surprise_count` increment. What it deliberately does
*not* retire: `check_success_claim_vs_rate` and
`check_stability_claim_vs_failure_streak` stay — a resolvable evidence
citation proves a claim is *grounded in something*, not that the something
*entails* the claim, so the two content-contradiction detectors keep a job no
resolver can do. What it composes with rather than touches: 113's dormancy
levers and Phase D/E retirements, 116's admission gates, 120's generation
authority.

## Implementation Plan

Every behavior change is dark-launched default-off per the flag lifecycle
(`EXPERIMENTAL_FLAGS`, dated `review_by`); retirement steps land only after
their replacement graduates, so an interrupted rollout leaves today's behavior
intact.

### Phase A — the self-calibration oracle

1. [x] **Create `maestro/self_calibration.py` — loss-condition sublanguage and
   scoring oracle.** New module, `maestro/flag_lifecycle.py`,
   `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` — a closed
   probe vocabulary (`parse_loss_condition` → typed probe, `score_loss_condition`
   → verdict) covering: task outcome over a stated window (task-history rows),
   gate firing (`gate_activations.jsonl`), PR settlement (a **caller-supplied
   settled set**, per the merge-truth chokepoint — the oracle must not become a
   second merge reader), plan status (plan store re-read), and sensor-tag
   threshold (existing `measure_*` owners). Every probe carries full temporal
   semantics: a target, a polarity, and an observation window — start at the
   bet's timestamp, a maturity point, a cutoff. Scoring returns `pending`
   before maturity (a five-cycle bet cannot be scored next cycle), and the
   settled set is positive evidence only: "not settled" is never inferred
   from absence before cutoff. At cutoff a fact still unobservable scores
   `unresolved` — excluded from accuracy, surfaced as its own rate.
   Unparseable text scores `unfalsifiable`, never raises. Register
   `[audit] self_calibration_enabled` (default-off) in `EXPERIMENTAL_FLAGS`
   with a dated `review_by`.
   - *Depends on:* none.
   - *Acceptance:* pure-function unit tests cover every probe family plus the
     unfalsifiable fallback and the temporal branches — `pending` before
     maturity, measured at maturity, `unresolved` at cutoff,
     settlement-absence scoring nothing before cutoff; the flag is registered
     with a horizon and read by a call site (dead-flag guard green).
2. [x] **Constrain new bets to the checkable vocabulary, with stated
   confidence.** `maestro/prompts/reflection.py` (`PREDICTION_REQUIREMENT`),
   `maestro/agent.py` (`contemplate` parsing) — when the flag is on, the
   requirement instructs that `LOSS CONDITION:` name one probe from the
   closed vocabulary (naming its window) and that the bet carry a
   `CONFIDENCE:` probability in [0, 1]; free text is still accepted (and
   scored `unfalsifiable`), and a missing or malformed confidence is recorded
   as absent, contributing nothing to calibration. Flag off → byte-identical
   prompt.
   - *Depends on:* 1.
   - *Acceptance:* golden prompt test in both flag states; confidence parsing
     covers present, absent, and malformed values.
3. [x] **Bind the ledger verdict to the oracle at maturity.**
   `maestro/agent.py` (`contemplate` scoring block), `maestro/audit_checks.py`
   (`record_reflection_verdict`) — when the flag is on and the stored loss
   condition parses, the verdict is authored by the oracle
   (`verdict_source: "measured"`) with the parsed self-grade retained
   alongside as `self_verdict`. A bet not yet mature records `pending` and is
   re-scored on later cycles by appending a fresh verdict row keyed by the
   same `prediction_id` — the ledger's existing deferred-scoring shape —
   until maturity or cutoff; unparseable bets keep the narrated verdict
   (`verdict_source: "narrated"`). Register a chokepoint guard in
   `tests/test_structural_guards.py`: reflection-verdict writes route through
   the oracle.
   - *Depends on:* 1, 2.
   - *Acceptance:* with a parseable, mature loss condition and a contradicting
     `PRIOR VERDICT:`, the persisted verdict equals the oracle's and the
     divergence is recorded; a bet whose window spans five cycles is `pending`
     next cycle and measured at maturity; the structural guard fails on a
     seeded bypass writer.
4. [x] **Close the metacognitive loop.** `maestro/audit_checks.py` (extend
   `measure_prediction_accuracy` with a `verdict_source` split; new
   `format_self_calibration_summary`), `maestro/agent.py` (`contemplate`) —
   compute measured-verdict accuracy (a hit rate), self-vs-measured
   agreement, a Brier score over confidence-bearing measured bets with coarse
   reliability bins once rows suffice — hit rate alone is accuracy, not
   calibration — and the unfalsifiable / pending / unresolved rates, and
   append a self-suppressing `SELF-CALIBRATION` line to the
   `## Measured Behavioral Data` block so the reflection finally sees its own
   track record. Zero SDK cost.
   - *Depends on:* 3.
   - *Acceptance:* the contemplation block carries the line when scored rows
     exist and suppresses at zero rows; Brier and bin arithmetic is
     table-tested.

### Phase B — evidence-bound self-knowledge

5. [x] **Deterministic evidence binding for capability claims — structured,
   not inline.** `maestro/consciousness.py` (`update_self_model` deterministic
   post-processing; new additive `SelfModel.claim_evidence` map),
   `maestro/self_calibration.py` (citation resolver) — the update prompt
   instructs the model to cite, per new or revised `capabilities_known`
   entry, one compact evidence reference (`<task_id | sha | SENSOR-TAG>`) in
   a separate structured field — never appended inside the entry prose, which
   `detect_restatement` would gate as a near-identical rewrite of the entry's
   old self. Post-processing keys the citation into
   `SelfModel.claim_evidence` by the entry's stable claim key (the
   concept-token fold shipped by 101), so a re-worded claim keeps its
   evidence. The resolver verifies the citation exists *and* points the right
   way — a cited task resolved `success` in task history, a cited SHA present
   in `version_archive.jsonl`, a cited tag in the lexicon tag map — and tags
   unresolved, absent, or wrong-polarity citations rather than rejecting the
   entry. Existence-plus-polarity is groundedness, not entailment (see Phase
   D). `[audit] evidence_bound_claims_enabled`, default-off.
   - *Depends on:* 1.
   - *Acceptance:* resolver tests per citation family including
     wrong-polarity; adding evidence to an existing claim does not trip the
     restatement gate; a re-worded claim above the claim-key fold keeps its
     evidence; an unresolvable citation flags, never crashes; flag off is
     byte-identical.
6. [x] **Audit rule: unbound capability claim.** `maestro/audit_checks.py`
   (registry) — `check_unbound_capability_claim` surfaces entries lacking a
   resolving evidence record (absent, unresolvable, or wrong-polarity),
   bounded per cycle, self-extinguishing when the entry gains evidence or is
   removed.
   - *Depends on:* 5.
   - *Acceptance:* fires on a seeded unbound claim, extinguishes on the bound
     rewrite; the registry summary reflects the new rule.

### Phase C — refutation accountability for the whole registry

7. [x] **Classify the registry by authority; extend accountability to the
   `mechanical_fact` class only.** `maestro/audit_checks.py`,
   `maestro/consciousness.py` — add a curated `RULE_AUTHORITY` map over
   `_registered_audit_checks` (drift-guarded the same way the registry count
   is) with three classes: `mechanical_fact` (git/ledger-proven — the current
   pair and their landing-bind peers), `derived_measure` (aggregates over
   ledgers), and `heuristic` (keyword and threshold shapes). An
   `[audit refuted]` question whose underlying `mechanical_fact` rule
   re-fires on fresh data for `[audit] refutation_rejection_cycles`
   consecutive cycles is mechanically restored to `[audit]` with a
   `refutation-rejected` stamp; `derived_measure` and `heuristic` findings
   stay refutable indefinitely — deterministic does not mean authoritative.
   The rejection count feeds the existing `REALITY-REFUTATION` line.
   `[audit] refutation_accountability_enabled`, default-off;
   `_REALITY_GROUNDED_RULES` is deleted at this flag's cutover, subsumed by
   the class predicate.
   - *Depends on:* none (composes with 088's shipped machinery).
   - *Acceptance:* a seeded refuted question over a re-firing
     `mechanical_fact` rule is restored after N cycles; the same seed over a
     `heuristic` rule stands; with a non-refiring rule the refutation stands;
     the authority map exactly covers the live registry (drift test).

### Phase D — the awareness report and the retirements

8. [x] **`CONSCIOUSNESS_CRITERIA` registry and `maestro consciousness-report`.**
   `maestro/self_calibration.py`, `maestro/cli.py` — a declarative registry
   binding each functional property to its measured reading, each labeled
   with exactly what it measures: *identity availability* (structural — the
   durable self-model reaches every SDK call, pinned by
   `tests/test_identity_prompt_slabs.py`; explicitly **not** limited-capacity
   selection of a fresh moment or receipted broadcast, which print
   `not_implemented` and defer to the workspace successor proposal),
   *perceptual grounding* (evidence-bound claim share plus wrong-polarity
   count), *metacognitive calibration* (measured accuracy, self-vs-measured
   agreement, Brier/reliability, unfalsifiable/pending/unresolved rates),
   *narration→actuation conversion* (`measure_narration_conversion`, labeled
   as the audit-detection → directive/handoff → settlement pipeline it
   actually measures, not general steering). Read-only, offline, `--json`;
   the report header states it measures functional properties only and makes
   no phenomenal claim.
   - *Depends on:* 4, 6.
   - *Acceptance:* renders with empty ledgers (explicit "no data" per row);
     the availability and conversion rows carry their scope labels and the
     workspace rows render `not_implemented`; reaches no SDK, push, or merge
     seam.
9. [ ] **Keep the content-contradiction pair; retire the write-only surprise
   counter.** `maestro/audit_checks.py`, `maestro/consciousness.py`
   (`update_self_model`) — `check_success_claim_vs_rate` and
   `check_stability_claim_vs_failure_streak` are **kept**: citation existence
   is not evidential support, so the two detectors that check claim *content*
   against measured rates retain a job the resolver cannot do (they enter the
   step 7 authority map as `derived_measure`). What does retire:
   `update_self_model` stops incrementing `surprise_count`; novelty
   accounting is now the evidence-bound admission itself. The field survives
   pinned so `ensure_schema()` does not churn.
   - *Depends on:* 5 (graduated), 7.
   - *Acceptance:* both claim rules remain in the registry and the authority
     map; no writer of `surprise_count` remains; the self-model round-trips
     through `ensure_schema()` unchanged.

## Testing Strategy

All tests honour CLAUDE.md testing principles: `mem_fs` for every file
operation, mocked SDK/provider seam, no real network, subprocess, or sleep.

- **`tests/test_self_calibration.py`** — parse/score pure functions per probe
  family; temporal branches (`pending` before maturity, measured at maturity,
  `unresolved` at cutoff, settlement-absence scoring nothing before cutoff);
  unfalsifiable fallback; Brier and reliability-bin arithmetic; citation
  resolver per evidence family (task id, SHA, sensor tag) including
  wrong-polarity; guard branches for missing ledgers and corrupt rows.
- **`tests/test_prompts.py`** — golden tests for the flag-gated
  `PREDICTION_REQUIREMENT` variant, both flag states, including the
  `CONFIDENCE:` requirement.
- **`tests/test_structural_guards.py`** — the verdict-write chokepoint: a
  seeded bypass writer of the reflection-verdict row fails the guard; the live
  tree passes (synthetic-parity pattern from `CHOKEPOINT_GUARDS`).
- **`tests/test_audit_checks.py`** — `check_unbound_capability_claim`
  fire/extinguish; refutation-accountability restore vs. stand branches per
  authority class (`mechanical_fact` restores, `heuristic` stands);
  `RULE_AUTHORITY` drift test against the live registry;
  `measure_prediction_accuracy` `verdict_source` split;
  `format_self_calibration_summary` suppression at zero rows.
- **`tests/test_consciousness.py`** — `claim_evidence` keying: adding
  evidence to an existing claim does not trip the restatement gate; a
  re-worded claim above the claim-key fold keeps its evidence; the additive
  field round-trips through `ensure_schema()`.
- **`tests/test_agent.py`** — maturity-aware bind: oracle verdict wins over a
  contradicting self-grade when parseable and mature, `pending` recorded then
  re-scored at maturity, narrated verdict kept when unparseable;
  `SELF-CALIBRATION` line present in the measured block.
- **`tests/test_cli.py`** — `consciousness-report` offline render, `--json`,
  empty-ledger rows, scope labels and `not_implemented` workspace rows;
  asserts no SDK or host call.
- Retirement steps ship with deletion coverage proving 100% remains and the
  registry summary matches the live rule set.

## Risk Assessment

- **Sublanguage too narrow (primary risk).** If the closed probe vocabulary
  can't express real bets, predictions stay `unfalsifiable` and the oracle
  scores nothing. That rate is itself the tripwire: it is surfaced on the
  `SELF-CALIBRATION` line from day one, and a sustained rate above ~80% over a
  full dream-window with flags on means the vocabulary must grow or the design
  has failed. Rollback is flag-off; the narrated verdict path never leaves the
  code until cutover.
- **Oracle mis-scoring.** A bug in `score_loss_condition` would stamp wrong
  measured verdicts. Mitigation: the self-grade is always recorded alongside,
  so systematic disagreement is visible in the agreement metric before it can
  mislead; probes delegate to existing owners (task history, gate ledger,
  supplied settled set) rather than re-deriving facts.
- **Falsifiability.** If the design is right: the unfalsifiable rate falls
  across successive windows, self-vs-measured agreement becomes visible and
  improves, and rules 1/2-class claim findings decline as claims arrive
  evidence-bound. The design has failed if the unfalsifiable rate stays high
  after a full window, or agreement is ~100% from the start (self-scoring was
  already accurate and the oracle adds only mechanism).
- **Burn-in interplay.** 113/116/120 are in burn-in on adjacent ground. This
  proposal touches only surfaces those proposals explicitly *retain* — the
  predictions ledger, the audit registry, `update_self_model`'s deterministic
  post-processing, the measured-data block — and none of the purpose/stage
  machinery slated for deletion, so it is unaffected by their graduation order.
- **Bets that never mature.** A pending bet is not a scored bet; without
  cutoffs the ledger would fill with immortal `pending` rows. Every probe
  carries a cutoff at which a still-unobservable fact scores `unresolved` —
  excluded from accuracy but surfaced as its own rate, on the same
  `SELF-CALIBRATION` line that reports accuracy, so a growing unresolved
  share reads as a vocabulary-or-window bug, not silence.
- **Prompt budget.** Evidence citations live in the structured
  `claim_evidence` sidecar, not inside `MAX_ITEM_CHARS` entry prose (the
  restatement gate forbids inline markers anyway); the constrained
  `LOSS CONDITION:` and `CONFIDENCE:` lines add tens of characters to the
  priced reflection. No cap change.
- **Trust chain.** Untouched — no approval, sandbox, tool-gating, or
  public-API changes; the new CLI is read-only.
- **Migration.** Minimal: new JSONL keys (`verdict_source`, `self_verdict`,
  `confidence`) are additive and the existing row readers tolerate unknown
  keys; the one `SelfModel` addition (`claim_evidence`) is an additive map
  the dataclass loaders default-normalise on read per the
  schema-normalisation invariant — no rewrite, no destructive step.

## Out of scope

- **Phenomenal consciousness.** Subjective experience is unfalsifiable from
  the outside; this proposal makes no claim to create or detect it, and the
  report's header says so. What is in scope is the measurable functional
  slice.
- **The bounded global workspace.** Limited-capacity moment selection,
  receipted broadcast, an attention schema, and counterfactual causal-reach
  evaluation belong to the successor conscious-access proposal, deliberately
  sequenced *after* this one so its calibration indicator consumes measured
  verdicts rather than the self-graded ledger this proposal retires. The
  report marks those indicators `not_implemented` instead of approximating
  them with the identity slab.
- **Semantic entailment of evidence.** The citation resolver checks existence
  and polarity, never that the evidence *entails* the claim — which is
  exactly why the content-contradiction rule pair survives this proposal.
- **Purpose/stage machinery.** LLM-final stage advancement, the LLM-authored
  hypothesis `confidence`, the `current_purpose_hypothesis` stage gate, and
  the write-only `stage_evidence` field are 113 Phase D/E's to retire; this
  proposal neither reads nor extends them.
- **Identity-slab priming value.** Whether `capabilities_known` /
  `limitations_known` in the identity prompt earn their keep is 113's named
  separate question; evidence binding makes that experiment easier later but
  this proposal does not run it.
- **Generator governance.** What the loop chooses to *do* with better-calibrated
  self-knowledge is owned by 116/120's admission and generation authorities.
- **The journal.** The diary stays as-is: subjective narration is fine in a
  diary; this proposal only stops it being the ledger of record for
  self-knowledge accuracy.
- **Memory-plumbing read-back gaps.** The audit also found
  `Dreamer.get_context_for_prompt` and `Consciousness.load_archived_items`
  written-but-unread in production — a different class (memory routing, not
  self-knowledge accuracy) belonging with 002/072's storage track, not here.
- **Moral-status or welfare questions.** Those are for the human, not the
  codebase.
