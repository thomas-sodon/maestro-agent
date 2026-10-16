# NNN: <Title>

| Field | Value |
|-------|-------|
| **Impact** | HIGH / MEDIUM / LOWER |
| **Status** | proposed |
| **Affects** | `path/to/file.py`, `path/to/other.py`, new `path/to/new_module.py`, tests |
| **Depends on** | NNN, NNN _(omit row if none)_ |
| **Derived from** | _(optional — link to research note, prior PR, external study)_ |

The **Status** row carries a single token from the closed status vocabulary in
the [README Status Key](README.md#status-key). A proposal in burn-in uses the
`burn-in:<substate>` shape — `burn-in:ready`, `burn-in:observing`, or
`burn-in:graduated` — naming where in the rollout it sits; never a bare
`burn-in`.

## Description

Two to four short paragraphs answering, in order: what the change does, why it
matters now, and what the end state looks like once it lands. Avoid restating
the title. Avoid implementation detail — that belongs in the Implementation
Plan section.

## Relevant Findings

Concrete citations grounding the proposal in the **current** state of the
codebase. Cite by file path and the symbol involved — a function, class,
method, dataclass, or config key — **never bare line numbers**, which drift as
the code changes and rot the moment an unrelated edit lands above them. Each
bullet is one finding: a fact about how the code behaves today, the pattern the
proposal will reuse or replace, or an adjacent system the change must compose
with. No speculation; no aspiration.

- `maestro/<file>.py` (`<symbol>`) — observation about current behaviour.
- `tests/<file>.py` (`<test_name>`) — observation about current coverage / gap.

## Implementation Plan

A numbered list of independently mergeable steps — the proposal's "stories."
Order steps so an interrupted run leaves the codebase in a working state, and
gate behaviour changes behind a config flag where possible so intermediate
steps are safe to ship. Each step is a self-contained unit a single PR can
land.

For a *flagged* feature, follow the four-stage flag lifecycle actually run:
**dark-launch off everywhere** behind a default-off `Config` switch registered
in `EXPERIMENTAL_FLAGS` (`maestro/flag_lifecycle.py`) with a dated `review_by`
horizon; **open a production canary** — a `maestro.production.toml` line plus a
`PROD_ONLY_CANARIES` entry carrying a `justification`, with the base
`maestro.toml` key tagged `[production: …]`; **observe for a bounded window**
against written tripwires; then graduate to the base default and **collapse via
`CUTOVER_FLAGS`**, deleting the flag and its now-dead off-branch. `maestro.toml`
shows base defaults only: a key's production value is its `[production: …]` tag
/ `maestro.production.toml`, so never read a base `false` as "off in
production".

Give every step these fields so it can be tracked, sequenced, and
verified on its own — the same shape the runtime already uses for
`PlanStep.depends_on` and `EvolutionPlan.acceptance_criteria`:

- a `[ ]` (pending) / `[x]` (landed) checkbox, a one-line **title** in bold,
  and the file(s) it touches,
- 1–3 sentences describing the change and why this is the right slot,
- **Depends on** — the step numbers that must land first, or `none`,
- **Acceptance** — one testable invariant that is true once the step is done.

The checkbox lets a `partial`-status proposal show which stories shipped
without a separate narrative; flip `[ ]` → `[x]` as each step lands.

1. [ ] **Step title.** `path/to/file.py` — what the step adds or changes, and
   why this is the right slot.
   - *Depends on:* none.
   - *Acceptance:* a single testable invariant proving this step is done.
2. [ ] **Step title.** `path/to/other.py` — continued.
   - *Depends on:* 1.
   - *Acceptance:* …

For a phased proposal, group steps under `### Phase N — <name>` headings and
number them continuously across phases; the `Depends on` field carries the
cross-phase ordering.

## Testing Strategy

What is added and what proves correctness. List the specific tests (unit,
component, browser) and the branches each one covers — happy path, fallback,
guard. Honour the project rules in CLAUDE.md (`mem_fs`, no real I/O, no real
`sleep`, mock externals).

## Risk Assessment

What could regress and what mitigates it. Include rollback posture (config
flag, feature gate, revert plan) and any data-migration concerns. If the
change touches the trust chain (approvals, sandbox, public API), call out the
attack surface and the test that proves the new control fires.

---

_Optional sections — add when the change calls for them:_

- `## Migration` — for state-file or schema changes.
- `## Performance` — when the change has a measurable runtime budget.
- `## What landed` / `## What is missing` — for `partial`-status proposals
  describing which slices have shipped.
- `## Out of scope` — when adjacent work is deliberately deferred.
