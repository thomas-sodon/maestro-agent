# 136: Ground the Plan Generator in the Tree, Not at the Door — One Reality Inventory the Generator Reads Before It Authors, So It Proposes Against What Ships Instead of Its Own Narrative

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | new `maestro/reality_inventory.py`, `maestro/evolution.py` (`propose_plan`, `reflect`, `submit_for_review`, `_read_grounding_tree_text`; `_shipped_cli_surface` relocated to `reality_inventory.read_shipped_cli_surface` over `tree_retrieval.shipped_cli_surface`, re-imported under its old alias), `maestro/agent.py` (`_pre_mint_generation_verdict`, `evolve`), `maestro/plan_grounding.py` (`confirm`), `maestro/audit_checks.py` (`measure_dead_on_arrival_mints`), `maestro/memory.py` (`EvolutionPlan`), `maestro/config.py`, `maestro/config_settings.py` (`_LoopSettings`), `maestro.toml`, `maestro/flag_lifecycle.py`, `maestro/cli.py`, `tests/structural_guards.py`, `tests/test_structural_guards.py`, new `tests/test_reality_inventory.py` |
| **Depends on** | 114 _(burn-in:observing — reuses and relocates its tree-reading `confirm` oracle and `_read_grounding_tree_text` seam from the submission door to the generator)_, 120 _(burn-in:observing — extends its `_pre_mint_generation_verdict` chokepoint and reuses its CLI-surface reader, now `reality_inventory.read_shipped_cli_surface`)_, 116 _(burn-in:observing — whose already-active prompt re-subordination this proposal shows is structurally insufficient)_, 009 _(proposed — owns the complementary over-complexity / scope-estimation axis)_ |
| **Derived from** | live production data via /propose, 2026-09-01; evidence refreshed against the live tree and `data/` on 2026-09-18 |
| **What landed** | Step 1 — `maestro/reality_inventory.py` (`build_reality_inventory`, `collect_inventory_sources`, `load_reality_inventory`, `read_shipped_cli_surface`) and the read-only `maestro reality-inventory [--json]` CLI (`cmd_reality_inventory`), tested in `tests/test_reality_inventory.py`; Step 2 — `audit_checks.classify_door_refusal_reason` and the additive `by_reason` / `content_dead` / `content_fraction` split of `measure_dead_on_arrival_mints`, `audit_checks.measure_rejection_classes`, and the read-only `maestro measure-rejection-classes [--last N] [--json]` CLI (`cmd_measure_rejection_classes`), tested in `tests/test_mint_death_metrics.py`; Step 4a — `reality_inventory.clear_mint` / `MintClearance`, `EvolutionEngine._load_generation_inventory`, the `submit_for_review` first guard recording `[ungrounded-generation:<reason>]` obsolete pre-notify, the optional nullable `problem_ref` / `new_capability_names` `propose_plan` fields, and default-off `[loop] generator_reality_grounding_enabled`, tested in `tests/test_generator_reality_grounding.py`; `_read_own_cli_source` now reads through `tree_retrieval.read_door_text`, and `measure_rejection_classes` gains a `tree_checkable` count via `is_tree_checkable_rejection`; Step 5 — `submit_for_review` skips `_cli_grounding_verdict` when the inventory clearance ran and cleared a declared `cli:` capability (`reality_inventory.declares_cli_capability`), the guard still running for every other plan and flag-off, tested in `tests/test_generator_reality_grounding.py`; Step 4b — `reality_inventory.parse_declared_intent` / `duplicate_intent_directive`, `_REFLECT_INTENT_PROMPT` appended to `reflect` under the flag, and `EvolutionEngine.refuses_reflected_intent` called by `evolve` between `reflect` and the priced `propose_plan` / `propose_plan_chain`, filling an empty `next_task_directive` and logging `[ungrounded-generation:duplicate]`, tested in `tests/test_generator_reality_grounding.py`; Step 6 guard — the `reality_inventory` `ChokepointGuard` in `tests/structural_guards.py` pinning the inventory's sole ownership of its vocabulary, its module purity, the `submit_for_review` notify co-occurring with `clear_mint`, and every `agent.py` priced-propose scope co-occurring with `refuses_reflected_intent` |
| **What is missing** | No behaviour-code remains. The burn-in window runs via the `maestro.production.toml` canary enabling `generator_reality_grounding_enabled` (a `[loop]` line, declared in `PROD_ONLY_CANARIES`), with criteria in the ["Generator reality grounding canary (proposal 136)"](../../self-evolution.md#generator-reality-grounding-canary-proposal-136) subsection of `docs/self-evolution.md`. Observation window: until at least 30 human-decided plans minted after the canary's production merge — the same window as 143's grounded-planning canary, so the two flags' attribution is read over one set of plans — with a `maestro measure-rejection-classes --json` and `maestro measure-dead-on-arrival-mints --json` baseline recorded on deploy day; the tripwires are those in the [Testing Strategy](#testing-strategy) falsification paragraph and the README burn-in bullet. After the window, Step 6's cut-over retires the `_cli_grounding_verdict` dead branch once the Step 2 metric proves Steps 4–5. Step 3 is struck — superseded by 143 step 4 (see [Coordination with 143](#coordination-with-143)). |

## Description

> **Burn-in.** Partially completed — all behaviour-code has landed and runs behind its flag; what remains is the burn-in observation window, then graduating the flag and decommissioning the retired logic. See [README.md](../README.md#status-key).

Every evolution plan Maestro authors is generated from one input: the self-model's
inward narrative plus an advisory prose nudge to "check whether the existing
codebase already provides it." The generator never reads the tree it proposes to
change. So it repeatedly proposes to build capabilities that already ship, on
premises the tree refutes, for problems nothing measures — and the human reviewer
becomes the grounding oracle, hand-writing rejections that all say some variant of
"this already exists / this premise is false against today's tree." Over thirty-three days of production, 84 plans reached human
review and were rejected; in the most recent seventeen-day window — with every
downstream control switched **on** in production — 30 rejections ran **87%
duplicative, 67% already-shipped, 53% false-premise or code misread**, and almost
every rejection is compound: "duplicates X that already ships, on a false claim about
the tree."

Every quality control shipped for this class is either a *plan-vs-plan* comparison,
a *plan-shape* heuristic, a *stated-premise refutation at the submission door*, a
*saturation* control, or a *prompt instruction*. Not one supplies the generator with
the ground truth of what already exists and what is actually broken. The decisive
datum: proposal 116 already improved the prompt — its `INSTRUMENTAL_PROMPT`
re-subordination ("a candidate improvement must name an external-observable
capability; adding a sensor is insufficient") landed as a non-flag-gated edit and is
**active in production right now** — and the rejection classes above are the residue
it leaves. Tweaking the generator's *instructions* was tried and did not work,
because the model cannot avoid re-proposing what ships when it is never shown what
ships. Proposal 114 built a tree-reading grounding oracle but installed it at the
*door*, and by its own Out-of-scope explicitly defers the *generation* side ("this
proposal closes the submission leak… even while the generator still produces one").
Proposal 120 relocated the *saturation* decision from the door to the generator but
left grounding at the door.

This proposal makes one structural change: a single tree-derived **reality
inventory** — the set of capabilities that already ship and the set of problems that
are actually measured — becomes the generator's *input* and the deterministic
authority every mint clears, reusing 114's tree-read oracle and 120's pre-mint
chokepoint rather than adding a rival check. The generator authors against reality
instead of narrative; a plan whose target capability already appears in the shipped
set, or whose cited problem is absent from the measured set, is refused before it
spends a human approval — and, where the intent is declared early, before it spends
the priced `reflect`/`propose_plan` calls. The end state: grounding is relocated
from the door to the source (the same move 120 made for saturation), the door-side
and plan-vs-plan guards keep only their thin backstop role, and the human sees plans
that propose real work against what the tree actually contains.

## Relevant Findings

- `maestro/evolution.py` (`propose_plan`) — the generator's whole input is the
  `reflection` string and `self._consciousness.load_self_model()`, assembled into a
  prompt whose only reuse control is one advisory sentence ("check whether the
  existing codebase, the standard library, or an [existing capability] already
  provides it"). It is prose, not a tree read; nothing verifies the model consulted
  the code, so a plan to add a mechanism that already exists sails through — exactly
  what proposal 114's finding on the same function records.
- `maestro/prompts/defaults.py` (`INSTRUMENTAL_PROMPT`) — proposal 116 Step 2
  amended this constant in place (no flag) so it is injected on every call in
  production: "a candidate improvement must name an external-observable capability…
  adding a sensor / audit rule / measurement helper… is insufficient on its own."
  The production rejection stream proves this instruction, active, does not stop the
  generator proposing already-shipped capabilities — the general instruction cannot
  substitute for the specific inventory.
- `maestro/plan_grounding.py` (`confirm`, `GroundingProbe`, `_read_grounding_tree_text`
  in `maestro/evolution.py`) — proposal 114's tree-reading oracle: `SYMBOL_ABSENT`
  greps the shipped `maestro/**/*.py` corpus for a definition of a named symbol
  (found ⇒ ungrounded-duplicate), `DEFECT_PRESENT` checks a needle exists. It runs
  as `submit_for_review`'s first guard — at the *door*, on a model-supplied
  single-locator probe, behind `[loop] plan_grounding_enabled` (default off in
  `maestro.toml`, **on in the production overlay** since this proposal's first pass).
  114's
  own Out-of-scope defers "reducing the generation of ungrounded plans." This
  proposal reuses `confirm` and the `_read_grounding_tree_text` seam and moves them
  upstream.
- `maestro/agent.py` (`_pre_mint_generation_verdict`) — proposal 120's single
  pre-mint chokepoint that every self-initiated channel (`evolve`,
  `_implement_approved_plans`, the doc-proposal path) clears before a priced propose,
  behind `[loop] pre_mint_generation_gate_enabled` (default off in `maestro.toml`,
  **on in the production overlay**). It decides
  admit/refuse on *backlog saturation and theme concentration only* — a volume
  control. It is the natural slot for the pre-mint grounding arm; grounding was never
  wired into it.
- `maestro/evolution.py` (`_shipped_cli_surface`, `_cli_grounding_verdict`,
  `_evidence_cli_grounding_verdict`) — the existing AST walk that already grounds a
  plan's *CLI* surface against the shipped tree (proposal-120-era): it enumerates
  registered subcommands/flags and refuses a plan proposing CLI tokens that all
  already ship, or citing a `maestro <token>` as evidence when the token is not
  shipped. This is a narrow, capability-specific instance of exactly the inventory
  this proposal generalizes; the reality-inventory owner reuses that reader for its
  CLI slice rather than re-deriving it — step 1 relocated it to `tree_retrieval.shipped_cli_surface`, wrapped by
  `reality_inventory.read_shipped_cli_surface`, which `evolution` imports back under the
  `_shipped_cli_surface` alias so the door guards are unchanged.
- `maestro/evolution.py` (`submit_for_review`, invoking `is_source_task_recited`,
  `is_plan_restatement`, `validate_success_criteria`, plus the sibling guards
  `find_duplicate_pending_plan` in `maestro/memory.py` and `check_plan_scope` in
  `maestro/planner.py`) — the accreted door-guard pile `submit_for_review` clears.
  Every one compares the candidate to *other plans*, its *own shape*, or a *cited
  premise*; none reads the tree the plan claims to modify or checks whether the
  capability it proposes already exists. This is the pile the reality inventory
  subordinates to a backstop.
- `maestro/audit_checks.py` (`measure_dead_on_arrival_mints`,
  `classify_plan_mint_channel`, `_DEAD_ON_ARRIVAL_REASON_RE`) — proposal 120's
  zero-SDK reader counting mints obsoleted at the submission door, by channel. It
  classifies death by *saturation reason*; extending it to split saturation-death
  from *content/grounding-death* is the falsification metric this proposal is
  measured by, with no new sensor family.
- `maestro/cli.py` (`cmd_measure_premise_accuracy`, over `premise_observations.jsonl`)
  — a shipped read: the fraction of accepted tasks whose goal cited only `.py` paths
  that exist on disk. It is the leading indicator this proposal moves; reused as the
  falsification anchor rather than inventing a rival metric.
- `tests/structural_guards.py` (`_GENERATION_AUTHORITY_GUARD`, the merge-truth /
  CI-truth chokepoint guards, 114's `plan_grounding` `ChokepointGuard`) — the house
  registry for "one owner, one door" AST invariants. "reality_inventory is the sole
  owner of the shipped-capability / measured-problem derivation, and every generation
  site consults it" is enforced in the same shape.

## Production Evidence

Read-only passes over the live bind-mount `data/` at `/opt/maestro/data`
on 2026-09-18. The live loop is on HEAD `04ba74dc` (current `main`),
`MAESTRO_ENV=production`. **The flag posture has changed since this proposal was
first written, and the change is the decisive new datum:** the production overlay now
sets `plan_grounding_enabled = true` (114), `pre_mint_generation_gate_enabled = true`
(120), `self_model_external_anchor_enabled = true` (120 Phase C) and
`generation_admission_enabled = true` (116) — every downstream control this proposal
names is **on** — while `self_directed_tasks_enabled` stays `false`. Windows are
stated per datum.

**The landing ratio (life-to-date corpus, `evolution_plans.jsonl`, 640 plans, `created_at` 2026-08-16 → 2026-09-18):**

- `obsolete` **522** (82%), `rejected` **84** (13%), `merged` **19** (3.0%),
  `failed` 11, `blocked` 2, `implemented` 1, `pending_review` 1. Roughly one authored
  plan in thirty-four reaches `main` — the ratio is unmoved since the first pass.
- **97% of the graveyard still dies at the submission door on backlog saturation:**
  508 of the 522 obsolete plans carry a saturation reason. (This volume class is
  proposal 120's domain and is Out of scope here; it is the backdrop against which
  the *content* class below sits.)

**The post-flag window is the falsification test — and it refutes "downstream
grounding is enough" (`created_at` 2026-09-01 → 2026-09-18, 99 plans: 54 obsolete,
30 rejected, 9 failed, 2 merged):**

- 52 of the 54 obsoletions are still saturation deaths at the door.
- The 30 human rejections, multi-tagged by `rejection_reason` keyword, reproduce the
  original distribution with **every** downstream control active: **duplicative /
  re-mint 26 (87%)**, **already-shipped 20 (67%)**, **false-premise / code misread
  16 (53%)**, **over-complex 8 (27%)** (proposal 009's axis), **wrong-seam /
  self-referential 7 (23%)**, **not-needed / zero-measured-problem 3 (10%)**. The
  already-shipped and false-premise shares did not fall; they rose.

  *(These are hand tallies. The step-2 reader's lexical `false_premise` class now
  also absorbs wrong-seam phrasings (`wrong module`, `lives in`, `phantom`,
  `nonexist`), and `already_shipped` gains `already has` / `registered` / `wired`
  and kin, since both became the sub-patterns of 143's one tree-checkable regex
  (#4392) — so read the reader's trend across windows, not against these
  percentages.)*

This is the experiment the first draft could only predict. Proposal 114's door-side
tree read, proposal 120's pre-mint saturation gate and external self-model anchor,
and proposal 116's admission gate plus `INSTRUMENTAL_PROMPT` re-subordination have all
been live for the whole window, and the generator still authors plans against a tree
it has not read. Verbatim, redacted, tight:

  - *"Reject — already done. The plan's premise, 'No pre-spend saturation guard exists
    in evolve() before propose_plan,' is false. evolve() already runs the 'Pre-mint
    generation gate (proposal 120, Phase B)'…"* — the generator re-proposed the very
    gate that was refusing its siblings that same iteration.
  - *"Reject — duplicative re-mint of a class the human already rejected… 'Flip
    pre_mint_generation_gate_enabled on' has been minted ~20 times."*
  - *"Already done. `maestro/plan_grounding.py` already resolves a claimed-new Python
    def name against the tree: `_has_definition` matches anchored `def` / `async def`
    / `class <symbol>` shapes…"* — the generator re-proposed proposal 114's oracle
    while that oracle was switched on.
  - *"Reject — already-done. `maestro pr-owed-act` already runs standalone and already
    grounds its settled set in `settled_verdicts.jsonl`."*
  - *"Reject — the rationale's premise is refuted by the ledger it cites…"*

The pattern is sharper than the original reading: the door-side guards reject the
plan *after* both priced calls have been spent, and the plans they reject are
increasingly re-mints of the grounding machinery itself. A control that runs
downstream of authorship cannot stop the generator from authoring; only an inventory
supplied as *input* can. That is this proposal's single claim, and the window above
is the strongest available evidence for it.

## Prior Point Fixes (whack-a-mole ledger)

Every quality control shipped for the evolution generator sits *downstream* of it and
is *subtractive*: it reads a candidate that already exists and refuses it, comparing
it to other plans, its own shape, a stated premise, or the backlog. None supplies the
generator a tree-derived inventory as its *input*, which is why an ungrounded
generator keeps feeding all of them and the already-shipped / false-premise class
survives every one.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| `pending_plan_submit_block_threshold` (cap pending at N) | Door queue depth | Caps the backlog; the generator authors undiminished |
| `find_duplicate_pending_plan` / `is_plan_restatement` (Jaccard dedup) | Plan-vs-plan text similarity | Compares text to prior text; blind to a mechanism already in the code |
| `is_source_task_recited` (source-task recital guard) | A stranded branch reused under fresh framing | Plan-vs-plan provenance; not a tree read |
| `_cli_grounding_verdict` / `_evidence_cli_grounding_verdict` (`_shipped_cli_surface`) | A plan's *CLI* surface vs the shipped tree | Grounds one capability kind (CLI tokens); every non-CLI capability is ungrounded |
| `check_plan_scope` / `validate_success_criteria` | Multi-file verification language; vacuous criteria | Checks a plan's *shape*, never that its premise is true |
| Proposal 060 (cluster + restatement-chain sweeps) | Prune the pending backlog post-hoc | Cleans the graveyard; does not reduce what enters it |
| Proposal 116 Step 2 (`INSTRUMENTAL_PROMPT` re-subordination) | The generator's *instruction* to name a real capability | **Active in production; rejections persist** — an instruction without the inventory cannot be obeyed |
| Proposal 116 Step 7 (`generation_admission_enabled`) | Door admission on backlog/theme saturation | Volume control; obsoletes after the priced propose, orthogonal to premise truth |
| Proposal 120 (`pre_mint_generation_gate_enabled`) | Relocate the *saturation* decision to pre-mint | **On in production; rejections persist** — grounds nothing, refuses on backlog depth, not on what already ships |
| Proposal 114 (`plan_grounding_enabled`) | Tree-read a plan's *stated* premise at the door | **On in production; rejections persist** — submission-side by design, single-locator, model-supplied; explicitly defers generation |

**Retired / subsumed by this design:** proposal 114's door-side `confirm` is
*relocated* to the generator (its target moves from the model-supplied probe to the
generator's declared capability) and kept in `submit_for_review` only as a thin
backstop for non-pre-gated paths — the same demotion proposal 120 Step 6 applied to
the admission gate. `_cli_grounding_verdict` / `_evidence_cli_grounding_verdict`
become the CLI slice of the one reality inventory rather than a bespoke parallel
check. The plan-vs-plan dedup guards keep only their genuine restatement role, no
longer asked to approximate a groundedness check they structurally cannot perform.
Nothing is deleted before its replacement is proven (Step 6).

## Implementation Plan

Each behavior change is flag-gated with the default preserving today's behavior and is
independently mergeable; an interrupted rollout leaves the loop running as it does now.
The inventory owner and the metric land first so every later step is measured; the
deterministic clearance (step 4) owns the flag and the `output_format` fields. The
positive input (Phase B) is struck: 143 is accepted and in progress, and its step 4
delivers the inventory to the planner as a read tool instead of a prompt appendix.
The demotions and the structural guard land last. Work shared with 143, and who owns
each piece, is fixed in [Coordination with 143](#coordination-with-143).

### Phase A — The one reality-inventory owner, and the metric

1. [x] **Add the reality-inventory owner.** New `maestro/reality_inventory.py` — a
   pure, zero-SDK `build_reality_inventory(sources: InventorySources) -> RealityInventory`
   returning a frozen `(shipped_capabilities: frozenset[str], measured_problems:
   tuple[Problem, ...])`; capability ids carry a kind prefix (`cli:`, `gate:`,
   `audit-rule:`, `sensor-tag:`, `flag:`) and problem ids a source prefix (`audit:`,
   `sensor:`, `rejection:`). The shipped set *composes the registries that already own
   the truth*: `read_shipped_cli_surface` (CLI, wrapping `tree_retrieval.shipped_cli_surface`, relocated from `evolution`), a `GATE_CHAIN` walk (task gates), the
   `audit_checks` rule registry, the sensor-tag set, and the config-flag manifest —
   no new registry. The measured set *composes existing signals*: live unresolved
   audit findings, currently-firing behavioral-sensor tags, and `rejection_history.jsonl`
   category aggregates — no new measurement. The shipped set is kept whole (membership
   checks must stay exact; a prompt renderer bounds it at render time), while each
   problem source is capped at `MAX_PROBLEMS_PER_SOURCE` with one-line summaries; a
   crossed sensor counts only when it moved the unfavourable way (a shrinking
   `OWNED-NET-DELTA` is not a problem). All reads go through one I/O seam,
   `collect_inventory_sources`, which `load_reality_inventory` composes with the pure
   builder; a read-only `maestro reality-inventory [--json]` renders the result.
   - *Depends on:* none.
   - *Acceptance:* a unit test seeds in-memory registries/telemetry with a known
     shipped set and a known measured-problem set and asserts `build_reality_inventory`
     reproduces both with no real I/O.

2. [x] **Split dead-on-arrival mints by reason, and count the rejection classes.**
   `maestro/audit_checks.py` (`measure_dead_on_arrival_mints`), `maestro/cli.py` — extend
   the reader to split saturation-death from *content/grounding-death*, and add a
   zero-SDK reader over `rejection_history.jsonl` / `evolution_plans.jsonl` counting the
   already-shipped / false-premise / not-needed rejection classes (the falsification
   metric). Exposed via CLI with `--json`; composes with the shipped
   `cmd_measure_premise_accuracy`.
   - *Depends on:* none.
   - *Acceptance:* a unit test seeds plans/rejections with known per-class counts and
     asserts the reader reproduces them; the CLI prints them.

### Phase B — Ground the generator's input (the positive half)

> **Superseded by [143](143-grounded-planning.md) step 4 — do not implement.** 143 lets the planner *pull* reads from the tree through a bounded read-only surface instead of receiving this pushed inventory. Phase A's inventory has landed, so 143 step 4 exposes it as the `read_reality_inventory` tool rather than a prompt appendix. Step 3 is kept below, struck, as the record of what was superseded.

3. [x] **(Superseded by 143 step 4 — not implemented) Inject the inventory as the generator's context; require it be cited.**
   `maestro/evolution.py` (`propose_plan`, `reflect`) — behind
   `[loop] generator_reality_grounding_enabled` (registered by step 4), append the
   reality inventory (the shipped-capability set and the measured-problem set) to the
   generator's prompt in place of the advisory "check the codebase" prose, and instruct
   it to fill step 4's `problem_ref` / `new_capability_names` fields from that
   inventory. Flag-off: the prompt is byte-identical to today.
   - *Depends on:* 1, 4.
   - *Acceptance:* with the flag on, `propose_plan`'s prompt carries the inventory;
     with the flag off, the prompt is unchanged.

### Phase C — Relocate grounding to the generator (the deterministic authority)

4. [x] **Clear every mint against the inventory — at the door (4a), then pre-mint
   where intent is declared (4b).** Both arms sit behind
   `[loop] generator_reality_grounding_enabled` (default `false`, registered in
   `EXPERIMENTAL_FLAGS` with a dated `review_by` horizon).

   **4a. [x] Door clearance.** `maestro/evolution.py` (`propose_plan`,
   `_plan_from_spec`, `submit_for_review`), `maestro/reality_inventory.py`
   (`clear_mint`, `_read_own_cli_source`), `maestro/memory.py` (`EvolutionPlan` and
   its known-field filter), `maestro/audit_checks.py` (`_DOOR_REFUSAL_REASON_TABLE`),
   `maestro/config.py`, `maestro/config_settings.py` (`_LoopSettings`), `maestro.toml`,
   `maestro/flag_lifecycle.py` — add two optional, nullable `output_format` fields
   to `propose_plan`: `problem_ref` (the measured-problem id the plan addresses) and
   `new_capability_names` (the names the plan will newly define). They are asked for
   by one short appended fragment (`_REALITY_GROUNDING_FIELDS_PROMPT`), which states
   the id grammar and does not render the inventory. It is appended after 114's
   `_PLAN_GROUNDING_PROMPT` and leaves the MINIMALISM prose alone, because 143 step 4
   owns that prose. Flag-off, prompt and schema are byte-identical to today. Then,
   as `submit_for_review`'s first guard, ahead of 114's probe: a
   `new_capability_names` entry already in the shipped set (or defined in the door
   corpus, through 114's `confirm` `SYMBOL_ABSENT` matcher), or a `problem_ref`
   absent from the measured set, records the plan `obsolete` with an
   `[ungrounded-generation:<reason>]` reason before any human notification. The
   reason is classified as `grounding` by `classify_door_refusal_reason`.
   Open-ended feature work declaring neither field passes untouched (114's
   `grounding=None` escape hatch), and an unreadable inventory fails open. In the
   same change, `reality_inventory._read_own_cli_source` reads `cli.py` through a
   `tree_retrieval` entry point instead of opening the file itself, so the inventory
   satisfies 143's reader-chokepoint guard (143 step 5). Also in this change,
   `measure_rejection_classes` gains a `tree_checkable` count computed through 143's
   `is_tree_checkable_rejection`. That keeps one tree-checkable definition, and the
   three lexical sub-classes stay as a breakdown under it rather than a rival
   definition.
   - *Depends on:* 1.
   - *Acceptance:* with the flag on, `propose_plan`'s schema carries
     `problem_ref` / `new_capability_names`; a plan whose `new_capability_names`
     already ships is recorded `obsolete` pre-notify; a plan citing an unmeasured
     `problem_ref` likewise; a grounded plan proceeds through the existing guards;
     the flag-off path calls no inventory read; `reality_inventory` opens no file
     itself.

   **4b. [x] Pre-mint clearance.** `maestro/agent.py` (`_pre_mint_generation_verdict`),
   `maestro/evolution.py` (`reflect`) — at the pre-mint chokepoint, when `reflect`
   has declared an intended capability, refuse before the priced `propose_plan` call
   if that capability already appears in the shipped set, writing
   `[ungrounded-generation:duplicate]` into an empty directive. Declaring an intent
   changes `reflect`'s output contract, and 143 step 4 rewrites `reflect`'s
   completion (a tool-loop-safe text variant). 4b therefore lands after 143 step 4
   and extends that variant rather than forking `reflect`'s completion.
   - *Depends on:* 4a, 143 step 4.
   - *Acceptance:* an already-shipped declared intent is refused before any
     `propose_plan` query (mocked usage recorder); `reflect` without a declared
     intent proceeds unchanged; flag-off parity.

5. [x] **Demote `_cli_grounding_verdict` to a backstop.** `maestro/evolution.py`
   (`submit_for_review`, `_cli_grounding_verdict`) — when
   `generator_reality_grounding_enabled` is on and the inventory clearance ran (the plan
   declared `new_capability_names`), its `cli:` duplicate arm is authoritative for the
   "plan *adds* already-shipped CLI surface" check. `_cli_grounding_verdict` then runs
   only for plans that declared nothing or reached submission by another path
   (external or user resubmission). Its `obsolete` reason is unchanged, and nothing
   changes with the flag off. The other door guards are 143's to retire, not this
   step's: `_evidence_cli_grounding_verdict`, `_cited_cli_tokens`, the #4109
   exemption, and 114's probe (demoted on `grounded_reads`). See 143 step 8.
   - *Depends on:* 4a.
   - *Acceptance:* with the flag on, a self-initiated plan declaring an
     already-shipped `cli:` name is refused by the inventory clearance without
     `_cli_grounding_verdict` firing; a plan declaring nothing still hits it.

### Phase D — Enforce and retire

6. [ ] **Structural guard — the inventory is the sole owner and every mint clears it.**
   `tests/structural_guards.py` (registry, run by `tests/test_structural_guards.py`) — an AST guard asserting `reality_inventory` is the
   only module deriving the shipped-capability / measured-problem sets (no rival ad-hoc
   registry walk at a generation site), and that on the
   `generator_reality_grounding_enabled` branch each priced `propose_plan` call site and
   the `submit_for_review` notify are dominated by an inventory clearance. Same
   synthetic-parity pattern as the merge-truth / 114 / 120 guards; bump
   `_MAX_STRUCTURAL_GUARDS`. This derivation-owner guard nests inside 143's
   reader-chokepoint guard (143 step 5): `reality_inventory` derives the sets,
   `tree_retrieval` performs the reads, and neither guard duplicates the other. Once
   Steps 4–5 are proven by the Step 2 metric, retire the `_cli_grounding_verdict`
   dead branch in a follow-up cut-over PR.
   - *Depends on:* 4a, 4b, 5.
   - *Acceptance:* the guard fails if a future edit adds an un-cleared priced
     `propose_plan` site or a second module re-derives the inventory, and passes on the
     Step 4–5 tree.

## Testing Strategy

All unit tests use the `mem_fs` fixture with mocked SDK/provider, VCS, notifier, and
tree-reader seams — no real network, subprocess, `sleep`, or filesystem — per CLAUDE.md
testing principles.

- **Step 1 (`tests/test_reality_inventory.py`):** seed in-memory CLI/gate/audit/flag
  registries and telemetry; assert `build_reality_inventory` reproduces the known
  shipped and measured sets; assert no real I/O (the reader seam is a stub).
- **Step 2 (`tests/test_audit_checks.py`, `tests/test_cli.py`):** a seeded
  `evolution_plans.jsonl` / `rejection_history.jsonl` with known saturation-death vs
  content-death and known rejection-class counts; assert the split reader and the
  `--json` CLI output.
- **Step 4a (`tests/test_generator_reality_grounding.py`, `tests/test_reality_inventory.py`,
  `tests/test_memory.py`, `tests/test_config.py`):** flag-on, the schema carries
  `problem_ref` / `new_capability_names`; flag-off, prompt and schema are byte-identical
  to today; the flag is registered with a `review_by` horizon; the door arm records an
  already-shipped `new_capability_names` plan and an unmeasured-`problem_ref` plan
  `obsolete` pre-notify (notifier mock never called); an open-ended plan with neither
  field, an unreadable inventory, and the flag-off path reach the existing guards
  unchanged.
- **Step 4b (`tests/test_agent.py`):** the pre-mint arm refuses an already-shipped
  declared intent before any `propose_plan` query (mocked usage recorder).
- **Step 5 (`tests/test_evolution.py`):** flag-on, a self-initiated plan declaring an
  already-shipped `cli:` name is refused by the inventory clearance with
  `_cli_grounding_verdict` not firing; a plan declaring nothing hits it; flag-off parity.
- **Step 6 (`tests/structural_guards.py` via `tests/test_structural_guards.py`):** a synthetic fault — an un-cleared
  priced `propose_plan` site, and a second module re-deriving the inventory — is
  flagged, and the live tree passes.

The deterministic enforcement anchors are Step 6's structural guard and Step 2's
metric. The proposal is falsified if, after Steps 4–5 activate, the already-shipped /
false-premise / not-needed rejection classes and the content-death share of
`measure_dead_on_arrival_mints` do not fall while `cmd_measure_premise_accuracy` holds
or rises — i.e. if grounding the generator's input does not reduce plans that propose
what already ships.

## Risk Assessment

- **False-refusing a legitimate plan.** The inventory clearance could refuse a genuinely
  novel plan whose capability name collides with a shipped symbol, or whose real problem
  is not yet in the measured set. Mitigations: the shipped-set arm is a literal
  definition match (114's `SYMBOL_ABSENT` semantics, no fuzzy matching); open-ended work
  declaring neither field passes untouched (114's escape hatch); every refusal writes an
  auditable `[ungrounded-generation:*]` reason and a redirect, never a silent drop; the
  whole layer is behind a default-off flag and reverts by flag flip. The Step 2 metric
  watches the refusal rate before any retirement.
- **The measured-problem set is fuzzier than the shipped set.** "Is this a real problem?"
  is softer than "does this symbol exist?". Mitigation: the `problem_ref`-absent arm keys
  on the *composition of existing signals* (audit findings, firing sensors, rejection
  aggregates), not a new judgment. The planner cannot see the measured ids until 143
  step 4 ships `read_reality_inventory`, because Phase B is struck. So
  `generator_reality_grounding_enabled` is not canaried in production before that
  tool is available, or the arm would refuse every guessed id. If the arm proves noisy
  even then, it can be dropped while the deterministic shipped-set arm carries the
  hard refusal. The two arms are independently valuable and independently revertible.
- **Re-introducing an instrument the loop loves to build.** A "reality inventory" is
  itself machinery, and the loop's failure mode is building instruments. Mitigation: it
  *reads* registries that already own the truth (`_shipped_cli_surface`, `GATE_CHAIN`,
  the audit registry) rather than adding a new one; it is a single AST-enforced owner,
  not a new sensor family; and it is subordinate to landed capability by construction —
  its whole job is to keep the generator from proposing more instruments.
- **Trust chain.** Untouched — no approval, sandbox, tool-gating, or public-API surface
  changes; the change only *withholds* a priced generation call or a human notification
  and never approves, merges, or writes code. The human approval gate for any plan that
  reaches review is unchanged.
- **Rollback posture.** All behavior is behind `[loop]
  generator_reality_grounding_enabled`, defaulting to today's behavior; the Step 6
  retirement is a normal revert. No state-file schema change beyond the additive
  `problem_ref` / `new_capability_names` plan fields, handled by the existing
  `ensure_schema()` fingerprint path with `None` defaults for legacy rows.

## Coordination with 143

136 and [143](143-grounded-planning.md) are implemented concurrently on separate
branches. Both docs carry this table, and each row has exactly one owner.

| Shared concern | Owner | The other proposal |
|---|---|---|
| Inventory reaching the planner | 143 step 4 (`read_reality_inventory` tool on the `tree` server, injected by the caller because `reality_inventory` imports `tree_retrieval`) | 136 step 3 struck; 136 does not render the inventory into any prompt |
| `propose_plan` prompt prose | 143 step 4 (MINIMALISM and grounding-probe prose) | 136 step 4a appends only `_REALITY_GROUNDING_FIELDS_PROMPT`, after `_PLAN_GROUNDING_PROMPT` |
| `reflect`'s completion and output contract | 143 step 4 (tool-loop-safe text completion) | 136 step 4b extends it after 143 step 4 lands |
| Tree reads for grounding | 143 (`tree_retrieval`; reader-chokepoint guard in step 5) | 136 step 4a routes `_read_own_cli_source` through `tree_retrieval`; 136 step 6's derivation guard nests inside 143's |
| CLI-surface AST walk | 143 (`tree_retrieval.shipped_cli_surface`) | 136 wraps it (`read_shipped_cli_surface`); no second walk |
| Door-refusal split by tag | 143 step 5 (`measure_planning_grounding` counts `[ungrounded:*]` read/unread/untagged, `[ungrounded-generation:*]` and the CLI reasons separately) | 136 mints `[ungrounded-generation:<reason>]` and files it under `grounding` in `classify_door_refusal_reason`; it does not edit 143's reader |
| Rejection-reason classification | 143 (`is_tree_checkable_rejection` is the one tree-checkable definition) | 136's `measure_rejection_classes` keeps its three sub-class proxies as a breakdown and adds a `tree_checkable` count via `is_tree_checkable_rejection` in step 4a, rather than widening its own patterns into a second definition |
| Retiring door guards | 143 step 8: `_evidence_cli_grounding_verdict`, `_cited_cli_tokens`, the #4109 exemption, 114's probe demotion | 136 step 5: `_cli_grounding_verdict` only |
| Shared edit sites | — | Both add a `[loop]` flag to `config.py`, `config_settings._LoopSettings`, `maestro.toml` and `EXPERIMENTAL_FLAGS` (whichever merges second updates the flag-count sentence above it), and both add nullable `EvolutionPlan` fields. These are textual merge points only, with no shared semantics. |
| Metric attribution | 143 step 7 | When both flags are on in one window, a fall in tree-checkable rejections that comes with a matching rise in `[ungrounded-generation:*]` refusals is credited to 136 |

## Out of scope

- **The backlog-saturation obsoletion volume** (508 of 522 obsolete plans
  life-to-date; 52 of 54 in the post-flag window). That is the *volume* of dead mints,
  owned by proposal 120's pre-mint saturation gate, which is now on in production and
  has not moved this share — completing that work stays proposal 120's and proposal
  117's rollout-cadence domain. This proposal reduces the *content* reason a mint is
  dead-on-arrival, not the saturation reason, and does not re-tune 120's thresholds.
- **Over-complexity / scope proportionality** (8 of the window's 30 rejections
  "over-complex"). A
  plan can be perfectly grounded and still be a disproportionate seven-file surface for a
  small problem; that axis is proposal 009's scope-estimation domain, complementary and
  independently mergeable.
- **Duplicative-of-a-pending-sibling** (the Jaccard-leak subset). The reality inventory
  grounds a plan against the *tree*, not against the pending backlog; sibling dedup stays
  the restatement guards' and proposal 060's sweeps' job, demoted here only for the
  tree-duplicate sub-job they structurally cannot do.
- **The self-referential *framing* of the seed** (the 9 wrong-seam rejections). Proposal
  116's `INSTRUMENTAL_PROMPT` and proposal 120 Phase C's self-model external anchor own
  the subtractive seed filter; this proposal supplies the positive inventory that
  complements it, and does not re-tune either prompt.
- **Curating the accumulated obsolete-plan graveyard or the inward self-model content
  already on disk.** Operator data actions; the design stops the generator authoring
  ungrounded plans, it does not retro-clean existing rows.
