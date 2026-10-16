# 059: Structured Implementation-Plan Steps in Slash Commands and Existing Proposals

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | partial |
| **Affects** | `.claude/commands/plan-proposal.md`, `.claude/commands/update-proposals.md`, open code-touching `docs/proposals/<category>/NNN-*.md` (backfill), `docs/proposals/README.md`, `tests/test_proposals_filename.py` <!-- TODO: no longer exists (removed in #2468); step 4 must recreate it --> |
| **What landed** | `docs/proposals/TEMPLATE.md` revised with the four-field per-step structure (checkbox, title + files, `Depends on`, `Acceptance`); proposals authored after the revision (056–063) carry the structure. |
| **What is missing** | Step 1 — `.claude/commands/plan-proposal.md` still emits steps without per-step `Depends on` / `Acceptance`. Step 2 — `.claude/commands/update-proposals.md` still enumerates five drift axes, not the sixth step-structure axis. Step 3 — backfill incomplete: open proposals 002–053 remain in flat-bullet form. Step 4 — `tests/test_proposals_filename.py` regression (removed in #2468) not recreated. |
| **Derived from** | `docs/proposals/TEMPLATE.md` revision establishing per-step structure |

## Description

`docs/proposals/TEMPLATE.md` now asks each Implementation-Plan step to carry
four fields — a `[ ]`/`[x]` checkbox, a bold title plus the file(s) it touches,
a `Depends on` line, and a one-line `Acceptance` invariant. This makes a
proposal's steps behave like agile stories under a feature: each is a
self-contained unit a single PR lands, ordered by an explicit dependency, with
a testable definition of done, and a checkbox that shows at a glance which
stories shipped. The shape deliberately mirrors what the runtime already
models — `PlanStep.depends_on` and `EvolutionPlan.acceptance_criteria` — so a
proposal step round-trips cleanly into the structured plans the loop executes.

The template change alone is inert: it only governs proposals written *after*
it lands. Two consumers and one corpus still speak the old flat-bullet shape.
The `/plan-proposal` command emits an `## Implementation steps` list with no
per-step `Depends on` / `Acceptance`; the `/update-proposals` command's
drift axes do not know the new fields exist, so it neither preserves nor
repairs them; and the ~40 open code-touching proposals predate the structure
entirely.

This proposal closes that gap. It teaches `/plan-proposal` to emit the
structured fields, teaches `/update-proposals` to read step checkboxes for
status and to repair missing step structure as a sixth drift axis, backfills
the existing open proposals into the new shape, and adds a regression test so
the structure cannot silently rot. End state: every open code-touching
proposal and every freshly generated plan presents ordered, individually
verifiable, dependency-linked steps; a `partial` proposal's progress is
readable from its checkboxes rather than from prose in the README.

## Relevant Findings

- `docs/proposals/TEMPLATE.md` — the `## Implementation Plan` section now
  defines the four per-step fields (checkbox, title + files, `Depends on`,
  `Acceptance`) and a phased-grouping convention. This proposal's consumers
  must be taught the same shape; the template is the source of truth.
- `.claude/commands/plan-proposal.md` — the code-touching plan layout's
  `## Implementation steps` entry specifies "a short title, the file(s) it
  touches and the symbol(s) within them, and a 1–3 sentence description" — no
  per-step `Depends on` or `Acceptance`. (The citation-format portion of this
  finding was addressed by PR #2467, which switched the layout from line ranges
  to file+symbol; only the missing per-step `Depends on` / `Acceptance` fields
  remain.) The command also reads the proposal's
  Implementation Plan during verification (`.claude/commands/plan-proposal.md`)
  but discards the new structure on output.
- `.claude/commands/update-proposals.md` — "What counts as drift"
  enumerates exactly five axes (path, line, symbol, status, dependency). None
  covers step-level structure, and the status axis (`update-proposals.md`)
  re-derives `partial`/`shipped` from code rather than from step checkboxes.
- `maestro/memory.py` — `PlanStep(step_id, description, status, result,
  depends_on)`: runtime steps already carry a `status` and a `depends_on`
  dependency list. The proposal step structure mirrors this 1:1.
- `maestro/memory.py` — `EvolutionPlan.acceptance_criteria: list[str]`:
  runtime plans already carry testable criteria; proposal *steps* do not yet,
  so a step's definition of done is implicit today.
- `maestro/agent.py` — `_build_execution_groups()` topo-groups
  `PlanStep.depends_on`. A proposal whose steps name their dependencies maps
  directly onto this graph when `/plan-proposal` produces the plan.
- `tests/test_proposals_filename.py` <!-- TODO: no longer exists (removed in #2468); step 4 must recreate it --> — the former per-number
  regression that walked `docs/proposals/**/*.md`. This would be the natural home for
  a step-structure regression: it globbed the corpus and excluded
  `README.md` / `TEMPLATE.md`.
- `docs/proposals/README.md` — the partial-status key and "How to author it"
  section describe status as narrative; the backfill plus checkbox convention
  lets `partial` be read from the proposal body.

## Implementation Plan

Steps are ordered so an interrupted run leaves the corpus consistent. The
tooling steps (1–2) precede the corpus backfill (3) so the backfill can lean on
the updated `/update-proposals`; the regression test (4) lands last so it
guards a corpus that is already conformant.

1. [ ] **`/plan-proposal` emits structured steps.** `.claude/commands/plan-proposal.md`
   — extend the `## Implementation steps` layout description so each emitted
   step carries a `[ ]` checkbox, its file(s), a `Depends on:` line (step
   numbers or `none`), and an `Acceptance:` invariant; add a note that the
   verification phase carries any `Depends on` / `Acceptance` already present in
   the source proposal into the plan rather than discarding it.
   - *Depends on:* none.
   - *Acceptance:* the command's code-touching plan layout instructs the writer
     to emit `Depends on:` and `Acceptance:` per step, and a generated plan for
     any proposal shows both fields on every step.
2. [ ] **`/update-proposals` learns the step structure.** `.claude/commands/update-proposals.md`
   — add a sixth drift axis ("step-structure drift": a step in `## Implementation
   Plan` missing its checkbox, `Depends on`, or `Acceptance` line is repaired in
   place), and amend the status axis so an all-`[x]` step list is corroborating
   evidence for `shipped` and a mixed list for `partial`. Scope the repair to
   structure only — never invent acceptance text the original author did not
   imply.
   - *Depends on:* none.
   - *Acceptance:* the "What counts as drift" section lists a sixth axis, and a
     dry run over a flat-bullet proposal reports step-structure drift.
3. [ ] **Backfill open code-touching proposals.** open `docs/proposals/<category>/NNN-*.md`
   — convert each open proposal that has a `## Implementation Plan` section to
   the structured step shape: add a checkbox per step, a `Depends on` line
   inferred from the existing ordering/prose, and a one-line `Acceptance`
   invariant drawn from the step's own description. Service-flavored income
   proposals using the Capability/Deliverable/Pricing layout have no
   Implementation Plan and are out of scope. Process one proposal per commit so
   each diff stays reviewable.
   - *Depends on:* 2.
   - *Acceptance:* every open proposal containing `## Implementation Plan` has
     a checkbox, `Depends on`, and `Acceptance` on each numbered step.
4. [ ] **Regression test for step structure.** `tests/test_proposals_filename.py` <!-- TODO: no longer exists (removed in #2468); recreate it here -->
   — add a test that, for every `docs/proposals/**/NNN-*.md` containing a
   `## Implementation Plan` heading, asserts each numbered step is preceded by a
   `[ ]`/`[x]` checkbox and is followed by a `Depends on:` and an `Acceptance:`
   line before the next step. Excludes `TEMPLATE.md` and `README.md`, matching
   the existing test's exclusions.
   - *Depends on:* 3.
   - *Acceptance:* the new test passes on the backfilled corpus and fails when a
     step's `Acceptance:` or `Depends on:` line is removed.

## Testing Strategy

The only executable surface is `tests/test_proposals_filename.py` <!-- TODO: no longer exists (removed in #2468); step 4 recreates it -->; the slash
commands and proposal bodies are Markdown and are validated by review plus the
regression test.

- **Step-structure regression (new).** Parametrize over every proposal file
  with an `## Implementation Plan` section. For each, split the section into
  numbered steps and assert: (a) the step line begins with `N. [ ]` or
  `N. [x]`; (b) a `Depends on:` line appears before the next step; (c) an
  `Acceptance:` line appears before the next step. Build the corpus list by
  globbing under `docs/proposals/`, reusing the existing test's `proposal_re`
  and the `TEMPLATE.md`/`README.md` exclusion.
- **Negative case.** A fixture string with a step missing its `Acceptance:`
  line asserts the validator rejects it, proving the guard catches regressions
  rather than only passing on the happy path.
- **Existing invariants unaffected.** `test_each_proposal_number_appears_exactly_once`
  and `test_proposals_do_not_reference_wrong_filename` continue to pass — the
  backfill edits step bodies, never filenames or the `evolution_plans.jsonl`
  references.

## Risk Assessment

**What could regress.** The backfill (step 3) is the largest blast radius —
~40 proposal files edited. The risk is mangling an author's prose while adding
structure. Mitigation: the edit is additive (checkbox + two annotation lines
per step); existing step titles, descriptions, and ordering are preserved
verbatim, mirroring the `/update-proposals` rule that motivation and argument
belong to the human author. One proposal per commit keeps each diff small
enough to eyeball.

**Inferred acceptance criteria.** A backfilled `Acceptance:` line is the
migrator's paraphrase of the step's own description, not the original author's
words. This is a known limitation: the line is a best-effort definition of
done, and a future `/update-proposals` pass (or the author) can sharpen it.
The regression test enforces *presence*, not *quality*, by design — quality is
a review concern, exactly as the existing `validate_success_criteria`
heuristics stay rationale-blind at the runtime layer.

**Tooling-only change.** Steps 1–2 edit Claude Code command docs, not Maestro
runtime code, so they cannot affect the loop, the dashboard, or persistence.
The regression test runs in the standard `pytest` suite with no new I/O,
network, or sleep — it reads files already on disk via the existing
`pathlib.rglob` pattern.

**Rollback posture.** Each step is independently revertible. Reverting step 4
removes the guard; reverting step 3 returns the corpus to flat bullets (the
template and tooling still accept either shape since the test is the only hard
gate); reverting steps 1–2 returns the commands to their prior layout
descriptions. No data migration, no config flag, no schema change.

## Out of scope

- **A structured (YAML/JSON) front-matter block per step.** This proposal keeps
  steps as human-readable Markdown with annotation lines, not a machine schema.
  A parser that materializes proposal steps directly into `PlanStep` objects is
  a possible follow-on but is not built here.
- **Cross-proposal "track" structuring.** The README's track narratives
  (021–029, 030–033, 047–052) stay prose; turning them into a structured
  epic→feature graph is a separate change.
- **Service-flavored income proposals.** Proposals using the
  Capability/Deliverable/Pricing layout have no Implementation Plan and are not
  touched by the backfill.
- **Enforcing acceptance-criterion *quality*.** The regression test checks that
  each step has an `Acceptance:` line, not that the invariant is genuinely
  testable; that judgement stays with the human reviewer.
