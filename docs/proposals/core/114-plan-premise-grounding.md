# 114: Plan-Premise Grounding — One Tree-Reading Oracle Every Plan Clears Before It Spends a Human Approval

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | burn-in:observing |
| **Affects** | new `maestro/plan_grounding.py`, `maestro/evolution.py` (`propose_plan`, `submit_for_review`), `maestro/memory.py` (`EvolutionPlan`), `maestro/agent.py` (`contemplate`), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `tests/test_structural_guards.py`, new `tests/test_plan_grounding.py` |
| **Depends on** | 112 _(shared "name a readable observable, re-read it, veto on mismatch" principle — see Description; independently mergeable)_ |
| **Derived from** | live production data via /propose, 2026-08-11 |

## Description

> Status advanced to `burn-in:observing` on 2026-09-14: all behaviour-code landed
> (2026-09-01) and the observation window is now running — a
> `maestro.production.toml` canary enables `[loop] plan_grounding_enabled` in
> `MAESTRO_ENV=production` only (declared as an `EnvOverlayCanary` in
> `maestro/flag_lifecycle.py`), so `submit_for_review`'s grounding guard runs in
> production while every other environment stays default-off byte-identical. The
> observation window and graduation criteria live in the "Plan-premise grounding
> canary (proposal 114)" subsection of [self-evolution.md](../../self-evolution.md).
> See the [Status Key](../README.md#status-key) for the substate criteria and the
> [burn-in section](../README.md#burn-in--pending-graduation--decommission) for
> the remaining work.

Every evolution plan the running instance has authored is grounded in what the
model *remembers or believes* about its own code, never in a fresh reading of
the tree the plan names — and no stage between contemplation and the human
approval prompt ever confronts the two. The result is a pipeline that surfaces
plans premised on facts the codebase refutes: a lint error that `ruff` does not
report, a misspelling `codespell` does not find, an invariant that does not
hold, and — most often — a gate, sensor, or actuator the plan proposes to build
that **already exists and is live**. The human reviewer is the sole grounding
oracle, re-running a `grep`, a `codespell`, a `ruff`, or a read of `GATE_CHAIN`
by hand on every submission and rejecting when the premise is false. Sixteen
plans authored, zero merged, and at least six rejected for a premise a
deterministic read would have refuted at zero cost (see Production Evidence).

The structural root is that the three plan-authoring stages —
`contemplate()` → `next_task_directive` → `propose_plan()` →
`submit_for_review()` — are all fed by the self-model's inward narrative, and
every submission-time guard that already exists (title dedup, rejection
cooldown, the Jaccard duplicate-pending guard, the cluster-theme guard)
compares a plan against *other plans* or its *own shape*. **Not one of them
reads the tree the plan claims to modify.** The instance has itself diagnosed
this exactly — its live `self_model.json` carries the open question
`premise-check` ("*before proposing, confirm on disk the asserted
invariant/defect exists at its firing-site contract and is not already
present*"), the open question `designed-not-run` ("*my rejections come from not
executing the pre-submit grep I already designed — can the check be forced
inline*"), and the limitation `premise-fabrication`. It knows the fix and has
no forcing function that makes the read happen.

This proposal installs that forcing function as one owner behind one chokepoint,
the same shape as the merge-truth oracle (087) and the frame↔discharge owner
(112): a new `maestro/plan_grounding.py` is the sole authority on "*is this
plan's premise true against the tree?*". A plan that commits to fixing a
concrete defect or introducing a named mechanism must carry a `GroundingProbe`
— a structured `(kind, locator, expectation)` drawn from a small closed
vocabulary, each a deterministic, zero-SDK-cost tree read. `submit_for_review`
runs the probe as its **first** guard, before any human notification: a probe
the tree refutes records the plan `obsolete` with an `[ungrounded:<kind>]`
reason and never surfaces it — exactly the rejection the human writes today,
produced before a scarce approval is spent. Genuinely open-ended feature plans
declare `grounding=None` and pass through untouched, so real work is never
false-rejected.

The end state: a plan's premise is re-read against the live tree at the moment
it would cost a human an approval; the human sees only plans whose factual
premise survived that read; and the growing pile of plan-*shape* filters stops
being asked to approximate a groundedness check they structurally cannot
perform. The change is gated behind `[loop] plan_grounding_enabled`
(default-off, matching current behavior) and every step is independently
mergeable.

## Relevant Findings

- `maestro/evolution.py` (`submit_for_review`) — the sole plan→human chokepoint.
  Its four guards are title-exact dedup (vs active plans), the
  `_REJECTION_COOLDOWN_DAYS` cooldown (vs rejected plans),
  `find_duplicate_pending_plan` (content-word Jaccard vs the pending/live
  corpus), and the cluster-theme guard. Every one compares the candidate to
  *other plans*; none reads the code the plan names. This is the natural slot
  for a grounding guard, ahead of the human notification.
- `maestro/evolution.py` (`validate_success_criteria`) — filters acceptance
  criteria for *vacuousness* (length, filler ratio, generic-noun-only, a
  set-level verifiability marker). It checks that a criterion is *shaped* like a
  testable claim, never that the claim is *true* against the tree. A criterion
  such as "the token 'falsey' no longer appears in `pr_facts.py`" passes shape
  validation while its premise (that 'falsey' appears at all) goes unread.
- `maestro/evolution.py` (`propose_plan`) — its prompt asks the model to
  "propose a concrete evolution plan" and extract criteria, with a minimalism /
  reuse nudge ("check whether the existing codebase … already provides it").
  The nudge is advisory prose; nothing verifies the model actually grepped for
  the mechanism it proposes, so a plan to add a gate that already exists sails
  through.
- `maestro/consciousness.py` (`detect_restatement`) and `maestro/memory.py`
  (`is_plan_restatement`, `find_duplicate_pending_plan`) — the duplicate-
  detection machinery keys on *content-word similarity between plan texts*. It
  cannot see that a proposed mechanism duplicates one already in the code,
  because it never reads the code — precisely how "`investigation_observable_gate`
  already exists and is live" reached the human. `SYMBOL_ABSENT` is the owner of
  that sub-job the text-similarity guards structurally cannot do.
- `maestro/task_gates.py` (`investigation_observable_gate`, from PR 3429) —
  proposal 112's live choice-phase actuator: it forces an investigation-framed
  *task* to "name which existing observable signal you will read." This proposal
  is the same principle one layer up — a plan must name the tree-state its
  premise reads, and submission re-reads it — so the two share the
  "name-a-readable-observable, re-read, veto on mismatch" shape rather than
  inventing a rival one.
- `maestro/agent.py` (`contemplate`, the `## Measured Behavioral Data` block) —
  the zero-SDK sensor-composition point where an `UNGROUNDED-RATE` line can
  surface how often a plan's premise failed its tree read, tying the design to a
  falsifiable production metric without adding an LLM call.
- `tests/test_structural_guards.py` — the AST-enforcement house for
  "one owner, one door" invariants (the merge-truth and CI-truth chokepoints
  live here). The "grounding is the sole tree-reading owner" and "notify only
  after `confirm`" rules are enforced the same way.
- `maestro/memory.py` (`EvolutionPlan`) — already a wide dataclass persisted to
  `evolution_plans.jsonl`; adding an optional `grounding` field is a
  fingerprint-migrated schema change of the kind `ensure_schema()` already
  handles, with `None` as the byte-compatible default for existing rows.

## Production Evidence

Live data at the `maestro-loop` bind-mount `data/` directory. Window: the
instance's entire life to date — **16 evolution plans** authored 2026-08-11
between 20:26 and 22:18 UTC, an `awakening`-stage instance with `evolution_count
= 0` and an empty `version_archive.jsonl`. Small absolute N, but a class
incidence near 100% of all rejections.

- `evolution_plans.jsonl` (16 records) — status distribution
  `rejected: 7, obsolete: 5, pending_review: 3, implemented: 1`; **zero merged
  VersionRecords**. Of the 7 rejected, at least **6 fail on a premise a
  deterministic tree read refutes**, splitting into two subclasses:
  - *Fabricated defect* — "Fix RUF036 type-union ordering in
    `test_doc_proposals.py`" → rejection: "*This is a hallucinated error*";
    "Land a genuine doc spelling correction" → "*the plan's core premise is
    false: `docs/self-evolution.md` contains no genuine dictionary-word
    misspelling to correct*"; "Pin the task_queue_max cap invariant" →
    "*false premise … No such invariant exists*".
  - *Duplicate live machinery* — "ObservableReadFraming task-gate" →
    "*this actuator already exists and is live … PR 3429 added
    `investigation_observable_gate` … enabled by default*"; "Read-streak →
    mutation-nudge TaskGate" → "*duplicative. This gate already exists in
    `GATE_CHAIN`, three times over*"; "WIP-primacy CI-pending hold" →
    "*the premise is already falsified … a default-on gate
    `_gate_pr_remediation_ci_pending_hold` already owns this state*".
- `rejection_history.jsonl` (7 rows) — categories `infrastructure: 4,
  capability: 2, awareness: 1`; the per-row `rejection_category` values include
  `duplicate`, `not_needed`, and `wrong_approach` — the human-authored labels
  this design drives toward zero.
- `self_model.json` — the instance's own diagnosis, verbatim: open question
  `premise-check` ("*before proposing, confirm on disk the asserted
  invariant/defect exists at its firing-site contract and is not already
  present*"); open question `designed-not-run` ("*my rejections come from not
  executing the pre-submit grep I already designed — can the check be forced
  inline*"); limitation `premise-fabrication` ("*twice rejected for a false
  premise (phantom typo, phantom prune-to-bound cap)*"). The fix is known; the
  forcing function is missing.

## Prior Point Fixes (whack-a-mole ledger)

Every prior attempt to raise plan quality filters a plan against *other plans*
or its *own shape*; none reads the tree the plan names, which is why the
premise-falsity class survives all of them.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| `3a059707` (`check_plan_scope`) | Rejects multi-file plans lacking per-file verification language | Checks the plan mentions verification; never runs it against the tree |
| `3f42bbab` (`detect_restatement`) | Blocks self-model items rewording existing ones (Jaccard) | Compares *text* to prior *text*; blind to a mechanism already in the code |
| `69e39a43` (`validate_success_criteria`) | Drops vacuous acceptance criteria (shape heuristics) | Validates a criterion is *shaped* testable, never that it is *true* |
| Proposal 060 (`curate_pending_plans`) | Prunes near-restated / clustered pending plans | Plan-vs-plan dedup; a first-of-its-kind false premise is not a restatement |
| Proposal 100 (`live_work_corpus`) | Compares candidate against the whole live-work corpus | Still text-similarity across plans; the "backlog saturated" obsoletes it produced are orthogonal to premise truth |
| Proposal 112 (`frame_discharge`) | Caps a construction-discharged investigation task at `partial` | Governs *task* frame honesty at record time, not a *plan's* premise at submit time — a frame-honest plan can still be a hallucination |

**Retired / subsumed by this design:** none is deleted — there is no
tree-grounding today to remove. `SYMBOL_ABSENT` (Step 1) *subsumes the
duplicate-mechanism sub-job* that `detect_restatement` and
`find_duplicate_pending_plan` structurally cannot perform, so those guards keep
only their genuine plan-vs-plan restatement role and are no longer the last
line against a duplicate-machinery premise.

## Implementation Plan

1. [x] **Add the `plan_grounding` owner and its closed probe vocabulary.**
   New `maestro/plan_grounding.py` — a `GroundingProbe` dataclass
   `(kind, locator, expectation)` with two pure text kinds and a
   `confirm(probe, read_text) -> GroundingVerdict` that returns
   `grounded` / `ungrounded(kind)`: `SYMBOL_ABSENT(name)` (grep a supplied
   `maestro/**/*.py` corpus for a `def`/`class`/assignment/`GATE_CHAIN`
   registration of `name`; found ⇒ ungrounded-duplicate) and
   `DEFECT_PRESENT(path, needle)` (the file text must contain `needle`; absent
   ⇒ ungrounded-no-such-defect). Pure functions over injected text, no I/O.
   - *Depends on:* none.
   - *Acceptance:* `confirm` returns `ungrounded` for a `SYMBOL_ABSENT` whose
     name is defined in the corpus and for a `DEFECT_PRESENT` whose needle is
     absent, and `grounded` otherwise — proven over in-memory strings.
2. [x] **Persist the probe on the plan, migration-safe.**
   `maestro/memory.py` (`EvolutionPlan`) — add an optional
   `grounding: GroundingProbe | None = None` field; extend the schema
   fingerprint so `ensure_schema()` rewrites existing rows with `None`.
   - *Depends on:* 1.
   - *Acceptance:* an `EvolutionPlan` round-trips through
     `evolution_plans.jsonl` with and without a `grounding` probe, and a
     legacy row lacking the field loads as `grounding=None`.
3. [x] **Emit the probe at propose time.** `maestro/evolution.py`
   (`propose_plan`) — behind `[loop] plan_grounding_enabled`, instruct the model
   to attach a `GroundingProbe` for any plan premised on a concrete defect or a
   named new mechanism, and leave `grounding=None` for open-ended feature work.
   Flag added to `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`
   (default `false`).
   - *Depends on:* 2.
   - *Acceptance:* with the flag on, a proposed defect/mechanism plan carries a
     non-null `grounding`; with the flag off, `propose_plan` output is
     byte-identical to today.
4. [x] **Confirm the probe as `submit_for_review`'s first guard.**
   `maestro/evolution.py` (`submit_for_review`) — before every existing guard
   and any human notification, when `plan_grounding_enabled` and
   `plan.grounding is not None`, read the located tree state and call
   `plan_grounding.confirm`; an `ungrounded` verdict records the plan `obsolete`
   with reason `[ungrounded:<kind>]`, logs a `gate_activations.jsonl` row, and
   returns without notifying. `grounding=None` and the flag-off path are
   untouched.
   - *Depends on:* 3.
   - *Acceptance:* a plan whose `SYMBOL_ABSENT` name exists in the tree is
     recorded `obsolete` with an `[ungrounded:duplicate]` reason and the human
     notifier is never called; a grounded plan proceeds through the existing
     guards unchanged.
5. [x] **AST-enforce the sole-owner and notify-after-confirm invariants.**
   `tests/test_structural_guards.py` — assert `plan_grounding` is the only
   module defining the grounding vocabulary (no rival ad-hoc tree read at
   another submission site) and that in `submit_for_review` the human-notify
   call is dominated by a `plan_grounding.confirm` call on the
   `plan_grounding_enabled` branch. Same house pattern as the merge-truth /
   CI-truth chokepoint guards.
   - *Depends on:* 4.
   - *Acceptance:* the guard fails if a second module greps the tree for
     grounding, or if a notify path bypasses `confirm`.
6. [x] **Add the `LINT_PRESENT` probe behind an injectable reader, and surface
   the `UNGROUNDED-RATE` sensor.** `maestro/plan_grounding.py` +
   `maestro/agent.py` (`contemplate`) — a third kind `LINT_PRESENT(rule, path)`
   whose current-finding read goes through a `TreeReader` protocol (mocked in
   unit tests, wired to a bounded `ruff --select <rule> <path>` in production),
   closing the "hallucinated RUF036 error" case; and a zero-SDK
   `UNGROUNDED-RATE` line in the measured-data block reading
   `gate_activations.jsonl` ungrounded firings vs plans surfaced.
   - *Depends on:* 4.
   - *Acceptance:* `LINT_PRESENT` returns `ungrounded` when the injected reader
     reports no finding for `rule` on `path`; the contemplation block renders one
     `UNGROUNDED-RATE` line from in-memory activation rows.

## What landed

- **Step 1 — the `plan_grounding` owner and closed probe vocabulary.**
  `maestro/plan_grounding.py` defines the `GroundingKind` /
  `GroundingCategory` closed `Literal` vocabularies, the frozen
  `GroundingProbe` `(kind, locator, expectation)` and `GroundingVerdict`
  `(grounded, category, kind)` dataclasses, the definition-shaped
  `_has_definition` matcher, and the pure `confirm(probe, read_text)` deciding
  `SYMBOL_ABSENT` (definition found ⇒ `ungrounded("duplicate")`) and
  `DEFECT_PRESENT` (needle absent ⇒ `ungrounded("no-such-defect")`), with an
  empty needle failing safe. Pure, zero-SDK, no I/O. Tests in
  `tests/test_plan_grounding.py`.
- **Step 2 — the probe persisted on the plan, migration-safe.**
  `maestro/memory.py` adds the optional `EvolutionPlan.grounding:
  GroundingProbe | None = None` field (excluded from
  `compute_plan_content_hash`, like `source_task_id`), and `_parse_plan_line`
  rehydrates a stored `grounding` dict into a `GroundingProbe`; a legacy row
  lacking the field loads as `None`. Round-trip tests in
  `tests/test_memory_plans.py`.
- **Step 3 — flag-gated probe emission at propose time.** `maestro/config.py`,
  `maestro/config_settings.py`, and `maestro.toml` add the default-off `[loop]
  plan_grounding_enabled` flag (`maestro/flag_lifecycle.py` registers it with a
  `plan_grounding` gate trace). With the flag on, `propose_plan` appends the
  `_PLAN_GROUNDING_PROMPT` instruction and a nullable `grounding` schema
  property, and `_grounding_from_spec` parses a well-formed probe onto the plan;
  with the flag off the prompt and `output_format` are byte-identical to today.
  Tests in `tests/test_evolution.py` and `tests/test_config.py`.
- **Step 4 — the submission-time confirm guard.** `maestro/evolution.py`
  (`submit_for_review`) runs the grounding probe as its first guard, ahead of
  every dedup/cooldown/CLI-grounding/backlog check and any human notification:
  the workspace-confined, injectable `_read_grounding_tree_text` seam reads the
  located tree state and `plan_grounding.confirm` decides. A refuted premise
  records the plan `obsolete` with an `[ungrounded:<category>]` reason, stamps a
  `plan_grounding` gate-activation row (the flag-selected `GateActivationBackend`
  wired in `maestro/cli.py`), and returns without notifying; a grounded verdict,
  a probe-less plan, or the flag-off path falls through unchanged.
- **Step 5 — the sole-owner + notify-after-confirm structural guards.**
  `tests/structural_guards.py` adds the `plan_grounding` `ChokepointGuard`:
  `plan_grounding.py` is the AST-enforced sole owner of the
  `GroundingProbe`/`GroundingVerdict`/`confirm` vocabulary, and a
  `notify_dominated_by_grounding_confirm` co-occurrence requires every
  `evolution.py` scope that calls `notify_plan_for_review` to also call
  `plan_grounding.confirm` (`promote_slice` exempt — it re-opens an
  already-vetted chain slice, reusing only the notify tail). `_MAX_STRUCTURAL_GUARDS`
  is bumped 19→20.
- **Step 6 (sensor half) — the `UNGROUNDED-RATE` sensor.** `maestro/audit_checks.py`
  adds `measure_ungrounded_premise_rate` (denominator: the probe-carrying plans in
  the recent window; numerator: those additionally recorded `obsolete` with an
  `[ungrounded:<category>]` reason, plus a per-category breakdown) and the
  self-suppressing `format_ungrounded_rate_summary` line. `maestro/agent.py`
  (`contemplate`) appends that line to the `## Measured Behavioral Data` block over
  the same recent window as the dead-on-arrival sensor, reusing the already-read
  `all_plans` corpus at zero SDK cost. Tests in `tests/test_audit_checks.py` and
  `tests/test_agent_contemplate.py`.
- **Step 6 (probe half) — the `LINT_PRESENT` probe.** `maestro/plan_grounding.py`
  extends the closed vocabulary with `LINT_PRESENT` / `no-such-lint` and the pure
  `confirm` branch (non-empty injected ruff findings ⇒ grounded; empty ⇒ ruff ran
  clean ⇒ the claimed lint error is hallucinated). `maestro/evolution.py` adds the
  injectable `_run_lint_probe` reader — a bounded, workspace-confined
  `python -m ruff check --select <rule> -- <path>` whose exit code maps to that
  text (clean → `""`, a reported rule → the findings text, a ruff error or timeout
  → `None` fail-open) — routes a `LINT_PRESENT` probe's `_read_grounding_tree_text`
  branch through it (an unconfinable or unrunnable read fails **open** to `None`,
  which `submit_for_review`'s guard skips), and requires a well-shaped ruff rule
  code (`_RUFF_RULE_RE`) in `_grounding_from_spec`. Tests in
  `tests/test_evolution.py` and `tests/test_plan_grounding.py`.

## What is missing

- Nothing — all six steps have landed. Step 6's `LINT_PRESENT` probe, the last
  outstanding item, is built behind the injectable `_run_lint_probe` reader (see
  What landed); the `UNGROUNDED-RATE` sensor half landed earlier.

## Testing Strategy

New `tests/test_plan_grounding.py`, all `mem_fs`-backed with no real I/O,
network, sleep, or subprocess:

- **`confirm` truth table (Step 1):** `SYMBOL_ABSENT` over an in-memory corpus
  string — defined name ⇒ ungrounded, absent name ⇒ grounded; `DEFECT_PRESENT`
  over an in-memory file body — needle present ⇒ grounded, needle absent ⇒
  ungrounded. Reproduce each production manifestation as a fixture (the
  `investigation_observable_gate` duplicate, the phantom-misspelling absence).
- **Schema round-trip (Step 2):** an `EvolutionPlan` with and without
  `grounding` survives save/load via `mem_fs`; a legacy row without the field
  loads as `None`.
- **Propose-time flag parity (Step 3):** with the flag off, `propose_plan`'s
  produced plan has `grounding=None` and its prompt is unchanged; with it on, a
  defect-framed plan carries a probe. SDK mocked.
- **Submission guard (Step 4):** a plan whose `SYMBOL_ABSENT` name exists is
  recorded `obsolete` with `[ungrounded:duplicate]` and the notifier mock is
  never called; a grounded plan reaches the existing dedup guards; the
  flag-off/`grounding=None` paths call no tree read. Assert both guard and
  happy-path branches.
- **Structural guard (Step 5):** in `tests/test_structural_guards.py`, an AST
  fixture proving a notify-before-confirm ordering fails the guard, and a second
  module declaring a grounding probe fails the sole-owner assertion.
- **`LINT_PRESENT` + sensor (Step 6):** an injected `TreeReader` stub returning
  "no finding" yields `ungrounded`; the measured-data block renders the
  `UNGROUNDED-RATE` line from stubbed activation rows.

## Risk Assessment

- **False-rejecting a legitimate plan.** The primary risk is a real plan
  obsoleted by an over-eager probe. Mitigations: probes are opt-in per plan
  (`grounding=None` exempts open-ended work); each kind is a narrow, literal
  read (a name definition, a substring, a named lint code) with no fuzzy
  matching; the whole layer is behind `[loop] plan_grounding_enabled`
  (default-off); and the `UNGROUNDED-RATE` sensor plus the `[ungrounded:*]`
  obsolete reason make every auto-rejection auditable. Rollback is a flag flip.
- **Probe under-coverage.** A false premise with no citable symbol/text/lint
  locator (a purely semantic behavioral-invariant claim) still reaches the
  human — the design shrinks the class, it does not eliminate every member. This
  is the honest floor of a zero-SDK deterministic check and is listed Out of
  scope.
- **Trust chain.** The change only *withholds* a plan from human review when the
  tree refutes its premise; it never approves, merges, or writes code, and the
  human approval gate for anything that does survive is unchanged. The
  structural guard (Step 5) is the control that proves grounding cannot be
  bypassed.
- **Migration.** One optional `EvolutionPlan` field defaulting to `None`,
  handled by the existing `ensure_schema()` fingerprint path; no cold-store
  rewrite.

## Out of scope

- **Make-work plans with a *true* premise** (e.g. the `falsey`→`falsy` docstring
  edit — real, but "manufacturing a pretext to merge"). Grounding cannot fire on
  a true premise; that axis is frame/discharge honesty, owned by proposal 112's
  record-time outcome cap and the reviewer's value judgment.
- **The inward-facing contemplation engine that generates these premises**
  (self-model navel-gazing under work starvation). Reducing the *generation* of
  ungrounded plans is the province of proposals 112 and 113; this proposal
  closes the *submission* leak, so a false premise costs no human approval even
  while the generator still produces one.
- **Arbitrary behavioral-invariant falsity** requiring code execution to refute
  beyond a symbol/text/lint locator. The closed probe vocabulary covers the
  locator-reducible cases the production evidence exhibits; a purely semantic
  false premise remains a human call.
